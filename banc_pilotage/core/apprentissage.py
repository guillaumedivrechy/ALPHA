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
                self.etat["statut"] = "annulé"
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
        self.pilote.commander_vanne_perte_charge_pct(0.0)   # 0 % = 0 V = ouverte
        ok = self._attendre_pdc_extreme(
            0.0, 90.0, "ouverture vanne perte de charge")
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

    def _avancer_vanne_3voies(self, vers_iso: bool, fc_cible: str,
                              label: str, timeout: float = 60.0) -> float | None:
        """Conduit la vanne 3 voies vers fc_cible en gérant l'arrêt intermédiaire
        automatique à 90° (Miller). Si la vanne part d'une extrémité (0° ou 180°)
        et la cible est l'autre extrémité, deux impulsions relais sont nécessaires
        (arrêt automatique à 90° entre les deux). Retourne la durée totale ou None.
        """
        es = self.pilote.es
        fc_miller = self.pilote._fc_par_diaphragme["miller"]   # fc_3voies_90
        t0 = time.monotonic()

        # Déjà à la cible ?
        if bool(self._brutes().get(fc_cible)):
            return 0.0

        # Départ depuis la position intermédiaire (90°) : aller directement
        starting_at_miller = bool(self._brutes().get(fc_miller))

        for pulse in range(2):
            restant = timeout - (time.monotonic() - t0)
            if restant <= 0.0:
                break

            es.ecrire_vanne_diaphragme(vers_iso)

            if starting_at_miller or pulse == 1:
                # Depuis 90° ou 2e impulsion : attendre uniquement la cible
                condition = lambda: bool(self._brutes().get(fc_cible))
            else:
                # 1re impulsion depuis une extrémité : peut s'arrêter à Miller
                condition = lambda: (bool(self._brutes().get(fc_cible))
                                     or bool(self._brutes().get(fc_miller)))

            ok = self._attendre(condition, restant, label)
            if ok is None:
                return None

            if bool(self._brutes().get(fc_cible)):
                return time.monotonic() - t0

            # Arrêt intermédiaire à Miller — courte pause avant 2e impulsion
            time.sleep(0.3)
            starting_at_miller = False

        self.etat["statut"] = f"{label} : cible non atteinte"
        return None

    def _test_vanne_3voies(self) -> None:
        """Chrono en 4 temps depuis FC1 (fermeture complète, 180°).
        L'actionneur 3 positions s'arrête automatiquement à 90° (Miller) dans
        les deux sens : T1 et T2 (course complète) nécessitent 2 impulsions relais ;
        T3 et T4 (demi-course) n'en nécessitent qu'une.

          T1 : FC1(180°) → FC2(ISO,0°)     — course complète, 2 impulsions
          T2 : FC2(ISO,0°) → FC1(180°)     — course complète, 2 impulsions
          T3 : FC1(180°) → FC3(Miller,90°) — demi-course, 1 impulsion
          T4 : FC3(Miller,90°) → FC1(180°) — demi-course, 1 impulsion
        """
        fc_iso    = self.pilote._fc_par_diaphragme["iso"]    # fc_3voies_0
        fc_miller = self.pilote._fc_par_diaphragme["miller"] # fc_3voies_90
        fc_ferme  = "fc_3voies_180"
        res = {}

        # Pré-positionnement à FC1 (180°, fermeture complète)
        t = self._avancer_vanne_3voies(False, fc_ferme, "pré-pos fermeture complète")
        if t is None:
            return
        time.sleep(0.5)

        # T1 : FC1(180°) → FC2(ISO, 0°) — arrêt intermédiaire à 90° géré en interne
        t = self._avancer_vanne_3voies(True, fc_iso, "T1 fermeture → ISO")
        if t is None:
            return
        res["T1_fc1_to_iso_s"] = round(t, 2)
        time.sleep(0.5)

        # T2 : FC2(0°) → FC1(180°) — arrêt intermédiaire à 90° géré en interne
        t = self._avancer_vanne_3voies(False, fc_ferme, "T2 ISO → fermeture")
        if t is None:
            return
        res["T2_iso_to_fc1_s"] = round(t, 2)
        time.sleep(0.5)

        # T3 : FC1(180°) → FC3(Miller, 90°) — demi-course, arrêt automatique à 90°
        t = self._chronometrer_vanne(lambda: self.pilote.es.ecrire_vanne_diaphragme(True),
                                     fc_miller, "T3 fermeture → Miller")
        if t is None:
            return
        res["T3_fc1_to_miller_s"] = round(t, 2)
        time.sleep(0.5)

        # T4 : FC3(90°) → FC1(180°) — demi-course directe depuis Miller
        t = self._chronometrer_vanne(lambda: self.pilote.es.ecrire_vanne_diaphragme(False),
                                     fc_ferme, "T4 Miller → fermeture")
        if t is None:
            return
        res["T4_miller_to_fc1_s"] = round(t, 2)

        res["max_s"] = max(res.values())

        # Repositionnement en ISO pour la reprise normale
        self._avancer_vanne_3voies(True, fc_iso, "retour ISO")
        self.pilote.etat["diaphragme"] = "iso"

        self.resultats["vanne_3voies"] = res
        self.etat["statut"] = (
            f"terminé : T1={res['T1_fc1_to_iso_s']} s  "
            f"T2={res['T2_iso_to_fc1_s']} s  "
            f"T3={res['T3_fc1_to_miller_s']} s  "
            f"T4={res['T4_miller_to_fc1_s']} s")

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
        0 V → fc_pdc_ouvert → v_ouvert ; 10 V → fc_pdc_ferme → v_ferme."""
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

        v_ouvert = _mesure_butee(0.0, "fc_pdc_ouvert", "butée ouverture (0 V)")
        if v_ouvert is None:
            return
        v_ferme = _mesure_butee(10.0, "fc_pdc_ferme", "butée fermeture (10 V)")
        if v_ferme is None:
            return

        plage = abs(v_ferme - v_ouvert)
        if plage < 1.0:
            self.etat["statut"] = (
                f"plage insuffisante ({plage:.2f} V) — vérifier câblage")
            return

        self.resultats["calibration_pdc"] = {
            "v_min_recopie": v_ouvert,
            "v_max_recopie": v_ferme,
            "plage_v": round(plage, 3),
        }
        self.pilote.pdc_recopie_min = v_ouvert
        self.pilote.pdc_recopie_max = v_ferme
        self.etat["statut"] = (
            f"terminé : ouvert = {v_ouvert} V, fermé = {v_ferme} V "
            f"(plage {plage:.3f} V)")

    def _test_vanne_pdc(self) -> None:
        """Chrono de la vanne perte de charge via ses deux fins de course.
          Pré-pos : fermeture complète (fc_pdc_ferme).
          T1 : fc_pdc_ferme → fc_pdc_ouvert  (ouverture_s)
          T2 : fc_pdc_ouvert → fc_pdc_ferme  (fermeture_s)
        """
        res = {}

        # Pré-positionnement en fermeture complète (10 V = fermé)
        self.pilote.commander_vanne_perte_charge(10.0)
        t = self._attendre(lambda: bool(self._brutes().get("fc_pdc_ferme")),
                           90.0, "pré-pos fermeture PdC")
        if t is None:
            return
        self.pilote._pdc_position_estimee = 10.0
        time.sleep(0.5)

        # T1 : fermeture → ouverture complète (0 V = ouvert)
        self.pilote.commander_vanne_perte_charge(0.0)
        t = self._attendre(lambda: bool(self._brutes().get("fc_pdc_ouvert")),
                           90.0, "T1 fermeture → ouverture")
        if t is None:
            return
        res["ouverture_s"] = round(t, 2)
        self.pilote._pdc_position_estimee = 0.0
        time.sleep(0.5)

        # T2 : ouverture → fermeture complète (10 V = fermé)
        self.pilote.commander_vanne_perte_charge(10.0)
        t = self._attendre(lambda: bool(self._brutes().get("fc_pdc_ferme")),
                           90.0, "T2 ouverture → fermeture")
        if t is None:
            return
        res["fermeture_s"] = round(t, 2)
        self.pilote._pdc_position_estimee = 10.0

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
        voies = ("dp_diaph_500", "dp_iso_500")
        series: dict[str, list] = {v: [] for v in voies}
        t0 = time.monotonic()
        while time.monotonic() - t0 < duree_s:
            if self._annule.is_set():
                self.etat["statut"] = "annulé"
                return
            brutes = self._brutes()
            for v in voies:
                if v in brutes:
                    series[v].append(brutes[v])
            self.etat["statut"] = f"mesure du zéro ({time.monotonic() - t0:.0f} s)"
            time.sleep(dt)
        offsets = {v: round(statistics.fmean(s), 3)
                   for v, s in series.items() if s}
        if not offsets:
            self.etat["statut"] = "aucune mesure — capteurs absents ?"
            return
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

    def _dp_moyens(self, duree_s: float, statut: str) -> tuple | None:
        """Moyennes (dp_iso, dp_miller) sur duree_s ; None si annulé."""
        dt = self.pilote.periode
        iso: list[float] = []
        miller: list[float] = []
        t0 = time.monotonic()
        while time.monotonic() - t0 < duree_s:
            if self._annule.is_set():
                self.etat["statut"] = "annulé"
                return None
            brutes = self._brutes()
            iso.append(brutes.get("dp_iso_500") or 0.0)
            miller.append(brutes.get("dp_diaph_500") or 0.0)
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
        self.pilote.commander_vanne_perte_charge(10.0)
        ok = self._attendre_pdc_extreme(10.0, 90.0, "câblage PdC (fermeture)")
        rapport["pdc_fermeture"] = ok is not None
        self.pilote.commander_vanne_perte_charge(0.0)
        ok = self._attendre_pdc_extreme(0.0, 90.0, "câblage PdC (ouverture)")
        rapport["pdc_ouverture"] = ok is not None
        if self._annule.is_set():
            return

        # 3) vanne 3 voies : les trois fins de course sont atteignables
        #    (fin du parcours : position ISO supposée, prête pour le point 4)
        fc_iso = self.pilote._fc_par_diaphragme["iso"]
        fc_miller = self.pilote._fc_par_diaphragme["miller"]
        for vers_iso, fc, nom in ((False, "fc_3voies_180", "180°"),
                                  (True, fc_miller, "90°"),
                                  (True, fc_iso, "0°")):
            t = self._avancer_vanne_3voies(vers_iso, fc,
                                           f"câblage 3 voies ({nom})")
            rapport[f"v3_{fc}"] = t is not None
            if self._annule.is_set():
                return

        # 4) variateur : RUN + tension -> un écoulement doit apparaître
        self.pilote.mode_manuel_tension(u_essai)

        def _flux_present():
            b = self._brutes()
            return ((b.get("dp_iso_500") or 0.0)
                    + (b.get("dp_diaph_500") or 0.0) > 5.0
                    or (b.get("frequence_variateur") or 0.0) > 0.5)

        t = self._attendre(_flux_present, 30.0, "câblage variateur (montée)")
        rapport["variateur_repond"] = t is not None

        # 5) identification ISO/Miller par le capteur ΔP qui répond
        mapping = None
        if rapport["variateur_repond"] and rapport.get(f"v3_{fc_iso}") \
                and rapport.get(f"v3_{fc_miller}"):
            if self._annule.wait(3.0):                 # établissement du flux
                self.etat["statut"] = "annulé"
                return
            m1 = self._dp_moyens(4.0, "identification : position ISO supposée")
            if m1 is None:
                return
            self._avancer_vanne_3voies(False, fc_miller,
                                       "identification : vers Miller supposé")
            if self._annule.wait(2.0):
                self.etat["statut"] = "annulé"
                return
            m2 = self._dp_moyens(4.0, "identification : position Miller supposée")
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
            rapport["dp_position_iso"] = [round(m1[0], 1), round(m1[1], 1)]
            rapport["dp_position_miller"] = [round(m2[0], 1), round(m2[1], 1)]
        self.pilote.mode_manuel_tension(0.0)

        rapport["mapping_inverse"] = mapping == "inverse"
        defauts = [k for k, v in rapport.items()
                   if v is False and k != "mapping_inverse"]
        if mapping == "inverse":
            resume = ("MAPPING ISO/MILLER INVERSÉ — la position dite ISO fait "
                      "réagir le capteur Miller et inversement (vanne 3 voies "
                      "ou capteurs intervertis) ; « Appliquer » corrige "
                      "positions_vanne_3voies")
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
                       duree_max: float = 120.0) -> None:
        """Descend la tension par paliers depuis u_depart et détecte le
        décrochage du ventilateur (ΔP/Re qui s'effondre, devient bruité ou
        ne répond plus). Retient la dernière tension stable + marge.

        Préparation : ferme le bypass et ouvre la vanne perte de charge à 100 %
        (état maintenu en fin de test)."""
        dt = self.pilote.periode
        fenetre = max(6, int(4.0 / dt))

        # 0) préparation des vannes
        if not self._preparer_circuit_normal():
            return

        re_prec = self._mesure_palier(u_depart, fenetre, 0.03, duree_max,
                                      f"stabilisation à {u_depart:.1f} V")
        if re_prec is None:
            return
        u = u_depart
        derniere_stable = u
        re_a_derniere = re_prec
        paliers = [(round(u, 2), round(re_prec))]

        while u - pas > 0.05:
            if self._annule.is_set():
                self.etat["statut"] = "annulé"
                return
            u = round(u - pas, 2)
            self.pilote.commander_variateur_manuel(u)
            série = []
            t0 = time.monotonic()
            while time.monotonic() - t0 < 4.0:
                if self._annule.is_set():
                    self.etat["statut"] = "annulé"
                    return
                série.append(self.pilote.etat.get("re_mesure") or 0.0)
                self.etat["statut"] = f"palier descendant {u:.2f} V"
                time.sleep(dt)
            d = série[-fenetre:] if len(série) >= fenetre else série
            re_moy = statistics.fmean(d)
            bruit = (max(d) - min(d)) / max(re_moy, 1.0)
            non_monotone = re_moy >= re_a_derniere * 0.98
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
        for k, u in enumerate(tensions):
            re = self._mesure_palier(
                u, fenetre, 0.02, duree_max,
                f"palier {k+1}/{len(tensions)} à {u:.1f} V")
            if re is None:
                return
            points.append((u, re))
        self.pilote.mode_manuel_tension(0.0)

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
            "verdict": "quasi-linéaire" if lineaire
                       else "non linéaire — table de gains recommandée",
        }
        # construction de la table (toujours, elle est exacte même si linéaire)
        self._table_points = points
        self.etat["statut"] = (
            f"terminé : {len(points)} points, gain moyen {g_moy:.0f} Re/V, "
            f"non-linéarité {ecart_max:.1f} % → "
            + ("quasi-linéaire" if lineaire else "table de gains recommandée"))

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
            pos["fc_iso"], pos["fc_miller"] = (
                pos.get("fc_miller", "fc_3voies_90"),
                pos.get("fc_iso", "fc_3voies_0"))
            self.pilote._fc_par_diaphragme = {"iso": pos["fc_iso"],
                                              "miller": pos["fc_miller"]}
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
                f"PdC recopie : ouvert = {r['v_min_recopie']} V, "
                f"fermé = {r['v_max_recopie']} V")
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
            json.dumps(self.cfg, indent=2, ensure_ascii=False),
            encoding="utf-8")
        return "Appliqué et enregistré : " + " ; ".join(lignes)
