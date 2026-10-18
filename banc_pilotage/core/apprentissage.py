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
import logging
import statistics
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


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
                 "re_mapping": self._test_re_mapping,
                 "re_mapping_miller": self._test_re_mapping_miller,
                 "re_mapping_iso": self._test_re_mapping_iso,
                 "zero_capteurs": self._test_zero_capteurs,
                 "verif_cablage": self._test_verif_cablage}.get(test)
        if cible is None:
            return f"Test inconnu : {test}"
        if test == "indiciel" and not self.pilote.table_gains.est_valide():
            return ("Lancer d'abord la courbe de gain : les kp/ki proposés "
                    "par l'essai indiciel s'appliquent à la correction en "
                    "volts, qui s'appuie sur la pente de la table de gains.")
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
            except Exception as e:
                log.warning(f"Erreur mise en sécurité apprentissage : {e}")
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

    def _preparer_circuit_normal(self, diaphragme: str = "miller") -> bool:
        """Bypass fermé + vanne PdC grande ouverte + vanne 3 voies sur le
        diaphragme spécifié : le circuit de mesure du régime normal.

        Args:
            diaphragme: "miller" (défaut, bas Re < 5000) ou "iso" (haut Re > 5000)

        Indispensable avant toute caractérisation — une table de gains ou un
        tau mesurés circuit dérivé ou sur le mauvais diaphragme seraient
        silencieusement faux, et le feed-forward, le gain scheduling et le
        seuil bas-Re en hériteraient.

        SÉCURITÉ R1.2 : pour une transition ISO→Miller (HAUT→BAS), ouvre le
        bypass AVANT de basculer la vanne 3V, pour éviter que Miller voie un
        pic de ΔP avec débit résiduel."""
        es = self.pilote.es

        # Lire position actuelle de la vanne 3 voies
        angle_actuel = self.pilote._angle_vanne_3voies(self._brutes())
        diaph_actuel = self.pilote._diaphragme_par_angle.get(angle_actuel)
        vers_iso = (diaphragme.lower() == "iso")
        diaph_cible = "iso" if vers_iso else "miller"

        # Détection transition HAUT→BAS (ISO vers Miller)
        transition_haut_bas = (diaph_actuel == "iso" and diaph_cible == "miller")

        # SÉCURITÉ R1.2 : si HAUT→BAS, ouvrir bypass AVANT basculement vanne 3V
        if transition_haut_bas:
            t = self._chronometrer_vanne(lambda: es.ecrire_vanne_bypass(True),
                                         "fc_bypass_ouvert",
                                         "ouverture bypass (sécurité transition ISO→Miller)")
            if t is None:
                return False
            # Attendre stabilisation débit (bypass ouvert réduit le Re)
            time.sleep(2.0)

        # 1. Vanne 3 voies sur le diaphragme spécifié
        label = f"positionnement {'ISO (0°)' if vers_iso else 'Miller (90°)'}"
        t = self._avancer_vanne_3voies(vers_iso, label)
        if t is None:
            return False

        # CRITIQUE : forcer pilote à relire position 3V (sync état logiciel / matériel)
        # Sans cela, re_mesure calculé depuis mauvais capteur ΔP → stabilisation impossible
        self.pilote._position_initiale_lue = False

        # 2. Bypass fermé (circuit de mesure) — sauf si déjà fermé post-transition
        if transition_haut_bas:
            # Bypass a été ouvert pour la sécurité, le refermer maintenant
            t = self._chronometrer_vanne(lambda: es.ecrire_vanne_bypass(False),
                                         "fc_bypass_ferme", "fermeture bypass")
            if t is None:
                return False
        else:
            # Cas normal : s'assurer que bypass est fermé
            t = self._chronometrer_vanne(lambda: es.ecrire_vanne_bypass(False),
                                         "fc_bypass_ferme", "fermeture bypass")
            if t is None:
                return False

        # 3. Vanne PdC grande ouverte (pas de restriction)
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

        Lit l'état initial des FC, puis effectue les deux mesures :
          - Si déjà fermée : ouvre puis referme
          - Si déjà ouverte : ferme puis rouvre
          - Si position intermédiaire : va vers ouverture d'abord

        Attendu sur un ER20 POSI : environ 25 s dans chaque sens.
        """
        res = {}

        # Attendre que la boucle publie au moins un cycle de mesures
        self.etat["statut"] = "attente des mesures..."
        tentatives = 0
        for _ in range(10):
            tentatives += 1
            brutes = self._brutes()
            etat_dispo = bool(self.pilote.etat)
            cycles = self.pilote.etat.get("cycles", 0)

            # Debug : afficher ce qui est lu
            self.etat["statut"] = (f"lecture #{tentatives}: etat={etat_dispo}, "
                                   f"cycles={cycles}, brutes={'oui' if brutes else 'NON'}")

            if brutes and "fc_pdc_ferme" in brutes:
                break
            time.sleep(0.3)

        # Vérifier que les mesures sont disponibles
        if not brutes:
            self.etat["statut"] = "ERREUR : aucune mesure disponible (boucle pilote arrêtée ?)"
            return

        if "fc_pdc_ferme" not in brutes or "fc_pdc_ouvert" not in brutes:
            cles = list(brutes.keys())[:5]
            self.etat["statut"] = f"ERREUR : FC manquantes (clés: {cles}...)"
            return

        # Lire l'état initial des FC
        fc_ouvert = bool(brutes.get("fc_pdc_ouvert", False))
        fc_ferme = bool(brutes.get("fc_pdc_ferme", False))

        # Afficher explicitement ce qui est détecté
        if fc_ouvert and fc_ferme:
            self.etat["statut"] = "ERREUR : les deux FC actives (court-circuit ?)"
            return
        elif fc_ouvert and not fc_ferme:
            position_init = "OUVERTE"
        elif fc_ferme and not fc_ouvert:
            position_init = "FERMÉE"
        else:
            position_init = "INTERMÉDIAIRE"

        self.etat["statut"] = f"position détectée : {position_init} (FC1={fc_ouvert}, FC2={fc_ferme})"
        time.sleep(2.0)

        # Stratégie selon position initiale
        if position_init == "FERMÉE":
            # Déjà fermée : mesurer ouverture puis fermeture
            # T1 : ouverture (FC2 → FC1)
            self.pilote.commander_vanne_perte_charge(10.0)
            # Attendre que la vanne QUITTE FC2 (démarrage du mouvement)
            self._attendre(lambda: not self._brutes().get("fc_pdc_ferme", True),
                          5.0, "démarrage ouverture")
            # Puis chronométrer jusqu'à FC1
            t0 = time.monotonic()
            t = self._attendre(lambda: bool(self._brutes().get("fc_pdc_ouvert")),
                               90.0, "ouverture (fermée → ouverte)")
            if t is None:
                return
            res["ouverture_s"] = round(time.monotonic() - t0, 2)
            self.pilote._pdc_position_estimee = 10.0
            time.sleep(0.5)

            # T2 : fermeture (FC1 → FC2)
            self.pilote.commander_vanne_perte_charge(0.0)
            # Attendre que la vanne QUITTE FC1 (démarrage du mouvement)
            self._attendre(lambda: not self._brutes().get("fc_pdc_ouvert", True),
                          5.0, "démarrage fermeture")
            # Puis chronométrer jusqu'à FC2
            t0 = time.monotonic()
            t = self._attendre(lambda: bool(self._brutes().get("fc_pdc_ferme")),
                               90.0, "fermeture (ouverte → fermée)")
            if t is None:
                return
            res["fermeture_s"] = round(time.monotonic() - t0, 2)
            self.pilote._pdc_position_estimee = 0.0

        elif position_init == "OUVERTE":
            # Déjà ouverte : mesurer fermeture puis ouverture
            # T1 : fermeture (FC1 → FC2)
            self.pilote.commander_vanne_perte_charge(0.0)
            # Attendre que la vanne QUITTE FC1
            self._attendre(lambda: not self._brutes().get("fc_pdc_ouvert", True),
                          5.0, "démarrage fermeture")
            # Puis chronométrer jusqu'à FC2
            t0 = time.monotonic()
            t = self._attendre(lambda: bool(self._brutes().get("fc_pdc_ferme")),
                               90.0, "fermeture (ouverte → fermée)")
            if t is None:
                return
            res["fermeture_s"] = round(time.monotonic() - t0, 2)
            self.pilote._pdc_position_estimee = 0.0
            time.sleep(0.5)

            # T2 : ouverture (FC2 → FC1)
            self.pilote.commander_vanne_perte_charge(10.0)
            # Attendre que la vanne QUITTE FC2
            self._attendre(lambda: not self._brutes().get("fc_pdc_ferme", True),
                          5.0, "démarrage ouverture")
            # Puis chronométrer jusqu'à FC1
            t0 = time.monotonic()
            t = self._attendre(lambda: bool(self._brutes().get("fc_pdc_ouvert")),
                               90.0, "ouverture (fermée → ouverte)")
            if t is None:
                return
            res["ouverture_s"] = round(time.monotonic() - t0, 2)
            self.pilote._pdc_position_estimee = 10.0

        else:
            # Position intermédiaire : aller vers ouverture complète d'abord
            self.pilote.commander_vanne_perte_charge(10.0)
            t = self._attendre(lambda: bool(self._brutes().get("fc_pdc_ouvert")),
                               90.0, "pré-pos ouverture (depuis intermédiaire)")
            if t is None:
                return
            time.sleep(0.5)

            # T1 : fermeture (FC1 → FC2)
            self.pilote.commander_vanne_perte_charge(0.0)
            self._attendre(lambda: not self._brutes().get("fc_pdc_ouvert", True),
                          5.0, "démarrage fermeture")
            t0 = time.monotonic()
            t = self._attendre(lambda: bool(self._brutes().get("fc_pdc_ferme")),
                               90.0, "fermeture (ouverte → fermée)")
            if t is None:
                return
            res["fermeture_s"] = round(time.monotonic() - t0, 2)
            self.pilote._pdc_position_estimee = 0.0
            time.sleep(0.5)

            # T2 : ouverture (FC2 → FC1)
            self.pilote.commander_vanne_perte_charge(10.0)
            self._attendre(lambda: not self._brutes().get("fc_pdc_ferme", True),
                          5.0, "démarrage ouverture")
            t0 = time.monotonic()
            t = self._attendre(lambda: bool(self._brutes().get("fc_pdc_ouvert")),
                               90.0, "ouverture (fermée → ouverte)")
            if t is None:
                return
            res["ouverture_s"] = round(time.monotonic() - t0, 2)
            self.pilote._pdc_position_estimee = 10.0

        res["max_s"] = max(res.values())
        self.resultats["vanne_pdc"] = res
        self.etat["statut"] = (f"terminé : ouverture={res['ouverture_s']} s, "
                               f"fermeture={res['fermeture_s']} s")

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

    def _test_re_mapping(self, u_depart: float = 5.0, pas: float = 1.0,
                         t_palier_s: float = 12.0) -> None:
        """Caractérisation complète Re mapping : couvre toute la plage 250-15000 Re.

        OPTIMISÉ : pas 0.2→1.0V (zone normale), 0.05→0.1V (zone PDC ≤1V).
        Gain temps : ~67% grâce à R² > 99.9% (linéarité validée).

        Phase 1 : Descend la tension par paliers depuis u_depart et détecte le
        décrochage du ventilateur (Re qui ne baisse plus, devient bruité ou
        s'effondre). Retient la dernière tension stable + marge.

        Chaque palier dure t_palier_s et n'est mesuré que sur sa fin (hors
        transitoire) : avec un procédé lent, mesurer trop tôt fait paraître
        « non monotone » une descente parfaitement saine et déclare un faux
        décrochage dès le premier palier.

        Préparation : BYPASS OUVERT + vanne PDC OUVERTE (100%) + Miller (bas Re < 5000).
        CRITIQUE : u_min est utilisé en mode bas-Re avec bypass ouvert. Le mode
        bas-Re DÉMARRE avec PDC ouverte (Re = seuil), puis la PDC se FERME pour
        atteindre les très bas Re. Apprendre avec PDC ouverte garantit la cohérence
        avec le point de départ du mode bas-Re. Méthode validée empiriquement :
        - Phase 1 (PDC 100%) : variateur descend 5.3V→0.3V → couvre Re 5000-274
        - Phase 2 (variateur 0.3V) : PDC ferme 100%→50% → couvre Re 274-250"""
        dt = self.pilote.periode
        fenetre = max(6, int(4.0 / dt))
        es = self.pilote.es

        # 0) préparation : BYPASS OUVERT (condition réelle mode bas-Re)
        # Vanne 3V sur Miller
        vers_iso = False
        t = self._avancer_vanne_3voies(vers_iso, "positionnement Miller (90°)")
        if t is None:
            return

        # Forcer pilote à relire position réelle (sync état logiciel / matériel)
        self.pilote._position_initiale_lue = False

        # Bypass OUVERT (différence critique vs circuit normal)
        t = self._chronometrer_vanne(lambda: es.ecrire_vanne_bypass(True),
                                     "fc_bypass_ouvert", "ouverture bypass")
        if t is None:
            return

        # Vanne PdC OUVERTE à 100% (point de départ du mode bas-Re)
        self.pilote.commander_vanne_perte_charge_pct(100.0)
        ok = self._attendre_pdc_extreme(10.0, 90.0, "ouverture PDC à 100%")
        if ok is None:
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
                re_actuel = self.pilote.etat.get("re_mesure") or 0.0
                série.append(re_actuel)
                # Publier re_mesure en temps réel pour l'IHM
                self.etat["re_mesure"] = re_actuel
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
        pas_precedent = pas  # Pour calcul marge finale

        while u > 0.05:
            # Pas adaptatif : 0.1V en dessous de 1V pour zone PDC (optimisé)
            pas_actuel = 0.1 if u <= 1.0 else pas
            if u - pas_actuel < 0.05:  # Arrêt si prochain pas < 0.05V
                break
            u = round(u - pas_actuel, 2)
            r = _palier(u, f"palier descendant {u:.2f} V")
            if r is None:
                return
            re_moy, bruit = r
            # baisse attendue ≈ pas/u : en exiger au moins 40 % sépare une
            # descente saine (régime établi) d'un plateau de décrochage
            seuil_baisse = 1.0 - 0.4 * pas_actuel / max(u + pas_actuel, pas_actuel)
            non_monotone = re_moy >= re_a_derniere * seuil_baisse
            paliers.append((u, round(re_moy)))
            self.etat["paliers_re_mapping"] = list(paliers)
            # Critères de décrochage physiques uniquement (bruit ou non-monotonie)
            # Pas de limite basse arbitraire : descendre jusqu'au vrai décrochage
            if bruit > 0.12 or non_monotone:
                break                                  # décrochage détecté
            derniere_stable = u
            re_a_derniere = re_moy
            pas_precedent = pas_actuel  # Mémoriser pour marge

        self.pilote.commander_variateur_manuel(0.0)
        u_min_reco = round(min(derniere_stable + pas_precedent, u_depart), 2)
        self.pilote.u_min_ventilateur = u_min_reco

        # ---- Phase 2 : mesure u_seuil (tension au seuil en mode bas-Re) ----
        # Trouver quelle tension variateur donne Re = seuil_cible (5000)
        # en config bas-Re (bypass ouvert, PDC 100%).
        # Cette tension (u_seuil) est la tension variateur au seuil bas-Re, pour mapper
        # correctement la plage variateur.
        self.etat["statut"] = "mesure u_seuil (tension au seuil bas-Re)"

        # Config déjà correcte : bypass ouvert (Phase 1), PDC 100%
        # Seuil cible (Re de transition mode normal ↔ bas-Re)
        re_seuil_cible = 5000.0
        u_seuil = None

        # Recherche u_seuil par paliers montants depuis u_min
        # (on cherche la tension qui donne Re ≈ seuil_cible)
        u_test = u_min_reco
        pas_u = 0.5  # Pas de 0.5V pour recherche rapide
        re_precedent = re_a_derniere  # Re à u_min (Phase 1)

        # Monter par paliers jusqu'à dépasser le seuil
        while u_test < 10.0 and re_precedent < re_seuil_cible:
            u_test = round(u_test + pas_u, 2)
            self.pilote.commander_variateur_manuel(u_test)
            self.etat["statut"] = f"recherche u_seuil : test {u_test:.1f}V"
            time.sleep(8.0)  # Stabilisation

            # Mesurer Re sur 10 échantillons
            re_mesures = []
            for _ in range(10):
                if self._annule.is_set():
                    self.etat["statut"] = "annulé"
                    break
                re_actuel = self.pilote.etat.get("re_mesure", 0.0)
                re_mesures.append(re_actuel)
                time.sleep(dt)

            if self._annule.is_set():
                break

            if len(re_mesures) >= 5:
                re_moy = statistics.fmean(re_mesures)

                # Si on a dépassé le seuil, interpoler entre u_precedent et u_test
                if re_moy >= re_seuil_cible:
                    # Interpolation linéaire
                    u_precedent = u_test - pas_u
                    u_seuil = u_precedent + (re_seuil_cible - re_precedent) / (re_moy - re_precedent) * pas_u
                    u_seuil = round(u_seuil, 2)
                    break

                re_precedent = re_moy

        # Si pas trouvé (u_test a atteint 10V), utiliser la dernière valeur
        if u_seuil is None:
            u_seuil = min(u_test, 10.0)

        # ---- Phase 3 : mesure PDC minimale exploitable ----
        # Déterminer automatiquement la position PDC min pour adaptation au
        # vieillissement de l'équipement. Méthode : descendre PDC par paliers
        # avec variateur fixé à u_min, détecter le décrochage.
        self.etat["statut"] = "mesure PDC minimale exploitable"

        # Revenir en configuration bas-Re (bypass ouvert + variateur à u_min)
        t = self._chronometrer_vanne(lambda: es.ecrire_vanne_bypass(True),
                                     "fc_bypass_ouvert", "ouverture bypass")
        pdc_min_exploitable = None
        re_min_exploitable = None

        if t is not None:
            # Variateur à u_min (mesuré en Phase 1)
            self.pilote.commander_variateur_manuel(u_min_reco)

            # PDC ouverte au départ
            self.pilote.commander_vanne_perte_charge_pct(100.0)
            ok = self._attendre_pdc_extreme(10.0, 90.0, "ouverture PDC")

            if ok is not None:
                time.sleep(5.0)  # Stabilisation initiale

                # Descendre PDC par paliers adaptatifs : 10% au-dessus de 50%, 2% en dessous
                pdc_derniere_stable = 100.0
                re_derniere_stable = None
                pdc_pct = 100.0
                pas_pdc_precedent = 10.0

                while pdc_pct > 0:
                    self.pilote.commander_vanne_perte_charge_pct(pdc_pct)
                    self.etat["statut"] = f"test PDC {pdc_pct:.0f}% (recherche min)"
                    time.sleep(12.0)  # Stabilisation palier (procédé lent, cohérent avec Phase 1)

                    # Mesurer Re sur 10 échantillons
                    re_mesures = []
                    for _ in range(10):
                        if self._annule.is_set():
                            self.etat["statut"] = "annulé"
                            break
                        re_actuel = self.pilote.etat.get("re_mesure", 0.0)
                        re_mesures.append(re_actuel)
                        time.sleep(dt)

                    if self._annule.is_set():
                        break

                    if len(re_mesures) >= 5:
                        re_moy = statistics.fmean(re_mesures)
                        bruit = (max(re_mesures) - min(re_mesures)) / max(re_moy, 1.0)

                        # Critères de décrochage assouplis :
                        # - Re < 200 (écoulement effondré)
                        # - Bruit > 20% (instabilité forte, tests empiriques : 50% OK)
                        decrochage = (re_moy < 200.0 or bruit > 0.20)

                        if decrochage:
                            # Décrochage détecté : garder la position précédente
                            break
                        else:
                            # Palier stable : sauvegarder et continuer
                            pdc_derniere_stable = pdc_pct
                            re_derniere_stable = re_moy
                            pas_pdc_precedent = 2.0 if pdc_pct <= 50.0 else 10.0

                    # Calculer prochain palier (pas adaptatif)
                    pas_pdc = 2.0 if pdc_pct <= 50.0 else 10.0
                    pdc_pct = round(pdc_pct - pas_pdc, 1)
                    if pdc_pct < 0:
                        break

                # PDC min = dernière stable + marge (pas utilisé pour cette position)
                pdc_min_exploitable = min(pdc_derniere_stable + pas_pdc_precedent, 100.0)
                re_min_exploitable = re_derniere_stable

        # ---- Phase 4 : montée plage haute (Re > 5000, mode normal) ----
        # Couvrir la plage haute en mode normal : bypass fermé, PDC ouverte, ISO
        paliers_plage_haute = []

        if u_seuil and u_seuil > 0:
            self.etat["statut"] = "Phase 4 : montée plage haute (bypass fermé, ISO)"

            # Basculer en mode normal : fermer bypass
            t = self._chronometrer_vanne(lambda: es.ecrire_vanne_bypass(False),
                                        "fc_bypass_ferme", "fermeture bypass")
            if t is not None:
                # PDC grande ouverte (mode normal)
                self.pilote.commander_vanne_perte_charge_pct(100.0)
                ok = self._attendre_pdc_extreme(10.0, 90.0, "ouverture PDC à 100%")

                if ok is not None:
                    # Basculer sur ISO (diaphragme pour plage haute)
                    t = self._avancer_vanne_3voies(True, "basculement ISO (0°)")

                    if t is not None:
                        # Attendre stabilisation circuit
                        time.sleep(5.0)

                        # Montée progressive de u_seuil à 10V par paliers de 0.3V
                        pas_montee = 0.3
                        u = u_seuil

                        while u <= 10.0:
                            r = _palier(u, f"palier montant {u:.2f} V (plage haute)")
                            if r is None:
                                break
                            re_moy, bruit = r

                            # Arrêt si saturation ΔP
                            if self.pilote.etat.get("dp_sature"):
                                self.etat["statut"] = f"capteur ΔP saturé à {u:.1f}V"
                                break

                            paliers_plage_haute.append((u, round(re_moy)))
                            self.etat["paliers_plage_haute"] = list(paliers_plage_haute)

                            u = round(u + pas_montee, 2)
                            if u > 10.0:
                                u = 10.0
                            # Éviter doublon à 10V (vérifier liste non vide)
                            if paliers_plage_haute and u == paliers_plage_haute[-1][0]:
                                break

        # Arrêter variateur
        self.pilote.commander_variateur_manuel(0.0)

        self.resultats["re_mapping"] = {
            "u_min_exploitable_v": u_min_reco,
            "u_seuil_bas_re_v": u_seuil,
            "derniere_stable_v": derniere_stable,
            "re_a_u_min": round(re_a_derniere),
            "pdc_min_exploitable_pct": round(pdc_min_exploitable, 1) if pdc_min_exploitable else None,
            "re_min_exploitable": round(re_min_exploitable) if re_min_exploitable else None,
            "paliers_V_Re": [[v, re] for v, re in paliers],
            "paliers_plage_haute": [[v, re] for v, re in paliers_plage_haute],
        }

        msg_u_seuil = f", u_seuil ≈ {u_seuil:.1f}V" if u_seuil else ""
        msg_pdc_min = f", PDC min ≈ {pdc_min_exploitable:.0f}%" if pdc_min_exploitable else ""
        msg_re_min = f" (Re min ≈ {re_min_exploitable:.0f})" if re_min_exploitable else ""
        self.etat["statut"] = (
            f"terminé : u_min ≈ {u_min_reco}V{msg_u_seuil}{msg_pdc_min}{msg_re_min}, marges incluses")

    def _test_re_mapping_miller(self, u_depart: float = 5.0, pas: float = 1.0,
                                 t_palier_s: float = 12.0) -> None:
        """Caractérisation Re mapping MILLER : couvre plage basse 250-6500 Re.

        Optimisé pour R² > 99.91% : ~15 points suffisent (vs 46 avant).
        Gain temps : ~67% (9.2 min → 3 min).
        Zone PDC (≤1V) garde pas fin 0.1V pour précision vanne lente.

        Phase 1  : Descente variateur (bypass ouvert, PDC 100%, Miller)
        Phase 1b : Montée jusqu'à 6.5V pour créer chevauchement avec ISO
        Phase 2  : Mesure u_seuil (tension au seuil Re=5000)
        Phase 3  : Mesure PDC minimale exploitable

        Cette séquence peut être lancée indépendamment, puis complétée par
        re_mapping_iso pour la plage haute. Le chevauchement 5000-6500 Re
        permet de valider la cohérence avec la table ISO."""
        dt = self.pilote.periode
        fenetre = max(6, int(4.0 / dt))
        es = self.pilote.es

        # ---- Phase 1 : descente Miller (bypass ouvert, PDC 100%) ----
        vers_iso = False
        t = self._avancer_vanne_3voies(vers_iso, "positionnement Miller (90°)")
        if t is None:
            return

        # Forcer pilote à relire position réelle (sync état logiciel / matériel)
        self.pilote._position_initiale_lue = False

        t = self._chronometrer_vanne(lambda: es.ecrire_vanne_bypass(True),
                                     "fc_bypass_ouvert", "ouverture bypass")
        if t is None:
            return

        self.pilote.commander_vanne_perte_charge_pct(100.0)
        ok = self._attendre_pdc_extreme(10.0, 90.0, "ouverture PDC à 100%")
        if ok is None:
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
                re_actuel = self.pilote.etat.get("re_mesure") or 0.0
                série.append(re_actuel)
                # Publier re_mesure en temps réel pour l'IHM
                self.etat["re_mesure"] = re_actuel
                self.etat["statut"] = statut
                time.sleep(dt)
            d = série[-fenetre:] if len(série) >= fenetre else série
            re_moy = statistics.fmean(d)
            return re_moy, (max(d) - min(d)) / max(re_moy, 1.0)

        r = _palier(u_depart, f"stabilisation à {u_depart:.1f} V",
                    duree=3.0 * t_palier_s)
        if r is None:
            return
        re_a_derniere = r[0]
        u = u_depart
        derniere_stable = u
        paliers = [(round(u, 2), round(re_a_derniere))]
        pas_precedent = pas

        while u > 0.05:
            pas_actuel = 0.1 if u <= 1.0 else pas  # PDC : 0.05→0.1V (toujours dense)
            if u - pas_actuel < 0.05:
                break
            u = round(u - pas_actuel, 2)
            r = _palier(u, f"palier descendant {u:.2f} V")
            if r is None:
                return
            re_moy, bruit = r
            seuil_baisse = 1.0 - 0.4 * pas_actuel / max(u + pas_actuel, pas_actuel)
            non_monotone = re_moy >= re_a_derniere * seuil_baisse
            paliers.append((u, round(re_moy)))
            self.etat["paliers_re_mapping"] = list(paliers)
            # Critères de décrochage physiques uniquement (bruit ou non-monotonie)
            # Pas de limite basse arbitraire : descendre jusqu'au vrai décrochage
            if bruit > 0.12 or non_monotone:
                break
            derniere_stable = u
            re_a_derniere = re_moy
            pas_precedent = pas_actuel

        self.pilote.commander_variateur_manuel(0.0)
        u_min_reco = round(min(derniere_stable + pas_precedent, u_depart), 2)
        self.pilote.u_min_ventilateur = u_min_reco

        # ---- Phase 1b : montée Miller (pour couvrir au-delà du seuil 5000 Re) ----
        # Objectif : étendre la table Miller jusqu'à ~6000-6500 Re pour créer un
        # chevauchement avec la table ISO et assurer continuité au seuil 5000 Re.
        self.etat["statut"] = "Phase 1b : montée Miller (extension plage haute)"

        # Partir de u_depart et monter jusqu'à 6.5V max (cible ~6000-6500 Re)
        u_max_miller = 6.5
        u = u_depart
        pas_montee = 1.0  # Optimisé : 0.2→1.0V (R²>99.91% valide pas large)

        while u <= u_max_miller:
            u = round(u + pas_montee, 2)
            if u > u_max_miller:
                u = u_max_miller

            r = _palier(u, f"palier montant Miller {u:.2f} V")
            if r is None:
                return
            re_moy, bruit = r

            # Vérifier saturation ΔP Miller (100 Pa pleine échelle)
            brutes = self.pilote.etat.get("mesures_brutes", {})
            dp_miller = brutes.get("dp_miller_100", 0.0)
            if dp_miller > 95.0:  # 95% de saturation
                self.etat["statut"] = f"ΔP Miller saturé ({dp_miller:.1f} Pa) à {u:.1f}V"
                break

            # Ajouter le point à la liste (en évitant doublon avec u_depart)
            if u != u_depart:
                paliers.append((u, round(re_moy)))
                self.etat["paliers_re_mapping"] = list(paliers)

            # Sortir si on a atteint u_max_miller
            if u >= u_max_miller:
                break

        # Arrêter variateur avant Phase 2
        self.pilote.commander_variateur_manuel(0.0)

        # ---- Phase 2 : mesure u_seuil ----
        self.etat["statut"] = "mesure u_seuil (tension au seuil bas-Re)"
        re_seuil_cible = 5000.0
        u_seuil = None
        u_test = u_min_reco
        pas_u = 0.5
        re_precedent = re_a_derniere

        while u_test < 10.0 and re_precedent < re_seuil_cible:
            u_test = round(u_test + pas_u, 2)
            self.pilote.commander_variateur_manuel(u_test)
            self.etat["statut"] = f"recherche u_seuil : test {u_test:.1f}V"
            time.sleep(8.0)

            re_mesures = []
            for _ in range(10):
                if self._annule.is_set():
                    self.etat["statut"] = "annulé"
                    break
                re_actuel = self.pilote.etat.get("re_mesure", 0.0)
                re_mesures.append(re_actuel)
                time.sleep(dt)

            if self._annule.is_set():
                break

            if len(re_mesures) >= 5:
                re_moy = statistics.fmean(re_mesures)
                if re_moy >= re_seuil_cible:
                    u_precedent = u_test - pas_u
                    u_seuil = u_precedent + (re_seuil_cible - re_precedent) / (re_moy - re_precedent) * pas_u
                    u_seuil = round(u_seuil, 2)
                    break
                re_precedent = re_moy

        if u_seuil is None:
            u_seuil = min(u_test, 10.0)

        # ---- Phase 3 : mesure PDC minimale et table de gains PDC ----
        self.etat["statut"] = "mesure PDC minimale exploitable"
        t = self._chronometrer_vanne(lambda: es.ecrire_vanne_bypass(True),
                                     "fc_bypass_ouvert", "ouverture bypass")
        pdc_min_exploitable = None
        re_min_exploitable = None
        paliers_pdc = []  # Table de gains PDC : [(pdc_pct, re), ...]

        if t is not None:
            self.pilote.commander_variateur_manuel(u_min_reco)
            self.pilote.commander_vanne_perte_charge_pct(100.0)
            ok = self._attendre_pdc_extreme(10.0, 90.0, "ouverture PDC")

            if ok is not None:
                time.sleep(5.0)
                pdc_derniere_stable = 100.0
                re_derniere_stable = None
                pdc_pct = 100.0
                pas_pdc_precedent = 10.0

                while pdc_pct > 0:
                    self.pilote.commander_vanne_perte_charge_pct(pdc_pct)
                    self.etat["statut"] = f"test PDC {pdc_pct:.0f}% (recherche min)"
                    time.sleep(12.0)

                    re_mesures = []
                    for _ in range(10):
                        if self._annule.is_set():
                            self.etat["statut"] = "annulé"
                            break
                        re_actuel = self.pilote.etat.get("re_mesure", 0.0)
                        re_mesures.append(re_actuel)
                        time.sleep(dt)

                    if self._annule.is_set():
                        break

                    if len(re_mesures) >= 5:
                        re_moy = statistics.fmean(re_mesures)
                        bruit = (max(re_mesures) - min(re_mesures)) / max(re_moy, 1.0)
                        decrochage = (re_moy < 200.0 or bruit > 0.20)

                        if not decrochage:
                            # Sauvegarder le point dans la table PDC
                            paliers_pdc.append((pdc_pct, round(re_moy)))
                            self.etat["paliers_pdc"] = list(paliers_pdc)
                            pdc_derniere_stable = pdc_pct
                            re_derniere_stable = re_moy
                            pas_pdc_precedent = 2.0 if pdc_pct <= 50.0 else 10.0
                        else:
                            break

                    pas_pdc = 2.0 if pdc_pct <= 50.0 else 10.0
                    pdc_pct = round(pdc_pct - pas_pdc, 1)
                    if pdc_pct < 0:
                        break

                pdc_min_exploitable = min(pdc_derniere_stable + pas_pdc_precedent, 100.0)
                re_min_exploitable = re_derniere_stable

        self.pilote.commander_variateur_manuel(0.0)

        # Sauvegarder résultats Miller
        self.resultats["re_mapping_miller"] = {
            "u_min_exploitable_v": u_min_reco,
            "u_seuil_bas_re_v": u_seuil,
            "derniere_stable_v": derniere_stable,
            "re_a_u_min": round(re_a_derniere),
            "pdc_min_exploitable_pct": round(pdc_min_exploitable, 1) if pdc_min_exploitable else None,
            "re_min_exploitable": round(re_min_exploitable) if re_min_exploitable else None,
            "paliers_V_Re": [[v, re] for v, re in paliers],
            "paliers_PDC_Re": [[pdc, re] for pdc, re in paliers_pdc],
        }

        msg_u_seuil = f", u_seuil ≈ {u_seuil:.1f}V" if u_seuil else ""
        msg_pdc_min = f", PDC min ≈ {pdc_min_exploitable:.0f}%" if pdc_min_exploitable else ""
        msg_re_min = f" (Re min ≈ {re_min_exploitable:.0f})" if re_min_exploitable else ""
        self.etat["statut"] = (
            f"terminé Miller : u_min ≈ {u_min_reco}V{msg_u_seuil}{msg_pdc_min}{msg_re_min}")

    def _test_re_mapping_iso(self, u_seuil: float | None = None,
                             pas: float = 1.5, t_palier_s: float = 12.0) -> None:
        """Caractérisation Re mapping ISO : plage haute 4000-30000 Re.

        Optimisé pour R² > 99.99% avec pas adaptatif :
        - 1.5V jusqu'à 6V (~20000 Re) : montée rapide
        - 0.5V de 6V à 7V : transition
        - 0.2V au-delà de 7V (~24000 Re) : détection précise saturation ΔP

        Arrêt automatique à ΔP > 495 Pa (99% pleine échelle capteur 500 Pa).

        Phase 4a : Descente depuis u_seuil jusqu'à 3.5V (chevauchement avec Miller)
        Phase 4b : Montée depuis u_seuil jusqu'à 10V ou saturation ΔP

        Nécessite u_seuil (tension au seuil Re=5000), soit :
        - fourni en paramètre
        - lu depuis config["strategie_bas_re"]["u_seuil_bas_re_v"]
        - lu depuis resultats["re_mapping_miller"]["u_seuil_bas_re_v"]

        Circuit : bypass fermé, PDC 100%, ISO (plage haute).
        Le chevauchement 4000-5000 Re permet de valider la cohérence avec Miller."""
        dt = self.pilote.periode
        fenetre = max(6, int(4.0 / dt))
        es = self.pilote.es

        # Récupérer u_seuil depuis paramètre, config ou résultats précédents
        if u_seuil is None:
            u_seuil = self.cfg.get("strategie_bas_re", {}).get("u_seuil_bas_re_v")
        if u_seuil is None and "re_mapping_miller" in self.resultats:
            u_seuil = self.resultats["re_mapping_miller"].get("u_seuil_bas_re_v")
        if u_seuil is None or u_seuil <= 0:
            self.etat["statut"] = ("u_seuil manquant : lancez d'abord "
                                   "re_mapping_miller ou spécifiez u_seuil")
            return

        def _palier(u: float, statut: str, duree: float | None = None):
            """Applique u, attend duree, mesure sur la fin. Retourne
            (re_moyen, bruit_relatif) ou None si annulé."""
            duree = t_palier_s if duree is None else duree
            self.pilote.commander_variateur_manuel(u)
            série: list[float] = []
            t0 = time.monotonic()
            while time.monotonic() - t0 < duree:
                if self._annule.is_set():
                    self.etat["statut"] = "annulé"
                    return None
                re_actuel = self.pilote.etat.get("re_mesure") or 0.0
                série.append(re_actuel)
                # Publier re_mesure en temps réel pour l'IHM
                self.etat["re_mesure"] = re_actuel
                self.etat["statut"] = statut
                time.sleep(dt)
            d = série[-fenetre:] if len(série) >= fenetre else série
            re_moy = statistics.fmean(d)
            return re_moy, (max(d) - min(d)) / max(re_moy, 1.0)

        # ---- Phase 4 : montée plage haute ISO ----
        self.etat["statut"] = "Phase 4 : montée plage haute (bypass fermé, ISO)"
        paliers_plage_haute = []

        # Fermer bypass (mode normal)
        t = self._chronometrer_vanne(lambda: es.ecrire_vanne_bypass(False),
                                     "fc_bypass_ferme", "fermeture bypass")
        if t is None:
            return

        # PDC grande ouverte
        self.pilote.commander_vanne_perte_charge_pct(100.0)
        ok = self._attendre_pdc_extreme(10.0, 90.0, "ouverture PDC à 100%")
        if ok is None:
            return

        # Basculer sur ISO
        t = self._avancer_vanne_3voies(True, "basculement ISO (0°)")
        if t is None:
            return

        # CRITIQUE : forcer le pilote à relire la position réelle de la vanne 3V
        # sur les FCs. Sans cela, pilote.etat["diaphragme"] reste sur "miller" alors
        # que le matériel est sur "iso", et le calcul de re_mesure lit le mauvais
        # capteur ΔP → re_mesure proche de 0 pendant tous les paliers ISO !
        self.pilote._position_initiale_lue = False

        time.sleep(5.0)  # Stabilisation circuit

        # ---- Phase 4a : descente ISO (pour couvrir en dessous du seuil 5000 Re) ----
        # Objectif : étendre la table ISO jusqu'à ~4000 Re pour créer un
        # chevauchement avec la table Miller et assurer continuité au seuil 5000 Re.
        # IMPORTANT : Commencer depuis une tension BASSE (2.0V) car en config ISO
        # (bypass fermé), les tensions sont beaucoup plus basses qu'en Miller pour
        # obtenir le même Re. u_seuil est mesuré en config Miller, donc inutilisable ici.
        self.etat["statut"] = "Phase 4a : descente ISO (extension plage basse)"

        # Estimer tension départ pour Re~4000-5000 en config ISO
        # Utiliser gain global comme estimation grossière
        gain_re_v = self.pilote._gain_re_par_volt or 2000.0
        u_depart_iso = max(1.5, min(3.0, 4500.0 / gain_re_v))  # Cible Re≈4500
        u_min_iso = max(0.5, 3000.0 / gain_re_v)  # Arrêt à Re≈3000
        re_min_cible = 3000.0  # Arrêt si Re descend en dessous
        pas_descente = 0.5  # Optimisé : 0.15→0.5V (R²>99.99% valide pas large)

        # Premier palier au point de départ estimé
        u = u_depart_iso
        r = _palier(u, f"palier ISO {u:.2f} V (descente, point départ)")
        if r is not None:
            re_moy, bruit = r
            paliers_plage_haute.append((u, round(re_moy)))
            self.etat["paliers_plage_haute"] = list(paliers_plage_haute)

            # Descente depuis u_depart_iso
            while u >= u_min_iso:
                u = round(u - pas_descente, 2)
                if u < u_min_iso:
                    u = u_min_iso

                r = _palier(u, f"palier descendant ISO {u:.2f} V")
                if r is None:
                    break
                re_moy, bruit = r

                # Arrêt si Re trop faible (sortie de plage ISO)
                if re_moy < re_min_cible:
                    self.etat["statut"] = f"Re trop faible ({re_moy:.0f}) à {u:.1f}V, arrêt descente"
                    break

                # Ajouter le point à la liste
                paliers_plage_haute.append((u, round(re_moy)))
                self.etat["paliers_plage_haute"] = list(paliers_plage_haute)

                # Sortir si on a atteint u_min_iso
                if u <= u_min_iso:
                    break

        # Arrêter variateur avant montée
        self.pilote.commander_variateur_manuel(0.0)
        time.sleep(3.0)  # Pause avant de relancer la montée

        # ---- Phase 4b : montée progressive u_seuil → 10V ----
        # Pas adaptatif : gros pas en bas (montée rapide), pas fin en haut (précision saturation)
        # Note : on part directement du premier pas au-dessus de u_seuil pour éviter doublon
        pas_actuel = 0.1 if u_seuil >= 6.0 else pas
        u = round(u_seuil + pas_actuel, 2)

        while u <= 10.0:
            r = _palier(u, f"palier montant {u:.2f} V (plage haute)")
            if r is None:
                break
            re_moy, bruit = r

            # Arrêt si ΔP ISO > 495 Pa (99% de 500 Pa, seuil urgence)
            # On utilise le ΔP brut directement, pas le drapeau dp_sature (qui est à 90%)
            # pour exploiter toute la plage du capteur ISO.
            # Seuil relevé de 490→495 Pa pour maximiser la plage explorée.
            brutes = self.pilote.etat.get("mesures_brutes", {})
            dp_iso = brutes.get("dp_iso_500", 0.0)
            if dp_iso > 495.0:
                self.etat["statut"] = f"ΔP ISO saturé ({dp_iso:.0f} Pa) à {u:.1f}V, Re≈{re_moy:.0f}"
                # Ajouter ce dernier point avant d'arrêter
                paliers_plage_haute.append((u, round(re_moy)))
                self.etat["paliers_plage_haute"] = list(paliers_plage_haute)
                break

            paliers_plage_haute.append((u, round(re_moy)))
            self.etat["paliers_plage_haute"] = list(paliers_plage_haute)

            # Pas adaptatif progressif pour détecter précisément la saturation :
            # - 1.5V jusqu'à 6V (montée rapide, zone connue)
            # - 0.5V de 6V à 7V (transition)
            # - 0.2V au-delà de 7V (zone critique >24000 Re, détection saturation)
            if u >= 7.0:
                pas_actuel = 0.2  # Très dense au-delà de ~24000 Re
            elif u >= 6.0:
                pas_actuel = 0.5  # Transition
            else:
                pas_actuel = pas  # Rapide (1.5V par défaut)

            u = round(u + pas_actuel, 2)
            if u > 10.0:
                u = 10.0
            if paliers_plage_haute and u == paliers_plage_haute[-1][0]:
                break

        self.pilote.commander_variateur_manuel(0.0)

        # Sauvegarder résultats ISO
        self.resultats["re_mapping_iso"] = {
            "u_seuil_utilise_v": u_seuil,
            "paliers_plage_haute": [[v, re] for v, re in paliers_plage_haute],
        }

        self.etat["statut"] = (
            f"terminé ISO : {len(paliers_plage_haute)} paliers depuis "
            f"u_seuil={u_seuil:.1f}V (plage haute)")

    # ------------------------------------------------- essai indiciel

    def _stable(self, série: list, fenêtre: int, tol_rel: float) -> bool:
        if len(série) < fenêtre:
            return False
        d = série[-fenêtre:]
        m = statistics.fmean(d)
        return m != 0 and (max(d) - min(d)) / abs(m) < tol_rel

    def _test_indiciel(self, u1: float = 2.0, u2: float = 4.0,
                       duree_max: float = 120.0) -> None:
        # Circuit normal obligatoire : un tau mesuré circuit dérivé
        # fausserait kp/ki et tau_consigne_s.
        # Utilise Miller pour les tensions moyennes (2-4V typiques).
        if not self._preparer_circuit_normal("miller"):
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
            self.pilote.trim.kp = r["kp_propose"]
            self.pilote.trim.ki = r["ki_propose"]
            # mise en forme de référence calée sur le tau mesuré du procédé
            # (la consigne interne suit alors la trajectoire naturelle)
            reg["tau_consigne_s"] = r["tau_s"]
            self.pilote.tau_consigne = r["tau_s"]
            lignes.append(f"kp={r['kp_propose']}, ki={r['ki_propose']}, "
                          f"tau_consigne={r['tau_s']} s")
        if "re_mapping" in self.resultats:
            r = self.resultats["re_mapping"]
            self.cfg.setdefault("strategie_bas_re", {})["u_min_ventilateur_v"] = \
                r["u_min_exploitable_v"]
            self.pilote.u_min_ventilateur = r["u_min_exploitable_v"]
            lignes.append(f"u_min ventilateur = {r['u_min_exploitable_v']} V")
            # Sauvegarder u_seuil (tension au seuil bas-Re)
            if r.get("u_seuil_bas_re_v") is not None:
                self.cfg["strategie_bas_re"]["u_seuil_bas_re_v"] = r["u_seuil_bas_re_v"]
                self.pilote.u_seuil_bas_re = r["u_seuil_bas_re_v"]  # Appliquer au pilote en temps réel
                lignes.append(f"u_seuil bas-Re = {r['u_seuil_bas_re_v']} V (tension au seuil, bypass ouvert)")
            # Créer table de gains mode bas-Re à partir des paliers mesurés
            if r.get("paliers_V_Re"):
                from core.pilote import TableGains
                self.pilote.table_gains_bas_re = TableGains(r["paliers_V_Re"])
                lignes.append(f"table gains bas-Re ({len(r['paliers_V_Re'])} points, bypass ouvert)")
            # Créer table de gains mode NORMAL à partir des paliers plage haute
            if r.get("paliers_plage_haute"):
                from core.pilote import TableGains
                t_haute = TableGains(r["paliers_plage_haute"])
                if t_haute.est_valide():
                    self.pilote.table_gains = t_haute
                    lignes.append(f"table gains plage haute ({len(r['paliers_plage_haute'])} points, bypass fermé)")
                else:
                    lignes.append("table gains plage haute ignorée (Re non monotone)")
            # Sauvegarder PDC min et Re min mesurés (si disponibles)
            if r.get("pdc_min_exploitable_pct") is not None:
                self.cfg["strategie_bas_re"]["pdc_min_exploitable_pct"] = r["pdc_min_exploitable_pct"]
                self.pilote.pdc_min_exploitable_pct = r["pdc_min_exploitable_pct"]  # Appliquer au pilote en temps réel
                lignes.append(f"PDC min exploitable = {r['pdc_min_exploitable_pct']:.1f}%")
            if r.get("re_min_exploitable") is not None:
                self.cfg["strategie_bas_re"]["re_min_exploitable"] = r["re_min_exploitable"]
                lignes.append(f"Re min exploitable = {r['re_min_exploitable']}")
        # Gestion séparée des résultats re_mapping_miller
        if "re_mapping_miller" in self.resultats:
            r = self.resultats["re_mapping_miller"]
            self.cfg.setdefault("strategie_bas_re", {})["u_min_ventilateur_v"] = \
                r["u_min_exploitable_v"]
            self.pilote.u_min_ventilateur = r["u_min_exploitable_v"]
            lignes.append(f"u_min ventilateur = {r['u_min_exploitable_v']} V")
            # Sauvegarder u_seuil
            if r.get("u_seuil_bas_re_v") is not None:
                self.cfg["strategie_bas_re"]["u_seuil_bas_re_v"] = r["u_seuil_bas_re_v"]
                self.pilote.u_seuil_bas_re = r["u_seuil_bas_re_v"]
                lignes.append(f"u_seuil bas-Re = {r['u_seuil_bas_re_v']} V (tension au seuil, bypass ouvert)")
            # Table de gains mode bas-Re (tension variable, PDC 100%)
            if r.get("paliers_V_Re"):
                from core.pilote import TableGains
                self.pilote.table_gains_bas_re = TableGains(r["paliers_V_Re"])
                lignes.append(f"table gains bas-Re ({len(r['paliers_V_Re'])} points, bypass ouvert)")
            # Table de gains PDC (PDC variable, tension fixe u_min)
            if r.get("paliers_PDC_Re"):
                from core.pilote import TableGains
                self.pilote.table_gains_pdc = TableGains(r["paliers_PDC_Re"])
                lignes.append(f"table gains PDC ({len(r['paliers_PDC_Re'])} points, mode très bas Re)")
            # PDC min et Re min
            if r.get("pdc_min_exploitable_pct") is not None:
                self.cfg["strategie_bas_re"]["pdc_min_exploitable_pct"] = r["pdc_min_exploitable_pct"]
                self.pilote.pdc_min_exploitable_pct = r["pdc_min_exploitable_pct"]
                lignes.append(f"PDC min exploitable = {r['pdc_min_exploitable_pct']:.1f}%")
            if r.get("re_min_exploitable") is not None:
                self.cfg["strategie_bas_re"]["re_min_exploitable"] = r["re_min_exploitable"]
                lignes.append(f"Re min exploitable = {r['re_min_exploitable']}")
        # Gestion séparée des résultats re_mapping_iso
        if "re_mapping_iso" in self.resultats:
            r = self.resultats["re_mapping_iso"]
            # Table de gains mode NORMAL à partir des paliers plage haute
            if r.get("paliers_plage_haute"):
                from core.pilote import TableGains
                t_haute = TableGains(r["paliers_plage_haute"])
                if t_haute.est_valide():
                    self.pilote.table_gains = t_haute
                    lignes.append(f"table gains plage haute ({len(r['paliers_plage_haute'])} points, bypass fermé)")
                else:
                    lignes.append("table gains plage haute ignorée (Re non monotone)")
        if not lignes:
            return "Aucun résultat à appliquer."

        # ========== NOUVELLE ARCHITECTURE : FICHIERS SÉPARÉS ==========
        # Sauvegarde dans fichiers spécialisés (miller.json, iso.json) au lieu
        # d'un fichier monolithique pour éviter les pertes de données
        try:
            log.info("=" * 70)
            log.info("DÉBUT SAUVEGARDE APPRENTISSAGE (architecture fichiers séparés)")
            log.info("=" * 70)

            # Déterminer quel type d'apprentissage a été fait
            apprentissage_miller = "re_mapping_miller" in self.resultats
            apprentissage_iso = "plage_haute" in self.resultats or "re_mapping_iso" in self.resultats

            # Récupérer les mesures de conditions ambiantes
            mesures = self.pilote.etat.get("mesures_brutes", {})
            temperature_c = mesures.get("temperature_air_c")
            # Pression atmosphérique (TODO: ajouter capteur si disponible)
            pression_pa = None

            # ========== SAUVEGARDE APPRENTISSAGE MILLER ==========
            if apprentissage_miller and (self.pilote.table_gains_bas_re.est_valide() or
                                        self.pilote.table_gains_pdc.est_valide()):
                log.info("\n[Miller] Préparation sauvegarde apprentissage Miller")

                tables_miller = {}
                if self.pilote.table_gains_bas_re.est_valide():
                    tables_miller['table_gains_bas_re'] = self.pilote.table_gains_bas_re.to_list()
                    log.info(f"  - table_gains_bas_re: {len(tables_miller['table_gains_bas_re'])} points")

                if self.pilote.table_gains_pdc.est_valide():
                    tables_miller['table_gains_pdc'] = self.pilote.table_gains_pdc.to_list()
                    log.info(f"  - table_gains_pdc: {len(tables_miller['table_gains_pdc'])} points")

                # Ajouter régressions
                if self.pilote.table_gains_bas_re.est_valide():
                    reg = self.pilote.table_gains_bas_re.obtenir_regression()
                    if reg['valide']:
                        tables_miller['regression_miller'] = {
                            'a': reg['a'],
                            'b': reg['b'],
                            'r2': reg['r2']
                        }
                        log.info(f"  - regression_miller: a={reg['a']:.2f}, b={reg['b']:.2f}, R²={reg['r2']:.4f}")

                if self.pilote.table_gains_pdc.est_valide():
                    reg_pdc = self.pilote.table_gains_pdc.obtenir_regression()
                    if reg_pdc['valide']:
                        tables_miller['regression_pdc'] = {
                            'a': reg_pdc['a'],
                            'b': reg_pdc['b'],
                            'r2': reg_pdc['r2']
                        }

                parametres_miller = {}
                if self.pilote.u_min_ventilateur:
                    parametres_miller['u_min_ventilateur_v'] = self.pilote.u_min_ventilateur
                    log.info(f"  - u_min_ventilateur_v: {self.pilote.u_min_ventilateur}V")

                r_miller = self.resultats.get("re_mapping_miller", {})
                if r_miller.get("u_seuil_bas_re_v"):
                    parametres_miller['u_seuil_bas_re_v'] = r_miller["u_seuil_bas_re_v"]
                if r_miller.get("pdc_min_exploitable_pct"):
                    parametres_miller['pdc_min_exploitable_pct'] = r_miller["pdc_min_exploitable_pct"]
                if r_miller.get("re_min_exploitable"):
                    parametres_miller['re_min_exploitable'] = r_miller["re_min_exploitable"]

                self.pilote.gestionnaire_apprentissages.sauvegarder_miller(
                    tables_miller, parametres_miller,
                    temperature_c, pression_pa,
                    commentaire="Apprentissage via interface graphique"
                )
                log.info(f"  → Sauvegardé dans apprentissages/miller.json")

            # ========== SAUVEGARDE APPRENTISSAGE ISO ==========
            if apprentissage_iso and self.pilote.table_gains.est_valide():
                log.info("\n[ISO] Préparation sauvegarde apprentissage ISO")

                tables_iso = {}
                tables_iso['table_gains'] = self.pilote.table_gains.to_list()
                log.info(f"  - table_gains: {len(tables_iso['table_gains'])} points")

                # Ajouter régression
                reg = self.pilote.table_gains.obtenir_regression()
                if reg['valide']:
                    tables_iso['regression_iso'] = {
                        'a': reg['a'],
                        'b': reg['b'],
                        'r2': reg['r2']
                    }
                    log.info(f"  - regression_iso: a={reg['a']:.2f}, b={reg['b']:.2f}, R²={reg['r2']:.4f}")

                parametres_iso = {}
                if self.pilote._gain_re_par_volt:
                    parametres_iso['gain_re_par_volt'] = self.pilote._gain_re_par_volt
                    log.info(f"  - gain_re_par_volt: {self.pilote._gain_re_par_volt:.2f} Re/V")

                self.pilote.gestionnaire_apprentissages.sauvegarder_iso(
                    tables_iso, parametres_iso,
                    temperature_c, pression_pa,
                    commentaire="Apprentissage via interface graphique"
                )
                log.info(f"  → Sauvegardé dans apprentissages/iso.json")

            log.info("")
            log.info("=" * 70)
            log.info("FIN SAUVEGARDE - Succès (architecture fichiers séparés)")
            log.info("=" * 70)

        except OSError as e:
            log.error(f"Erreur lors de la sauvegarde: {e}")
            pass
        self.chemin_cfg.write_text(
            json.dumps(self.cfg, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        return "Appliqué et enregistré : " + " ; ".join(lignes)
