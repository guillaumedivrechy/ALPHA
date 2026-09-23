# -*- coding: utf-8 -*-
"""Séquences d'apprentissage (caractérisation automatique du banc) :

  - temps de course réels des 3 vannes (fins de course / recopie)
  - réponse indicielle du variateur : temps mort, constante de temps,
    temps de stabilisation, gain statique -> proposition de gains PI (IMC)

Les séquences s'exécutent dans un thread, ne sont autorisées que régulation
arrêtée, et remettent toujours le banc dans un état sûr en sortie.
"""

from __future__ import annotations
import json
import statistics
import threading
import time
from pathlib import Path


class Apprentissage:
    def __init__(self, pilote, cfg: dict, chemin_cfg):
        self.pilote = pilote
        self.cfg = cfg
        self.chemin_cfg = Path(chemin_cfg)
        self.etat = {"en_cours": False, "statut": "", "test": None}
        self.resultats: dict = {}
        self._annule = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- pilotage

    def demarrer(self, test: str, **params) -> str | None:
        """Lance une séquence. Retourne un message d'erreur ou None."""
        if self.etat["en_cours"]:
            return "Une séquence est déjà en cours."
        if self.pilote.etat.get("regulation"):
            return "Arrêtez la régulation avant de lancer un apprentissage."
        # Le ventilateur peut tourner en mode manuel (commandes de l'IHM ou
        # page de mise en service) sans que « regulation » soit vrai. Toute
        # séquence part d'un état connu : sans cela verif_cablage voit un
        # écoulement dès son premier instant et conclut « variateur répond »
        # alors que la référence 0-10 V pourrait n'être pas raccordée, et
        # zero_capteurs relèverait un zéro pollué par le débit résiduel.
        # On interroge l'état AUTORITAIRE du pilote, pas etat["u_variateur"]
        # qui n'est publié qu'au cycle suivant : enchaîner deux séquences
        # ferait sinon refuser la seconde sur une valeur périmée.
        if (self.pilote.mode_manuel and self.pilote.tension_manuelle > 0.05) \
                or self.pilote._run_actif:
            return ("Arrêtez le ventilateur avant de lancer un apprentissage "
                    "(bouton ARRÊT VARIATEUR) : les séquences doivent partir "
                    "d'un banc à l'arrêt.")
        cible = {"calibration_pdc": self._test_calibration_pdc,
                 "vanne_3voies": self._test_vanne_3voies,
                 "vanne_bypass": self._test_vanne_bypass,
                 "vanne_pdc": self._test_vanne_pdc,
                 "indiciel": self._test_indiciel,
                 "courbe_gain": self._test_courbe_gain,
                 "freq_min": self._test_freq_min,
                 "zero_capteurs": self._test_zero_capteurs,
                 "verif_cablage": self._test_verif_cablage}.get(test)
        if cible is None:
            return f"Test inconnu : {test}"
        if test == "indiciel" and self.pilote.gain_scheduling \
                and not self.pilote.table_gains.est_valide():
            return ("Lancer d'abord la courbe de gain : sans table de "
                    "gains, les kp/ki proposés par l'essai indiciel ne "
                    "vaudraient qu'au point d'essai (gain scheduling actif).")
        self._annule.clear()
        self.etat.update({"en_cours": True, "test": test, "statut": "démarrage…"})
        self._thread = threading.Thread(target=self._executer,
                                        args=(cible, params), daemon=True)
        self._thread.start()
        return None

    def annuler(self) -> None:
        self._annule.set()

    def _executer(self, cible, params) -> None:
        # les séquences manœuvrent les vannes en direct (es.*) pour mesurer
        # leurs temps de course : la boucle ne doit pas réaffirmer ses ordres
        self.pilote.suspendre_maintien_tor()
        try:
            cible(**params)
        except Exception as exc:
            self.etat["statut"] = f"échec : {exc}"
        finally:
            # état sûr systématique
            try:
                self.pilote.mode_manuel_tension(0.0)
                self.pilote.arreter_regulation()
            except Exception:
                pass
            # les tests de vannes commandent es.* directement et peuvent laisser
            # etat["diaphragme"] désynchronisé (surtout si annulés en cours) :
            # on force la boucle à relire la position réelle sur les fins de
            # course au prochain cycle, pour ne pas réguler sur le mauvais capteur.
            self.pilote._position_initiale_lue = False
            self.pilote.reprendre_maintien_tor()
            self.etat["en_cours"] = False
            self.etat["test"] = None

    # ------------------------------------------------------------ utilitaires

    def _brutes(self) -> dict:
        return self.pilote.etat.get("mesures_brutes") or {}

    def _attendre(self, condition, timeout: float, statut: str) -> float | None:
        """Attend que condition() soit vraie. Retourne la durée écoulée,
        ou None si timeout/annulation."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if self._annule.is_set():
                # ne pas écraser un motif d'abandon déjà posé (perte de
                # communication), qui renseigne mieux que « annulé »
                if not self.etat["statut"].startswith("communication perdue"):
                    self.etat["statut"] = "annulé"
                return None
            # La boucle publie ses mesures dans pilote.etat ; sur erreur
            # Modbus elle ne les met plus à jour et les séquences liraient
            # des valeurs figées. Sans ce contrôle, une coupure en pleine
            # course faisait attendre tout le chien de garde puis conclure
            # « délai dépassé » — l'opérateur allait vérifier une vanne
            # parfaitement saine. On abandonne la séquence entière : la
            # poursuivre à l'aveugle donnerait des temps de course et des
            # gains faux, qui seraient ensuite écrits en configuration.
            err = self.pilote.etat.get("erreur_com")
            if err:
                self.etat["statut"] = f"communication perdue : {err}"
                self._annule.set()
                return None
            if condition():
                return time.monotonic() - t0
            self.etat["statut"] = f"{statut} ({time.monotonic()-t0:.1f} s)"
            time.sleep(self.pilote.periode / 2)
        self.etat["statut"] = f"{statut} : délai dépassé"
        return None

    def _preparer_circuit_normal(self) -> bool:
        """Bypass fermé + vanne PdC grande ouverte : le circuit de mesure du
        régime normal. Indispensable avant toute caractérisation — une table
        de gains ou un tau mesurés circuit dérivé seraient silencieusement
        faux, et le feed-forward, le gain scheduling et le seuil bas-Re en
        hériteraient."""
        es = self.pilote.es
        t = self._chronometrer_vanne(lambda: es.ecrire_vanne_bypass(False),
                                     "fc_bypass_ferme", "fermeture bypass")
        if t is None:
            return False
        self.pilote.commander_vanne_perte_charge_pct(100.0)  # 100 % = 10 V = ouverte
        ok = self._attendre_pdc_extreme(
            10.0, 90.0, "ouverture vanne perte de charge")
        return ok is not None

    # ------------------------------------------------- utilitaire recopie PdC

    def _attendre_pdc_extreme(self, cible_v: float, timeout: float,
                               statut: str) -> float | None:
        """Attend que la recopie PdC reste dans la zone extrême pendant ~3 s.
        Les seuils sont déduits des butées calibrées (±5 % de la plage)."""
        dt = self.pilote.periode
        n_stable = max(4, int(3.0 / dt))
        v_min = self.pilote.pdc_recopie_min
        v_max = self.pilote.pdc_recopie_max
        marge = max(0.3, 0.05 * (v_max - v_min))
        serie: list[float] = []
        if cible_v >= (v_min + v_max) / 2:
            atteint = lambda v: v > v_max - marge
        else:
            atteint = lambda v: v < v_min + marge
        return self._attendre(
            lambda: (serie.append(
                         self._brutes().get("pdc_recopie") or 0.0)
                     or (len(serie) >= n_stable
                         and all(atteint(v) for v in serie[-n_stable:]))),
            timeout, statut)

    # ------------------------------------------------- tests vannes TOR

    def _chronometrer_vanne(self, ecrire, fc_cible: str, sens: str,
                            timeout: float = 60.0) -> float | None:
        """Commande la vanne et chronomètre jusqu'à la fin de course cible.
        Résolution : la période de la boucle de mesure."""
        ecrire()
        t = self._attendre(lambda: bool(self._brutes().get(fc_cible)),
                           timeout, f"course {sens}")
        return t

    def _avancer_vanne_3voies(self, vers_0deg: bool, label: str,
                              timeout: float = 60.0) -> float | None:
        """Conduit la vanne 3 voies vers une butée et chronomètre. La carte GF3
        rejoint directement la position demandée, sans impulsion de relance :
        l'ordre F mène à 0°, l'ordre I à 90°. Retourne la durée, ou None."""
        angle = "0" if vers_0deg else "90"
        if self.pilote._angle_vanne_3voies(self._brutes()) == angle:
            return 0.0
        t0 = time.monotonic()
        self.pilote.es.ecrire_vanne_diaphragme(vers_0deg)
        ok = self._attendre(
            lambda: self.pilote._angle_vanne_3voies(self._brutes()) == angle,
            timeout, label)
        return None if ok is None else time.monotonic() - t0

    def _test_vanne_3voies(self) -> None:
        """Chrono des deux courses utiles de la vanne 3 voies. La position 180°
        n'est pas commandée (borne O non câblée), il ne reste donc que le
        va-et-vient entre les deux butées de travail :

          T1 : 0° → 90°
          T2 : 90° → 0°

        Attendu sur un ER20 standard : environ 11 s dans chaque sens.
        """
        res = {}

        # Pré-positionnement sur la butée 0°
        t = self._avancer_vanne_3voies(True, "pré-pos butée 0°")
        if t is None:
            return
        time.sleep(0.5)

        t = self._avancer_vanne_3voies(False, "T1 : 0° → 90°")
        if t is None:
            return
        res["T1_0_vers_90_s"] = round(t, 2)
        time.sleep(0.5)

        t = self._avancer_vanne_3voies(True, "T2 : 90° → 0°")
        if t is None:
            return
        res["T2_90_vers_0_s"] = round(t, 2)

        res["max_s"] = max(res.values())
        self.pilote.etat["diaphragme"] = \
            self.pilote._diaphragme_par_angle["0"]

        self.resultats["vanne_3voies"] = res
        self.etat["statut"] = (
            f"terminé : T1={res['T1_0_vers_90_s']} s  "
            f"T2={res['T2_90_vers_0_s']} s")

    def _test_vanne_bypass(self) -> None:
        es = self.pilote.es
        res = {}

        # Pré-positionnement à FC1 (fermeture complète = fc_bypass_ferme)
        t = self._chronometrer_vanne(lambda: es.ecrire_vanne_bypass(False),
                                     "fc_bypass_ferme", "pré-pos fermeture")
        if t is None:
            return
        time.sleep(0.5)

        # T1 : FC1 (fermeture) → FC2 (ouverture)
        t = self._chronometrer_vanne(lambda: es.ecrire_vanne_bypass(True),
                                     "fc_bypass_ouvert", "T1 fermeture → ouverture")
        if t is None:
            return
        res["ouverture_s"] = round(t, 2)
        time.sleep(0.5)

        # T2 : FC2 (ouverture) → FC1 (fermeture)
        t = self._chronometrer_vanne(lambda: es.ecrire_vanne_bypass(False),
                                     "fc_bypass_ferme", "T2 ouverture → fermeture")
        if t is None:
            return
        res["fermeture_s"] = round(t, 2)
        res["max_s"] = max(res.values())
        self.resultats["vanne_bypass"] = res
        self.etat["statut"] = (f"terminé : T1={res['ouverture_s']} s, "
                               f"T2={res['fermeture_s']} s")

    # ------------------------------------------------- test vanne PdC

    def _test_calibration_pdc(self) -> None:
        """Mesure la recopie analogique aux deux butées physiques via les FC.
        Sens usine de la carte P6 : 0 V → fc_pdc_ferme, 10 V → fc_pdc_ouvert.
        Un échec ici est le symptôme d'une carte réglée en sens inverse."""
        dt = self.pilote.periode
        n = max(4, int(2.0 / dt))   # 2 s de mesure après déclenchement FC

        def _mesure_butee(commande_v: float, fc_cle: str,
                          label: str) -> float | None:
            self.pilote.commander_vanne_perte_charge(commande_v)
            t = self._attendre(
                lambda: bool(self._brutes().get(fc_cle)), 90.0, label)
            if t is None:
                return None
            serie: list[float] = []
            self._attendre(
                lambda: (serie.append(
                             self._brutes().get("pdc_recopie") or 0.0)
                         or len(serie) >= n),
                n * dt * 2 + 1.0, label + " mesure")
            return round(statistics.fmean(serie), 3) if serie else None

        v_ferme = _mesure_butee(0.0, "fc_pdc_ferme", "butée fermeture (0 V)")
        if v_ferme is None:
            return
        v_ouvert = _mesure_butee(10.0, "fc_pdc_ouvert", "butée ouverture (10 V)")
        if v_ouvert is None:
            return

        plage = abs(v_ferme - v_ouvert)
        if plage < 1.0:
            self.etat["statut"] = (
                f"plage insuffisante ({plage:.2f} V) — vérifier câblage")
            return

        self.resultats["calibration_pdc"] = {
            "v_min_recopie": v_ferme,
            "v_max_recopie": v_ouvert,
            "plage_v": round(plage, 3),
        }
        self.pilote.pdc_recopie_min = v_ferme
        self.pilote.pdc_recopie_max = v_ouvert
        self.etat["statut"] = (
            f"terminé : fermé = {v_ferme} V, ouvert = {v_ouvert} V "
            f"(plage {plage:.3f} V)")

    def _test_vanne_pdc(self) -> None:
        """Chrono de la vanne perte de charge via ses deux fins de course.
          Pré-pos : fermeture complète (0 V).
          T1 : fc_pdc_ferme → fc_pdc_ouvert  (ouverture_s)
          T2 : fc_pdc_ouvert → fc_pdc_ferme  (fermeture_s)
        Attendu sur un ER20 POSI : environ 25 s dans chaque sens.
        """
        res = {}

        # Pré-positionnement en fermeture complète (0 V = fermé)
        self.pilote.commander_vanne_perte_charge(0.0)
        t = self._attendre(lambda: bool(self._brutes().get("fc_pdc_ferme")),
                           90.0, "pré-pos fermeture PdC")
        if t is None:
            return
        self.pilote._pdc_position_estimee = 0.0
        time.sleep(0.5)

        # T1 : fermeture → ouverture complète (10 V = ouvert)
        self.pilote.commander_vanne_perte_charge(10.0)
        t = self._attendre(lambda: bool(self._brutes().get("fc_pdc_ouvert")),
                           90.0, "T1 fermeture → ouverture")
        if t is None:
            return
        res["ouverture_s"] = round(t, 2)
        self.pilote._pdc_position_estimee = 10.0
        time.sleep(0.5)

        # T2 : ouverture → fermeture complète (0 V = fermé)
        self.pilote.commander_vanne_perte_charge(0.0)
        t = self._attendre(lambda: bool(self._brutes().get("fc_pdc_ferme")),
                           90.0, "T2 ouverture → fermeture")
        if t is None:
            return
        res["fermeture_s"] = round(t, 2)
        self.pilote._pdc_position_estimee = 0.0

        res["max_s"] = max(res.values())
        self.resultats["vanne_pdc"] = res
        self.etat["statut"] = (f"terminé : T1={res['ouverture_s']} s, "
                               f"T2={res['fermeture_s']} s")

    # ------------------------------------------------- zéro des capteurs ΔP

    def _test_zero_capteurs(self, attente_s: float = 8.0,
                            duree_s: float = 10.0) -> None:
        """Zéro des capteurs ΔP : moyenne à débit nul (ventilateur arrêté).
        Les offsets sont ensuite soustraits des mesures par la boucle —
        décisif en bas-Re, où le ΔP utile ne vaut que quelques Pa."""
        self.pilote.mode_manuel_tension(0.0)
        dt = self.pilote.periode
        t0 = time.monotonic()
        while time.monotonic() - t0 < attente_s:
            if self._annule.is_set():
                self.etat["statut"] = "annulé"
                return
            self.etat["statut"] = ("extinction de l'écoulement "
                                   f"({attente_s - (time.monotonic() - t0):.0f} s)")
            time.sleep(dt)
        m = self._dp_moyens(duree_s, "mesure du zéro", corriger=False)
        if m is None:
            return
        offsets = {"dp_iso_500": round(m[0], 3),
                   "dp_diaph_500": round(m[1], 3)}
        aberrants = {v: o for v, o in offsets.items() if abs(o) > 25.0}
        if aberrants:
            self.etat["statut"] = (
                "zéro anormalement élevé ("
                + ", ".join(f"{v} = {o} Pa" for v, o in aberrants.items())
                + ") — écoulement résiduel ou capteur en défaut ?")
            return
        self.resultats["zero_capteurs"] = offsets
        self.etat["statut"] = "terminé : " + ", ".join(
            f"{v} = {o} Pa" for v, o in offsets.items())

    # ------------------------------------------------- contrôle de câblage

    @staticmethod
    def _capteur_dominant(dp_iso: float, dp_miller: float) -> str | None:
        """Quel capteur ΔP voit l'écoulement ? None si trop faible ou ambigu."""
        if max(dp_iso, dp_miller) < 5.0:
            return None
        if dp_iso > 3.0 * max(dp_miller, 0.1):
            return "iso"
        if dp_miller > 3.0 * max(dp_iso, 0.1):
            return "miller"
        return None

    def _dp_moyens(self, duree_s: float, statut: str,
                   corriger: bool = True) -> tuple | None:
        """Moyennes (dp_iso, dp_miller) sur duree_s, corrigées du zéro
        appris — un offset capteur fausserait les seuils d'identification
        (corriger=False pour la mesure du zéro elle-même). None si annulé."""
        z = self.pilote.zero_capteurs if corriger else {}
        dt = self.pilote.periode
        iso: list[float] = []
        miller: list[float] = []
        t0 = time.monotonic()
        while time.monotonic() - t0 < duree_s:
            if self._annule.is_set():
                self.etat["statut"] = "annulé"
                return None
            brutes = self._brutes()
            iso.append((brutes.get("dp_iso_500") or 0.0)
                       - z.get("dp_iso_500", 0.0))
            miller.append((brutes.get("dp_diaph_500") or 0.0)
                          - z.get("dp_diaph_500", 0.0))
            self.etat["statut"] = statut
            time.sleep(dt)
        return statistics.fmean(iso), statistics.fmean(miller)

    def _test_verif_cablage(self, u_essai: float = 3.0) -> None:
        """Contrôle de câblage guidé : actionne chaque sortie et vérifie le
        retour attendu (fins de course, recopie, réponse du variateur), puis
        identifie quelle position de la vanne 3 voies alimente quel
        diaphragme — le capteur ΔP qui répond fait foi. Rien n'est modifié
        pendant le test ; un mapping ISO/Miller inversé est corrigé par
        « Appliquer »."""
        rapport: dict = {}
        es = self.pilote.es

        # 1) bypass : les deux fins de course répondent (état final : fermé)
        t = self._chronometrer_vanne(
            lambda: es.ecrire_vanne_bypass(True),
            "fc_bypass_ouvert", "câblage bypass (ouverture)")
        rapport["bypass_ouverture"] = t is not None
        t = self._chronometrer_vanne(
            lambda: es.ecrire_vanne_bypass(False),
            "fc_bypass_ferme", "câblage bypass (fermeture)")
        rapport["bypass_fermeture"] = t is not None
        if self._annule.is_set():
            return

        # 2) vanne PdC : recopie suivie dans les deux sens
        #    (état final : grande ouverte = circuit de mesure normal)
        self.pilote.commander_vanne_perte_charge(0.0)
        ok = self._attendre_pdc_extreme(0.0, 90.0, "câblage PdC (fermeture)")
        rapport["pdc_fermeture"] = ok is not None
        self.pilote.commander_vanne_perte_charge(10.0)
        ok = self._attendre_pdc_extreme(10.0, 90.0, "câblage PdC (ouverture)")
        rapport["pdc_ouverture"] = ok is not None
        if self._annule.is_set():
            return

        # 3) vanne 3 voies : les trois fins de course sont atteignables
        #    (fin du parcours : position ISO supposée, prête pour le point 4)
        for vers_0deg, nom in ((False, "90°"), (True, "0°")):
            t = self._avancer_vanne_3voies(vers_0deg,
                                           f"câblage 3 voies ({nom})")
            rapport[f"v3_{nom}"] = t is not None
            if self._annule.is_set():
                return

        # 4) variateur : RUN + tension -> un écoulement doit apparaître
        self.pilote.mode_manuel_tension(u_essai)

        def _flux_present():
            b = self._brutes()
            z = self.pilote.zero_capteurs
            dp = ((b.get("dp_iso_500") or 0.0) - z.get("dp_iso_500", 0.0)
                  + (b.get("dp_diaph_500") or 0.0)
                  - z.get("dp_diaph_500", 0.0))
            return dp > 5.0 or (b.get("frequence_variateur") or 0.0) > 0.5

        t = self._attendre(_flux_present, 30.0, "câblage variateur (montée)")
        rapport["variateur_repond"] = t is not None

        # 5) identification ISO/Miller par le capteur ΔP qui répond
        mapping = None
        if rapport["variateur_repond"] and rapport.get("v3_0°") \
                and rapport.get("v3_90°"):
            if self._annule.wait(3.0):                 # établissement du flux
                self.etat["statut"] = "annulé"
                return
            m1 = self._dp_moyens(4.0, "identification : butée 0°")
            if m1 is None:
                return
            t = self._avancer_vanne_3voies(False,
                                           "identification : vers butée 90°")
            if self._annule.is_set():
                return
            if t is None:
                # vanne pas arrivée : diagnostic « vanne », pas « capteurs »
                # (mesurer m2 ici accuserait à tort les capteurs ΔP)
                rapport["v3_identification"] = False
            else:
                if self._annule.wait(2.0):
                    self.etat["statut"] = "annulé"
                    return
                m2 = self._dp_moyens(4.0, "identification : butée 90°")
                if m2 is None:
                    return
                d1 = self._capteur_dominant(*m1)
                d2 = self._capteur_dominant(*m2)
                if d1 == "iso" and d2 == "miller":
                    mapping = "ok"
                elif d1 == "miller" and d2 == "iso":
                    mapping = "inverse"
                else:
                    mapping = "indetermine"
                rapport["mapping_iso_miller"] = mapping
                rapport["dp_butee_0deg"] = [round(m1[0], 1), round(m1[1], 1)]
                rapport["dp_butee_90deg"] = [round(m2[0], 1), round(m2[1], 1)]
        self.pilote.mode_manuel_tension(0.0)

        rapport["mapping_inverse"] = mapping == "inverse"
        defauts = [k for k, v in rapport.items()
                   if v is False and k != "mapping_inverse"]
        if mapping == "inverse":
            resume = ("MAPPING ISO/MILLER INVERSÉ — la butée 0° fait réagir le "
                      "capteur Miller et la butée 90° le capteur ISO "
                      "(plomberie croisée ou capteurs intervertis) ; "
                      "« Appliquer » corrige positions_vanne_3voies")
        elif defauts:
            resume = "défauts : " + ", ".join(defauts)
        elif mapping == "indetermine":
            resume = "identification ISO/Miller indéterminée (ΔP trop faible ?)"
        else:
            resume = "câblage conforme"
        rapport["resume"] = resume
        self.resultats["verif_cablage"] = rapport
        self.etat["statut"] = "terminé : " + resume

    # ------------------------------------------------- courbe de gain multi-points

    def _mesure_palier(self, u: float, fenetre: int, tol: float,
                       timeout: float, statut: str) -> float | None:
        """Applique une tension, attend la stabilisation, renvoie le Re moyen."""
        self.pilote.mode_manuel_tension(u)
        série = []
        ok = self._attendre(
            lambda: (série.append(self.pilote.etat.get("re_mesure") or 0.0)
                     or self._stable(série, fenetre, tol)),
            timeout, statut)
        if ok is None:
            return None
        return statistics.fmean(série[-fenetre:])

    def _test_freq_min(self, u_depart: float = 5.0, pas: float = 0.25,
                       t_palier_s: float = 8.0) -> None:
        """Descend la tension par paliers depuis u_depart et détecte le
        décrochage du ventilateur (Re qui ne baisse plus, devient bruité ou
        s'effondre). Retient la dernière tension stable + marge.

        Chaque palier dure t_palier_s et n'est mesuré que sur sa fin (hors
        transitoire) : avec un procédé lent, mesurer trop tôt fait paraître
        « non monotone » une descente parfaitement saine et déclare un faux
        décrochage dès le premier palier.

        Préparation : ferme le bypass et ouvre la vanne perte de charge à 100 %
        (état maintenu en fin de test)."""
        dt = self.pilote.periode
        fenetre = max(6, int(4.0 / dt))

        # 0) préparation des vannes
        if not self._preparer_circuit_normal():
            return

        def _palier(u: float, statut: str, duree: float | None = None):
            """Applique u, attend duree (t_palier_s par défaut), mesure sur
            la fin du palier. Retourne (re_moyen, bruit_relatif) ou None si
            annulé."""
            duree = t_palier_s if duree is None else duree
            self.pilote.commander_variateur_manuel(u)
            série: list[float] = []
            t0 = time.monotonic()
            while time.monotonic() - t0 < duree:
                if self._annule.is_set():
                    self.etat["statut"] = "annulé"
                    return None
                série.append(self.pilote.etat.get("re_mesure") or 0.0)
                self.etat["statut"] = statut
                time.sleep(dt)
            d = série[-fenetre:] if len(série) >= fenetre else série
            re_moy = statistics.fmean(d)
            return re_moy, (max(d) - min(d)) / max(re_moy, 1.0)

        # le palier de départ part loin de l'équilibre (ventilateur arrêté) :
        # sans stabilisation longue, la base est mesurée trop bas et le
        # premier pas de descente paraît « non monotone » (faux décrochage)
        r = _palier(u_depart, f"stabilisation à {u_depart:.1f} V",
                    duree=3.0 * t_palier_s)
        if r is None:
            return
        re_a_derniere = r[0]
        u = u_depart
        derniere_stable = u
        paliers = [(round(u, 2), round(re_a_derniere))]

        while u - pas > 0.05:
            u = round(u - pas, 2)
            r = _palier(u, f"palier descendant {u:.2f} V")
            if r is None:
                return
            re_moy, bruit = r
            # baisse attendue ≈ pas/u : en exiger au moins 40 % sépare une
            # descente saine (régime établi) d'un plateau de décrochage
            seuil_baisse = 1.0 - 0.4 * pas / max(u + pas, pas)
            non_monotone = re_moy >= re_a_derniere * seuil_baisse
            paliers.append((u, round(re_moy)))
            self.etat["paliers_freq_min"] = list(paliers)
            if bruit > 0.06 or non_monotone or re_moy < 50:
                break                                  # décrochage détecté
            derniere_stable = u
            re_a_derniere = re_moy

        self.pilote.commander_variateur_manuel(0.0)
        u_min_reco = round(min(derniere_stable + pas, u_depart), 2)
        self.pilote.u_min_ventilateur = u_min_reco
        self.resultats["freq_min"] = {
            "u_min_exploitable_v": u_min_reco,
            "derniere_stable_v": derniere_stable,
            "re_a_u_min": round(re_a_derniere),
            "paliers_V_Re": [[v, re] for v, re in paliers],
        }
        self.etat["statut"] = (
            f"terminé : tension min exploitable ≈ {u_min_reco} V "
            f"(Re ≈ {re_a_derniere:.0f}), marge incluse")

    def _test_courbe_gain(self, tensions=None, duree_max: float = 90.0) -> None:
        """Mesure Re stabilisé à plusieurs tensions -> table de gains
        (tension->Re) pour le pré-positionnement, et vérifie la linéarité."""
        if tensions is None:
            cfg_app = self.cfg.get("apprentissage", {})
            pas    = float(cfg_app.get("courbe_gain_pas_v", 1.0))
            u_max  = float(cfg_app.get("courbe_gain_u_max_v", 9.0))
            u_deb  = float(cfg_app.get("courbe_gain_u_depart_defaut_v", 2.0))
            u_min  = self.pilote.u_min_ventilateur or u_deb
            n      = max(1, round((u_max - u_min) / pas))
            tensions = [round(u_min + i * pas, 2) for i in range(n + 1)
                        if u_min + i * pas <= u_max + 1e-9]
        # circuit de mesure normal obligatoire : une table relevée bypass
        # ouvert ou PdC fermée serait silencieusement fausse
        if not self._preparer_circuit_normal():
            return
        dt = self.pilote.periode
        fenetre = max(6, int(5.0 / dt))
        points = []                                  # (volts, re)
        sature_a = None
        for k, u in enumerate(tensions):
            re = self._mesure_palier(
                u, fenetre, 0.02, duree_max,
                f"palier {k+1}/{len(tensions)} à {u:.1f} V")
            if re is None:
                return
            # capteur ΔP saturé : le palier mesure le plafond du capteur,
            # pas le banc — point faux qui corromprait table et verdict.
            # Critère unique : le drapeau anti-rebond calculé par la boucle.
            if self.pilote.etat.get("dp_sature"):
                sature_a = u
                break
            points.append((u, re))
        self.pilote.mode_manuel_tension(0.0)
        if len(points) < 2:
            self.etat["statut"] = ("capteur ΔP saturé dès les premiers "
                                   "paliers — vérifier la gamme capteur "
                                   "et le diaphragme actif")
            return

        # gains locaux entre paliers successifs et mesure de non-linéarité
        gains = [(points[i+1][1] - points[i][1]) / (points[i+1][0] - points[i][0])
                 for i in range(len(points) - 1)]
        g_moy = statistics.fmean(gains)
        ecart_max = max(abs(g - g_moy) for g in gains) / g_moy * 100 \
            if g_moy else 0.0
        lineaire = ecart_max < 10.0                  # < 10 % => quasi linéaire

        self.resultats["courbe_gain"] = {
            "points_V_Re": [[round(u, 2), round(re)] for u, re in points],
            "gains_locaux_Re_par_V": [round(g) for g in gains],
            "gain_moyen": round(g_moy),
            "non_linearite_pct": round(ecart_max, 1),
            "sature_a_v": sature_a,
            "verdict": "quasi-linéaire" if lineaire
                       else "non linéaire — table de gains recommandée",
        }
        # construction de la table (toujours, elle est exacte même si linéaire)
        self._table_points = points
        note_sat = (f" — capteur ΔP saturé au-delà de {points[-1][0]:.1f} V "
                    f"(Re mesurable ≤ {points[-1][1]:.0f})" if sature_a else "")
        self.etat["statut"] = (
            f"terminé : {len(points)} points, gain moyen {g_moy:.0f} Re/V, "
            f"non-linéarité {ecart_max:.1f} % → "
            + ("quasi-linéaire" if lineaire else "table de gains recommandée")
            + note_sat)

    # ------------------------------------------------- essai indiciel

    def _stable(self, série: list, fenêtre: int, tol_rel: float) -> bool:
        if len(série) < fenêtre:
            return False
        d = série[-fenêtre:]
        m = statistics.fmean(d)
        return m != 0 and (max(d) - min(d)) / abs(m) < tol_rel

    def _test_indiciel(self, u1: float = 2.0, u2: float = 4.0,
                       duree_max: float = 120.0) -> None:
        # même exigence que la courbe de gain : un tau mesuré circuit
        # dérivé fausserait kp/ki et tau_consigne_s
        if not self._preparer_circuit_normal():
            return
        dt = self.pilote.periode
        fenêtre = max(6, int(5.0 / dt))      # ~5 s de mesures stables

        # 1) stabilisation au point de départ U1
        self.pilote.mode_manuel_tension(u1)
        série = []
        ok = self._attendre(
            lambda: (série.append(self.pilote.etat.get("re_mesure") or 0.0)
                     or self._stable(série, fenêtre, 0.02)),
            duree_max, f"stabilisation à U1 = {u1:.1f} V")
        if ok is None:
            return
        re1 = statistics.fmean(série[-fenêtre:])

        # 2) échelon U1 -> U2, enregistrement
        t0 = time.monotonic()
        points = []                          # (t, re, dp)
        self.pilote.mode_manuel_tension(u2)
        while time.monotonic() - t0 < duree_max:
            if self._annule.is_set():
                self.etat["statut"] = "annulé"
                return
            e = self.pilote.etat
            points.append((time.monotonic() - t0,
                           e.get("re_mesure") or 0.0, e.get("dp") or 0.0))
            res_série = [p[1] for p in points]
            if len(points) > 2 * fenêtre and \
                    self._stable(res_série, fenêtre, 0.02):
                break
            self.etat["statut"] = \
                f"échelon {u1:.1f}→{u2:.1f} V ({points[-1][0]:.1f} s)"
            time.sleep(dt)
        else:
            self.etat["statut"] = "échelon : stabilisation non atteinte"
            return

        # 3) dépouillement
        re2 = statistics.fmean([p[1] for p in points[-fenêtre:]])
        delta = re2 - re1
        if abs(delta) < max(50.0, 0.02 * abs(re2)):
            self.etat["statut"] = "échelon trop faible pour être exploité"
            return

        def t_franchissement(frac):
            seuil = re1 + frac * delta
            for t, re, _ in points:
                if (delta > 0 and re >= seuil) or (delta < 0 and re <= seuil):
                    return t
            return None

        t_mort = t_franchissement(0.05) or 0.0
        t63 = t_franchissement(0.632)
        tau = max((t63 - t_mort) if t63 else dt, dt)
        # temps d'entrée définitive dans la bande ±2 % de la valeur finale
        t_stab = points[-1][0]
        for t, re, _ in reversed(points):
            if abs(re - re2) > 0.02 * abs(re2):
                break
            t_stab = t
        gain = delta / (u2 - u1)

        # 4) proposition de gains PI (IMC, plante normalisée gain~1, cste tau)
        lam = max(tau / 2.0, 3.0 * dt, 2.0 * t_mort)
        kp = min(max(tau / lam, 0.5), 10.0)
        ki = min(max(1.0 / lam, 0.05), 10.0)

        self.resultats["indiciel"] = {
            "u1_v": u1, "u2_v": u2, "re1": round(re1), "re2": round(re2),
            "gain_re_par_v": round(gain, 1), "temps_mort_s": round(t_mort, 2),
            "tau_s": round(tau, 2), "t_stabilisation_s": round(t_stab, 2),
            "kp_propose": round(kp, 2), "ki_propose": round(ki, 2),
        }
        self.etat["statut"] = (f"terminé : K={gain:.0f} Re/V, τ={tau:.1f} s, "
                               f"stab. ±2 % en {t_stab:.1f} s → "
                               f"kp={kp:.2f}, ki={ki:.2f}")

    # ------------------------------------------------- application config

    def appliquer(self) -> str:
        """Reporte les résultats dans la configuration (avec marge sur les
        temps de course) et la persiste. Retourne un résumé."""
        act = self.cfg.setdefault("actionneurs", {})
        reg = self.cfg.setdefault("regulation", {})
        lignes = []
        if "zero_capteurs" in self.resultats:
            z = dict(self.resultats["zero_capteurs"])
            self.cfg.setdefault("zero_capteurs", {}).update(z)
            self.pilote.zero_capteurs = z
            lignes.append("zéros capteurs : " + ", ".join(
                f"{v} = {o} Pa" for v, o in z.items()))
        if self.resultats.get("verif_cablage", {}).get("mapping_inverse"):
            pos = self.cfg.setdefault("modbus", {}).setdefault(
                "positions_vanne_3voies", {})
            pos["diaphragme_a_0deg"], pos["diaphragme_a_90deg"] = (
                pos.get("diaphragme_a_90deg", "miller"),
                pos.get("diaphragme_a_0deg", "iso"))
            d0, d90 = pos["diaphragme_a_0deg"], pos["diaphragme_a_90deg"]
            self.pilote._diaphragme_par_angle = {"0": d0, "90": d90}
            self.pilote._angle_par_diaphragme = {d0: "0", d90: "90"}
            # la position réelle sera relue sur les fins de course
            self.pilote._position_initiale_lue = False
            lignes.append("mapping vanne 3 voies ISO/Miller interverti")
        if "calibration_pdc" in self.resultats:
            r = self.resultats["calibration_pdc"]
            act["pdc_recopie_min_v"] = r["v_min_recopie"]
            act["pdc_recopie_max_v"] = r["v_max_recopie"]
            self.pilote.pdc_recopie_min = r["v_min_recopie"]
            self.pilote.pdc_recopie_max = r["v_max_recopie"]
            lignes.append(
                f"PdC recopie : fermé = {r['v_min_recopie']} V, "
                f"ouvert = {r['v_max_recopie']} V")
        if "vanne_3voies" in self.resultats:
            v = round(self.resultats["vanne_3voies"]["max_s"] * 1.1, 1)
            act["vanne_3voies_temps_s"] = v
            self.pilote.t_vanne_3voies = v
            lignes.append(f"vanne 3 voies : {v} s")
        if "vanne_bypass" in self.resultats:
            v = round(self.resultats["vanne_bypass"]["max_s"] * 1.1, 1)
            act["vanne_bypass_temps_s"] = v
            self.pilote.t_vanne_bypass = v
            lignes.append(f"bypass : {v} s")
        if "vanne_pdc" in self.resultats:
            v = round(self.resultats["vanne_pdc"]["max_s"] * 1.05, 1)
            act["vanne_perte_charge_course_s"] = v
            self.pilote.t_vanne_pdc_course = v
            lignes.append(f"vanne perte de charge : {v} s")
        if "indiciel" in self.resultats:
            r = self.resultats["indiciel"]
            reg["kp"] = r["kp_propose"]
            reg["ki"] = r["ki_propose"]
            self.pilote.pi.kp = r["kp_propose"]
            self.pilote.pi.ki = r["ki_propose"]
            # mise en forme de référence calée sur le tau mesuré du procédé
            # (la consigne interne suit alors la trajectoire naturelle)
            reg["tau_consigne_s"] = r["tau_s"]
            self.pilote.tau_consigne = r["tau_s"]
            lignes.append(f"kp={r['kp_propose']}, ki={r['ki_propose']}, "
                          f"tau_consigne={r['tau_s']} s")
        if "courbe_gain" in self.resultats and getattr(self, "_table_points", None):
            from core.pilote import TableGains
            t = TableGains(self._table_points)
            if t.est_valide():
                self.pilote.table_gains = t
                lignes.append(f"table de gains ({len(self._table_points)} points)")
            else:
                lignes.append("table de gains ignorée (Re non monotone)")
        if "freq_min" in self.resultats:
            r = self.resultats["freq_min"]
            self.cfg.setdefault("strategie_bas_re", {})["u_min_ventilateur_v"] = \
                r["u_min_exploitable_v"]
            self.pilote.u_min_ventilateur = r["u_min_exploitable_v"]
            lignes.append(f"u_min ventilateur = {r['u_min_exploitable_v']} V")
        if not lignes:
            return "Aucun résultat à appliquer."
        # persistance immédiate de l'état (table de gains, u_min) + config
        try:
            etat = {}
            if self.pilote._gain_re_par_volt:
                etat["gain_re_par_volt"] = self.pilote._gain_re_par_volt
            if self.pilote.table_gains.est_valide():
                etat["table_gains"] = self.pilote.table_gains.to_list()
            if self.pilote.u_min_ventilateur:
                etat["u_min_ventilateur_v"] = self.pilote.u_min_ventilateur
            self.pilote._chemin_etat.write_text(json.dumps(etat),
                                                encoding="utf-8")
        except OSError:
            pass
        self.chemin_cfg.write_text(
            json.dumps(self.cfg, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        return "Appliqué et enregistré : " + " ; ".join(lignes)
