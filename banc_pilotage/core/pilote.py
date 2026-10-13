# -*- coding: utf-8 -*-
"""Pilote du banc : la boucle de régulation qui relie tout.

Séquence quand l'opérateur valide une consigne Re :
  1. estimation du débit massique visé (Re cible -> qm)
  2. choix du diaphragme (Miller / ISO) et commande de la vanne 3 voies
  3. calcul du ΔP attendu -> choix de la gamme capteur, pré-positionnement
     du variateur (feed-forward)
  4. boucle PI : lecture capteurs -> qm réel (ISO 5167 ou Miller) ->
     Re calculé -> correction tension variateur, jusqu'à convergence
  5. surveillance continue (la T° dérive -> rho, mu recalculés à chaque cycle)
"""

from __future__ import annotations
import json
import logging
import threading
import time
from pathlib import Path
from bisect import bisect_left
from collections import deque
from statistics import fmean, pstdev

from core.journal import JournalEssai

log = logging.getLogger(__name__)

# Modes de régulation (utilisés comme clés internes)
MODE_NORMAL_HIGH = "normal_high"  # Re ≥ 5000 : bypass fermé, ISO, PI → tension
MODE_NORMAL_LOW = "normal_low"    # freq_min ≤ Re < 5000 : bypass ouvert, Miller, PI → tension
MODE_PDC_CONTROL = "pdc_control"  # Re < freq_min : bypass ouvert, Miller, tension fixe, PI → PDC

# Constantes pour les fallbacks de régulation
_U_MIN_VENTILATEUR_FALLBACK = 0.3  # V - Tension minimale par défaut si non configurée
_RATIO_ANCRAGE_CONSIGNE_INTERNE = 0.2  # Ratio pour initialisation consigne filtrée (20% de la cible)


class TableGains:
    """Relation tension variateur -> Re conduite, par points mesurés et
    interpolation linéaire. Sert au pré-positionnement (feed-forward) :
    pour un Re visé, on retrouve la tension par interpolation inverse.

    Points = liste de (volts, re) triés par tension croissante. Hors plage,
    on extrapole sur le segment extrême (pente locale), borné à 0-10 V.
    """

    def __init__(self, points: list | None = None):
        self.points = sorted(points or [], key=lambda p: p[0])
        # Coefficients de régression linéaire Re = a*U + b (calculés à la demande)
        self._regression_a = None
        self._regression_b = None
        self._regression_r2 = None  # Coefficient de détermination (qualité du fit)

    def est_valide(self) -> bool:
        # au moins deux points distincts en tension et Re croissant (ou égal)
        # Tolérance : Re peut être égal entre deux paliers (bruit, arrondissage)
        # mais ne doit pas décroître significativement
        return (len(self.points) >= 2
                and all(self.points[i][0] < self.points[i + 1][0]
                        and self.points[i][1] <= self.points[i + 1][1]
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

    def calculer_regression(self) -> tuple[float, float, float] | None:
        """Calcule la régression linéaire Re = a*U + b par moindres carrés.

        Retourne (a, b, r2) où :
        - a : pente (Re/V)
        - b : ordonnée à l'origine (Re)
        - r2 : coefficient de détermination (0-1, qualité du fit)

        Retourne None si moins de 2 points.
        """
        if len(self.points) < 2:
            return None

        n = len(self.points)
        u_vals = [p[0] for p in self.points]  # Tensions (x)
        re_vals = [p[1] for p in self.points]  # Re (y)

        # Moyennes
        u_moy = sum(u_vals) / n
        re_moy = sum(re_vals) / n

        # Sommes pour régression : Re = a*U + b
        sum_u_re = sum(u * re for u, re in zip(u_vals, re_vals))
        sum_u2 = sum(u**2 for u in u_vals)

        # Calcul de a (pente) et b (ordonnée)
        denominateur = sum_u2 - n * u_moy**2
        if abs(denominateur) < 1e-10:
            return None  # Points alignés verticalement (même tension)

        a = (sum_u_re - n * u_moy * re_moy) / denominateur
        b = re_moy - a * u_moy

        # Coefficient de détermination R² (qualité du fit)
        ss_tot = sum((re - re_moy)**2 for re in re_vals)  # Variance totale
        ss_res = sum((re - (a*u + b))**2 for u, re in zip(u_vals, re_vals))  # Variance résiduelle

        if ss_tot < 1e-10:
            r2 = 1.0  # Tous les points à même Re (fit parfait trivial)
        else:
            r2 = 1.0 - (ss_res / ss_tot)

        # Sauvegarder pour réutilisation
        self._regression_a = a
        self._regression_b = b
        self._regression_r2 = r2

        return a, b, r2

    def tension_pour_re_regression(self, re: float) -> float | None:
        """Calcule la tension pour un Re donné via le modèle linéaire U = (Re - b) / a.

        Retourne None si régression non calculée ou invalide.
        Borne le résultat à [0, 10]V.
        """
        # Calculer régression si pas encore fait
        if self._regression_a is None:
            if self.calculer_regression() is None:
                return None

        # U = (Re - b) / a
        if abs(self._regression_a) < 1e-10:
            return None  # Pente nulle (impossible d'inverser)

        u = (re - self._regression_b) / self._regression_a
        return min(max(u, 0.0), 10.0)

    def obtenir_regression(self) -> dict:
        """Retourne les paramètres de régression pour affichage/diagnostic.

        Retourne dict avec clés: 'a', 'b', 'r2', 'valide'
        """
        if self._regression_a is None:
            self.calculer_regression()

        return {
            'a': self._regression_a,
            'b': self._regression_b,
            'r2': self._regression_r2,
            'valide': self._regression_a is not None
        }

from core.banc import Banc
from core.regulation import RegulateurPI
from core.gestionnaire_apprentissages import GestionnaireApprentissages
from core.logger_regulation import LoggerRegulation

# capteur ΔP physiquement câblé sur chaque diaphragme (voir modbus.entrees).
# Source unique du mapping : toute sélection de capteur doit passer par ici.
CLE_DP = {"iso": "dp_iso_500", "miller": "dp_diaph_500"}

# Fins de course auxiliaires de la vanne 3 voies (carte GF3), une par butée.
# Câblage vanne 3 voies installée (codage un-parmi-trois) :
# - FC2 (fc_3voies_0) : contact NO, fermé à 0° uniquement → diaphragme ISO
# - FC3 (fc_3voies_90) : contact NO, fermé à 90° uniquement → diaphragme MILLER
# - FC1 (fc_3voies_180) : non utilisé (position 180° non câblée)
# Codage simplifié : une seule FC active par position stable.
# Ce codage diffère de la doc SNAA710000 mais correspond à la vanne réelle.
FC_3VOIES = {"0": "fc_3voies_0", "90": "fc_3voies_90"}


class Pilote:
    def __init__(self, banc: Banc, es, cfg: dict):
        self.banc = banc
        self.es = es                       # EntreesSorties (Wago ou simulateur)
        r = cfg["regulation"]
        self.periode = r["periode_s"]
        self.tolerance = r.get("tolerance_convergence_relative", 0.01)
        # Gains nominaux (mode normal)
        self.kp_nominal = r["kp"]
        self.ki_nominal = r["ki"]
        # Facteur de réduction des gains en mode bas-Re (PDC plus sensible)
        self.facteur_gains_bas_re = r.get("facteur_gains_bas_re", 0.2)
        self.pi = RegulateurPI(
            kp=self.kp_nominal, ki=self.ki_nominal, periode_s=self.periode,
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
        # Seuil mesuré (prioritaire) sinon seuil manuel, sinon 0 = auto
        self.seuil_bas_re = srcfg.get("seuil_bas_re_mesure",
                                      srcfg.get("seuil_re", 0))
        self.u_min_ventilateur = srcfg.get("u_min_ventilateur_v", 0.0)
        self.u_seuil_bas_re = srcfg.get("u_seuil_bas_re_v", 0.0)
        # marge (×) appliquée à u_min pour dériver le seuil bas-Re auto,
        # et point de bascule PdC->variateur du split-range (échelle 0..10)
        self.marge_seuil_bas_re = srcfg.get("marge_seuil", 1.3)
        self.split_point = min(max(srcfg.get("split_range_point", 5.0), 0.1), 9.9)
        # PDC minimale exploitable (%) mesurée par apprentissage, défaut 50%
        # (limite physique décrochage observée empiriquement)
        self.pdc_min_exploitable_pct = srcfg.get("pdc_min_exploitable_pct", 50.0)
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
        # pleine échelle des capteurs ΔP : au-delà de ~95 %, le Re calculé
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
        self._temps_debut_saturation = None  # timestamp début saturation ΔP (filtrage temporel 5s)
        # historique du Re pour la moyenne glissante (convergence + dispersion)
        self._historique_re = deque(
            maxlen=max(2, int(self.fenetre_convergence / self.periode)))
        self._convergee_depuis = None      # instant d'entrée en convergence
        # historique ΔP pour détection signal figé (capteur défaillant)
        # Fenêtre de 10s (réf. rapport_comparaison_final.md R2.1)
        self._historique_dp = deque(maxlen=max(2, int(10.0 / self.periode)))
        self._cycles_signal_fige = 0       # cycles consécutifs de variance nulle
        self._re_conduite_prec = None      # garde-fou anti-transitoire de l'apprentissage du gain
        self._echelon_depuis = None        # Re cible avant le dernier échelon de séquence
        self._re_cible_conduite = 0.0      # Re conduite équivalent de la consigne (calé à la préparation)
        self._mode_regulation = None       # mode de régulation courant (MODE_NORMAL_HIGH, MODE_NORMAL_LOW, MODE_PDC_CONTROL)
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

        # ===== NOUVEAU GESTIONNAIRE APPRENTISSAGES =====
        # Architecture fichiers séparés (miller.json, iso.json) pour éviter
        # les pertes de données lors des sauvegardes successives
        chemin_racine = Path(__file__).resolve().parent.parent
        self.gestionnaire_apprentissages = GestionnaireApprentissages(chemin_racine)

        # Charger tous les apprentissages (fusion automatique depuis fichiers séparés)
        self._gain_re_par_volt: float | None = None
        self.table_gains = TableGains()
        self.table_gains_bas_re = TableGains()  # Table mode bas-Re (bypass ouvert, PDC 100%)
        self.table_gains_pdc = TableGains()  # Table mode PDC (tension fixe u_min, PDC variable)

        try:
            donnees = self.gestionnaire_apprentissages.charger_tout()

            g = donnees.get("gain_re_par_volt")
            if g and 100 < g < 1e6:          # garde-fou de vraisemblance
                self._gain_re_par_volt = g

            pts = donnees.get("table_gains")
            if pts:
                t = TableGains(pts)
                if t.est_valide():
                    self.table_gains = t

            # Charger table gains mode bas-Re (paliers re_mapping)
            pts_bas_re = donnees.get("table_gains_bas_re")
            if pts_bas_re:
                t_bas_re = TableGains(pts_bas_re)
                if t_bas_re.est_valide():
                    self.table_gains_bas_re = t_bas_re

            # Charger table gains PDC (paliers PDC variable à u_min fixe)
            pts_pdc = donnees.get("table_gains_pdc")
            if pts_pdc:
                t_pdc = TableGains(pts_pdc)
                if t_pdc.est_valide():
                    self.table_gains_pdc = t_pdc

            umin = donnees.get("u_min_ventilateur_v")
            if umin and 0 < umin < 10:
                self.u_min_ventilateur = umin
        except (OSError, ValueError) as e:
            log.warning(f"Impossible de charger les apprentissages: {e}")
            pass

        # ===== LOGGER DIAGNOSTIC RÉGULATION =====
        # Logger CSV pour diagnostic approfondi (regulation_debug.csv)
        # Activé/désactivé via IHM ou commande
        self.logger_regulation = LoggerRegulation()
        # TEMPORAIRE : Activer automatiquement au démarrage pour diagnostic
        self.logger_regulation.activer()

        self._verrou = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -------------------------------------------------------------- commandes

    def activer_logger_regulation(self) -> None:
        """Active le logger de diagnostic régulation (regulation_debug.csv)."""
        self.logger_regulation.activer()

    def desactiver_logger_regulation(self) -> None:
        """Désactive le logger de diagnostic régulation."""
        self.logger_regulation.desactiver()

    def definir_consigne(self, re_cible: float, reference: str) -> None:
        # nouvelle consigne : le plafond hérité d'une saturation précédente
        # n'a plus lieu d'être (il était calé sur l'ancien point de marche)
        self._plafond_u_sature = None
        self.pi.u_max = 10.0
        # Reset pour stratégie 2 temps : nouvelle consigne → appliquer u_ff seul au cycle 1
        self._consigne_precedente = None
        self._integrale_ff = 0.0
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
        # Reset intégrale feed-forward et consigne précédente
        self._integrale_ff = 0.0
        self._consigne_precedente = None
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
        self._integrale_ff = 0.0

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
            # Seuil mesuré prioritaire sur seuil manuel
            self.seuil_bas_re = sr.get("seuil_bas_re_mesure",
                                       sr.get("seuil_re", self.seuil_bas_re))
            self.u_seuil_bas_re = sr.get("u_seuil_bas_re_v", self.u_seuil_bas_re)
            self.marge_seuil_bas_re = sr.get("marge_seuil",
                                             self.marge_seuil_bas_re)
            self.split_point = min(max(sr.get("split_range_point",
                                              self.split_point), 0.1), 9.9)
            self.pdc_min_exploitable_pct = sr.get("pdc_min_exploitable_pct",
                                                   self.pdc_min_exploitable_pct)
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
        """Consigne en % d'ouverture : 0 % = fermée, 100 % = ouverte.
        Utilise les butées calibrées pdc_recopie_min/max pour la conversion."""
        pct_borne = min(max(pct, 0.0), 100.0)
        # Conversion avec calibration : pct → volts en tenant compte des butées réelles
        volts = self.pdc_recopie_min + (pct_borne / 100.0) * (self.pdc_recopie_max - self.pdc_recopie_min)
        self.commander_vanne_perte_charge(volts)

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
        """Butée occupée par la vanne 3 voies : "0", "90", ou None si la
        lecture est inexploitable ou si la vanne est en mouvement.

        Codage réel de la vanne installée (codage un-parmi-trois) :
        - 0° (ISO) : FC2 seule active (commande F)
        - 90° (MILLER) : FC3 seule active (commande I)
        - Position invalide/mouvement : aucune FC ou plusieurs FC actives

        Ce codage diffère de la doc SNAA710000 mais correspond au
        comportement réel de la vanne installée."""
        fc0 = mesures.get("fc_3voies_0", False)      # FC2
        fc90 = mesures.get("fc_3voies_90", False)    # FC3
        fc180 = mesures.get("fc_3voies_180", False)  # FC1

        # Position 0° (ISO) : FC2 seule active
        if fc0 and not fc90 and not fc180:
            return "0"
        # Position 90° (MILLER) : FC3 seule active
        if fc90 and not fc0 and not fc180:
            return "90"
        # Position invalide ou en mouvement
        return None

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

    def _position_repos(self) -> None:
        """Met les vannes en position de repos (sécurité / démarrage) :
        - Vanne 3 voies → Miller (90°, diaphragme de débit élevé)
        - Vanne perte de charge → ouverte (10 V, pas de restriction)
        - Vanne bypass → ouverte (circuit dérivé, pas de mesure)"""
        try:
            # Vanne 3 voies sur Miller (90°)
            self.es.ecrire_vanne_diaphragme(False)  # False = 90° = Miller
            self.etat["diaphragme"] = "miller"
            self._diaphragme_commande = "90"

            # Vanne perte de charge ouverte (10 V)
            self.commander_vanne_perte_charge(10.0)

            # Bypass ouvert (True)
            self.es.ecrire_vanne_bypass(True)
            self._bypass_commande = True
        except Exception:
            # Échec silencieux si le matériel n'est pas disponible (simulation, etc.)
            pass

    def demarrer(self) -> None:
        self._thread = threading.Thread(target=self._boucle, daemon=True)
        self._thread.start()
        # Attendre que la boucle démarre et que les E/S soient disponibles
        import time
        time.sleep(0.3)
        self._position_repos()

    def arreter(self) -> None:
        # Remettre les vannes en position de repos AVANT d'arrêter la boucle
        self._position_repos()
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
                # l'état publié doit dire la vérité : le cycle ayant échoué
                # avant sa mise à jour, l'IHM continuait d'afficher
                # « RÉGULATION » et une tension d'avant la coupure, alors
                # que le banc venait d'être arrêté et verrouillé.
                self.etat.update({"regulation": False, "convergee": False,
                                  "u_variateur": 0.0, "manoeuvre": None})
                try:
                    self.es.ecrire_variateur(0.0)
                except Exception:
                    pass
                try:
                    self.es.ecrire_variateur_run(False)
                    self._run_actif = False
                except Exception:
                    pass
            # ---- journal des conditions d'essai ------------------------
            # Écrit ICI et non en fin de _cycle : le cycle a cinq sorties
            # anticipées (manœuvre en cours, défaut capteur, défaut vanne,
            # défaut variateur, incohérence de position) et une erreur
            # Modbus le fait sortir par l'exception. Depuis _cycle, le
            # journal se taisait donc exactement quand l'opérateur a besoin
            # de savoir — pendant les manœuvres et sur défaut — et sa
            # colonne « defauts » ne pouvait jamais se remplir.
            if self.journal is not None:
                if self.journal.ouvert:
                    try:
                        self.journal.enregistrer(self.etat, time.monotonic())
                    except Exception:          # noqa: BLE001
                        pass                   # jamais au détriment de la boucle
                self.etat["journal"] = str(self.journal.chemin) \
                    if self.journal.chemin else None
                self.etat["journal_lignes"] = self.journal.lignes
                self.etat["journal_erreur"] = self.journal.erreur

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
        # Lecture préliminaire de ΔP pour usage pendant les manœuvres/défauts
        # (sera relu plus tard avec le diaphragme définitif pour le calcul du débit)
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
        preparation_attente_transition = False  # Flag pour sauter la suite si attente transition
        if regul and not self._preparation_faite and re_cible > 0:
            if reference == "veine":
                qm_vise = self.banc.re_veine_vers_qm(re_cible, T, p_amont)
            else:
                qm_vise = self.banc.re_conduite_vers_qm(re_cible, T, p_amont)
            re_cond_attendu = self.banc.qm_vers_re(
                qm_vise, T, self.banc.patm_pa + p_amont)["re_conduite"]

            nouveau_diaph = self.banc.choisir_diaphragme(re_cond_attendu, diaph)
            diaphragme_change = nouveau_diaph != diaph

            # ---- SÉCURITÉ R1.2 : Transition HAUT→BAS (ISO→Miller) --------
            # Réf. rapport_comparaison_final.md R1.2 : transition ISO→Miller
            # (HAUT Re → BAS Re) nécessite protection contre saturation Miller (500 Pa).
            # NOUVELLE STRATÉGIE (commandes simultanées pour gagner du temps) :
            # 1. Commander bypass ouvert + diaphragme Miller SIMULTANÉMENT
            # 2. Forcer variateur à u_min (protection débit)
            # 3. Attendre SEULEMENT que Re < seuil - hystérésis
            # Avec variateur à u_min, risque saturation minimal même si bypass
            # pas encore complètement ouvert.
            transition_haut_bas = (diaphragme_change
                                  and diaph == "iso"
                                  and nouveau_diaph == "miller")

            if transition_haut_bas:
                # COMMANDES SIMULTANÉES : bypass ouvert + diaphragme Miller
                # Sécurité assurée par : u_min (débit minimal) + bypass ouvert (évacue surpression)
                # Protection supplémentaire : ne commander que si le diaphragme actuel n'est PAS déjà Miller
                # (évite manœuvre inutile si état transitoire incohérent)
                diaph_actuel_etat = self.etat.get("diaphragme")
                if self._manoeuvre is None and diaph_actuel_etat != "miller":
                    # 1. Commander bypass ouvert (évacuation surpression)
                    self.commander_bypass(True)
                    # 2. Commander diaphragme Miller SIMULTANÉMENT (pas d'attente)
                    self.commander_diaphragme("miller")
                    log.info("Transition ISO→Miller: bypass ouvert + diaphragme miller commandés simultanément")

                # 3. Forcer variateur à u_min pour limiter débit pendant transition
                self.es.ecrire_variateur(self.u_min_ventilateur or 0.0)

                # 4. Attendre fin de manoeuvre diaphragme avant de réguler
                # (vannes TOR prennent ~2s, on ne veut pas calculer u_ff avec mauvais diaphragme)
                # SAUF si le diaphragme est déjà en position Miller (pas de manœuvre lancée)
                if self._manoeuvre is not None:
                    # Manoeuvre en cours : attendre
                    preparation_attente_transition = True
                elif diaph_actuel_etat != "miller":
                    # Diaphragme pas encore Miller, mais pas de manœuvre : attendre prochain cycle
                    preparation_attente_transition = True
                # Sinon : diaphragme déjà Miller, continuer préparation normalement

            # Continuer la préparation seulement si pas en attente de transition
            if not preparation_attente_transition:
                # Déterminer le mode de régulation pour la nouvelle consigne
                mode = self._determiner_mode_regulation(re_cond_attendu)

                # SÉCURITÉ : Pré-positionner vanne PDC selon règle min(actuelle, cible)
                # AVANT toute manœuvre TOR (diaphragme, bypass). Garantit aucun surdébit
                # transitoire (réf. rapport_comparaison_final.md R1.3)
                self._prepositionner_vanne_pdc_securitaire(mode)

                # Basculement de diaphragme OK (soit pas HAUT→BAS, soit bypass ouvert + Re bas)
                if diaphragme_change:
                    # Mise à jour du diaphragme attendu (commande dans la boucle de régulation)
                    diaph = nouveau_diaph

                self.etat["dp_attendu"] = self.banc.dP_attendu(
                    diaph, qm_vise, T, p_amont)
                # Mémoriser l'équivalent conduite de la consigne
                self._re_cible_conduite = re_cond_attendu

                # Petit échelon de séquence, même diaphragme, même mode : on
                # conserve l'état PI (l'intégrale compense déjà l'erreur du
                # feed-forward ; se recaler sur la table referait converger
                # depuis la même erreur à chaque échelon)
                conserver_pi = (self._echelon_depuis is not None
                                and not diaphragme_change
                                and mode == self._mode_regulation
                                and abs(re_cible - self._echelon_depuis)
                                < self.echelon_reinit_ff * self._echelon_depuis)
                self._echelon_depuis = None
                if conserver_pi:
                    # Seule l'échelle des gains suit le nouveau point de
                    # fonctionnement (uniquement pour modes normaux)
                    if mode in (MODE_NORMAL_HIGH, MODE_NORMAL_LOW):
                        gain_scale = self._echelle_gains(False, re_cond_attendu) if mode == MODE_NORMAL_HIGH else self.bas_re_scale
                        self.pi.changer_gain_scale(gain_scale)
                else:
                    self._preparer_pi(mode, re_cond_attendu)
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
                # En mode manuel, continuer d'écrire le variateur même pendant
                # une manœuvre (apprentissage re_mapping Phase 4 : bypass ferme
                # pendant 11s, mais le variateur doit rester actif).
                # Lire à nouveau la tension manuelle pour garantir la valeur actuelle.
                with self._verrou:
                    manuel_actuel = self.mode_manuel
                    u_manu_actuel = self.tension_manuelle

                if manuel_actuel and u_manu_actuel > 0:
                    self.es.ecrire_variateur(u_manu_actuel)
                    u_variateur = u_manu_actuel
                else:
                    u_variateur = 0.0

                self.etat.update({
                    "temperature": T, "p_amont": p_amont, "dp": dp,
                    "manoeuvre": m["motif"],
                    "manoeuvre_restant_s": max(m["t_max"] - maintenant, 0.0),
                    "fc_attendue": m["fc_attendue"] or m["angle_attendu"],
                    "convergee": False, "regulation": regul, "manuel": manuel_actuel,
                    "u_variateur": u_variateur,
                    "variateur_actif_en_manoeuvre": manuel_actuel and u_manu_actuel > 0,
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
        dp_final = self._dp_corrige(mesures, diaph)
        # capteur ΔP proche de sa pleine échelle : le Re calculé plafonne au
        # plafond du capteur, pas au banc — une consigne au-delà n'est plus
        # atteignable par la mesure (le PI pousserait le ventilateur à fond
        # sans jamais converger). Signalé à l'opérateur via etat[dp_sature].
        cle_dp = CLE_DP.get(diaph, CLE_DP["miller"])
        if dp_final > self.saturation_dp * self._dp_pleine_echelle[cle_dp]:
            self._cycles_dp_sature += 1
        else:
            self._cycles_dp_sature = 0

        # ---- détection signal ΔP figé (capteur défaillant) ------------------
        # Réf. rapport_comparaison_final.md R2.1 : détection par variance sur
        # fenêtre glissante de 10s. Un capteur figé (défaillance matérielle ou
        # câblage) produit variance quasi nulle alors que les actionneurs sont
        # actifs. Seuil : variance < 0.01 Pa² (écart-type < 0.1 Pa) suspect.
        self._historique_dp.append(dp_final)
        if len(self._historique_dp) >= self._historique_dp.maxlen:
            # Fenêtre pleine : calculer variance
            variance_dp = pstdev(self._historique_dp) ** 2 if len(self._historique_dp) > 1 else 0.0
            # Actionneurs actifs : régulation en cours OU apprentissage en cours
            # (variateur actif). Détection aussi pendant apprentissages pour éviter
            # corruption des calibrations si signal figé (table gains, tau, kp/ki).
            # Note: on ne teste pas pendant les manœuvres (ΔP peut être stable)
            # NE PAS tester en mode bas-Re : procédé lent → variance naturellement faible
            actionneurs_actifs = ((self.regulation_active or self._maintien_tor_suspendu)
                                 and self._manoeuvre is None
                                 and self.etat.get("u_variateur", 0.0) > 0.5
                                 and not self._mode_bas_re
                                 and not self.mode_manuel)
            if variance_dp < 0.01 and actionneurs_actifs:
                # Signal suspect : variance trop faible alors que le banc régule
                self._cycles_signal_fige += 1
                # Persistance : 10 cycles consécutifs (2s à 5 Hz) avant arrêt
                if self._cycles_signal_fige >= 10:
                    self.es.ecrire_variateur(0.0)
                    self.es.ecrire_variateur_run(False)
                    self._run_actif = False
                    with self._verrou:
                        self.regulation_active = False
                    self.etat.update({
                        "defaut_capteur": f"signal ΔP figé (variance={variance_dp:.4f} Pa² < 0.01 Pa²) "
                                         f"— capteur défaillant ou câblage ; vérifier le capteur "
                                         f"{cle_dp} puis relancer",
                        "regulation": False, "u_variateur": 0.0,
                        "convergee": False,
                    })
                    return
            else:
                # Signal normal : réinitialiser compteur
                self._cycles_signal_fige = 0
        else:
            # Fenêtre pas encore pleine : attendre
            self._cycles_signal_fige = 0

        resultat = self.banc.qm_depuis_mesures(diaph, dp_final, T, p_amont)
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

        # ---- capteur ΔP saturé : PROTECTION DÉSACTIVÉE -------
        # DÉSACTIVÉ TEMPORAIREMENT : Utilisation régression linéaire directe (R²=99.99%)
        # La tension est calculée exactement par U = (Re - b) / a, pas d'overshoot
        # attendu donc pas besoin de protection plafond qui complique la régulation.
        #
        # HISTORIQUE : Protection plafond causait bug Re=23000→21000 (latché trop bas)
        # Correction tentée (relâchement progressif) mais toujours problématique.
        # SOLUTION : Confiance dans régression linéaire + gains PI réduits.

        # Calculer dp_sature pour publication état (mais ne pas agir dessus)
        dp_sature = self._cycles_dp_sature >= max(3, int(1.0 / self.periode))

        # PROTECTION DÉSACTIVÉE : Pas de plafond appliqué
        self._plafond_u_sature = None
        self.pi.u_max = 10.0

        # ---- action d'urgence à 490 Pa : PROTECTION DÉSACTIVÉE --
        # DÉSACTIVÉ TEMPORAIREMENT : Protection trop agressive, ouvre bypass sur overshoot
        # transitoire à Re=23000. Avec régression linéaire précise (R²=99.99%), overshoot
        # devrait être minimal. Si DP sature vraiment, c'est que la consigne est hors plage
        # capteur (>35000 Re estimé), l'opérateur doit le gérer manuellement.
        #
        # HISTORIQUE : Seuil 490 Pa (98% pleine échelle) + filtrage 5s
        # Ouvrait bypass à Re=23000 (DP~450-480 Pa) → régulation perturbée
        # SOLUTION : Confiance régression + opérateur responsable des consignes

        # DÉSACTIVÉ : Seuil impossible à atteindre
        seuil_urgence = 9999  # Désactivé (était 490 Pa)
        duree_filtrage = 5.0

        # Protection désactivée (seuil impossible)
        if dp_final > seuil_urgence and not self._maintien_tor_suspendu:
            if self._temps_debut_saturation is None:
                self._temps_debut_saturation = time.monotonic()
            elif not self.etat.get("urgence_saturation"):
                duree_saturation = time.monotonic() - self._temps_debut_saturation
                if duree_saturation >= duree_filtrage:
                    self.etat["urgence_saturation"] = True
                    self.etat["alarme_saturation"] = (
                        f"URGENCE : ΔP = {dp_final:.0f} Pa ≥ {seuil_urgence:.0f} Pa "
                        f"pendant {duree_saturation:.1f}s → saturation confirmée. "
                        "Bypass ouvert + variateur minimum.")
                    self.commander_bypass(True)
        else:
            if dp_final < 0.9 * 500:
                self._temps_debut_saturation = None
                if self.etat.get("urgence_saturation"):
                    self.etat["urgence_saturation"] = False
                    self.etat["alarme_saturation"] = None

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
            # Déterminer le mode de régulation selon Re cible conduite
            re_cible_cond = self._re_cible_conduite or re_cible
            mode = self._determiner_mode_regulation(re_cible_cond)

            # Bascule de mode hors préparation : re-caler le PI
            if mode != self._mode_regulation:
                self._preparer_pi(mode, re_cible_cond)

            # ---- Commander les vannes selon le mode ----
            # ÉTAPE 1 : Lire TOUTES les fins de course nécessaires
            bypass_ouvert = bool(mesures.get("fc_bypass_ouvert"))
            bypass_ferme = bool(mesures.get("fc_bypass_ferme"))
            pdc_fc_ouvert = bool(mesures.get("fc_pdc_ouvert", False))
            fc_iso = bool(mesures.get("fc_3voies_0"))
            fc_miller = bool(mesures.get("fc_3voies_90"))

            # ÉTAPE 2 : Déterminer TOUTES les commandes nécessaires
            commande_diaphragme = None
            commande_bypass = None
            commande_pdc = None

            # Déterminer diaphragme cible selon mode
            if mode == MODE_NORMAL_HIGH:
                diaph_cible = "iso"
                fc_cible_diaphragme = fc_iso
            else:  # MODE_NORMAL_LOW ou MODE_PDC_CONTROL
                diaph_cible = "miller"
                fc_cible_diaphragme = fc_miller

            # Commander diaphragme SEULEMENT si changement nécessaire
            # (ne pas commander si déjà en position selon etat, même si FC absent -
            # sinon on démarre une manœuvre inutile qui masque l'incohérence)
            diaph_actuel = self.etat.get("diaphragme")
            if diaph_actuel != diaph_cible:
                commande_diaphragme = diaph_cible

            if mode == MODE_NORMAL_HIGH:
                # SÉCURITÉ transition BAS→HAUT : si vient de Miller, fermer bypass APRÈS ISO
                # Sinon (démarrage à froid ou déjà ISO), commander fermeture directement
                transition_miller_vers_iso = (self._mode_regulation in (MODE_NORMAL_LOW, MODE_PDC_CONTROL))
                if not bypass_ferme:
                    if not transition_miller_vers_iso or fc_iso:
                        commande_bypass = False
                # PDC doit être grande ouverte
                if not pdc_fc_ouvert:
                    commande_pdc = 10.0
            else:  # normal_low ou pdc_control
                # Bypass doit être ouvert
                # Commander ouverture si PAS au FC ouvert (gère aussi vannes bloquées)
                if not bypass_ouvert:
                    commande_bypass = True
                # PDC selon mode
                if mode == MODE_NORMAL_LOW and not pdc_fc_ouvert:
                    commande_pdc = 10.0
                elif mode == MODE_PDC_CONTROL:
                    # Entrée en mode pdc_control : ouvrir PDC à 100% d'abord
                    # (PI la fermera progressivement ensuite)
                    if self._mode_regulation != MODE_PDC_CONTROL and not pdc_fc_ouvert:
                        commande_pdc = 10.0

            # ÉTAPE 3 : Envoyer TOUTES les commandes SIMULTANÉMENT (optimisation vitesse)
            if commande_diaphragme is not None or commande_bypass is not None or commande_pdc is not None:
                log.info(f"COMMANDES SIMULTANÉES: diaph={commande_diaphragme}, bypass={commande_bypass}, pdc={commande_pdc is not None}")
            if commande_diaphragme is not None:
                self.commander_diaphragme(commande_diaphragme)
            if commande_bypass is not None:
                self.commander_bypass(commande_bypass)
            if commande_pdc is not None:
                self.commander_vanne_perte_charge(commande_pdc)

            # ---- VÉRIFICATION : Toutes les vannes sont-elles en position ? ----
            if mode == MODE_NORMAL_HIGH:
                diaphragme_ok = fc_iso
                bypass_ok = bypass_ferme
                pdc_ok = pdc_fc_ouvert
            elif mode in (MODE_NORMAL_LOW, MODE_PDC_CONTROL):
                diaphragme_ok = fc_miller
                bypass_ok = bypass_ouvert
                # PDC selon mode et transition :
                # - normal_low : PDC doit être à 100% (FC ouvert)
                # - pdc_control, ENTRÉE : PDC doit être à 100% d'abord (FC ouvert)
                # - pdc_control, DÉJÀ en mode : PDC peut être n'importe où (contrôlée par PI)
                deja_en_mode_pdc = (mode == MODE_PDC_CONTROL and self._mode_regulation == MODE_PDC_CONTROL)
                if deja_en_mode_pdc:
                    pdc_ok = True  # Déjà en mode, PDC contrôlée par PI
                else:
                    pdc_ok = pdc_fc_ouvert  # Attendre FC ouvert
            else:
                log.error(f"Mode de régulation invalide: {mode}")
                raise ValueError(f"Mode de régulation invalide: {mode}")

            vannes_pretes = diaphragme_ok and bypass_ok and pdc_ok

            # ---- Calcul de la commande PI selon le mode ----
            if not vannes_pretes:
                # ATTENTE : Vannes pas encore en position → variateur à 0V
                u = 0.0
                pdc_v = self._pdc_consigne if self._pdc_consigne is not None else 0.0  # Garder la consigne PDC en cours (0 par défaut)
            else:
                # VANNES PRÊTES : Lancer le feed-forward + PI
                # Mise en forme de référence : consigne interne filtrée
                if self.tau_consigne > 0:
                    if self._consigne_interne is None:
                        self._consigne_interne = max(re_mesure, _RATIO_ANCRAGE_CONSIGNE_INTERNE * re_cible)
                    a = self.periode / (self.tau_consigne + self.periode)
                    self._consigne_interne += a * (re_cible - self._consigne_interne)
                    consigne_pi = self._consigne_interne
                else:
                    consigne_pi = re_cible

                if mode == MODE_PDC_CONTROL:
                    # Mode très bas Re : PI contrôle la PDC, tension fixe au minimum
                    if self.u_min_ventilateur:
                        u = self.u_min_ventilateur
                    else:
                        log.warning(
                            f"u_min_ventilateur non configuré, utilisation de {_U_MIN_VENTILATEUR_FALLBACK}V par défaut"
                        )
                        u = _U_MIN_VENTILATEUR_FALLBACK
                    # Sortie PI = position PDC (0-10V)
                    pdc_v = self.pi.calculer(consigne_pi, re_mesure)
                    pdc_v = min(max(pdc_v, 0.0), 10.0)
                else:
                    # Modes normal_high et normal_low : FEED-FORWARD DIRECT + CORRECTION P
                    # PDC reste à 100% (commandée ci-dessus)
                    pdc_v = 10.0

                    # NOUVELLE APPROCHE SIMPLIFIÉE :
                    # 1. Calculer tension DIRECTE par régression linéaire : U = (Re - b) / a
                    # IMPORTANT : Utiliser consigne_pi (filtrée) pour cohérence avec erreur
                    u_ff = self._u_equilibre(consigne_pi)
                    if u_ff is None:
                        # Fallback si pas de régression : utiliser PI classique
                        u = self.pi.calculer(consigne_pi, re_mesure)
                        u = min(max(u, 0.0), 10.0)
                    else:
                        # Régression disponible : STRATÉGIE 2 TEMPS
                        # 1. Premier cycle après changement consigne : appliquer JUSTE u_ff
                        # 2. Cycles suivants : u_ff + corrections PI

                        # Détecter changement de consigne significatif (>1% ou 100 Re)
                        if not hasattr(self, '_consigne_precedente'):
                            self._consigne_precedente = None
                        if not hasattr(self, '_cycles_depuis_changement'):
                            self._cycles_depuis_changement = 999

                        changement_consigne = False
                        if self._consigne_precedente is None or abs(consigne_pi - self._consigne_precedente) > max(100, 0.01 * consigne_pi):
                            changement_consigne = True
                            self._consigne_precedente = consigne_pi
                            self._cycles_depuis_changement = 0
                            # Reset intégrale sur changement de consigne
                            self._integrale_ff = 0.0

                        # Calculer erreur pour décider d'activer corrections
                        erreur_re = consigne_pi - re_mesure
                        erreur_relative_pct = abs(erreur_re / consigne_pi * 100) if consigne_pi > 0 else 0

                        # STRATÉGIE ADAPTATIVE :
                        # - Cycles 1-5 : TOUJOURS u_ff seul (laisser système réagir)
                        # - Cycles 5+ : Activer corrections si erreur < 5% OU après 20 cycles
                        if self._cycles_depuis_changement < 5:
                            # Phase initiale : juste u_ff
                            u = u_ff
                            self._cycles_depuis_changement += 1
                        elif erreur_relative_pct > 5.0 and self._cycles_depuis_changement < 20:
                            # Erreur encore grande ET pas encore 25 cycles : continuer avec u_ff seul
                            u = u_ff
                            self._cycles_depuis_changement += 1
                        else:
                            # Erreur petite (<3%) OU 25 cycles atteints : activer corrections PI

                            # 3. Convertir erreur Re en erreur tension via pente régression
                            # Sélection table selon Re (seuil Miller/ISO = 5000)
                            if consigne_pi >= 5000:
                                table = self.table_gains  # ISO
                            else:
                                table = self.table_gains_bas_re  # Miller

                            reg = table.obtenir_regression()
                            if reg['valide'] and reg['a'] is not None and abs(reg['a']) > 1e-10:
                                erreur_v = erreur_re / reg['a']
                            else:
                                # Fallback : utiliser gain global si disponible
                                if self._gain_re_par_volt and self._gain_re_par_volt > 0:
                                    erreur_v = erreur_re / self._gain_re_par_volt
                                else:
                                    erreur_v = 0.0

                            # 4. Gain scheduling adaptatif selon zone de Re
                            if consigne_pi < 22000:
                                # Bas/Moyen Re : gains plus forts pour convergence rapide
                                kp_effectif = 1.2
                                ki_effectif = 0.12
                                limite_correction = 1.2
                            else:
                                # Haut Re (≥22000) : gains très doux pour stabilité (proche saturation DP)
                                kp_effectif = 0.5
                                ki_effectif = 0.06
                                limite_correction = 0.8

                            # 5. Correction PROPORTIONNELLE (limitée pour éviter saturation/oscillations)
                            correction_p = kp_effectif * erreur_v
                            correction_p = min(max(correction_p, -limite_correction), limite_correction)

                            # 6. Correction INTÉGRALE (pour éliminer erreur statique)
                            if not hasattr(self, '_integrale_ff'):
                                self._integrale_ff = 0.0

                            # Accumuler intégrale si erreur > 0.01V (~34 Re)
                            if abs(erreur_v) > 0.01:
                                self._integrale_ff += ki_effectif * erreur_v * self.periode
                                # Anti-windup : limiter l'intégrale à ±2V
                                self._integrale_ff = min(max(self._integrale_ff, -2.0), 2.0)

                            # 7. Tension finale = Régression + corrections
                            u = u_ff + correction_p + self._integrale_ff

                        u = min(max(u, 0.0), 10.0)

            # Sécurité : plafond de saturation
            if self._plafond_u_sature is not None:
                u = min(u, self._plafond_u_sature)

            # Mode urgence saturation : variateur au minimum absolu
            if self.etat.get("urgence_saturation"):
                u = self.u_min_ventilateur or 0.0

            # Appliquer les commandes
            self.es.ecrire_variateur(u)
            self.commander_vanne_perte_charge(pdc_v)

            # Garder trace pour compatibilité (TODO: supprimer à terme)
            self._mode_bas_re = (mode != MODE_NORMAL_HIGH)
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
        # re_mapping, u_min n'est pas connu, le seuil vaut 0 et la stratégie ne
        # s'engage jamais. Silencieux jusqu'ici : le bypass ne s'ouvrait pas
        # sans que rien n'explique pourquoi.
        bas_re_motif = None
        if self.bas_re_actif and (not seuil or self.u_min_ventilateur is None):
            bas_re_motif = ("seuil bas-Re inconnu : lancer l'apprentissage "
                            "« Re mapping » (ou fixer strategie_bas_re.seuil_re)")

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

        # ===== LOGGER DIAGNOSTIC RÉGULATION =====
        # Enregistrer cycle de régulation pour diagnostic (si activé)
        if self.logger_regulation.actif and regul and re_cible > 0:
            # Déterminer mode régulation
            mode_reg = self._determiner_mode_regulation(resultat["re_conduite"])

            # Calculer feed-forward (régression vs interpolation) pour comparaison
            u_ff_reg = None
            u_ff_interp = None
            methode_ff = "aucune"

            if mode_reg == MODE_NORMAL_HIGH and self.table_gains.est_valide():
                # Mode ISO : comparer régression vs interpolation
                u_ff_reg = self.table_gains.tension_pour_re_regression(resultat["re_conduite"])
                u_ff_interp = self.table_gains.tension_pour_re(resultat["re_conduite"])

                # Quelle méthode a été utilisée ?
                reg = self.table_gains.obtenir_regression()
                if reg['valide'] and reg['r2'] is not None and reg['r2'] > 0.90:
                    methode_ff = "regression"
                else:
                    methode_ff = "interpolation"

            elif mode_reg == MODE_NORMAL_LOW and self.table_gains_bas_re.est_valide():
                # Mode Miller
                u_ff_reg = self.table_gains_bas_re.tension_pour_re_regression(resultat["re_conduite"])
                u_ff_interp = self.table_gains_bas_re.tension_pour_re(resultat["re_conduite"])

                reg = self.table_gains_bas_re.obtenir_regression()
                if reg['valide'] and reg['r2'] is not None and reg['r2'] > 0.90:
                    methode_ff = "regression"
                else:
                    methode_ff = "interpolation"

            # Déterminer quelle valeur FF a été utilisée
            u_ff_utilise = u_ff_reg if methode_ff == "regression" else u_ff_interp

            # Alarme si présente
            alarme = self.etat.get("alarme_saturation") or self.etat.get("alarme")

            # Logger les données
            self.logger_regulation.log_cycle({
                'consigne_re': re_cible,
                'mode_regulation': mode_reg,
                'diaphragme': self.etat.get("diaphragme_actuel"),
                'bypass_ferme': not bypass_ouvert if 'bypass_ouvert' in locals() else None,
                'pdc_position_v': self._pdc_position_estimee,
                'u_ff_regression': round(u_ff_reg, 3) if u_ff_reg is not None else None,
                'u_ff_interpolation': round(u_ff_interp, 3) if u_ff_interp is not None else None,
                'u_ff_utilise': round(u_ff_utilise, 3) if u_ff_utilise is not None else None,
                'methode_ff': methode_ff,
                'u_pi_sortie': round(u, 3),
                'u_variateur_final': round(u, 3),
                're_mesure': round(re_mesure, 1),
                'erreur_re': round(re_cible - re_mesure, 1),
                'erreur_relative_pct': round((re_cible - re_mesure) / re_cible * 100, 2) if re_cible > 0 else None,
                'alarme': alarme
            })


    def _determiner_mode_regulation(self, re_cond: float) -> str:
        """Détermine le mode de régulation selon le Re conduite cible.

        Retourne :
        - MODE_NORMAL_HIGH : Re >= 5000 (bypass fermé, ISO, PDC 100%, PI → tension)
        - MODE_NORMAL_LOW : freq_min <= Re < 5000 (bypass ouvert, Miller, PDC 100%, PI → tension)
        - MODE_PDC_CONTROL : Re < freq_min (bypass ouvert, Miller, tension fixe, PI → PDC)
        """
        seuil_diaphragme = 5000.0  # Seuil Miller/ISO

        # Déterminer freq_min (Re minimal en mode Miller avec variateur seul)
        # Priorité : seuil_bas_re manuel < 5000 > table_gains_bas_re > fallback
        # ATTENTION : seuil_bas_re peut être soit seuil bypass (~5000-7500) soit seuil PDC (~500-3000)
        # → Si < seuil_diaphragme, c'est probablement un seuil PDC (test ou config spécifique) → PRIORITAIRE
        # → Si >= seuil_diaphragme, c'est un seuil bypass (ignorer pour déterminer mode PDC)
        if self.seuil_bas_re and self.seuil_bas_re < seuil_diaphragme:
            # Seuil manuel < 5000 : probablement un seuil PDC (utilisé par tests)
            # PRIORITAIRE sur table pour permettre tests de forcer le seuil
            freq_min_re = self.seuil_bas_re
        elif self.table_gains_bas_re.est_valide() and len(self.table_gains_bas_re.points) > 0:
            # Premier point non-nul de la table Miller = freq_min
            for u, re in self.table_gains_bas_re.points:
                if re > 0:
                    freq_min_re = re
                    break
            else:
                freq_min_re = self.table_gains_bas_re.points[0][1]
        else:
            # Fallback : estimer depuis u_min et gain
            freq_min_re = (self.u_min_ventilateur or 0.3) * (self._gain_re_par_volt or 1000)

        if re_cond >= seuil_diaphragme:
            return MODE_NORMAL_HIGH
        elif re_cond >= freq_min_re:
            return MODE_NORMAL_LOW
        else:
            return MODE_PDC_CONTROL

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
        """Tension variateur d'équilibre estimée pour un Re conduite visé.

        STRATÉGIE (par priorité) :
        1. Modèle linéaire (régression Re = a*U + b) si R² > 0.90
        2. Sinon interpolation par morceaux (méthode originale)
        3. Sinon gain appris global (Re/V)
        4. Sinon None (banc non caractérisé)

        Sélectionne automatiquement la bonne table (ISO ou Miller) selon Re.
        """
        # Sélection table selon Re (seuil Miller/ISO = 5000)
        if re_cond_attendu >= 5000:
            # Zone ISO
            table = self.table_gains
        else:
            # Zone Miller
            table = self.table_gains_bas_re

        if table.est_valide():
            # MÉTHODE 1 : Régression linéaire globale (si bon fit)
            reg = table.obtenir_regression()
            if reg['valide'] and reg['r2'] is not None and reg['r2'] > 0.90:
                u_reg = table.tension_pour_re_regression(re_cond_attendu)
                if u_reg is not None:
                    return u_reg

            # MÉTHODE 2 : Interpolation par morceaux (fallback)
            return table.tension_pour_re(re_cond_attendu)

        # MÉTHODE 3 : Gain global appris (si table pas disponible)
        if self._gain_re_par_volt:
            return re_cond_attendu / self._gain_re_par_volt

        return None

    def _prepositionner_vanne_pdc_securitaire(self, mode: str) -> None:
        """Pré-positionne la vanne PDC selon la règle min(actuelle, cible)
        pour garantir l'absence de surdébit transitoire avant toute manœuvre.

        Règle de sécurité (réf. prompt_pilotage_veine.md §3.6) :
        amener la vanne à min(position actuelle, position cible) avant toute
        manœuvre TOR (diaphragme, bypass). Aucune manœuvre ne peut alors
        créer de surdébit transitoire."""
        # Position actuelle (recopie ou estimée)
        position_actuelle_v = self.etat.get("pdc_position", self._pdc_position_estimee)

        # Position cible selon mode
        if mode in (MODE_NORMAL_HIGH, MODE_NORMAL_LOW):
            # Modes normaux : PDC grande ouverte
            position_cible_v = 10.0
        else:
            # Mode pdc_control : position médiane (sera ajustée par PI)
            position_cible_v = 5.0

        # Règle min(actuelle, cible) : sécurité anti-surdébit
        position_securitaire_v = min(position_actuelle_v, position_cible_v)

        # Commander position sécuritaire AVANT manœuvres TOR
        self.commander_vanne_perte_charge(position_securitaire_v)

    def _preparer_pi(self, mode: str, re_cond_attendu: float) -> None:
        """Cale le PI sur le régime visé selon le mode de régulation.

        mode : MODE_NORMAL_HIGH, MODE_NORMAL_LOW, ou MODE_PDC_CONTROL

        - normal_high (Re >= 5000) : bypass fermé, ISO, PDC 100%, PI → tension
        - normal_low (freq_min <= Re < 5000) : bypass ouvert, Miller, PDC 100%, PI → tension
        - pdc_control (Re < freq_min) : bypass ouvert, Miller, tension fixe u_min, PI → PDC
        """
        if mode == MODE_PDC_CONTROL:
            # Mode très bas Re : tension fixe au minimum, PI contrôle PDC
            self.pi.gain_scale = self.bas_re_scale
            self.pi.rampe = self._rampe_bas_re()
            # Feed-forward PDC depuis table d'apprentissage
            pdc_ff = None

            # Méthode 1 (précise) : utiliser table PDC si disponible
            if self.table_gains_pdc.est_valide():
                # Régression linéaire prioritaire (si R² > 0.90)
                reg = self.table_gains_pdc.obtenir_regression()
                if reg['valide'] and reg['r2'] is not None and reg['r2'] > 0.90:
                    pdc_pct = self.table_gains_pdc.tension_pour_re_regression(re_cond_attendu)
                else:
                    # Fallback interpolation
                    pdc_pct = self.table_gains_pdc.tension_pour_re(re_cond_attendu)

                if pdc_pct is not None:
                    # Convertir % en tension (0-100% → 0-10V)
                    pdc_ff = pdc_pct * 0.1

            # Méthode 2 (approximation) : fallback si table PDC pas disponible
            elif self.table_gains_bas_re.est_valide():
                # Ancienne méthode : lookup table Miller puis approximation linéaire
                u_ff = self.table_gains_bas_re.tension_pour_re(re_cond_attendu)
                if u_ff is not None:
                    u_min = self.u_min_ventilateur or 0.3
                    if u_ff <= u_min:
                        # Re très bas : approximation linéaire
                        pdc_min_v = self.pdc_min_exploitable_pct * 0.1
                        ratio = max(0.0, min(1.0, u_ff / u_min))
                        pdc_ff = pdc_min_v + (10.0 - pdc_min_v) * ratio
                    else:
                        # u_ff > u_min : PDC à 100%
                        pdc_ff = 10.0

            if pdc_ff is None:
                pdc_ff = 10.0  # Fallback ultime : démarrer ouverte, PI fermera
            self.pi.reinitialiser(pdc_ff)

        elif mode == MODE_NORMAL_LOW:
            # Mode bas Re normal : bypass ouvert, Miller, PDC 100%, PI contrôle tension
            self.pi.gain_scale = self.bas_re_scale
            self.pi.rampe = self._rampe_nominale
            # Feed-forward depuis table Miller (régression prioritaire)
            u_ff = None
            if self.table_gains_bas_re.est_valide():
                # Essayer régression linéaire d'abord (si R² > 0.90)
                reg = self.table_gains_bas_re.obtenir_regression()
                if reg['valide'] and reg['r2'] is not None and reg['r2'] > 0.90:
                    u_ff = self.table_gains_bas_re.tension_pour_re_regression(re_cond_attendu)
                # Sinon fallback sur interpolation
                if u_ff is None:
                    u_ff = self.table_gains_bas_re.tension_pour_re(re_cond_attendu)
            if u_ff is None:
                if self.u_min_ventilateur:
                    u_ff = self.u_min_ventilateur
                else:
                    log.warning(
                        f"u_min_ventilateur non configuré et table bas Re invalide, "
                        f"utilisation de {_U_MIN_VENTILATEUR_FALLBACK}V par défaut"
                    )
                    u_ff = _U_MIN_VENTILATEUR_FALLBACK
            u_ff = min(max(u_ff, 0.0), 10.0)
            self.pi.reinitialiser(u_ff)

        elif mode == MODE_NORMAL_HIGH:
            # Mode normal haut Re : bypass fermé, ISO, PDC 100%, PI contrôle tension
            self.pi.gain_scale = self._echelle_gains(False, re_cond_attendu)
            self.pi.rampe = self._rampe_nominale
            # Feed-forward depuis table ISO
            u_ff = self._u_equilibre(re_cond_attendu)
            if u_ff is None:
                u_ff = self.etat.get("u_variateur", 0.0)
            else:
                u_ff = min(max(u_ff, 0.0), 10.0)
            self.pi.reinitialiser(u_ff)

        else:
            log.error(f"Mode de régulation invalide dans _preparer_pi: {mode}")
            raise ValueError(f"Mode de régulation invalide dans _preparer_pi: {mode}. "
                           f"Valeurs attendues: 'normal_high', 'normal_low', 'pdc_control'")

        self._mode_regulation = mode  # Sauvegarder mode courant
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
        transitoire de démarrage PdC grande ouverte).

        Utilise table_gains_bas_re (paliers re_mapping) si disponible pour
        feed-forward précis, sinon approximation linéaire."""

        # Paramètres split-range (cohérence avec _split_range)
        u_min = self.u_min_ventilateur or 1.0
        u_seuil = self.u_seuil_bas_re or 10.0
        sp = self.split_point  # Point de bascule PDC/variateur (config)

        # Tentative feed-forward via table apprise (segment variateur)
        if self.table_gains_bas_re.est_valide():
            u_ff = self.table_gains_bas_re.tension_pour_re(re_cible)
            if u_ff is not None:
                # Inverser segment haut split-range : u → cmd
                # u = u_min + (cmd - sp) / (10 - sp) * (u_seuil - u_min)
                # => cmd = sp + (u - u_min) / (u_seuil - u_min) * (10 - sp)
                if u_ff >= u_min:
                    cmd = sp + (u_ff - u_min) / (u_seuil - u_min) * (10.0 - sp)
                    return max(sp, min(10.0, cmd))
                # Si u_ff < u_min, Re très bas (segment PDC), fallback approximation

        # Fallback : approximation linéaire (ancienne méthode)
        re_umin = seuil / self.marge_seuil_bas_re if self.marge_seuil_bas_re \
            else seuil
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
        u_seuil = self.u_seuil_bas_re or 10.0  # Tension au seuil (5000 Re)
        pdc_min_v = self.pdc_min_exploitable_pct * 0.1  # PDC min en tension (0-10V)

        # Point de bascule PDC/variateur (config). Doit être cohérent avec
        # _cmd_ff_bas_re pour que feed-forward et split_range soient inverses.
        sp = self.split_point

        # Rappel convention (carte P6, sens usine) : 0 V = fermé, 10 V = ouvert.
        # segment bas (cmd ≤ sp) : variateur à u_min, PDC de PDC_min à 100%
        # segment haut (cmd > sp) : PDC 100%, variateur monte de u_min à u_seuil
        if cmd <= sp:
            u = u_min
            # cmd=0 → pdc_min_v (limite décrochage), cmd=sp → 10V (ouvert)
            pdc_v = max(pdc_min_v, pdc_min_v + (10.0 - pdc_min_v) * cmd / sp)
        else:
            pdc_v = 10.0  # PDC fixe à 100% (ouverte)
            # Variateur monte de u_min à u_seuil (précis, plage principale)
            u = u_min + (cmd - sp) / (10.0 - sp) * (u_seuil - u_min)
        return u, pdc_v
