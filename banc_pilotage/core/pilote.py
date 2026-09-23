# -*- coding: utf-8 -*-
"""Pilote du banc : la boucle de régulation qui relie tout.

Séquence quand l'opérateur valide une consigne Re :
  1. estimation du débit massique visé (Re cible -> qm)
  2. choix du diaphragme (Miller / ISO) et commande de la vanne 3 voies
  3. calcul du ΔP attendu -> choix de la gamme capteur, pré-positionnement
     du variateur (feed-forward)
  4. boucle PI : lecture capteurs -> qm réel (ISO 5167 ou Miller) ->
     Re mesuré -> correction tension variateur, jusqu'à convergence
  5. surveillance continue (la T° dérive -> rho, mu recalculés à chaque cycle)
"""

from __future__ import annotations
import json
import threading
import time
from pathlib import Path
from bisect import bisect_left
from collections import deque
from statistics import fmean, pstdev

from core.journal import JournalEssai


class TableGains:
    """Relation tension variateur -> Re conduite, par points mesurés et
    interpolation linéaire. Sert au pré-positionnement (feed-forward) :
    pour un Re visé, on retrouve la tension par interpolation inverse.

    Points = liste de (volts, re) triés par tension croissante. Hors plage,
    on extrapole sur le segment extrême (pente locale), borné à 0-10 V.
    """

    def __init__(self, points: list | None = None):
        self.points = sorted(points or [], key=lambda p: p[0])

    def est_valide(self) -> bool:
        # au moins deux points distincts en tension et Re croissant
        return (len(self.points) >= 2
                and all(self.points[i][0] < self.points[i + 1][0]
                        and self.points[i][1] < self.points[i + 1][1]
                        for i in range(len(self.points) - 1)))

    def re_pour_tension(self, volts: float) -> float:
        pts = self.points
        if volts <= pts[0][0]:
            (x0, y0), (x1, y1) = pts[0], pts[1]
        elif volts >= pts[-1][0]:
            (x0, y0), (x1, y1) = pts[-2], pts[-1]
        else:
            i = bisect_left([p[0] for p in pts], volts)
            (x0, y0), (x1, y1) = pts[i - 1], pts[i]
        return y0 + (y1 - y0) * (volts - x0) / (x1 - x0)

    def tension_pour_re(self, re: float) -> float:
        """Interpolation inverse : tension à appliquer pour viser ce Re."""
        pts = self.points
        res = [p[1] for p in pts]
        if re <= res[0]:
            (x0, y0), (x1, y1) = pts[0], pts[1]
        elif re >= res[-1]:
            (x0, y0), (x1, y1) = pts[-2], pts[-1]
        else:
            i = bisect_left(res, re)
            (x0, y0), (x1, y1) = pts[i - 1], pts[i]
        v = x0 + (x1 - x0) * (re - y0) / (y1 - y0)
        return min(max(v, 0.0), 10.0)

    def to_list(self) -> list:
        return [[round(v, 3), round(re, 1)] for v, re in self.points]

from core.banc import Banc
from core.regulation import RegulateurPI

# capteur ΔP physiquement câblé sur chaque diaphragme (voir modbus.entrees).
# Source unique du mapping : toute sélection de capteur doit passer par ici.
CLE_DP = {"iso": "dp_iso_500", "miller": "dp_diaph_500"}

# Fins de course auxiliaires de la vanne 3 voies (carte GF3), une par butée.
# Câblage : FC2 ferme à 0°, FC1 ferme à 180°, FC3 est un contact NF qui
# s'ouvre en position intermédiaire — déclaré "inverse" dans config.json pour
# être lu comme une présence à 90°. Exactement une des trois doit être vraie ;
# toute autre combinaison signale une rupture de fil ou un commun 24 V absent.
FC_3VOIES = {"0": "fc_3voies_0", "90": "fc_3voies_90", "180": "fc_3voies_180"}


class Pilote:
    def __init__(self, banc: Banc, es, cfg: dict):
        self.banc = banc
        self.es = es                       # EntreesSorties (Wago ou simulateur)
        r = cfg["regulation"]
        self.periode = r["periode_s"]
        self.tolerance = r.get("tolerance_convergence_relative", 0.01)
        self.pi = RegulateurPI(
            kp=r["kp"], ki=r["ki"], periode_s=self.periode,
            rampe_v_par_s=r.get("rampe_v_par_s", 0.5),
            bande_morte_relative=r.get("bande_morte_relative", 0.0))
        self._rampe_nominale = self.pi.rampe
        # échelon relatif au-delà duquel un échelon de séquence réinitialise
        # le PI sur le feed-forward ; en deçà, l'état PI est conservé
        # (l'intégrale compense déjà l'erreur de la table de gains)
        self.echelon_reinit_ff = r.get("echelon_reinit_ff_relatif", 0.2)
        # gain scheduling : l'erreur du PI étant normalisée par la consigne,
        # le gain de boucle vaut ~kp/u_equilibre et varie avec le point de
        # fonctionnement ; quand le banc est caractérisé, on compense
        # (voir _echelle_gains). Désactivable au banc.
        self.gain_scheduling = r.get("gain_scheduling_actif", True)
        # fraction de la pleine échelle du capteur ΔP au-delà de laquelle
        # la mesure est considérée saturée (drapeau dp_sature)
        self.saturation_dp = r.get("saturation_dp_relative", 0.9)
        # fenêtre de moyennage du Re pour juger la convergence. Le bruit des
        # capteurs ΔP est du même ordre que la tolérance : un critère sur
        # échantillon isolé exige N tirages consécutifs dans la bande et
        # retombe dès qu'un seul en sort, alors que le débit est stable.
        self.fenetre_convergence = r.get("fenetre_convergence_s", 5.0)
        # bornes de vraisemblance de la PT100. Une sonde coupée sature le
        # module 750-450, et le coupleur Modbus n'a pas accès à ses octets de
        # statut : ce test est la seule détection possible. Sans lui, un T
        # aberrant fausse rho et mu, donc le Re, et le PI pousse le
        # ventilateur à fond sans que rien ne le signale.
        self.t_min_c = r.get("temperature_min_c", -40.0)
        self.t_max_c = r.get("temperature_max_c", 80.0)
        # vitesse de repli de la commande quand le capteur ΔP sature
        self.recul_saturation = r.get("recul_saturation_v_par_s", 0.5)
        # mise en forme de référence : le PI suit une consigne interne
        # filtrée au 1er ordre (trajectoire réalisable par la plante) au
        # lieu de l'échelon brut -> supprime le coup de bélier du terme P
        # et le dépassement associé. 0 = désactivé (échelon brut).
        self.tau_consigne = r.get("tau_consigne_s", 0.0)
        # temps de course des actionneurs (servent de CHIEN DE GARDE :
        # défaut vanne si la fin de course n'est pas atteinte à temps)
        act = cfg.get("actionneurs", {})
        self.t_vanne_3voies = act.get("vanne_3voies_temps_s", 11.0)
        self.t_vanne_bypass = act.get("vanne_bypass_temps_s", 11.0)
        self.t_vanne_pdc_course = act.get("vanne_perte_charge_course_s", 25.0)
        self.marge_manoeuvre = act.get("marge_apres_manoeuvre_s", 2.0)
        self.facteur_chien_garde = act.get("facteur_chien_garde", 1.5)

        # quel diaphragme est alimenté à chaque butée de la vanne 3 voies.
        # La position 180° n'est pas commandée : le constructeur ne la définit
        # pas pour un boisseau en L, et la borne 2 (ordre O) n'est pas câblée.
        pos = cfg.get("modbus", {}).get("positions_vanne_3voies", {})
        d0 = pos.get("diaphragme_a_0deg", "iso")
        d90 = pos.get("diaphragme_a_90deg", "miller")
        self._diaphragme_par_angle = {"0": d0, "90": d90}
        self._angle_par_diaphragme = {d0: "0", d90: "90"}

        # ordres TOR maintenus, réaffirmés à chaque cycle (voir _cycle) : sur
        # la carte GF3 l'actionneur est alimenté par ses bornes de commande,
        # un relais retombé le laisse sans résistance anti-condensation.
        self._diaphragme_commande: str | None = None
        self._bypass_commande: bool | None = None
        self._maintien_tor_suspendu = False

        # manœuvre en cours : None ou dict {motif, fc_attendue | angle_attendu,
        # t_debut, t_max}
        self._manoeuvre: dict | None = None
        self._t_fin_gel = 0.0              # petite marge après fin de course

        # vanne perte de charge : recopie 0-10 V (pas de fin de course).
        # L'estimation interne (consigne suivie à 10 V / temps de course)
        # sert de diagnostic : divergence avec la recopie = défaut actionneur.
        self._pdc_consigne = 0.0
        self._pdc_position_estimee = 0.0
        self._pdc_cycles_divergents = 0
        self._pdc_recopie_vue = False      # estimée pas encore calée sur la recopie
        self._t_dernier_cycle = None
        # butées calibrées de la recopie (0 % et 100 % réels en volts)
        self.pdc_recopie_min = act.get("pdc_recopie_min_v", 0.0)
        self.pdc_recopie_max = act.get("pdc_recopie_max_v", 10.0)

        # consigne
        self.re_cible = 0.0
        self.reference = "conduite"        # "conduite" ou "veine"
        self.regulation_active = False
        self.mode_manuel = False
        self.tension_manuelle = 0.0

        # séquence : profil (t_s, re_cible) suivi par la boucle, ou None
        self._sequence: dict | None = None

        # stratégie bas-Re : sous ce Re, on ouvre le bypass et on règle en
        # split-range (variateur + vanne perte de charge). Le seuil dérive de
        # la tension minimale exploitable du ventilateur, mesurée par
        # l'apprentissage (sinon valeur de config, sinon désactivé).
        srcfg = cfg.get("strategie_bas_re", {})
        self.bas_re_actif = srcfg.get("actif", True)
        self.seuil_bas_re = srcfg.get("seuil_re", 0)        # 0 = auto/désactivé
        self.u_min_ventilateur = srcfg.get("u_min_ventilateur_v", 0.0)
        # marge (×) appliquée à u_min pour dériver le seuil bas-Re auto,
        # et point de bascule PdC->variateur du split-range (échelle 0..10)
        self.marge_seuil_bas_re = srcfg.get("marge_seuil", 1.3)
        self.split_point = min(max(srcfg.get("split_range_point", 5.0), 0.1), 9.9)
        # facteur (×) sur les gains PI en bas-Re. 1.0 = nominal (le séquencement
        # de démarrage suffit à maîtriser le dépassement) ; à réduire au banc si
        # la vraie vanne PdC fait osciller la boucle.
        self.bas_re_scale = min(max(srcfg.get("gain_scale", 1.0), 0.05), 1.0)
        self._mode_bas_re = False          # état courant de la stratégie
        sel = cfg.get("selection_diaphragme", {})
        self._hysteresis_bypass = sel.get("hysteresis_re", 500)

        # zéros des capteurs ΔP (Pa), mesurés à débit nul par l'apprentissage
        # 'zero_capteurs' et soustraits des mesures (voir _dp_corrige)
        self.zero_capteurs = {k: v for k, v
                              in cfg.get("zero_capteurs", {}).items()
                              if not k.startswith("_")}
        # pleine échelle des capteurs ΔP : au-delà de ~95 %, le Re mesuré
        # plafonne au plafond du capteur, pas au banc (drapeau dp_sature)
        entrees = cfg.get("modbus", {}).get("entrees", {})
        self._dp_pleine_echelle = {
            cle: entrees.get(cle, {}).get("valeur_max", 500.0)
            for cle in CLE_DP.values()}

        # état de la boucle, déclaré explicitement (plutôt que créé au fil de
        # l'eau et lu via getattr) : sur du code de sécurité, une faute de
        # frappe sur un de ces noms doit lever une erreur, pas renvoyer un défaut.
        self._run_actif = None             # None = inconnu -> écriture RUN forcée au 1er cycle
        self._preparation_faite = False
        self._position_initiale_lue = False
        self._cycles_incoherence = 0
        self._cycles_dp_sature = 0         # cycles consécutifs ΔP proche de la pleine échelle
        self._cycles_t_invalide = 0        # cycles consécutifs de PT100 hors bornes
        self._derniere_T_valide = None     # tenue pendant un transitoire capteur
        self._plafond_u_sature = None      # tension gelée quand le ΔP sature
        # historique du Re pour la moyenne glissante (convergence + dispersion)
        self._historique_re = deque(
            maxlen=max(2, int(self.fenetre_convergence / self.periode)))
        self._convergee_depuis = None      # instant d'entrée en convergence
        self._re_conduite_prec = None      # garde-fou anti-transitoire de l'apprentissage du gain
        self._echelon_depuis = None        # Re cible avant le dernier échelon de séquence
        self._re_cible_conduite = 0.0      # Re conduite équivalent de la consigne (calé à la préparation)
        self._regime_pi = False            # régime pour lequel le PI est calé (True = bas-Re)
        self._consigne_interne = None      # consigne filtrée suivie par le PI (None = à ancrer)

        # état courant (lu par l'IHM)
        self.etat = {"diaphragme": "miller", "u_variateur": 0.0,
                     "convergee": False, "erreur_com": None}
        # gain statique estimé du banc [Re conduite / volt], appris en
        # fonctionnement -> pré-positionnement (feed-forward) des consignes.
        # Persisté sur disque pour que la première consigne d'une session
        # bénéficie du gain appris lors des sessions précédentes.
        # journal CSV des conditions d'essai (une session = un fichier)
        j = cfg.get("journal", {})
        dossier_j = Path(j.get("dossier", "journaux"))
        if not dossier_j.is_absolute():
            dossier_j = Path(__file__).resolve().parent.parent / dossier_j
        self.journal = JournalEssai(dossier_j, j.get("periode_s", 1.0)) \
            if j.get("actif", True) else None

        self._chemin_etat = Path(cfg.get("fichier_etat", "etat_banc.json"))
        if not self._chemin_etat.is_absolute():
            # relatif au dossier du programme, pas au répertoire de lancement
            self._chemin_etat = (Path(__file__).resolve().parent.parent
                                 / self._chemin_etat)
        self._gain_re_par_volt: float | None = None
        self.table_gains = TableGains()
        try:
            donnees = json.loads(self._chemin_etat.read_text(encoding="utf-8"))
            g = donnees.get("gain_re_par_volt")
            if g and 100 < g < 1e6:          # garde-fou de vraisemblance
                self._gain_re_par_volt = g
            pts = donnees.get("table_gains")
            if pts:
                t = TableGains(pts)
                if t.est_valide():
                    self.table_gains = t
            umin = donnees.get("u_min_ventilateur_v")
            if umin and 0 < umin < 10:
                self.u_min_ventilateur = umin
        except (OSError, ValueError):
            pass
        self._verrou = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -------------------------------------------------------------- commandes

    def definir_consigne(self, re_cible: float, reference: str) -> None:
        # nouvelle consigne : le plafond hérité d'une saturation précédente
        # n'a plus lieu d'être (il était calé sur l'ancien point de marche)
        self._plafond_u_sature = None
        self.pi.u_max = 10.0
        with self._verrou:
            self.re_cible = re_cible
            self.reference = reference
            # nouvelle consigne = nouvelle préparation (choix diaphragme,
            # feed-forward, Re conduite équivalent), même si la régulation
            # tourne : sans ce reset, un appel isolé laisserait le critère
            # bas-Re et le split-range calés sur l'ancienne consigne
            self._preparation_faite = False

    def _acquitter_defauts(self) -> None:
        """Efface les défauts latchés. Appelé au (re)démarrage : l'opérateur
        a vu l'alerte et relance en connaissance de cause. Sans cela un défaut
        vanne resterait affiché indéfiniment sur un banc qui régule
        normalement, et masquerait le défaut suivant."""
        for cle in ("defaut_vanne", "defaut_capteur", "defaut_variateur"):
            self.etat[cle] = None
        self._cycles_t_invalide = 0
        self._cycles_incoherence = 0
        self._plafond_u_sature = None
        self.pi.u_max = 10.0

    def demarrer_regulation(self) -> None:
        self._acquitter_defauts()
        with self._verrou:
            self.mode_manuel = False
            self.regulation_active = True
            self._sequence = None          # une consigne fixe annule la séquence
            self._preparation_faite = False
        self._echelon_depuis = None
        self.etat["sequence"] = None
        self._historique_re.clear()
        self._convergee_depuis = None
        if self.journal is not None:
            self.journal.ouvrir(time.monotonic())

    def demarrer_sequence(self, points: list, reference: str) -> str | None:
        """Suivi d'un profil (t_s, Re cible) : la consigne évolue par
        échelons aux échéances du profil ; le temps de la dernière ligne
        marque la fin, sa consigne est ensuite maintenue.
        Retourne un message d'erreur ou None."""
        if not points:
            return "Séquence vide."
        self._acquitter_defauts()
        with self._verrou:
            self.mode_manuel = False
            self.tension_manuelle = 0.0
            self.reference = reference
            self.re_cible = float(points[0][1])
            self.regulation_active = True
            self._sequence = {
                "points": [(float(t), float(re)) for t, re in points],
                "t0": time.monotonic(),
                "index": 0,
            }
        self._preparation_faite = False
        self._echelon_depuis = None        # 1er échelon : préparation complète
        self._historique_re.clear()
        self._convergee_depuis = None
        if self.journal is not None:
            self.journal.ouvrir(time.monotonic())
        return None

    def arreter_regulation(self) -> None:
        """Arrêt du variateur : coupe la régulation, le mode manuel ET la
        séquence (sinon la boucle réappliquerait une consigne au cycle
        suivant — inacceptable pour un bouton d'arrêt)."""
        with self._verrou:
            self.regulation_active = False
            self.mode_manuel = False
            self.tension_manuelle = 0.0
            self._sequence = None
        self._echelon_depuis = None        # PI remis à zéro : ne pas conserver
        self.etat["sequence"] = None
        self._historique_re.clear()
        self._convergee_depuis = None
        if self.journal is not None:
            self.journal.fermer()
        self.es.ecrire_variateur(0.0)
        self.es.ecrire_variateur_run(False)
        self._run_actif = False
        self.pi.reinitialiser(0.0)

    def mode_manuel_tension(self, volts: float) -> None:
        with self._verrou:
            self.mode_manuel = True
            self.regulation_active = False
            self.tension_manuelle = volts
            self._sequence = None          # le mode manuel annule la séquence
        self._echelon_depuis = None
        self.etat["sequence"] = None

    def mode_asservi(self) -> None:
        """Quitte le mode manuel sans démarrer la régulation."""
        with self._verrou:
            self.mode_manuel = False

    def commander_variateur_manuel(self, volts: float) -> None:
        """Mode manuel : fixe la tension variateur (RUN géré automatiquement)."""
        self.mode_manuel_tension(volts)

    def appliquer_banc(self, banc: Banc) -> None:
        """Remplace le moteur de calcul après modification de la
        configuration (géométries…). À appeler régulation arrêtée ;
        le gain appris est conservé (la dynamique du banc ne change pas)."""
        with self._verrou:
            self.banc = banc

    def appliquer_config(self, cfg: dict) -> None:
        """Relit les réglages mis en cache au démarrage, après une édition
        depuis l'IHM. À appeler régulation arrêtée.

        Sans cela, la moitié des champs de l'onglet Configuration étaient
        écrits dans config.json et n'agissaient qu'au redémarrage suivant :
        kp, ki, rampe, tolérance, chiens de garde, bornes PT100… Un réglage
        accessible qui ne fait rien est pire qu'un réglage absent, parce
        qu'on croit avoir agi. Le seuil de bascule et l'hystérésis, eux,
        vivent sur le Banc et étaient déjà repris par appliquer_banc().

        Les grandeurs APPRISES (gain, table, u_min) ne sont pas touchées :
        elles ne viennent pas de la saisie utilisateur.
        """
        with self._verrou:
            r = cfg.get("regulation", {})
            self.periode = r.get("periode_s", self.periode)
            self.tolerance = r.get("tolerance_convergence_relative",
                                   self.tolerance)
            self.pi.kp = r.get("kp", self.pi.kp)
            self.pi.ki = r.get("ki", self.pi.ki)
            self.pi.dt = self.periode
            self.pi.rampe = r.get("rampe_v_par_s", self.pi.rampe)
            self._rampe_nominale = self.pi.rampe
            self.pi.bande_morte = r.get("bande_morte_relative",
                                        self.pi.bande_morte)
            self.echelon_reinit_ff = r.get("echelon_reinit_ff_relatif",
                                           self.echelon_reinit_ff)
            self.gain_scheduling = r.get("gain_scheduling_actif",
                                         self.gain_scheduling)
            self.saturation_dp = r.get("saturation_dp_relative",
                                       self.saturation_dp)
            self.tau_consigne = r.get("tau_consigne_s", self.tau_consigne)
            self.t_min_c = r.get("temperature_min_c", self.t_min_c)
            self.t_max_c = r.get("temperature_max_c", self.t_max_c)
            self.recul_saturation = r.get("recul_saturation_v_par_s",
                                          self.recul_saturation)
            # la fenêtre est un nombre d'échantillons : la reconstruire à
            # chaque fois (la période a pu changer aussi), et repartir d'un
            # historique vide plutôt que de mélanger deux profondeurs de
            # moyennage dans une même moyenne
            self.fenetre_convergence = r.get("fenetre_convergence_s",
                                             self.fenetre_convergence)
            self._historique_re = deque(
                maxlen=max(2, int(self.fenetre_convergence / self.periode)))
            self._convergee_depuis = None

            act = cfg.get("actionneurs", {})
            self.t_vanne_3voies = act.get("vanne_3voies_temps_s",
                                          self.t_vanne_3voies)
            self.t_vanne_bypass = act.get("vanne_bypass_temps_s",
                                          self.t_vanne_bypass)
            self.t_vanne_pdc_course = act.get("vanne_perte_charge_course_s",
                                              self.t_vanne_pdc_course)
            self.marge_manoeuvre = act.get("marge_apres_manoeuvre_s",
                                           self.marge_manoeuvre)
            self.facteur_chien_garde = act.get("facteur_chien_garde",
                                               self.facteur_chien_garde)
            self.pdc_recopie_min = act.get("pdc_recopie_min_v",
                                           self.pdc_recopie_min)
            self.pdc_recopie_max = act.get("pdc_recopie_max_v",
                                           self.pdc_recopie_max)

            sr = cfg.get("strategie_bas_re", {})
            self.bas_re_actif = sr.get("actif", self.bas_re_actif)
            self.seuil_bas_re = sr.get("seuil_re", self.seuil_bas_re)
            self.marge_seuil_bas_re = sr.get("marge_seuil",
                                             self.marge_seuil_bas_re)
            self.split_point = min(max(sr.get("split_range_point",
                                              self.split_point), 0.1), 9.9)
            self.bas_re_scale = min(max(sr.get("gain_scale",
                                               self.bas_re_scale), 0.05), 1.0)

            # règle 6 de CLAUDE.md : les clés « _ » sont des commentaires
            self.zero_capteurs = {k: v for k, v
                                  in cfg.get("zero_capteurs", {}).items()
                                  if not k.startswith("_")}

    def commander_vanne_perte_charge(self, volts: float) -> None:
        """Consigne 0-10 V de la vanne proportionnelle (carte P6, sens usine) :
        0 V = FERMÉE, 10 V = grande ouverte. La position réelle est lue sur la
        recopie 0-10 V (etat['pdc_position']) ; l'ER20 POSI met ~25 s pour 90°.

        Ce sens est celui du constructeur et le plus sûr : sur rupture du câble
        de consigne ou coupure du coffret, la sortie analogique retombe à 0 V
        et la vanne étrangle le débit au lieu de l'ouvrir en grand."""
        self._pdc_consigne = min(max(volts, 0.0), 10.0)
        self.es.ecrire_vanne_perte_charge(self._pdc_consigne)

    def commander_vanne_perte_charge_pct(self, pct: float) -> None:
        """Consigne en % d'ouverture : 0 % = 0 V = fermée, 100 % = 10 V = ouverte."""
        self.commander_vanne_perte_charge(min(max(pct, 0.0), 100.0) / 10.0)

    def commander_bypass(self, ouverte: bool) -> None:
        """Vanne TOR bypass : régulation gelée jusqu'à la fin de course
        (fc_bypass_ouvert / fc_bypass_ferme), chien de garde sur le temps
        de course."""
        self._bypass_commande = ouverte
        self.es.ecrire_vanne_bypass(ouverte)
        self._demarrer_manoeuvre(
            motif="manœuvre bypass " + ("(ouverture)" if ouverte
                                        else "(fermeture)"),
            fc_attendue="fc_bypass_ouvert" if ouverte else "fc_bypass_ferme",
            t_course=self.t_vanne_bypass)

    def suspendre_maintien_tor(self) -> None:
        """Relâche le maintien des ordres TOR. Les séquences d'apprentissage
        manœuvrent les vannes directement pour chronométrer leurs courses ;
        sans cette suspension, la réaffirmation faite à chaque cycle les
        ramènerait aussitôt en place.

        C'est un état explicite et non la simple mise à None des ordres : la
        relecture de position remet sinon le maintien en route au cycle
        suivant, et la boucle se remettrait à contrarier la séquence."""
        self._maintien_tor_suspendu = True
        self._diaphragme_commande = None
        self._bypass_commande = None

    def reprendre_maintien_tor(self) -> None:
        """Rend la main à la boucle. Le maintien de la vanne 3 voies se
        rétablit au cycle suivant la relecture de position, que
        l'apprentissage force en sortie de séquence."""
        self._maintien_tor_suspendu = False

    def commander_diaphragme(self, position: str) -> None:
        """Vanne 3 voies vers "iso" ou "miller". La carte GF3 rejoint
        directement la butée demandée depuis n'importe quelle position :
        ordre F (borne 3) pour 0°, ordre I (borne 4) pour 90°.

        L'ordre est mémorisé et réaffirmé à chaque cycle, y compris quand la
        vanne est déjà en place — c'est lui qui alimente l'actionneur, donc sa
        résistance anti-condensation. Régulation gelée jusqu'à la butée
        (chien de garde)."""
        angle = self._angle_par_diaphragme.get(position)
        if angle is None:
            raise ValueError(f"position de vanne 3 voies inconnue : {position}")
        self._diaphragme_commande = angle
        self.es.ecrire_vanne_diaphragme(angle == "0")
        self.etat["diaphragme"] = position
        mesures = self.etat.get("mesures_brutes") or {}
        if self._angle_vanne_3voies(mesures) == angle:
            return                              # déjà en position
        self._demarrer_manoeuvre(
            motif=f"vanne 3 voies → {position}",
            t_course=self.t_vanne_3voies, angle_attendu=angle)

    @staticmethod
    def _angle_vanne_3voies(mesures: dict) -> str | None:
        """Butée occupée par la vanne 3 voies : "0", "90", "180", ou None si la
        lecture est inexploitable. Les trois contacts forment un codage
        un-parmi-trois : zéro ou plusieurs actifs = rupture de fil, commun 24 V
        absent, ou vanne en cours de course."""
        actifs = [angle for angle, cle in FC_3VOIES.items() if mesures.get(cle)]
        return actifs[0] if len(actifs) == 1 else None

    def _demarrer_manoeuvre(self, motif: str, t_course: float,
                            fc_attendue: str | None = None,
                            angle_attendu: str | None = None) -> None:
        self._manoeuvre = {
            "motif": motif,
            "fc_attendue": fc_attendue,      # vannes TOR : une fin de course
            "angle_attendu": angle_attendu,  # vanne 3 voies : une butée
            "t_debut": time.monotonic(),
            "t_max": time.monotonic() + t_course * self.facteur_chien_garde,
        }

    # ----------------------------------------------------------------- boucle

    def demarrer(self) -> None:
        self._thread = threading.Thread(target=self._boucle, daemon=True)
        self._thread.start()

    def arreter(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        if self.journal is not None:
            self.journal.fermer()
        self.es.fermer()
        # persistance des grandeurs apprises pour les sessions suivantes
        # (mêmes clés que la relecture du __init__ : gain, table, u_min)
        if self._gain_re_par_volt or self.table_gains.est_valide() \
                or self.u_min_ventilateur:
            try:
                donnees = {}
                if self._gain_re_par_volt:
                    donnees["gain_re_par_volt"] = self._gain_re_par_volt
                if self.table_gains.est_valide():
                    donnees["table_gains"] = self.table_gains.to_list()
                if self.u_min_ventilateur:
                    donnees["u_min_ventilateur_v"] = self.u_min_ventilateur
                self._chemin_etat.write_text(
                    json.dumps(donnees), encoding="utf-8")
            except OSError:
                pass

    def _boucle(self) -> None:
        self._preparation_faite = False
        while not self._stop.is_set():
            debut = time.monotonic()
            try:
                self._cycle()
                self.etat["erreur_com"] = None
            except Exception as exc:           # erreur Modbus : repli sûr
                self.etat["erreur_com"] = str(exc)
                # état sûr documenté : variateur 0 V ET ordre RUN retiré.
                # On latche l'arrêt (regulation_active=False), comme les
                # défauts variateur/vanne, pour ne pas relancer la soufflerie
                # tout seul dès que la communication revient.
                with self._verrou:
                    self.regulation_active = False
                try:
                    self.es.ecrire_variateur(0.0)
                except Exception:
                    pass
                try:
                    self.es.ecrire_variateur_run(False)
                    self._run_actif = False
                except Exception:
                    pass
            # durée réelle du cycle publiée : un dépassement durable de la
            # période signale une communication lente (le dt du PI est fixe)
            duree = time.monotonic() - debut
            self.etat["duree_cycle_s"] = round(duree, 3)
            reste = self.periode - duree
            if reste > 0:
                self._stop.wait(reste)

    def _cycle(self) -> None:
        mesures = self.es.lire_mesures()
        T = mesures["temperature"]
        # mesures brutes publiées telles quelles pour la supervision
        # (onglet lecture seule de l'IHM)
        self.etat["mesures_brutes"] = dict(mesures)
        self.etat["image_process"] = getattr(self.es, "image_process", None)
        self.etat["cycles"] = self.etat.get("cycles", 0) + 1

        # ---- vraisemblance de la PT100 -------------------------------------
        # Hors bornes = sonde coupée ou module saturé. rho et mu en dépendent,
        # donc le Re : sans ce test le PI poursuit une mesure fantaisiste et
        # emmène le ventilateur en butée sans lever le moindre défaut.
        if self.t_min_c <= T <= self.t_max_c:
            self._cycles_t_invalide = 0
            self._derniere_T_valide = T
        else:
            self._cycles_t_invalide += 1
            persistant = self._cycles_t_invalide > max(3, int(2.0 / self.periode))
            if not persistant and self._derniere_T_valide is not None:
                # transitoire : tenir la dernière valeur valide plutôt que de
                # propager l'aberration. La température de l'air varie
                # lentement, la figer quelques secondes est sans conséquence ;
                # en face, un T sous le zéro absolu fait rendre des grandeurs
                # COMPLEXES aux équations de diaphragme (puissance 1,5 d'un
                # nombre négatif), donc une exception au lieu d'un diagnostic.
                T = self._derniere_T_valide
                mesures["temperature"] = T
                self.etat["mesures_brutes"] = dict(mesures)
            else:
                self.es.ecrire_variateur(0.0)
                self.es.ecrire_variateur_run(False)
                self._run_actif = False
                with self._verrou:
                    self.regulation_active = False
                self.etat.update({
                    "defaut_capteur": f"température invraisemblable ({T:.1f} °C, "
                                      f"attendu {self.t_min_c:.0f} à "
                                      f"{self.t_max_c:.0f} °C) — PT100 coupée "
                                      "ou module saturé ; vérifier la sonde "
                                      "puis relancer",
                    "regulation": False, "u_variateur": 0.0,
                    "convergee": False,
                })
                return

        # au premier cycle : position réelle de la vanne 3 voies lue sur les
        # fins de course (ne pas supposer la position au démarrage)
        if not self._position_initiale_lue:
            angle = self._angle_vanne_3voies(mesures)
            if angle in self._diaphragme_par_angle:
                self.etat["diaphragme"] = self._diaphragme_par_angle[angle]
                # reprendre l'ordre correspondant : sans lui l'actionneur
                # resterait hors tension, donc sans anti-condensation
                if not self._maintien_tor_suspendu:
                    self._diaphragme_commande = angle
            self._position_initiale_lue = True

        # réaffirmation des ordres TOR maintenus. io_banc/wago.py déduplique
        # et ne réémet qu'au changement ou périodiquement ; c'est ce qui permet
        # à un coupleur redémarré de retrouver l'état des relais.
        if not self._maintien_tor_suspendu:
            if self._diaphragme_commande is not None:
                self.es.ecrire_vanne_diaphragme(self._diaphragme_commande == "0")
            if self._bypass_commande is not None:
                self.es.ecrire_vanne_bypass(self._bypass_commande)

        p_amont = mesures.get("p_amont_500", 0.0)
        # Chaque diaphragme a son propre capteur physique câblé sur ses prises
        # de pression : on lit le bon capteur selon la position de la vanne 3 voies.
        dp = self._dp_corrige(mesures, self.etat["diaphragme"])

        # ---- déroulement de la séquence (profil temps -> Re cible) ----------
        # Avancé même pendant un gel de manœuvre : le profil est temporel.
        with self._verrou:
            seq = self._sequence
        if seq is not None:
            pts = seq["points"]
            t_ecoule = time.monotonic() - seq["t0"]
            i = 0
            for j, (t_pt, _) in enumerate(pts):
                if t_ecoule >= t_pt:
                    i = j
            if i != seq["index"]:
                seq["index"] = i
                with self._verrou:
                    # consigne précédente mémorisée : un petit échelon
                    # conservera l'état PI au lieu de repartir du feed-forward
                    self._echelon_depuis = self.re_cible
                    self.re_cible = pts[i][1]
                # rechoix diaphragme + feed-forward sur le nouvel échelon
                self._preparation_faite = False
            terminee = t_ecoule >= pts[-1][0]
            self.etat["sequence"] = {
                "t": min(t_ecoule, pts[-1][0]), "t_fin": pts[-1][0],
                "etape": i + 1, "n": len(pts),
                "re_cible": pts[i][1], "terminee": terminee,
            }
            if terminee:
                # fin de profil : la consigne finale reste appliquée,
                # la régulation continue jusqu'à l'arrêt par l'opérateur
                with self._verrou:
                    self._sequence = None

        with self._verrou:
            re_cible = self.re_cible
            reference = self.reference
            regul = self.regulation_active
            manuel = self.mode_manuel
            u_manu = self.tension_manuelle

        diaph = self.etat["diaphragme"]

        # ---- préparation à l'activation de la consigne --------------------
        if regul and not self._preparation_faite and re_cible > 0:
            if reference == "veine":
                qm_vise = self.banc.re_veine_vers_qm(re_cible, T, p_amont)
            else:
                qm_vise = self.banc.re_conduite_vers_qm(re_cible, T, p_amont)
            re_cond_attendu = self.banc.qm_vers_re(
                qm_vise, T, self.banc.patm_pa + p_amont)["re_conduite"]

            nouveau_diaph = self.banc.choisir_diaphragme(re_cond_attendu, diaph)
            diaphragme_change = nouveau_diaph != diaph
            if diaphragme_change:
                # commande + gel jusqu'à la fin de course (chien de garde),
                # double impulsion gérée si la vanne part de 180°
                self.commander_diaphragme(nouveau_diaph)
                diaph = nouveau_diaph

            self.etat["dp_attendu"] = self.banc.dP_attendu(
                diaph, qm_vise, T, p_amont)
            # le critère bas-Re et le split-range travaillent en Re conduite :
            # mémoriser l'équivalent conduite de la consigne (rapport purement
            # géométrique en référence veine, identité en référence conduite)
            self._re_cible_conduite = re_cond_attendu
            seuil = self._seuil_bas_re()
            bas_re = self._besoin_bas_re(re_cond_attendu, seuil)
            # petit échelon de séquence, même diaphragme, même régime : on
            # conserve l'état PI (l'intégrale compense déjà l'erreur du
            # feed-forward ; se recaler sur la table referait converger
            # depuis la même erreur à chaque échelon)
            conserver_pi = (self._echelon_depuis is not None
                            and not diaphragme_change
                            and bas_re == self._regime_pi
                            and abs(re_cible - self._echelon_depuis)
                            < self.echelon_reinit_ff * self._echelon_depuis)
            self._echelon_depuis = None
            if conserver_pi:
                # seule l'échelle des gains suit le nouveau point de
                # fonctionnement, sans à-coup (intégrale recalée)
                self.pi.changer_gain_scale(
                    self._echelle_gains(bas_re, re_cond_attendu))
            else:
                self._preparer_pi(bas_re, re_cond_attendu, seuil)
            self._preparation_faite = True

        # ---- vanne perte de charge : recopie réelle + diagnostic ------------
        maintenant = time.monotonic()
        if self._t_dernier_cycle is not None:
            dt = maintenant - self._t_dernier_cycle
            vitesse = 10.0 / self.t_vanne_pdc_course      # V/s de course
            ecart_pdc = self._pdc_consigne - self._pdc_position_estimee
            pas = min(abs(ecart_pdc), vitesse * dt)
            self._pdc_position_estimee += pas if ecart_pdc > 0 else -pas
        self._t_dernier_cycle = maintenant

        pdc_recopie = mesures.get("pdc_recopie")
        if pdc_recopie is not None:
            self.etat["pdc_position"] = pdc_recopie
            if not self._pdc_recopie_vue:
                # première recopie : caler l'estimée sur la position réelle
                # (sinon fausse divergence tant que la vanne rejoint la consigne)
                self._pdc_position_estimee = pdc_recopie
                self._pdc_recopie_vue = True
                self._pdc_cycles_divergents = 0
                self.etat["defaut_pdc"] = False
            # diagnostic : recopie incohérente avec le modèle d'actionneur
            elif abs(pdc_recopie - self._pdc_position_estimee) > 1.0:
                self._pdc_cycles_divergents += 1
            else:
                self._pdc_cycles_divergents = 0
                # recalage du modèle sur la réalité
                self._pdc_position_estimee = pdc_recopie
            self.etat["defaut_pdc"] = self._pdc_cycles_divergents \
                > max(3, int(5.0 / self.periode))
        else:
            self.etat["pdc_position"] = self._pdc_position_estimee
            self.etat["defaut_pdc"] = False

        # ---- gel pendant une manœuvre de vanne TOR -------------------------
        # Fin de gel : fin de course atteinte (+ marge). Chien de garde :
        # défaut vanne si la fin de course n'arrive pas à temps.
        if self._manoeuvre is not None:
            m = self._manoeuvre
            if m["angle_attendu"] is not None:
                atteinte = (self._angle_vanne_3voies(mesures)
                            == m["angle_attendu"])
            else:
                atteinte = bool(mesures.get(m["fc_attendue"], False))
            if atteinte:
                if self._t_fin_gel == 0.0:
                    self._t_fin_gel = maintenant + self.marge_manoeuvre
                if maintenant >= self._t_fin_gel:
                    self._manoeuvre = None
                    self._t_fin_gel = 0.0
            elif maintenant > m["t_max"]:
                # DÉFAUT VANNE : fin de course jamais atteinte -> repli sûr
                # (variateur 0 V ET ordre RUN retiré, comme sur défaut variateur)
                self.es.ecrire_variateur(0.0)
                self.es.ecrire_variateur_run(False)
                self._run_actif = False
                with self._verrou:
                    self.regulation_active = False
                cible = m["fc_attendue"] or f"butée {m['angle_attendu']}°"
                self.etat.update({
                    "defaut_vanne": f"{m['motif']} : {cible} non atteinte "
                                    f"(vanne bloquée ou câblage)",
                    "manoeuvre": None, "regulation": False,
                    "u_variateur": 0.0, "convergee": False,
                })
                self._manoeuvre = None
                self._t_fin_gel = 0.0
                return
            if self._manoeuvre is not None:
                self.etat.update({
                    "temperature": T, "p_amont": p_amont, "dp": dp,
                    "manoeuvre": m["motif"],
                    "manoeuvre_restant_s": max(m["t_max"] - maintenant, 0.0),
                    "fc_attendue": m["fc_attendue"] or m["angle_attendu"],
                    "convergee": False, "regulation": regul, "manuel": manuel,
                })
                return
        self.etat["manoeuvre"] = None

        # ---- cohérence position vanne 3 voies / diaphragme attendu ---------
        # Hors manœuvre, la butée lue doit être celle du diaphragme actif.
        # Sinon : lecture invalide (rupture de fil, commun 24 V absent), vanne
        # restée à 180° après une manœuvre manuelle, ou plomberie croisée.
        angle_lu = self._angle_vanne_3voies(mesures)
        angle_attendu = self._angle_par_diaphragme.get(diaph)
        if FC_3VOIES["0"] in mesures and angle_lu != angle_attendu:
            self._cycles_incoherence += 1
        else:
            self._cycles_incoherence = 0
        self.etat["incoherence_vanne"] = self._cycles_incoherence \
            > max(3, int(3.0 / self.periode))

        # incohérence persistante en régulation : on réguerait sur un capteur
        # ΔP qui ne correspond plus à la position réelle de la vanne -> repli
        # sûr + latch (l'opérateur vérifie la vanne puis relance).
        if self.etat["incoherence_vanne"] and regul:
            self.es.ecrire_variateur(0.0)
            self.es.ecrire_variateur_run(False)
            self._run_actif = False
            with self._verrou:
                self.regulation_active = False
            self.etat.update({
                "defaut_vanne": "position vanne 3 voies inexploitable ou "
                                "incohérente avec le diaphragme actif "
                                f"(butée lue : {angle_lu or 'aucune'}) — "
                                "vérifier la vanne et son câblage, puis relancer",
                "regulation": False, "u_variateur": 0.0, "convergee": False,
            })
            return

        # ---- mesure du débit et des Reynolds -------------------------------
        # le diaphragme a pu changer pendant la préparation (vanne déjà en
        # position, sans manœuvre) : relire le ΔP sur le capteur correspondant
        # au diaphragme finalement retenu, pas celui du début de cycle.
        dp = self._dp_corrige(mesures, diaph)
        # capteur ΔP proche de sa pleine échelle : le Re mesuré plafonne au
        # plafond du capteur, pas au banc — une consigne au-delà n'est plus
        # atteignable par la mesure (le PI pousserait le ventilateur à fond
        # sans jamais converger). Signalé à l'opérateur via etat[dp_sature].
        cle_dp = CLE_DP.get(diaph, CLE_DP["miller"])
        if dp > self.saturation_dp * self._dp_pleine_echelle[cle_dp]:
            self._cycles_dp_sature += 1
        else:
            self._cycles_dp_sature = 0
        resultat = self.banc.qm_depuis_mesures(diaph, dp, T, p_amont)
        re_mesure = resultat["re_veine"] if reference == "veine" \
            else resultat["re_conduite"]

        # apprentissage du gain statique du banc (filtré), pour le
        # pré-positionnement des prochaines consignes. Suspendu en stratégie
        # bas-Re / bypass ouvert : le Re y dépend de la vanne PdC et du
        # bypass, pas seulement de la tension -> le rapport Re/volt n'est
        # plus le gain du banc.
        u_courant = self.etat.get("u_variateur", 0.0)
        re_cond = resultat["re_conduite"]
        # garde-fou : n'apprendre que sur un régime établi. Le 3G3MV ne
        # remonte pas d'information 'fréquence atteinte' (P1/P2 non câblées,
        # cf. plan de câblage) ; on exige donc que le Re ait peu bougé depuis
        # le cycle précédent, sans quoi une montée fausserait le gain.
        etabli = (self._re_conduite_prec is not None and re_cond > 0
                  and abs(re_cond - self._re_conduite_prec) < 0.01 * re_cond)
        if (etabli and u_courant > 0.3 and re_cond > 200
                and not self._mode_bas_re
                and not mesures.get("fc_bypass_ouvert")):
            g = re_cond / u_courant
            if self._gain_re_par_volt is None:
                self._gain_re_par_volt = g
            else:
                self._gain_re_par_volt += 0.05 * (g - self._gain_re_par_volt)
        self._re_conduite_prec = re_cond

        # ---- relecture autoritaire de la consigne avant d'agir -------------
        # Un arrêt (arreter_regulation) survenu APRÈS l'instantané de début de
        # cycle doit primer : sinon RUN et tension seraient réémis ici alors
        # que l'opérateur vient de couper. On relit l'état juste avant les
        # écritures pour réduire la fenêtre de course à ce strict minimum.
        with self._verrou:
            regul = self.regulation_active
            manuel = self.mode_manuel
            re_cible = self.re_cible
            u_manu = self.tension_manuelle

        # ---- ordre RUN du variateur (3G3MV : S1, n003=1) --------------------
        # RUN dès qu'une tension > 0 doit être appliquée ; arrêt sinon.
        run_voulu = (manuel and u_manu > 0.05) or (regul and re_cible > 0)
        self.es.ecrire_variateur_run(run_voulu)
        self._run_actif = run_voulu

        # défaut variateur remonté par le contact NF MB du 3G3MV. L'entrée est
        # déclarée "inverse" en config : un fil coupé se lit donc comme un
        # défaut, et non comme une absence de défaut.
        if mesures.get("defaut_variateur"):
            self.es.ecrire_variateur(0.0)
            self.es.ecrire_variateur_run(False)
            self._run_actif = False
            with self._verrou:
                self.regulation_active = False
            self.etat.update({
                "defaut_variateur": "défaut signalé par le 3G3MV "
                                    "(relais MA/MB) — acquitter sur le "
                                    "variateur puis relancer",
                "regulation": False, "u_variateur": 0.0, "convergee": False,
            })
            return
        self.etat["defaut_variateur"] = None

        # ---- capteur ΔP saturé : ramener la commande au mesurable -------
        # Au-delà de la pleine échelle du capteur, le Re mesuré plafonne alors
        # que le banc peut encore accélérer : l'erreur ne se résorbe jamais et
        # le PI pousse le ventilateur en butée pour rien. Geler la commande à
        # sa valeur du moment ne suffit pas — le pré-positionnement
        # feed-forward d'une consigne hors plage l'a déjà mise à 10 V. On la
        # fait donc RECULER tant que la saturation dure, puis on maintient :
        # le banc se stabilise juste sous la pleine échelle, au maximum
        # réellement mesurable.
        #
        # Le plafond est latché jusqu'à la consigne suivante (definir_consigne
        # ou redémarrage) : le relâcher dès la sortie de saturation ferait
        # repartir le PI vers la butée, donc un cycle limite.
        dp_sature = self._cycles_dp_sature >= max(3, int(1.0 / self.periode))
        if regul and dp_sature:
            depart = self._plafond_u_sature if self._plafond_u_sature is not None                 else max(self.etat.get("u_variateur", 0.0), 0.0)
            self._plafond_u_sature = max(
                depart - self.recul_saturation * self.periode, 0.0)
        if self._plafond_u_sature is not None:
            self.pi.u_max = self._plafond_u_sature

        # ---- décision stratégie bas-Re (mode automatique) ------------------
        # Seuil effectif : valeur de config si fixée, sinon dérivé de la
        # tension min exploitable et du gain (Re atteignable à u_min + marge).
        seuil = self._seuil_bas_re()

        # ---- action -------------------------------------------------------
        if manuel:
            # mode manuel : tension imposée, vanne PdC et bypass pilotés
            # explicitement par l'opérateur (commandes dédiées)
            u = u_manu
            self.es.ecrire_variateur(u)
            self._mode_bas_re = False
        elif regul and re_cible > 0:
            # critère sur le Re CONDUITE équivalent de la cible : une consigne
            # veine n'est pas à l'échelle du seuil (qui est un Re conduite)
            re_cible_cond = self._re_cible_conduite or re_cible
            besoin_bas_re = self._besoin_bas_re(re_cible_cond, seuil)
            if besoin_bas_re != self._regime_pi:
                # bascule de régime hors préparation (dérive du seuil appris) :
                # la commande PI change de signification (tension <-> split-
                # range), on la re-cale sur le feed-forward du nouveau régime
                self._preparer_pi(besoin_bas_re, re_cible_cond, seuil)
            # gestion du bypass selon le besoin (manœuvre = gel géré ailleurs)
            bypass_ouvert = bool(mesures.get("fc_bypass_ouvert"))
            if besoin_bas_re and not bypass_ouvert \
                    and self._manoeuvre is None:
                self.commander_bypass(True)
            elif not besoin_bas_re and bypass_ouvert \
                    and self._manoeuvre is None:
                self.commander_bypass(False)
            # en régime normal la vanne PdC doit être grande ouverte. Si elle
            # ne l'est pas (coupleur redémarré, sortie de bas-Re), on gèle la
            # régulation le temps de la course : 25 s de circuit étranglé sous
            # PI donneraient une montée faussée et un gain appris erroné.
            if not besoin_bas_re and "fc_pdc_ouvert" in mesures \
                    and not mesures["fc_pdc_ouvert"] and self._manoeuvre is None:
                self.commander_vanne_perte_charge(10.0)
                self._demarrer_manoeuvre(
                    motif="ouverture vanne perte de charge",
                    t_course=self.t_vanne_pdc_course,
                    fc_attendue="fc_pdc_ouvert")
            self._mode_bas_re = besoin_bas_re

            if besoin_bas_re and not bypass_ouvert:
                # by-pass pas encore ouvert : le refoulement n'est pas dérivé,
                # tout le débit irait à la veine. On coupe le ventilateur (0 V)
                # pour ne pas charger l'écoulement, et on pré-ferme la PdC (elle
                # se positionne pendant la manœuvre du by-pass), PI gelé. Dès le
                # by-pass ouvert, le PI reprend et le ventilateur remonte vers
                # u_min sur un débit déjà dérivé -> pas de dépassement.
                u = 0.0
                _, pdc_v = self._split_range(
                    self._cmd_ff_bas_re(re_cible_cond, seuil), True)
            else:
                # mise en forme de référence : consigne interne filtrée au
                # 1er ordre depuis la mesure courante (plancher 20 % de la
                # cible pour garder l'erreur normalisée bornée). Le PI suit
                # une trajectoire réalisable -> pas de dépassement lié au
                # coup de bélier du terme P sur l'échelon.
                if self.tau_consigne > 0:
                    if self._consigne_interne is None:
                        self._consigne_interne = max(re_mesure,
                                                     0.2 * re_cible)
                    a = self.periode / (self.tau_consigne + self.periode)
                    self._consigne_interne += a * (re_cible
                                                   - self._consigne_interne)
                    consigne_pi = self._consigne_interne
                else:
                    consigne_pi = re_cible
                # sortie unique du PI (gains, rampe et pré-positionnement
                # calés par _preparer_pi), répartie en split-range
                cmd = self.pi.calculer(consigne_pi, re_mesure)  # 0..10 (V équiv.)
                u, pdc_v = self._split_range(cmd, besoin_bas_re)
            if self._plafond_u_sature is not None:
                u = min(u, self._plafond_u_sature)
            self.es.ecrire_variateur(u)
            self.commander_vanne_perte_charge(pdc_v)
        else:
            u = 0.0
            self.es.ecrire_variateur(u)
            self._mode_bas_re = False

        # ---- convergence jugée sur la MOYENNE, pas sur l'échantillon ----
        # Le bruit des capteurs ΔP est du même ordre que la tolérance : exiger
        # N échantillons consécutifs dans la bande fait retomber le voyant
        # dès qu'un seul en sort, alors que le débit ne bouge pas. On moyenne
        # sur une fenêtre et on publie la dispersion, qui est l'information
        # dont l'opérateur a besoin pour chiffrer l'incertitude de ses relevés.
        if regul and re_cible > 0:
            self._historique_re.append(re_mesure)
        else:
            self._historique_re.clear()

        # référence locale : appliquer_config() remplace la fenêtre (sa
        # profondeur dépend de la période). Sans cela, un enregistrement de
        # configuration tombant entre ces lignes donnait fenetre_pleine sur
        # l'ancienne et une moyenne None sur la nouvelle -> TypeError, donc
        # un arrêt de sécurité assorti d'un message incompréhensible.
        fenetre = self._historique_re
        fenetre_pleine = len(fenetre) == fenetre.maxlen
        re_moyen = fmean(fenetre) if fenetre else None
        re_sigma = pstdev(fenetre) if len(fenetre) > 1 else None

        convergee = bool(
            regul and re_cible > 0 and fenetre_pleine
            and abs(re_moyen - re_cible) / re_cible < self.tolerance)
        if convergee:
            if self._convergee_depuis is None:
                self._convergee_depuis = maintenant
        else:
            self._convergee_depuis = None

        # motif d'indisponibilité de la stratégie bas-Re : sans la séquence
        # freq_min, u_min n'est pas connu, le seuil vaut 0 et la stratégie ne
        # s'engage jamais. Silencieux jusqu'ici : le bypass ne s'ouvrait pas
        # sans que rien n'explique pourquoi.
        bas_re_motif = None
        if self.bas_re_actif and not seuil:
            bas_re_motif = ("seuil bas-Re inconnu : lancer l'apprentissage "
                            "« freq_min » (ou fixer strategie_bas_re.seuil_re)")

        self.etat.update({
            "re_moyen": re_moyen,
            "re_ecart_type": re_sigma,
            "re_ecart_type_relatif": (re_sigma / re_moyen)
                                     if re_sigma and re_moyen else None,
            "convergee_depuis_s": (maintenant - self._convergee_depuis)
                                  if self._convergee_depuis else None,
            "fenetre_convergence_s": self.fenetre_convergence,
            "bas_re_motif": bas_re_motif,
            "plafond_u_sature": self._plafond_u_sature,
            "temperature": T, "p_amont": p_amont, "dp": dp,
            "qm": resultat["qm"], "qv": resultat.get("qv", 0.0),
            "re_conduite": resultat["re_conduite"],
            "re_veine": resultat["re_veine"],
            "rho": resultat.get("rho", 0.0),
            "C": resultat.get("C"), "u_variateur": u,
            "re_cible": re_cible, "reference": reference,
            "re_mesure": re_mesure, "convergee": convergee,
            "regulation": regul, "manuel": manuel,
            "pdc_consigne": self._pdc_consigne,
            "mode_bas_re": self._mode_bas_re,
            "seuil_bas_re_effectif": round(seuil) if seuil else None,
            "dp_sature": dp_sature,
        })

        # ---- journal des conditions d'essai --------------------------------
        # écrit depuis le thread du pilote, après publication de l'état ; le
        # module se désarme tout seul en cas d'erreur disque (voir journal.py)
        if self.journal is not None and self.journal.ouvert:
            self.journal.enregistrer(self.etat, maintenant)
        if self.journal is not None:
            self.etat["journal"] = str(self.journal.chemin) \
                if self.journal.chemin else None
            self.etat["journal_lignes"] = self.journal.lignes
            self.etat["journal_erreur"] = self.journal.erreur

    def _seuil_bas_re(self) -> float:
        """Re en dessous duquel on bascule en stratégie bas-Re (bypass +
        split-range PdC). Valeur de config si fixée, sinon dérivée de la
        tension mini exploitable et du gain (Re atteignable à u_min × marge)."""
        if self.seuil_bas_re:
            return self.seuil_bas_re
        if self.u_min_ventilateur and self.table_gains.est_valide():
            return self.table_gains.re_pour_tension(
                self.u_min_ventilateur) * self.marge_seuil_bas_re
        if self.u_min_ventilateur and self._gain_re_par_volt:
            return self.u_min_ventilateur * self._gain_re_par_volt \
                * self.marge_seuil_bas_re
        return 0.0

    def _dp_corrige(self, mesures: dict, diaphragme: str) -> float:
        """ΔP du capteur correspondant au diaphragme actif, corrigé du zéro
        appris à débit nul (décisif en bas-Re : le ΔP utile ne vaut que
        quelques Pa, un offset capteur serait poursuivi par le PI)."""
        cle = CLE_DP.get(diaphragme, CLE_DP["miller"])
        return mesures.get(cle, 0.0) - self.zero_capteurs.get(cle, 0.0)

    def _besoin_bas_re(self, re_cond_cible: float, seuil: float) -> bool:
        """Critère UNIQUE d'engagement de la stratégie bas-Re (préparation et
        boucle), avec hystérésis : entrée si le Re conduite cible < seuil,
        sortie seulement s'il dépasse seuil + hystérésis."""
        if not (self.bas_re_actif and seuil):
            return False
        if self._mode_bas_re:
            return re_cond_cible < seuil + self._hysteresis_bypass
        return re_cond_cible < seuil

    def _echelle_gains(self, bas_re: bool, re_cond_attendu: float) -> float:
        """Facteur d'échelle des gains PI au point de fonctionnement visé.

        L'erreur du PI est normalisée par la consigne : le gain de boucle
        vaut kp·G/Re_cible ≈ kp/u_équilibre et varie donc avec le point
        (boucle vive aux faibles Re, molle aux forts Re). Quand le banc est
        caractérisé (table ou gain appris), on compense en multipliant les
        gains par la tension d'équilibre estimée -> gain de boucle ≈ kp
        partout. Sans caractérisation ou si désactivé : échelle neutre.
        En bas-Re, la PdC tient la boucle : facteur dédié bas_re_scale."""
        if bas_re:
            return self.bas_re_scale
        if not self.gain_scheduling:
            return 1.0
        u_eq = self._u_equilibre(re_cond_attendu)
        if u_eq is None:
            return 1.0
        return min(max(u_eq, 0.5), 10.0)

    def _u_equilibre(self, re_cond_attendu: float) -> float | None:
        """Tension variateur d'équilibre estimée pour un Re conduite visé :
        table de gains, sinon gain appris, sinon None (banc non caractérisé).
        Source unique pour le gain scheduling ET le feed-forward."""
        if self.table_gains.est_valide():
            return self.table_gains.tension_pour_re(re_cond_attendu)
        if self._gain_re_par_volt:
            return re_cond_attendu / self._gain_re_par_volt
        return None

    def _preparer_pi(self, bas_re: bool, re_cond_attendu: float,
                     seuil: float) -> None:
        """Cale le PI sur le régime visé : échelle des gains, rampe et
        pré-positionnement (feed-forward). Appelé à la préparation d'une
        consigne et sur bascule de régime en cours de régulation."""
        self.pi.gain_scale = self._echelle_gains(bas_re, re_cond_attendu)
        if bas_re:
            # la commande PI pilote le split-range : initialisée près de
            # l'équilibre estimé, rampe bornée à ce que la vanne PdC
            # (actionneur lent) sait suivre
            self.pi.rampe = self._rampe_bas_re()
            self.pi.reinitialiser(self._cmd_ff_bas_re(re_cond_attendu, seuil))
        else:
            # régime normal : la commande PI est la tension variateur
            self.pi.rampe = self._rampe_nominale
            u_eq = self._u_equilibre(re_cond_attendu)
            u_ff = (min(max(u_eq, 0.0), 10.0) if u_eq is not None
                    else self.etat.get("u_variateur", 0.0))
            self.pi.reinitialiser(u_ff)
        self._regime_pi = bas_re
        self._consigne_interne = None      # ré-ancrage sur la mesure courante

    def _rampe_bas_re(self) -> float:
        """Rampe PI en bas-Re. Sur le segment PdC du split-range, 1 unité de
        commande vaut 10/split_point V de vanne ; la vanne parcourt 10 V en
        t_vanne_pdc_course s. Au-delà de split_point/t_course par seconde, la
        commande devance l'actionneur et l'intégrale s'enroule contre lui
        (l'anti-windup se recale sur la sortie commandée, pas sur la position
        réelle) -> dépassement puis oscillation lente."""
        return min(self._rampe_nominale,
                   self.split_point / self.t_vanne_pdc_course)

    def _cmd_ff_bas_re(self, re_cible: float, seuil: float) -> float:
        """Commande split-range estimée pour un Re cible en bas-Re — sert à
        pré-positionner PdC + variateur à l'entrée du régime (évite le
        transitoire de démarrage PdC grande ouverte). Re_umin = Re atteint à
        u_min, PdC ouverte (~ seuil / marge) correspond à cmd = split_point."""
        re_umin = seuil / self.marge_seuil_bas_re if self.marge_seuil_bas_re \
            else seuil
        sp = self.split_point
        if re_umin <= 0:
            return sp
        if re_cible <= re_umin:                    # segment PdC (variateur figé)
            return max(0.0, min(sp, sp * re_cible / re_umin))
        # segment variateur : reprise depuis u_min au-delà de Re_umin
        return max(sp, min(10.0,
                           sp + (10.0 - sp) * min((re_cible - re_umin) / re_umin,
                                                  1.0)))

    def _split_range(self, cmd: float, bas_re: bool) -> tuple[float, float]:
        """Répartit la commande PI unique (0..10) sur variateur + vanne PdC.

        Hors bas-Re : variateur seul sur toute la plage, vanne PdC ouverte
        (résistance minimale).

        En bas-Re : le variateur est maintenu à sa tension minimale
        exploitable (zone stable). Le bypass est ouvert en parallèle sur le
        ventilateur : une partie du refoulement court-circuite la boucle
        principale. La PdC, en série sur le refoulement vers la chambre,
        dose la fraction de débit qui atteint la veine :
          cmd faible  -> PdC fermée  (résistance forte, débit dévié par bypass, Re bas)
          cmd élevée  -> PdC ouverte (résistance faible, débit forcé vers la veine, Re haut)
        Au-delà de la pleine ouverture de la PdC, le variateur reprend la
        main depuis u_min pour continuer à monter (transition sans à-coup).
        """
        cmd = min(max(cmd, 0.0), 10.0)
        if not bas_re:
            return cmd, 10.0               # variateur seul, vanne grande ouverte (10 V)

        u_min = self.u_min_ventilateur or 1.0
        sp = self.split_point
        # Rappel convention (carte P6, sens usine) : 0 V = fermé, 10 V = ouvert.
        # segment bas (cmd ≤ sp) : variateur à u_min, PdC de fermée (0V) à ouverte (10V)
        # segment haut (cmd > sp) : PdC grande ouverte (10V), variateur monte depuis u_min
        if cmd <= sp:
            u = u_min
            pdc_v = 10.0 * cmd / sp                 # cmd=0 → 0V (fermé), cmd=sp → 10V (ouvert)
        else:
            pdc_v = 10.0                             # grande ouverte (10 V)
            u = u_min + (cmd - sp) / (10.0 - sp) * (10.0 - u_min)
        return u, pdc_v
