# -*- coding: utf-8 -*-
"""Pilote du banc : la boucle de régulation qui relie tout.

Séquence quand l'opérateur valide une consigne Re :
  1. estimation du débit massique visé (Re cible -> qm)
  2. choix du diaphragme (Miller / ISO) et commande de la vanne 3 voies
  3. calcul du ΔP attendu -> choix de la gamme capteur, pré-positionnement
     du variateur (feed-forward)
  4. boucle : lecture capteurs -> qm réel (ISO 5167 ou Miller) ->
     Re calculé -> tension variateur = feed-forward (table apprise) +
     correction PI en volts, jusqu'à convergence
  5. surveillance continue (la T° dérive -> rho, mu recalculés à chaque cycle)
"""

from __future__ import annotations
import logging
import threading
import time
from pathlib import Path
from bisect import bisect_left
from collections import deque
from statistics import fmean, pstdev

from core.journal import JournalEssai
from core.etat_vannes import lire_etat_vannes, PositionDiaphragme, PositionBypass, PositionPDC
from core.gestion_modes import (
    determiner_mode_cible,
    determiner_mode_effectif,
    ConfigurationModes,
    ModeRegulation
)
from core.commandes_vannes import calculer_commandes_vannes
from core.table_gains_pdc import TableGainsPDC
from core.fichiers import ecrire_json_atomique

log = logging.getLogger(__name__)

# Dossier du programme : racine des chemins relatifs de données (journaux,
# état du banc, apprentissages, log de diagnostic). Variable de module pour
# que les tests la redirigent vers un dossier temporaire au lieu d'écrire
# dans l'arborescence des sources.
RACINE = Path(__file__).resolve().parent.parent

# Modes de régulation (utilisés comme clés internes)
MODE_NORMAL_HIGH = "normal_high"  # Re ≥ 5000 : bypass fermé, ISO, PI → tension
MODE_NORMAL_LOW = "normal_low"    # freq_min ≤ Re < 5000 : bypass ouvert, Miller, PI → tension
MODE_PDC_CONTROL = "pdc_control"  # Re < freq_min : bypass ouvert, Miller, tension fixe, PI → PDC

# Constantes pour les fallbacks de régulation
_U_MIN_VENTILATEUR_FALLBACK = 0.3  # V - Tension minimale par défaut si non configurée
_RATIO_ANCRAGE_CONSIGNE_INTERNE = 0.2  # Ratio pour initialisation consigne filtrée (20% de la cible)

def _seuil_saturation(valeur: float) -> float:
    """Seuil de saturation ΔP, en fraction de la pleine échelle. La lecture
    est écrêtée à la pleine échelle (io_banc/wago.py) : un seuil à 1 ou plus
    ne serait jamais franchi, et toute la protection de saturation (plafond
    de commande, arrêt des paliers d'apprentissage) serait inopérante."""
    if 0.0 < valeur < 1.0:
        return valeur
    log.warning("saturation_dp_relative = %s hors ]0 ; 1[ : 0,95 utilisé "
                "(la lecture ΔP est écrêtée à la pleine échelle)", valeur)
    return 0.95


def _seuil_bas_re_de_config(sr: dict, seuil_diaphragme: float,
                            defaut: float) -> float:
    """Seuil bas-Re fixé en config : seuil mesuré (seuil_bas_re_mesure)
    prioritaire sur le seuil manuel (seuil_re), sauf s'il est inutilisable
    (au-delà du seuil Miller/ISO) — le seuil manuel, celui que règle l'IHM,
    restait sinon sans effet. 0 = seuil dérivé de u_min."""
    mesure = sr.get("seuil_bas_re_mesure") or 0.0
    manuel = sr.get("seuil_re", defaut) or 0.0
    if mesure and (mesure < seuil_diaphragme or not manuel):
        return mesure
    return manuel


def _marge_pdc(valeur: float) -> float:
    """Facteur de tension du ventilateur en mode PdC. Sous ~1,11, le Re
    maximal du mode PdC ne couvre plus la bande d'hystérésis (±5 %) du seuil
    bas-Re ; au-delà de 2, le bas de plage serait inutilement sacrifié."""
    return min(max(valeur, 1.0), 2.0)


# Mappings Enum → string pour publication état vannes vers IHM
_MAP_DIAPHRAGME = {
    PositionDiaphragme.ISO: "iso",
    PositionDiaphragme.MILLER: "miller",
    PositionDiaphragme.EN_TRANSIT: "en_transit",
    PositionDiaphragme.INCONNU: "?"
}
_MAP_BYPASS = {
    PositionBypass.OUVERT: "ouvert",
    PositionBypass.FERME: "fermé",
    PositionBypass.EN_TRANSIT: "en_transit",
    PositionBypass.INCONNU: "?"
}
_MAP_PDC = {
    PositionPDC.OUVERT: "ouvert",
    PositionPDC.FERME: "fermé",
    PositionPDC.EN_TRANSIT: "en_transit",
    PositionPDC.INCONNU: "?"
}


# défauts qui provoquent un repli : cause publiée de l'arrêt de sécurité
_CLES_DEFAUT = ("erreur_com", "defaut_capteur", "defaut_vanne",
                "defaut_variateur", "defaut_pdc", "incoherence_vanne")


def _nombre(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) \
        and x == x and abs(x) != float("inf")


def _table_depuis_fichier(points, nom: str):
    """TableGains lue d'un fichier d'apprentissage, ou None si absente ou
    inexploitable. Chaque point doit être un couple (tension, Re) numérique :
    une ligne mal formée rend toute la table suspecte (elle faisait planter
    le démarrage sur IndexError)."""
    if not points:
        return None
    try:
        propres = [[float(p[0]), float(p[1])] for p in points
                   if len(p) == 2 and _nombre(p[0]) and _nombre(p[1])]
    except TypeError:
        propres = []
    if len(propres) != len(points):
        log.error(f"Apprentissage : table {nom} mal formée, ignorée "
                  "(relancer l'apprentissage correspondant)")
        return None
    t = TableGains(propres)
    if not t.valide_ff():
        return None
    if t.nb_points_ecartes():
        log.warning(f"Apprentissage : table {nom}, {t.nb_points_ecartes()} "
                    "palier(s) écarté(s) (Re non croissant ou nul)")
    return t


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

    def vide(self) -> bool:
        """Retourne True si la table est vide (pas de feed-forward disponible)."""
        return len(self.points) == 0

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
        # paliers sous le décrochage (Re = 0) exclus : ils n'appartiennent
        # pas à la droite et faussaient la pente en bas de table
        pts = [p for p in self.points if p[1] > 0]
        if len(pts) < 2:
            return None

        n = len(pts)
        u_vals = [p[0] for p in pts]  # Tensions (x)
        re_vals = [p[1] for p in pts]  # Re (y)

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

    # ---- modèle de feed-forward ------------------------------------------
    # L'interpolation entre paliers MESURÉS est exacte aux paliers ; la
    # régression globale s'en écarte de 3 % (ISO) à 15 % (Miller) en bas de
    # table. Le feed-forward passe donc par l'interpolation, sur les seuls
    # paliers exploitables (Re > 0 et strictement croissant : les paliers
    # sous le décrochage rendraient l'inversion ambiguë, voire une division
    # par zéro).

    def _points_ff(self) -> list:
        pts = []
        for u, re in self.points:
            if re > 0 and (not pts or re > pts[-1][1]):
                pts.append((u, re))
        return pts

    def valide_ff(self) -> bool:
        """Table exploitable : au moins deux paliers à Re strictement
        croissant. C'est LE critère d'acceptation d'une table (Appliquer,
        sauvegarde, relecture) : sur le banc réel, le bruit ou un palier pas
        encore établi suffit à rendre deux paliers voisins non croissants ;
        exiger une table parfaitement monotone (est_valide) la faisait
        rejeter en bloc et la régulation perdait son feed-forward."""
        return len(self._points_ff()) >= 2

    def nb_points_ecartes(self) -> int:
        """Paliers ignorés par le feed-forward (Re nul ou non croissant)."""
        return len(self.points) - len(self._points_ff())

    def u_pour_re_ff(self, re: float) -> float:
        """Tension d'équilibre pour un Re visé (interpolation inverse,
        extrapolation sur le segment extrême), bornée à 0-10 V."""
        pts = self._points_ff()
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

    def re_pour_u_ff(self, volts: float) -> float:
        """Re attendu pour une tension (sens direct du même modèle)."""
        pts = self._points_ff()
        if volts <= pts[0][0]:
            (x0, y0), (x1, y1) = pts[0], pts[1]
        elif volts >= pts[-1][0]:
            (x0, y0), (x1, y1) = pts[-2], pts[-1]
        else:
            i = bisect_left([p[0] for p in pts], volts)
            (x0, y0), (x1, y1) = pts[i - 1], pts[i]
        return max(0.0, y0 + (y1 - y0) * (volts - x0) / (x1 - x0))

    def pente_ff(self, volts: float) -> float:
        """dRe/dU au point [Re/V], lissée sur ±0,5 V : les segments de 0,1 V
        de la table sont bruités (±15 %), et cette pente fixe le gain de
        boucle du PI de correction. Bornée à [0,5 ; 2] × la pente globale."""
        pts = self._points_ff()

        # Protection division par zéro : table plate (tous les U identiques)
        denom_global = pts[-1][0] - pts[0][0]
        if abs(denom_global) < 1e-6:
            # Table invalide ou plate : retourner pente par défaut
            return self._gain_re_par_volt if self._gain_re_par_volt else 1000.0

        globale = (pts[-1][1] - pts[0][1]) / denom_global
        u0 = max(volts - 0.5, pts[0][0])
        u1 = min(volts + 0.5, pts[-1][0])
        if u1 - u0 < 0.2:
            locale = globale
        else:
            locale = (self.re_pour_u_ff(u1) - self.re_pour_u_ff(u0)) / (u1 - u0)
        return min(max(locale, 0.5 * globale), 2.0 * globale)

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
from core.regulation import RegulateurPI, RegulateurTrim
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

        # CRITIQUE : Valider période > 0 pour éviter division par zéro
        self.periode = r.get("periode_s", 0.1)
        if self.periode <= 0:
            raise ValueError(
                f"Période invalide dans config.json : {self.periode}s "
                f"(doit être > 0, typiquement 0.05-0.2s)"
            )
        if self.periode < 0.01:  # Warn si < 10ms (irréaliste)
            log.warning(f"Période très faible : {self.periode}s (< 10ms), risque surcharge CPU")

        self.tolerance = r.get("tolerance_convergence_relative", 0.01)
        # hystérésis : convergence déclarée sous tolerance, retirée au-delà
        # de cette seconde bande (sinon le voyant clignote au bruit près)
        self.tolerance_sortie = max(
            r.get("tolerance_sortie_convergence_relative", 2 * self.tolerance),
            self.tolerance)
        # Gains nominaux (mode normal)
        self.kp_nominal = r["kp"]
        self.ki_nominal = r["ki"]
        # PI à erreur relative : vanne PdC (très bas Re) et repli si le banc
        # n'est pas caractérisé (ni table de gains ni gain appris)
        self.pi = RegulateurPI(
            kp=self.kp_nominal, ki=self.ki_nominal, periode_s=self.periode,
            rampe_v_par_s=r.get("rampe_v_par_s", 0.5),
            bande_morte_relative=r.get("bande_morte_relative", 0.0))
        self._rampe_nominale = self.pi.rampe
        # régulateur des modes normaux : feed-forward (table apprise) + PI de
        # correction en volts. Mêmes kp/ki : l'erreur étant ramenée en volts
        # par la pente de la table, la plante vue par le PI a un gain ~1 et
        # les kp/ki IMC de l'essai indiciel s'appliquent tels quels.
        self.trim = RegulateurTrim(
            kp=self.kp_nominal, ki=self.ki_nominal, periode_s=self.periode,
            rampe_v_par_s=self._rampe_nominale,
            bande_morte_relative=r.get("bande_morte_relative", 0.0),
            trim_max_v=r.get("trim_max_v", 1.5))
        # suivi du variateur par sa recopie AM : tant que la fréquence n'a
        # pas rejoint la commande, l'erreur ne dit rien du réglage (le
        # ventilateur accélère sur la rampe interne du 3G3MV) -> correction
        # gelée. Durée bornée : une recopie absente ou fausse ne doit pas
        # geler la boucle indéfiniment.
        self.suivi_variateur_max_s = r.get("suivi_variateur_max_s", 15.0)
        self.tol_suivi_variateur = cfg.get("variateur", {}).get(
            "tolerance_consigne_v", 0.15)
        # marge sous le seuil de saturation ΔP pour la consigne maximale
        # acceptée (Re ~ racine(ΔP) : 0,95 en ΔP = 2,5 % en Re)
        self.marge_consigne_dp = r.get("marge_consigne_dp_relative", 0.95)
        # séparation d'intégrale (voir _commande_variateur) : au-delà de cet
        # écart relatif, on n'intègre que si la mesure est stable, c'est-à-dire
        # si sa moyenne a varié de moins de seuil_stabilite en 1 s
        self.bande_integration = r.get("bande_integration_relative", 0.03)
        self.seuil_stabilite = r.get("seuil_stabilite_relatif", 0.005)
        self._hist_re_court = deque(maxlen=max(4, int(1.0 / self.periode)))
        # échelon relatif au-delà duquel un échelon de séquence réinitialise
        # le PI sur le feed-forward ; en deçà, l'état PI est conservé
        # (l'intégrale compense déjà l'erreur de la table de gains)
        self.echelon_reinit_ff = r.get("echelon_reinit_ff_relatif", 0.2)
        # fraction de la pleine échelle du capteur ΔP au-delà de laquelle
        # la mesure est considérée saturée (drapeau dp_sature)
        self.saturation_dp = _seuil_saturation(
            r.get("saturation_dp_relative", 0.9))
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
        # limite maximale de consigne Re pour le diaphragme ISO (évite
        # saturation ΔP capteur). None = pas de limite.
        self.re_max_iso = r.get("re_max_iso", None)
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
        # manœuvres en cours, une par vanne ("3voies", "bypass") : bypass et
        # vanne 3 voies sont souvent commandés ensemble, et chacun garde son
        # propre chien de garde. dict {motif, fc_attendue | angle_attendu,
        # t_debut, t_max, t_fin_gel}
        self._manoeuvres: dict[str, dict] = {}

        # vanne perte de charge : recopie 0-10 V (pas de fin de course).
        # L'estimation interne (consigne suivie à 10 V / temps de course)
        # sert de diagnostic : divergence avec la recopie = défaut actionneur.
        self._pdc_consigne = 0.0
        self._pdc_position_estimee = 0.0
        # position de la PdC en échelle de COMMANDE (0-10 V), par modèle de
        # course à la vitesse apprise, recalé sur ses seules fins de course.
        # Indépendant de l'échelle de la recopie (2,4-9,2 V sur le banc si la
        # carte P6 n'est pas réglée sur 0-10 V) : c'est lui qui dit si la
        # vanne est arrivée (attente du ventilateur, tenue sur la table).
        self._pdc_position_modele = 10.0
        # modèle de diagnostic : la commande suivie à vitesse nominale, SANS
        # recalage (ni recopie ni fins de course) -> une vanne bloquée s'en
        # écarte durablement. Calé une fois, sur la première recopie.
        self._pdc_modele_diag = 10.0
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
        self.arrets = 0                    # arrêts et replis sûrs (cf. _latcher_arret)

        # séquence : profil (t_s, re_cible) suivi par la boucle, ou None
        self._sequence: dict | None = None

        # stratégie bas-Re : sous ce Re, on ouvre le bypass et on règle en
        # split-range (variateur + vanne perte de charge). Le seuil dérive de
        # la tension minimale exploitable du ventilateur, mesurée par
        # l'apprentissage (sinon valeur de config, sinon désactivé).
        srcfg = cfg.get("strategie_bas_re", {})
        self.bas_re_actif = srcfg.get("actif", True)
        self.seuil_bas_re = _seuil_bas_re_de_config(
            srcfg, banc.seuil_re, 0.0)
        self.u_min_ventilateur = srcfg.get("u_min_ventilateur_v", 0.0)
        self.u_seuil_bas_re = srcfg.get("u_seuil_bas_re_v", 0.0)
        # facteur (×) sur u_min : tension du ventilateur en mode PdC (cf. _u_pdc)
        self.marge_ventilateur_pdc = _marge_pdc(srcfg.get("marge_ventilateur_pdc", 1.15))
        # tension imposée au ventilateur en mode PdC (0 = u_min × marge)
        self.u_ventilateur_pdc = srcfg.get("u_ventilateur_pdc_v", 0.0) or 0.0
        # PDC minimale exploitable (%) mesurée par apprentissage, défaut 50%
        # (limite physique décrochage observée empiriquement)
        self.pdc_min_exploitable_pct = srcfg.get("pdc_min_exploitable_pct", 50.0)
        # Re conduite minimal tenu en mode PdC (Re mapping Miller) : borne
        # basse des consignes, comme le capteur ΔP borne le haut
        self.re_min_exploitable = srcfg.get("re_min_exploitable")
        # écart commande / position de la vanne PdC [V] : en deçà, la vanne
        # est arrivée (alarme « hors de portée » armée seulement en butée
        # PHYSIQUE, pas dès que la commande y est)
        self.ecart_course_pdc_v = srcfg.get("ecart_course_pdc_v", 0.5)
        # facteur (×) sur les gains PI en bas-Re. 1.0 = nominal (le séquencement
        # de démarrage suffit à maîtriser le dépassement) ; à réduire au banc si
        # la vraie vanne PdC fait osciller la boucle.
        self.bas_re_scale = min(max(srcfg.get("gain_scale", 1.0), 0.05), 1.0)
        self._mode_bas_re = False          # état courant de la stratégie
        sel = cfg.get("selection_diaphragme", {})
        self._hysteresis_bypass = sel.get("hysteresis_re", 500)

        # CORRECTION-2 : Timeout FC + arrêt sécurisé
        self._cycles_attente_vannes = 0

        # CORRECTION-3 : Arrêt si FC incohérents
        self._cycles_fc_incoherents = 0

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
        # historique du Re pour la moyenne glissante (convergence + dispersion)
        self._historique_re = deque(
            maxlen=max(2, int(self.fenetre_convergence / self.periode)))
        self._convergee_depuis = None      # instant d'entrée en convergence
        # historique ΔP pour détection signal figé (capteur défaillant)
        # Fenêtre de 10s (réf. rapport_comparaison_final.md R2.1)
        self._historique_dp = deque(maxlen=max(2, int(10.0 / self.periode)))
        self._cycles_signal_fige = 0       # cycles consécutifs de variance nulle
        self._ventilateur_depuis: float | None = None   # u > 0,5 V continûment
        self._re_conduite_prec = None      # garde-fou anti-transitoire de l'apprentissage du gain
        self._echelon_depuis = None        # Re cible avant le dernier échelon de séquence
        self._re_cible_conduite = 0.0      # Re conduite équivalent de la consigne (calé à la préparation)
        self._k_conversion = 1.0           # facteur veine→conduite (sauvegardé avant limitation)
        self._mode_regulation = None       # mode de régulation courant (MODE_NORMAL_HIGH, MODE_NORMAL_LOW, MODE_PDC_CONTROL)
        self._consigne_interne = None      # référence suivie par le PI (None = à ancrer)
        # feed-forward + modèle de référence (modes normaux)
        self._u_ff_applique = 0.0          # feed-forward après rampe [V]
        self._biais_ff: dict = {}          # mode -> (id table, biais relatif appris)
        self._t_ecart_variateur = None     # début de l'écart commande / recopie AM
        self._re_cible_max = None          # consigne max mesurable (None = pas de limite)
        self._cache_re_max_miller: tuple = (None, None)   # ((T, P), Re max)
        self._re_cible_min = None          # consigne min tenable en mode PdC
        self._pdc_attente_depuis = None    # démarrage PdC : vanne pas encore placée
        # dernière butée LUE de la vanne 3 voies (diaphragme dont le capteur
        # ΔP est exploité) : tenue pendant une course, faute de butée lue
        self._diaphragme_mesure_dernier: str | None = None
        self._diag: dict = {}              # grandeurs internes publiées / journalisées
        self._cycles_trim_sature = 0       # cycles consécutifs correction en butée
        self._cycles_pdc_butee = 0         # cycles consécutifs PdC en butée, consigne non atteinte
        # PdC sous régulation (mode PdC engagé, vannes prêtes) : une nouvelle
        # consigne de mode PdC ne la rouvre pas (scénario 3 bis) ; une entrée
        # en mode PdC l'attend ouverte à 100 % (scénario 3)
        self._pdc_regulee = False
        self._cycles_bypass_sans_fc = 0    # bypass sans fin de course hors manœuvre

        # état courant (lu par l'IHM)
        # Initialiser rho, mu, nu avec des valeurs par défaut (air à 20°C, 101325 Pa)
        # pour affichage IHM même avant le premier cycle
        rho_init = self.banc.fluide.rho(self.banc.patm_pa, 20.0)
        mu_init = self.banc.fluide.mu(20.0)
        nu_init = self.banc.fluide.nu(self.banc.patm_pa, 20.0)

        self.etat = {"diaphragme": "miller", "u_variateur": 0.0,
                     "convergee": False, "erreur_com": None,
                     "rho": rho_init, "mu": mu_init, "nu": nu_init}
        # gain statique estimé du banc [Re conduite / volt], appris en
        # fonctionnement -> pré-positionnement (feed-forward) des consignes.
        # Persisté sur disque pour que la première consigne d'une session
        # bénéficie du gain appris lors des sessions précédentes.
        # journal CSV des conditions d'essai (une session = un fichier)
        j = cfg.get("journal", {})
        dossier_j = Path(j.get("dossier", "journaux"))
        if not dossier_j.is_absolute():
            dossier_j = RACINE / dossier_j
        self.journal = JournalEssai(dossier_j, j.get("periode_s", 1.0)) \
            if j.get("actif", True) else None

        self._chemin_etat = Path(cfg.get("fichier_etat", "etat_banc.json"))
        if not self._chemin_etat.is_absolute():
            # relatif au dossier du programme, pas au répertoire de lancement
            self._chemin_etat = RACINE / self._chemin_etat

        # ===== NOUVEAU GESTIONNAIRE APPRENTISSAGES =====
        # Architecture fichiers séparés (miller.json, iso.json) pour éviter
        # les pertes de données lors des sauvegardes successives
        self.gestionnaire_apprentissages = GestionnaireApprentissages(
            RACINE, chemin_etat=self._chemin_etat)

        # Charger tous les apprentissages (fusion automatique depuis fichiers séparés)
        self._gain_re_par_volt: float | None = None
        self.table_gains = TableGains()
        self.table_gains_bas_re = TableGains()  # Table mode bas-Re (bypass ouvert, PDC 100%)
        self.table_gains_pdc = TableGains()  # Table mode PDC (tension fixe u_min, PDC variable)

        try:
            donnees = self.gestionnaire_apprentissages.charger_tout()

            g = donnees.get("gain_re_par_volt")
            if _nombre(g) and 100 < g < 1e6:     # garde-fou de vraisemblance
                self._gain_re_par_volt = g

            # chaque table chargée séparément : une table mal formée (fichier
            # édité, version antérieure) est ignorée et signalée, sans
            # empêcher le démarrage ni le chargement des autres
            for cle, attribut in (("table_gains", "table_gains"),
                                  ("table_gains_bas_re", "table_gains_bas_re"),
                                  ("table_gains_pdc", "table_gains_pdc")):
                t = _table_depuis_fichier(donnees.get(cle), cle)
                if t is not None:
                    setattr(self, attribut, t)

            umin = donnees.get("u_min_ventilateur_v")
            if _nombre(umin) and 0 < umin < 10:
                self.u_min_ventilateur = umin
        except (OSError, ValueError) as e:
            log.warning(f"Impossible de charger les apprentissages: {e}")
            pass

        # ===== LOGGER DIAGNOSTIC RÉGULATION =====
        # Logger CSV pour diagnostic approfondi (regulation_debug.csv)
        # Activé/désactivé via IHM ou commande
        # Désactivé par défaut (journal.diagnostic_regulation) : il écrit une
        # ligne CSV à chaque cycle, sans limite de taille, dans le dossier du
        # programme.
        self.logger_regulation = LoggerRegulation(
            RACINE / "regulation_debug.csv")
        if j.get("diagnostic_regulation", False):
            self.logger_regulation.activer()

        # ===== NOUVEAUX MODULES REFACTORÉS =====
        # Modes précédents (pour hystérésis)
        self._mode_cible_precedent: ModeRegulation | None = None
        self._mode_effectif_precedent: ModeRegulation | None = None

        # Configuration modes (construit à partir de config.json)
        self._config_modes = self._configuration_modes()

        self._verrou = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()  # Signale que le thread est prêt
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

        # Appliquer limite Re_max ISO si configurée (protection capteur ΔP)
        # IMPORTANT : la limite s'applique au Re CONDUITE (celui du diaphragme ISO)
        # Si référence = "veine", on ne limite pas ici car la conversion veine→conduite
        # se fait plus tard dans la boucle (ligne ~1098).
        re_limite = re_cible
        if self.re_max_iso and reference == "conduite" and re_cible > self.re_max_iso:
            re_limite = self.re_max_iso
            log.warning(f"Consigne {re_cible:.0f} Re conduite limitée à {self.re_max_iso:.0f} Re "
                       f"(re_max_iso configuré pour éviter saturation capteur ΔP ISO)")

        with self._verrou:
            nouvelle = (re_limite, reference) != (self.re_cible, self.reference)
            if reference != self.reference:
                # modèle de référence et mémoire de stabilité sont dans
                # l'unité de l'ancienne référence : comparés au Re mesuré
                # dans la nouvelle (×/÷ 1,87), ils provoquaient un coup de
                # commande (+20 % de Re constaté en simulation)
                self._consigne_interne = None
                self._hist_re_court.clear()
            self.re_cible = re_limite
            self.reference = reference
            # nouvelle consigne = nouvelle préparation (choix diaphragme,
            # feed-forward, Re conduite équivalent), même si la régulation
            # tourne : sans ce reset, un appel isolé laisserait le critère
            # bas-Re et le split-range calés sur l'ancienne consigne
            self._preparation_faite = False
        if nouvelle:
            self._oublier_convergence()

    def _oublier_convergence(self) -> None:
        """Nouvelle consigne : la moyenne glissante repart de zéro. Elle
        mélangeait sinon les Re de l'ancienne consigne et de la nouvelle, et
        passait « par » la consigne pendant la transition : faux RÉGULÉ ✓."""
        self._historique_re.clear()
        self._convergee_depuis = None

    def definir_reference_affichage(self, reference: str) -> None:
        """Référence (conduite / veine) du Re mesuré publié, choisie sur
        l'IHM. Prise en compte hors régulation seulement (manuel, arrêt) :
        en régulation, la référence est celle de la consigne appliquée.
        Sans cela, le manuel affichait un Re conduite sous un sélecteur
        « Veine » (le pilote démarre en conduite)."""
        if reference not in ("conduite", "veine"):
            raise ValueError(f"référence inconnue : {reference}")
        with self._verrou:
            if self.regulation_active or self.reference == reference:
                return
            self.reference = reference
        self._historique_re.clear()       # moyenne/écart-type d'une seule référence
        self._convergee_depuis = None

    def _acquitter_defauts(self) -> None:
        """Efface les défauts latchés. Appelé au (re)démarrage : l'opérateur
        a vu l'alerte et relance en connaissance de cause. Sans cela un défaut
        vanne resterait affiché indéfiniment sur un banc qui régule
        normalement, et masquerait le défaut suivant."""
        for cle in ("defaut_vanne", "defaut_capteur", "defaut_variateur"):
            self.etat[cle] = None
        self._cycles_t_invalide = 0
        self._cycles_incoherence = 0
        self._cycles_attente_vannes = 0    # sinon l'attente d'avant l'arrêt est décomptée
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

    def figer_sequence(self) -> float | None:
        """Arrête le défilement du profil SANS couper le ventilateur : la
        consigne de l'étape en cours reste régulée (consigne fixe). Retourne
        cette consigne, ou None s'il n'y avait pas de séquence."""
        with self._verrou:
            if self._sequence is None:
                return None
            self._sequence = None
            re_fige = self.re_cible
        self.etat["sequence"] = None
        return re_fige

    def _latcher_arret(self) -> None:
        """Coupe régulation, mode manuel et séquence, et compte l'arrêt.

        Le mode manuel est coupé lui aussi : sinon, une fois le défaut
        disparu (communication revenue, PT100 rebranchée), la boucle
        réappliquait d'elle-même la tension manuelle. Le compteur `arrets`
        permet aux séquences d'apprentissage, qui pilotent le ventilateur en
        manuel, de détecter un arrêt survenu pendant leur déroulement."""
        with self._verrou:
            self.regulation_active = False
            self.mode_manuel = False
            self.tension_manuelle = 0.0
            self._sequence = None
            self.arrets += 1
        # après un arrêt, la PdC n'est plus réputée régulée : une consigne de
        # mode PdC la rouvrira à 100 % avant de reprendre
        self._pdc_regulee = False

    def _repli_sur(self) -> None:
        """Repli sûr latché sur défaut : variateur 0 V et RUN retiré, après
        avoir coupé toute commande (une écriture refusée laisse au moins
        l'arrêt latché ; la boucle retentera au cycle suivant)."""
        self._latcher_arret()
        self.etat.update({"regulation": False, "manuel": False,
                          "u_variateur": 0.0, "convergee": False,
                          "sequence": None})
        # heure du PREMIER repli, gardée jusqu'à l'acquittement explicite par
        # l'opérateur (acquitter_arret_securite) : l'IHM l'affiche en bandeau
        if not self.etat.get("arret_securite"):
            self.etat["arret_securite"] = time.strftime("%H:%M:%S")
        self.es.ecrire_variateur(0.0)
        self.es.ecrire_variateur_run(False)
        self._run_actif = False

    def acquitter_arret_securite(self) -> None:
        """Acquittement volontaire d'un arrêt de sécurité par l'opérateur :
        efface le bandeau et les défauts latchés."""
        self._acquitter_defauts()
        self.etat["arret_securite"] = None
        self.etat["arret_motif"] = None

    def arreter_regulation(self) -> None:
        """Arrêt du variateur : coupe la régulation, le mode manuel ET la
        séquence (sinon la boucle réappliquerait une consigne au cycle
        suivant — inacceptable pour un bouton d'arrêt)."""
        self._latcher_arret()
        self._echelon_depuis = None        # PI remis à zéro : ne pas conserver
        self.etat["sequence"] = None
        self._historique_re.clear()
        self._convergee_depuis = None
        if self.journal is not None:
            self.journal.fermer()
        # liaison coupée : l'arrêt est latché ci-dessus, la boucle réécrira
        # 0 V et RUN off dès le retour du coupleur — le bouton ne doit pas
        # lever d'exception dans l'IHM
        try:
            self.es.ecrire_variateur(0.0)
            self.es.ecrire_variateur_run(False)
        except OSError as exc:
            log.warning(f"Arrêt : écriture variateur impossible ({exc})")
        self._run_actif = False
        self.pi.reinitialiser(0.0)
        # le biais de table appris (_biais_ff) est conservé : il traduit la
        # dérive des conditions (T, rho, mu) et vaut encore au redémarrage
        self.trim.reinitialiser(0.0, 0.0)
        self._u_ff_applique = 0.0
        self._consigne_interne = None
        self._mode_regulation = None
        # mémoire d'hystérésis des modes : la vanne a pu être manœuvrée depuis
        self._mode_cible_precedent = None
        self._mode_effectif_precedent = None
        self._re_cible_max = None
        self._re_cible_min = None
        self.etat["consigne_limitee"] = None

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
            self._cache_re_max_miller = (None, None)

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
        self._cache_re_max_miller = (None, None)
        with self._verrou:
            r = cfg.get("regulation", {})
            self.periode = r.get("periode_s", self.periode)
            self.tolerance = r.get("tolerance_convergence_relative",
                                   self.tolerance)
            self.tolerance_sortie = max(r.get(
                "tolerance_sortie_convergence_relative",
                self.tolerance_sortie), self.tolerance)
            self.pi.kp = r.get("kp", self.pi.kp)
            self.pi.ki = r.get("ki", self.pi.ki)
            self.pi.dt = self.periode
            self.pi.rampe = r.get("rampe_v_par_s", self.pi.rampe)
            self._rampe_nominale = self.pi.rampe
            self.pi.bande_morte = r.get("bande_morte_relative",
                                        self.pi.bande_morte)
            self.trim.kp = self.pi.kp
            self.trim.ki = self.pi.ki
            self.trim.dt = self.periode
            self.trim.rampe = self._rampe_nominale
            self.trim.bande_morte = self.pi.bande_morte
            self.trim.trim_max = r.get("trim_max_v", self.trim.trim_max)
            self.suivi_variateur_max_s = r.get("suivi_variateur_max_s",
                                               self.suivi_variateur_max_s)
            self.bande_integration = r.get("bande_integration_relative",
                                           self.bande_integration)
            self.seuil_stabilite = r.get("seuil_stabilite_relatif",
                                         self.seuil_stabilite)
            self._hist_re_court = deque(
                maxlen=max(4, int(1.0 / self.periode)))
            self.marge_consigne_dp = r.get("marge_consigne_dp_relative",
                                           self.marge_consigne_dp)
            self.tol_suivi_variateur = cfg.get("variateur", {}).get(
                "tolerance_consigne_v", self.tol_suivi_variateur)
            self.echelon_reinit_ff = r.get("echelon_reinit_ff_relatif",
                                           self.echelon_reinit_ff)
            self.saturation_dp = _seuil_saturation(
                r.get("saturation_dp_relative", self.saturation_dp))
            self.re_max_iso = r.get("re_max_iso", self.re_max_iso)
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
            self.seuil_bas_re = _seuil_bas_re_de_config(
                sr, self.banc.seuil_re, self.seuil_bas_re)
            self.u_seuil_bas_re = sr.get("u_seuil_bas_re_v", self.u_seuil_bas_re)
            self.marge_ventilateur_pdc = _marge_pdc(sr.get(
                "marge_ventilateur_pdc", self.marge_ventilateur_pdc))
            self.u_ventilateur_pdc = sr.get("u_ventilateur_pdc_v",
                                            self.u_ventilateur_pdc) or 0.0
            self.pdc_min_exploitable_pct = sr.get("pdc_min_exploitable_pct",
                                                   self.pdc_min_exploitable_pct)
            self.re_min_exploitable = sr.get("re_min_exploitable",
                                             self.re_min_exploitable)
            self.ecart_course_pdc_v = sr.get("ecart_course_pdc_v",
                                             self.ecart_course_pdc_v)
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
        """Consigne en % d'ouverture : 0 % = fermée (0 V), 100 % = ouverte
        (10 V)."""
        self.commander_vanne_perte_charge(self._pdc_pct_vers_v(pct))

    def _pdc_pct_vers_v(self, pct: float) -> float:
        """Ouverture PdC en % -> consigne en volts de COMMANDE (0 V = fermée,
        10 V = ouverte : ce sont les commandes que la calibration envoie pour
        atteindre les fins de course). Les bornes pdc_recopie_min/max sont
        des tensions de RECOPIE : s'en servir comme commande laissait la
        vanne à 91 % à « 100 % » avec une recopie 2,4-9,2 V, et le
        positionnement PdC attendait sans fin une position jamais commandée."""
        pct_borne = min(max(pct, 0.0), 100.0)
        return pct_borne / 100.0 * 10.0

    def _pdc_ff_v(self, re_cond: float) -> float | None:
        """Consigne PdC [V] d'équilibre pour un Re conduite, d'après la table
        PdC apprise (paliers en % d'ouverture, variateur à u_min). Hors de la
        plage apprise : palier extrême (pas d'extrapolation). None sans table."""
        pts = self.table_gains_pdc._points_ff()
        if len(pts) < 2:
            return None
        pct = TableGainsPDC(pts).v_pour_re(re_cond)
        return self._pdc_pct_vers_v(pct)

    def commander_bypass(self, ouverte: bool) -> None:
        """Vanne TOR bypass : régulation gelée jusqu'à la fin de course
        (fc_bypass_ouvert / fc_bypass_ferme), chien de garde sur le temps
        de course."""
        self._bypass_commande = ouverte
        self.es.ecrire_vanne_bypass(ouverte)
        # déjà en butée : ordre réaffirmé, sans manœuvre (comme la vanne 3
        # voies) — sinon gel de la régulation pour la marge post-manœuvre
        mesures = self.etat.get("mesures_brutes") or {}
        fc = "fc_bypass_ouvert" if ouverte else "fc_bypass_ferme"
        autre = "fc_bypass_ferme" if ouverte else "fc_bypass_ouvert"
        if mesures.get(fc) and not mesures.get(autre):
            self._manoeuvres.pop("bypass", None)
            return
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
        vanne = "3voies" if angle_attendu is not None else "bypass"
        self._manoeuvres[vanne] = {
            "motif": motif,
            "fc_attendue": fc_attendue,      # vannes TOR : une fin de course
            "angle_attendu": angle_attendu,  # vanne 3 voies : une butée
            "t_debut": time.monotonic(),
            "t_max": time.monotonic() + t_course * self.facteur_chien_garde,
            "t_course": t_course,            # temps de course configuré
            "t_fin_gel": None,               # fin de course + marge
        }

    @property
    def _manoeuvre(self) -> dict | None:
        """Manœuvre en cours qui finit le plus tard (None si aucune)."""
        # copie : un clic IHM (autre thread) peut ajouter ou retirer une
        # manœuvre pendant le parcours -> RuntimeError, repli intempestif
        manoeuvres = list(self._manoeuvres.values())
        if not manoeuvres:
            return None
        return max(manoeuvres, key=lambda m: m["t_max"])

    def _timeout_attente_vannes_s(self) -> float:
        """Délai maximal d'attente des vannes avant régulation : pire cas
        séquentiel (bypass, puis vanne 3 voies, puis ouverture de la PdC),
        chien de garde et marges compris — dérivé des temps de course de la
        config au lieu des 40 s codés en dur."""
        return (self.facteur_chien_garde
                * (self.t_vanne_bypass + self.t_vanne_3voies
                   + self.t_vanne_pdc_course)
                + 3.0 * self.marge_manoeuvre)

    # ----------------------------------------------------------------- boucle

    def _position_repos(self) -> None:
        """Met les vannes en position de repos (sécurité / démarrage) :
        - Vanne 3 voies → Miller (butée donnée par positions_vanne_3voies)
        - Vanne perte de charge → ouverte (10 V, pas de restriction)
        - Vanne bypass → ouverte (circuit dérivé, pas de mesure)"""
        try:
            angle_miller = self._angle_par_diaphragme.get("miller", "90")
            self.es.ecrire_vanne_diaphragme(angle_miller == "0")
            self.etat["diaphragme"] = "miller"
            self._diaphragme_commande = angle_miller

            # Vanne perte de charge ouverte (10 V)
            self.commander_vanne_perte_charge(10.0)

            # Bypass ouvert (True)
            self.es.ecrire_vanne_bypass(True)
            self._bypass_commande = True
        except (IOError, RuntimeError, OSError) as exc:
            # liaison indisponible : signalé, sans bloquer le démarrage ni l'arrêt
            log.warning(f"Position repos non appliquée: {exc}")

    def demarrer(self) -> None:
        """Démarre le thread de régulation et attend qu'il soit prêt."""
        self._ready.clear()  # Réinitialiser l'event
        self._thread = threading.Thread(target=self._boucle, daemon=True)
        self._thread.start()
        # Attendre que le thread signale qu'il est prêt (max 2s)
        if not self._ready.wait(timeout=2.0):
            raise RuntimeError(
                "Thread de régulation non démarré en 2s. "
                "Vérifier connexion Wago ou erreur d'initialisation."
            )
        self._position_repos()

    def arreter(self) -> None:
        """Arrêt complet, dans cet ordre : ventilateur coupé, boucle arrêtée,
        vannes en position de repos, journal et liaison fermés.

        Le ventilateur est coupé AVANT toute manœuvre : basculer la vanne 3
        voies sur le Miller ventilateur lancé enverrait tout le débit dans le
        petit orifice. La boucle est arrêtée avant la position de repos, sans
        quoi sa réaffirmation des ordres TOR la contrarierait.
        Log les erreurs critiques mais ne bloque pas l'arrêt complet.
        """
        erreurs_arret = []

        # 1. Ventilateur coupé (régulation, manuel, séquence)
        try:
            self.arreter_regulation()
        except Exception as e:
            msg = f"Arrêt variateur non confirmé : {e}"
            erreurs_arret.append(msg)
            log.error(msg)

        # 2. Arrêter la boucle de régulation
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            if self._thread.is_alive():
                msg = "Thread régulation non terminé après 3s (timeout)"
                erreurs_arret.append(msg)
                log.warning(msg)

        # 3. Vannes en position de repos (sécurité matérielle)
        try:
            self._position_repos()
        except Exception as e:
            msg = f"Position repos non atteinte : {e}"
            erreurs_arret.append(msg)
            log.error(msg)

        # 3. Fermer journal (best-effort, erreur non critique)
        if self.journal is not None:
            try:
                self.journal.fermer()
            except Exception as e:
                log.debug(f"Fermeture journal (erreur ignorée) : {e}")

        # 4. Fermer E/S matérielles (PRIORITÉ 1, déjà géré par wago.py)
        try:
            self.es.fermer()
        except Exception as e:
            msg = f"Fermeture E/S échouée : {e}"
            erreurs_arret.append(msg)
            log.error(msg)

        # Signaler les échecs critiques dans etat (visible IHM)
        if erreurs_arret:
            with self._verrou:
                self.etat["erreurs_arret"] = erreurs_arret
            log.critical(f"ARRÊT INCOMPLET : {len(erreurs_arret)} erreurs")
        # persistance des grandeurs apprises pour les sessions suivantes
        # (mêmes clés que la relecture du __init__ : gain, table, u_min)
        if self._gain_re_par_volt or self.table_gains.valide_ff() \
                or self.u_min_ventilateur:
            try:
                donnees = {}
                if self._gain_re_par_volt:
                    donnees["gain_re_par_volt"] = self._gain_re_par_volt
                if self.table_gains.valide_ff():
                    donnees["table_gains"] = self.table_gains.to_list()
                if self.u_min_ventilateur:
                    donnees["u_min_ventilateur_v"] = self.u_min_ventilateur

                ecrire_json_atomique(self._chemin_etat, donnees)
            except (OSError, ValueError) as e:
                log.error(f"Erreur sauvegarde état : {e}")

    def _cycle_protege(self) -> None:
        """Un cycle, avec repli sûr sur toute exception (Modbus, capteur,
        calcul ou défaut de programmation)."""
        try:
            self._cycle_protege_corps()
        finally:
            self._memoriser_motif_arret()

    def _memoriser_motif_arret(self) -> None:
        """Cause d'un arrêt de sécurité, figée au premier repli : le défaut
        peut disparaître de lui-même (liaison revenue) et l'IHM peut être
        rouverte — la cause reste lisible jusqu'à l'acquittement."""
        if not self.etat.get("arret_securite") or self.etat.get("arret_motif"):
            return
        motifs = [str(self.etat[c]) if isinstance(self.etat.get(c), str)
                  else c.replace("_", " ")
                  for c in _CLES_DEFAUT if self.etat.get(c)]
        if motifs:
            self.etat["arret_motif"] = " ; ".join(motifs)

    def _cycle_protege_corps(self) -> None:
        try:
            self._cycle()
            self.etat["erreur_com"] = None
        except Exception as exc:  # noqa: BLE001
            # Erreurs attendues (Modbus, capteurs, calculs) ou défaut de
            # programmation : repli sûr dans tous les cas. Une exception
            # non rattrapée tuerait le thread en laissant le variateur
            # sous tension, RUN actif, sans plus aucune supervision.
            if not isinstance(exc, (IOError, OSError, ConnectionError,
                                    TimeoutError, ValueError, KeyError,
                                    RuntimeError)):
                log.exception("Erreur inattendue dans le cycle de régulation")
                exc = f"erreur interne ({type(exc).__name__}: {exc})"
            self.etat["erreur_com"] = str(exc)
            # état sûr documenté : variateur 0 V ET ordre RUN retiré.
            # On latche l'arrêt (régulation ET mode manuel), comme les
            # défauts variateur/vanne, pour ne pas relancer la soufflerie
            # tout seul dès que la communication revient.
            en_marche = self.regulation_active or self.mode_manuel
            self._latcher_arret()
            # banc en marche coupé : arrêt de sécurité à acquitter, comme
            # tout repli (bandeau IHM). Banc à l'arrêt, une liaison absente
            # reste signalée par le voyant, sans bandeau.
            if en_marche and not self.etat.get("arret_securite"):
                self.etat["arret_securite"] = time.strftime("%H:%M:%S")
            # l'état publié doit dire la vérité : le cycle ayant échoué
            # avant sa mise à jour, l'IHM continuait d'afficher
            # « RÉGULATION » et une tension d'avant la coupure, alors
            # que le banc venait d'être arrêté et verrouillé.
            self.etat.update({"regulation": False, "manuel": False,
                              "convergee": False, "u_variateur": 0.0,
                              "manoeuvre": None, "sequence": None})
            # cause publiée tout de suite : sur coupleur muet, chaque
            # écriture de repli ci-dessous bloque délai × tentatives
            self._memoriser_motif_arret()
            # écritures de repli : AUCUNE exception ne doit sortir d'ici (le
            # thread de la boucle en mourrait, variateur à sa dernière
            # consigne) ; la liaison absente, on retente au cycle suivant
            try:
                self.es.ecrire_variateur(0.0)
            except Exception:  # noqa: BLE001
                pass
            try:
                self.es.ecrire_variateur_run(False)
                self._run_actif = False
            except Exception:  # noqa: BLE001
                pass

    def _boucle(self) -> None:
        """Boucle de régulation principale (thread dédié)."""
        self._preparation_faite = False
        # Signaler que le thread est prêt et initialisé
        self._ready.set()
        while not self._stop.is_set():
            debut = time.monotonic()
            try:
                self._cycle_protege()
            except Exception:  # noqa: BLE001
                # dernier filet : la supervision ne s'arrête jamais
                log.exception("Exception hors du cycle protégé")
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
                    except Exception:  # noqa: BLE001
                        # Erreur d'écriture journal : jamais au détriment de
                        # la boucle (hors du try du cycle, une exception ici
                        # tuerait le thread)
                        log.exception("Écriture du journal d'essai")
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
        """Cycle principal de régulation (appelé toutes les 200 ms).

        MACHINE À ÉTATS — Transitions automatiques des modes de régulation
        ===================================================================

        ┌─────────────────────────────────────────────────────────────────┐
        │ MODE_NORMAL_HIGH (mode ISO, Re > seuil_re + hystérésis)        │
        ├─────────────────────────────────────────────────────────────────┤
        │ • Diaphragme      : ISO (vanne 3V à 0°)                        │
        │ • Bypass          : FERMÉ (vanne bypass fermée)                │
        │ • Vanne PdC       : 100% ouverte (10V)                         │
        │ • Commande        : U_variateur = U_FF(Re) + Trim_PI           │
        │ • Régulateur      : RegulateurTrim (feed-forward + correction) │
        │                                                                 │
        │ Transition ↓ : Re_mesure < seuil_re - hystérésis               │
        └─────────────────────────────────────────────────────────────────┘
                                   ↓
        ┌─────────────────────────────────────────────────────────────────┐
        │ MODE_NORMAL_LOW (mode Miller, seuil_bas_re < Re < seuil_re)    │
        ├─────────────────────────────────────────────────────────────────┤
        │ • Diaphragme      : MILLER (vanne 3V à 90°)                    │
        │ • Bypass          : OUVERT (vanne bypass ouverte)              │
        │ • Vanne PdC       : 100% ouverte (10V)                         │
        │ • Commande        : U_variateur = U_FF(Re) + Trim_PI           │
        │ • Régulateur      : RegulateurTrim (feed-forward + correction) │
        │                                                                 │
        │ Transition ↓ : Re_mesure < seuil_bas_re (u_min atteint)        │
        │ Transition ↑ : Re_mesure > seuil_re + hystérésis               │
        └─────────────────────────────────────────────────────────────────┘
                                   ↓
        ┌─────────────────────────────────────────────────────────────────┐
        │ MODE_PDC_CONTROL (très bas Re, Re < seuil_bas_re)              │
        ├─────────────────────────────────────────────────────────────────┤
        │ • Diaphragme      : MILLER (vanne 3V à 90°)                    │
        │ • Bypass          : OUVERT (vanne bypass ouverte)              │
        │ • Variateur       : FIXE à u_min (fréquence minimale)          │
        │ • Commande        : U_PdC = PI(Re_erreur) en % ouverture       │
        │ • Régulateur      : RegulateurPI (relatif, sans feed-forward)  │
        │                                                                 │
        │ Transition ↑ : Re_mesure > seuil_bas_re * 1.05 (hystérésis 5%)│
        └─────────────────────────────────────────────────────────────────┘

        PHASES DU CYCLE (ordre d'exécution)
        ====================================

        1. ACQUISITION
           - Lecture capteurs Modbus (température, ΔP, pressions, FC)
           - Validation température (t_min_c < T < t_max_c)
           - Calcul pression absolue P_abs = P_atm + P_amont_eff

        2. SÉQUENCE TEMPORELLE (si mode séquence actif)
           - Avancement du profil temps → Re_cible(t)
           - Lecture fichier CSV (temps, Re_consigne)

        3. MANŒUVRES VANNES (si changement diaphragme/bypass)
           - Commande vanne 3 voies (ISO ↔ Miller)
           - Commande bypass (ouvert/fermé)
           - Attente fins de course avec timeout (chien de garde)
           - Gel régulation pendant manœuvre

        4. DÉTERMINATION MODE RÉGULATION
           - Choix diaphragme selon Re_cible (ISO vs Miller)
           - Calcul Re_mesure depuis ΔP et température
           - Transitions automatiques entre modes (hystérésis)

        5. CALCUL COMMANDE
           - MODE_NORMAL : U_total = U_FF(Re) + Trim_PI
             * U_FF : interpolation table_gains apprise
             * Trim_PI : correction pour biais/drift (±trim_max)
           - MODE_PDC : U_variateur fixe, PI → vanne PdC

        6. SORTIES MATÉRIELLES
           - Écriture U_variateur (0-10V → 0-50Hz)
           - Écriture signal RUN variateur
           - Écriture vanne PdC (0-10V → 0-100% ouverture)

        7. PUBLICATION ÉTAT
           - Mise à jour self.etat (dict public thread-safe)
           - Convergence, phase, erreurs, diagnostics

        GELS ET PROTECTIONS
        ===================

        • Gel correction PI (geler_p, geler_integrale) :
          - Pendant rampe variateur (suivi recopie AM)
          - Pendant filtrage consigne (tau_consigne_s)
          - Évite overshoot et wind-up

        • Saturation ΔP :
          - Si ΔP > saturation_dp_relative × pleine_échelle
          - Plafonnement U_variateur et recul progressif

        • Défauts critiques → ARRÊT immédiat :
          - Température hors plage (PT100 coupée ou saturée)
          - Pression absolue invalide (capteur défaillant)
          - Timeout manœuvre vanne (chien de garde)

        VARIABLES CLÉS
        ==============

        - self._mode_regulation : "normal_high" | "normal_low" | "pdc_control"
        - self._consigne_interne : Re_cible filtré (tau_consigne_s)
        - self._u_ff_applique : Tension feed-forward appliquée [V]
        - self.trim.integrale : Biais table appris (réutilisé entre consignes)
        - self._manoeuvre : None | dict (pendant changement vanne)

        Voir aussi :
        - _commande_variateur() : logique feed-forward + trim
        - _determiner_mode() : transitions entre modes
        - README.md : description fonctionnelle
        """
        mesures = self.es.lire_mesures()
        T = mesures["temperature"]
        # mesures brutes publiées telles quelles pour la supervision
        # (onglet lecture seule de l'IHM)
        self.etat["mesures_brutes"] = dict(mesures)
        self.etat["image_process"] = getattr(self.es, "image_process", None)
        self.etat["cycles"] = self.etat.get("cycles", 0) + 1
        # depuis quand le ventilateur tourne (commande du cycle précédent,
        # publiée sur tous les chemins : manœuvre et attente l'écrivent à 0)
        if (self.etat.get("u_variateur") or 0.0) > 0.5:
            if self._ventilateur_depuis is None:
                self._ventilateur_depuis = time.monotonic()
        else:
            self._ventilateur_depuis = None
        # attente « PdC avant ventilateur » : ne vaut que si elle se poursuit
        # d'un cycle au suivant (sinon un épisode ancien, interrompu par une
        # manœuvre, le manuel ou un arrêt, déclenchait le chien de garde)
        attente_pdc = self._pdc_attente_depuis
        self._pdc_attente_depuis = None
        # position LUE de la vanne 3 voies, pour l'affichage, à chaque cycle
        # (etat["diaphragme"] est le diaphragme retenu pour le calcul, qui
        # vaut la position commandée hors régulation, avant la butée)
        self.etat["diaphragme_lu"] = _MAP_DIAPHRAGME.get(
            lire_etat_vannes(mesures, self._diaphragme_par_angle).diaphragme, "?")
        self.etat["diaphragme_commande"] = self._diaphragme_par_angle.get(
            self._diaphragme_commande)

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
                self._repli_sur()
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

        # ---- vraisemblance de la pression amont -----------------------------
        # Validation critique : pression amont utilisée pour calcul rho et Re.
        # Plage physique réaliste : -2000 à +5000 Pa effectifs (≈ 800-1050 mbar abs).
        # Hors bornes = capteur saturé, câblage, ou alimentation module défaillante.
        if not (-2000 <= p_amont <= 5000):
            self._repli_sur()
            self.etat.update({
                "defaut_capteur": f"pression amont invraisemblable ({p_amont:.0f} Pa effectifs, "
                                  f"attendu -2000 à +5000 Pa) — capteur défaillant, "
                                  "câblage ou alimentation module ; vérifier le capteur puis relancer",
                "regulation": False, "u_variateur": 0.0,
                "convergee": False,
            })
            return

        # Lecture préliminaire de ΔP pour usage pendant les manœuvres/défauts
        # (sera relu plus tard avec le diaphragme définitif pour le calcul du débit)
        diaph_mesure = self._diaphragme_mesure(mesures)
        self.etat["diaphragme_mesure"] = diaph_mesure
        dp = self._dp_corrige(mesures, diaph_mesure)

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
                self._oublier_convergence()
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
        # publiée dès maintenant : les cycles de manœuvre sortent avant la
        # publication complète, et l'IHM affichait l'ancienne référence
        self.etat["reference"] = reference

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
            if nouveau_diaph == "miller" and self._miller_insuffisant(
                    re_cond_attendu, T, p_amont):
                nouveau_diaph = "iso"     # le Miller saturerait son capteur
            diaphragme_change = nouveau_diaph != diaph

            # La préparation ne commande AUCUNE vanne ni tension : la boucle
            # (plus bas) commande les vannes d'après les fins de course lues au
            # cycle, simultanément, et n'envoie de consigne qu'une fois toutes
            # en butée. Ici : choix du mode, bornes, calage des régulateurs.
            mode = self._determiner_mode_regulation(re_cond_attendu)

            # diaphragme visé (la vanne est commandée par la boucle)
            if diaphragme_change:
                diaph = nouveau_diaph

            self.etat["dp_attendu"] = self.banc.dP_attendu(
                diaph, qm_vise, T, p_amont)
            # plafond capteur ΔP exprimé dans la référence de la consigne : il
            # borne re_vise, qui est dans cette référence. Calculé en conduite
            # (26 260 pour un capteur ISO à 475 Pa) puis comparé à une consigne
            # veine (15 000), il ne bornait rien : la régulation visait un
            # Re au-delà de la mesure et ne convergeait jamais.
            self._borner_consigne(diaph, qm_vise, re_cible, T, p_amont,
                                  reference)
            # borne basse : sous le Re minimal tenu en mode PdC (vanne PdC
            # au minimum exploitable, ventilateur à u_pdc), la consigne était
            # attendue ~30 s puis déclarée « hors de portée »
            re_min, quoi = self.re_min_exploitable, "en mode PdC"
            if mode == MODE_PDC_CONTROL and not (_nombre(re_min) and re_min > 0):
                # Re min non mesuré : le plus bas palier de la table PdC
                pts = self.table_gains_pdc._points_ff()
                re_min = min(p[1] for p in pts) if pts else None
            if mode == MODE_NORMAL_LOW and not self._mode_pdc_caracterise():
                # pas de mode PdC : le variateur ne descend pas sous u_min
                # (décrochage) -> plancher = Re(u_min) en Miller
                re_min, quoi = self._re_plancher_miller(), "par le variateur à u_min"
            if mode in (MODE_PDC_CONTROL, MODE_NORMAL_LOW) \
                    and _nombre(re_min) and re_min > 0 \
                    and 0 < re_cond_attendu < re_min:
                self._re_cible_min = re_cible * re_min / re_cond_attendu
                msg = (f"consigne {re_cible:.0f} Re {reference} sous le minimum "
                       f"tenu {quoi} ({re_min:.0f} Re conduite) : "
                       f"régulation limitée à {self._re_cible_min:.0f} Re {reference}")
                self.etat["consigne_limitee"] = msg
                log.warning(msg)

            # Mémoriser l'équivalent conduite de la consigne
            self._re_cible_conduite = re_cond_attendu

            # Calculer et mémoriser le facteur de conversion k AVANT toute limitation
            # k sera utilisé pour le feed-forward même si _re_cible_conduite est limité
            # Cela garantit que le lookup de table utilise le bon facteur de conversion
            self._k_conversion = (self._re_cible_conduite / re_cible) \
                if re_cible > 0 else 1.0

            # Appliquer limite Re_max ISO si configurée (après conversion veine→conduite)
            # Cette limite s'applique au Re CONDUITE pour protéger le capteur ΔP ISO
            if self.re_max_iso and self._re_cible_conduite > self.re_max_iso:
                log.warning(f"Consigne {self._re_cible_conduite:.0f} Re conduite limitée à {self.re_max_iso:.0f} Re "
                           f"(re_max_iso configuré, référence={reference})")
                self._re_cible_conduite = self.re_max_iso
                # Note: self._k_conversion reste inchangé (basé sur les valeurs avant limitation)

                # IMPORTANT: Mettre à jour _re_cible_max pour limiter re_vise dans le référentiel utilisateur
                # Sans cela, en mode veine, re_vise reste à 15000 alors que Re_conduite est limité à 24500
                # et le feed-forward essaie d'atteindre 15000*k = 46950 Re_conduite → 10V
                if reference == "veine":
                    # Convertir la limite Re_conduite en Re_veine
                    re_cible_max_veine = self.re_max_iso / self._k_conversion
                    # Appliquer comme limite de consigne
                    if self._re_cible_max is None or re_cible_max_veine < self._re_cible_max:
                        self._re_cible_max = re_cible_max_veine
                else:
                    # En mode conduite, appliquer directement la limite
                    if self._re_cible_max is None or self.re_max_iso < self._re_cible_max:
                        self._re_cible_max = self.re_max_iso

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
                # rien à recaler : l'intégrale (biais de table) est
                # gardée, le feed-forward rejoint la nouvelle consigne
                # sur sa rampe et la référence le suit
                pass
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
            ecart_m = self._pdc_consigne - self._pdc_position_modele
            pas_m = min(abs(ecart_m), vitesse * dt)
            self._pdc_position_modele += pas_m if ecart_m > 0 else -pas_m
            ecart_d = self._pdc_consigne - self._pdc_modele_diag
            pas_d = min(abs(ecart_d), vitesse * dt)
            self._pdc_modele_diag += pas_d if ecart_d > 0 else -pas_d
        if mesures.get("fc_pdc_ouvert") and not mesures.get("fc_pdc_ferme"):
            self._pdc_position_modele = max(self._pdc_position_modele,
                                            10.0 - self.ecart_course_pdc_v / 2)
        elif mesures.get("fc_pdc_ferme") and not mesures.get("fc_pdc_ouvert"):
            self._pdc_position_modele = min(self._pdc_position_modele,
                                            self.ecart_course_pdc_v / 2)
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
                plage0 = self.pdc_recopie_max - self.pdc_recopie_min
                if plage0 > 0:
                    self._pdc_modele_diag = min(max(
                        (pdc_recopie - self.pdc_recopie_min) / plage0 * 10.0,
                        0.0), 10.0)
            else:
                # recalage de l'estimée (position publiée sans recopie, log)
                self._pdc_position_estimee = pdc_recopie
            # diagnostic : recopie comparée à la commande suivie à vitesse
            # nominale (modèle jamais recalé). Comparée à l'estimée recalée
            # sur la recopie à chaque cycle (pas de course ~0,1 V < seuil
            # 1 V), une vanne bloquée n'était jamais signalée.
            plage = self.pdc_recopie_max - self.pdc_recopie_min
            if self._pdc_recopie_vue and plage > 0:
                attendue = self.pdc_recopie_min \
                    + self._pdc_modele_diag / 10.0 * plage
                if abs(pdc_recopie - attendue) > max(1.0, 0.2 * plage):
                    self._pdc_cycles_divergents += 1
                else:
                    self._pdc_cycles_divergents = 0
            self.etat["defaut_pdc"] = self._pdc_cycles_divergents \
                > max(3, int(5.0 / self.periode))
        else:
            # sans recopie, les fins de course recalent le modèle : butées
            # physiques certaines (l'estimée part sinon de 0 V, vanne fermée)
            if mesures.get("fc_pdc_ouvert") and not mesures.get("fc_pdc_ferme"):
                self._pdc_position_estimee = 10.0
            elif mesures.get("fc_pdc_ferme") and not mesures.get("fc_pdc_ouvert"):
                self._pdc_position_estimee = 0.0
            self.etat["pdc_position"] = self._pdc_position_estimee
            self.etat["defaut_pdc"] = False

        # ---- gel pendant une manœuvre de vanne TOR -------------------------
        # Fin de gel : fin de course atteinte (+ marge). Chien de garde :
        # défaut vanne si la fin de course n'arrive pas à temps.
        for vanne, m in list(self._manoeuvres.items()):
            if m["angle_attendu"] is not None:
                atteinte = (self._angle_vanne_3voies(mesures)
                            == m["angle_attendu"])
            else:
                atteinte = bool(mesures.get(m["fc_attendue"], False))
            if atteinte:
                if m["t_fin_gel"] is None:
                    m["t_fin_gel"] = maintenant + self.marge_manoeuvre
                if maintenant >= m["t_fin_gel"]:
                    self._manoeuvres.pop(vanne, None)
            elif maintenant > m["t_max"]:
                # DÉFAUT VANNE : fin de course jamais atteinte -> repli sûr
                # (variateur 0 V ET ordre RUN retiré, comme sur défaut variateur)
                self._repli_sur()
                cible = m["fc_attendue"] or f"butée {m['angle_attendu']}°"
                # durées dans le message : une vanne simplement plus lente
                # que le temps de course configuré (valeur non apprise sur ce
                # banc) se reconnaît à sa butée atteinte peu après
                self.etat.update({
                    "defaut_vanne": f"{m['motif']} : {cible} non atteinte en "
                                    f"{maintenant - m['t_debut']:.1f} s (course "
                                    f"configurée {m['t_course']:.1f} s × "
                                    f"{self.facteur_chien_garde:g}) — vanne "
                                    f"bloquée ou câblage ; si la vanne arrive "
                                    f"en butée peu après, refaire "
                                    f"l'apprentissage de sa course",
                    "manoeuvre": None, "regulation": False,
                    "u_variateur": 0.0, "convergee": False,
                })
                self._manoeuvres.clear()
                return
        if self._manoeuvres and regul and re_cible > 0:
            # consigne (éventuellement nouvelle) : vannes redirigées vers ses
            # positions, sans attendre la fin des courses en cours
            self._rediriger_vannes(
                self._mode_cible_courant(self._re_cible_conduite or re_cible),
                mesures)
        if self._manoeuvres:
            m = self._manoeuvre
            # En mode manuel, continuer d'écrire le variateur même pendant
            # une manœuvre (apprentissage re_mapping Phase 4 : bypass ferme
            # pendant 11s, mais le variateur doit rester actif).
            # Lire à nouveau la tension manuelle pour garantir la valeur actuelle.
            with self._verrou:
                manuel_actuel = self.mode_manuel
                u_manu_actuel = self.tension_manuelle

            if manuel_actuel and u_manu_actuel > 0:
                u_variateur = u_manu_actuel
            else:
                # hors manuel, ventilateur à 0 V pendant la manœuvre (comme
                # dans l'attente des vannes) : l'état publié valait déjà 0,
                # mais la sortie gardait la dernière tension écrite
                u_variateur = 0.0
            self.es.ecrire_variateur(u_variateur)

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

        # ---- cohérence de la position de la vanne 3 voies -----------------
        # Hors manœuvre, la vanne doit être sur une butée franche (0° ou 90°).
        # Sinon : lecture invalide (rupture de fil, commun 24 V absent) ou
        # vanne restée à 180° après une manœuvre manuelle. etat_vannes lit ce
        # cas comme « en transit », ce qui ferait attendre une manœuvre que
        # personne n'a commandée : on le traite ici comme un défaut.
        # Une butée franche mais différente du diaphragme visé n'est pas un
        # défaut : la gestion des modes commande alors la manœuvre.
        angle_lu = self._angle_vanne_3voies(mesures)
        if FC_3VOIES["0"] in mesures and angle_lu is None:
            self._cycles_incoherence += 1
        else:
            self._cycles_incoherence = 0
        self.etat["incoherence_vanne"] = self._cycles_incoherence \
            > max(3, int(3.0 / self.periode))

        # incohérence persistante en régulation : on régulerait sur un capteur
        # ΔP qui ne correspond plus à la position réelle de la vanne -> repli
        # sûr + latch (l'opérateur vérifie la vanne puis relance).
        if self.etat["incoherence_vanne"] and regul:
            self._repli_sur()
            self.etat.update({
                "defaut_vanne": "position vanne 3 voies inexploitable hors "
                                "manœuvre (aucune butée franche lue) — "
                                "vérifier la vanne et son câblage, puis relancer",
                "regulation": False, "u_variateur": 0.0, "convergee": False,
            })
            return

        # ---- mesure du débit et des Reynolds -------------------------------
        # capteur ΔP et géométrie du diaphragme de la butée LUE (le
        # diaphragme visé par la préparation n'est en ligne qu'une fois la
        # vanne arrivée) : ISO en butée ISO, Miller en butée Miller
        dp_final = self._dp_corrige(mesures, diaph_mesure)
        # capteur ΔP proche de sa pleine échelle : le Re calculé plafonne au
        # plafond du capteur, pas au banc — une consigne au-delà n'est plus
        # atteignable par la mesure (le PI pousserait le ventilateur à fond
        # sans jamais converger). Signalé à l'opérateur via etat[dp_sature].
        cle_dp = CLE_DP.get(diaph_mesure, CLE_DP["miller"])
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
            # ventilateur en marche depuis au moins la fenêtre entière : la
            # fenêtre ne contient alors que des mesures ventilateur tournant.
            # Sinon, au démarrage, elle est pleine des zéros d'avant la
            # relance et le débit n'est pas encore établi (rampe interne du
            # 3G3MV, inertie) : faux « signal figé » et arrêt de sécurité.
            ventilateur_etabli = (
                self._ventilateur_depuis is not None
                and time.monotonic() - self._ventilateur_depuis
                >= self._historique_dp.maxlen * self.periode)
            actionneurs_actifs = ((self.regulation_active or self._maintien_tor_suspendu)
                                 and self._manoeuvre is None
                                 and self.etat.get("u_variateur", 0.0) > 0.5
                                 and ventilateur_etabli
                                 and not self._mode_bas_re
                                 and not self.mode_manuel)
            if variance_dp < 0.01 and actionneurs_actifs:
                # Signal suspect : variance trop faible alors que le banc régule
                self._cycles_signal_fige += 1
                # Persistance : 10 cycles consécutifs (2s à 5 Hz) avant arrêt
                if self._cycles_signal_fige >= 10:
                    self._repli_sur()
                    if dp_final >= 0.99 * self._dp_pleine_echelle[cle_dp]:
                        # signal figé EN BUTÉE : le capteur est écrêté à sa
                        # pleine échelle, il n'est pas défaillant
                        motif = (f"ΔP en butée de pleine échelle ({dp_final:.0f} Pa) "
                                 f"— débit au-delà de la plage du capteur {cle_dp} ; "
                                 "réduire la consigne puis relancer")
                    else:
                        motif = (f"signal ΔP figé (variance={variance_dp:.4f} Pa² < 0.01 Pa²) "
                                 f"— capteur défaillant ou câblage ; vérifier le capteur "
                                 f"{cle_dp} puis relancer")
                    self.etat.update({
                        "defaut_capteur": motif,
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

        # ---- vraisemblance de ΔP --------------------------------------------
        # Validation critique avant calcul Re : ΔP doit être physiquement plausible.
        # Plage réaliste : -50 à 550 Pa (PE capteur = 500 Pa, tolérance +10%).
        # Valeurs négatives acceptées (bruit, offset capteur) mais limitées à -50 Pa.
        # Hors bornes = capteur saturé, câblage, ou signal aberrant.
        if not (-50 <= dp_final <= 550):
            self._repli_sur()
            self.etat.update({
                "defaut_capteur": f"ΔP invraisemblable ({dp_final:.1f} Pa, attendu -50 à +550 Pa) — "
                                  f"capteur saturé, câblage ou signal aberrant ; "
                                  "vérifier le capteur puis relancer",
                "regulation": False, "u_variateur": 0.0,
                "convergee": False,
            })
            return

        resultat = self.banc.qm_depuis_mesures(diaph_mesure, dp_final, T, p_amont)
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
            self._repli_sur()
            self.etat.update({
                "defaut_variateur": "défaut signalé par le 3G3MV "
                                    "(relais MA/MB) — acquitter sur le "
                                    "variateur puis relancer",
                "regulation": False, "u_variateur": 0.0, "convergee": False,
            })
            return
        self.etat["defaut_variateur"] = None

        # ---- capteur ΔP saturé : plafond de commande ----------------------
        # Au-delà de saturation_dp × pleine échelle, le Re calculé plafonne au
        # capteur, pas au banc : poursuivre la consigne enverrait le
        # ventilateur en butée sans jamais converger. La consigne est déjà
        # bornée au maximum mesurable (_borner_consigne) ; ce plafond ne
        # traite que les dépassements transitoires et les dérives (T, P) :
        #   - saturation confirmée : plafond = commande du moment, qui recule
        #     à recul_saturation tant que la saturation dure ;
        #   - bande d'hystérésis (5 % de pleine échelle sous le seuil) : tenu ;
        #   - en dessous : relevé à recul/5 jusqu'à disparaître (10 V).
        # Le régulateur voit ce plafond dans son anti-windup.
        # Version antérieure : plafond latché jusqu'à la consigne suivante ->
        # Re 23000 tenu à 21000 après un dépassement. Le relâchement corrige ce
        # défaut ; la borne de consigne empêche le cycle limite qu'il causerait
        # sur une consigne hors plage.
        dp_sature = self._cycles_dp_sature >= max(3, int(1.0 / self.periode))
        pleine_echelle = self._dp_pleine_echelle[cle_dp]
        if regul and not manuel and dp_sature:
            u_prec = self.etat.get("u_variateur", 0.0) or 0.0
            base = u_prec if self._plafond_u_sature is None \
                else min(self._plafond_u_sature, u_prec)
            self._plafond_u_sature = max(
                0.0, base - self.recul_saturation * self.periode)
        elif self._plafond_u_sature is not None:
            if not regul or manuel:
                self._plafond_u_sature = None
            elif dp_final < (self.saturation_dp - 0.05) * pleine_echelle:
                self._plafond_u_sature += \
                    self.recul_saturation / 5.0 * self.periode
                if self._plafond_u_sature >= 10.0:
                    self._plafond_u_sature = None

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
            self._diag = {}
        elif regul and re_cible > 0:
            # ===== ARCHITECTURE REFACTORISÉE : NOUVEAUX MODULES =====
            # Utilisation des modules refactorés pour :
            # - Lecture état vannes (etat_vannes.py)
            # - Détermination modes (gestion_modes.py)
            # - Calcul commandes vannes (commandes_vannes.py)
            # Cette architecture corrige définitivement CRITIQUE-1 et évite les cercles vicieux.

            # 1. LECTURE ÉTAT VANNES (nouveau module)
            etat_vannes = lire_etat_vannes(mesures, self._diaphragme_par_angle)

            # Publier positions vannes vers IHM (mapping Enum → string)
            self.etat["diaphragme"] = _MAP_DIAPHRAGME.get(etat_vannes.diaphragme, "?")
            self.etat["bypass"] = _MAP_BYPASS.get(etat_vannes.bypass, "?")
            self.etat["vanne_pdc"] = _MAP_PDC.get(etat_vannes.pdc, "?")

            # CORRECTION-3 : Arrêt si FC incohérents persistants
            if not etat_vannes.coherent:
                self._cycles_fc_incoherents += 1
                # Persistance : 3 cycles consécutifs avant arrêt
                if self._cycles_fc_incoherents >= 3:
                    self._repli_sur()
                    self.etat.update({
                        "defaut_vanne": f"Fins de course incohérents : "
                                       f"diaphragme={etat_vannes.diaphragme.value}, "
                                       f"bypass={etat_vannes.bypass.value}, "
                                       f"pdc={etat_vannes.pdc.value} — "
                                       "Vérifier câblage FC et alimentations 24V, puis relancer",
                        "regulation": False, "u_variateur": 0.0, "convergee": False,
                    })
                    return
                else:
                    log.warning(f"État vannes incohérent détecté ({self._cycles_fc_incoherents}/3)")
            else:
                self._cycles_fc_incoherents = 0

            # Bypass sans aucune fin de course hors manœuvre (fil coupé,
            # commun 24 V absent) : défaut au bout d'une course chien de garde
            # comprise, au lieu d'attendre le délai global des vannes.
            if etat_vannes.bypass == PositionBypass.EN_TRANSIT \
                    and "bypass" not in self._manoeuvres:
                self._cycles_bypass_sans_fc += 1
            else:
                self._cycles_bypass_sans_fc = 0
            if self._cycles_bypass_sans_fc * self.periode \
                    > self.facteur_chien_garde * self.t_vanne_bypass:
                self._repli_sur()
                self.etat.update({
                    "defaut_vanne": "bypass sans fin de course hors manœuvre "
                                    "(fil coupé ou commun 24 V absent) — "
                                    "vérifier la vanne et son câblage, puis relancer",
                    "regulation": False, "u_variateur": 0.0, "convergee": False,
                })
                return

            # 2. CONVERSION REYNOLDS (pour critères en conduite)
            re_cible_cond = self._re_cible_conduite or re_cible

            # 3. DÉTERMINATION MODES (nouveaux modules)
            mode_cible_enum = self._mode_cible_courant(re_cible_cond)

            mode_effectif_enum = determiner_mode_effectif(
                etat_vannes,
                re_cond,
                self._config_modes,
                self._mode_effectif_precedent
            )

            # Sauvegarder pour hystérésis
            self._mode_effectif_precedent = mode_effectif_enum

            # Conversion enum → str (compatibilité avec ancien code)
            MODE_MAP = {
                ModeRegulation.NORMAL_HIGH: MODE_NORMAL_HIGH,
                ModeRegulation.NORMAL_LOW: MODE_NORMAL_LOW,
                ModeRegulation.PDC_CONTROL: MODE_PDC_CONTROL
            }
            mode_cible = MODE_MAP[mode_cible_enum]
            mode_effectif = MODE_MAP[mode_effectif_enum]

            # La stratégie de commande suit le mode CIBLE. Elle ne s'applique
            # qu'une fois les vannes en place (vannes_pretes ci-dessous), donc
            # avec le diaphragme physique du mode cible : c'est cette attente
            # qui garantit la cohérence mode/diaphragme. Choisir la stratégie
            # sur le Re MESURÉ (mode effectif) bouclait sur elle-même : au
            # démarrage en Miller le Re mesuré est bas -> mode PdC -> variateur
            # figé à u_min -> le Re reste bas, la consigne n'est jamais atteinte.
            mode = mode_cible
            mode_table = mode_cible
            if mode_effectif != mode_cible:
                log.debug("mode effectif %s, mode cible %s", mode_effectif,
                          mode_cible)

            # Bascule de mode hors préparation : re-caler le PI
            # IMPORTANT : Re-caler sur mode_cible (où on VA), pas mode_effectif (où on EST)
            if mode_cible != self._mode_regulation:
                self._preparer_pi(mode_cible, re_cible_cond)

            # 4. CALCUL COMMANDES VANNES (nouveau module)
            commandes_vannes = calculer_commandes_vannes(
                etat_actuel=etat_vannes,
                mode_cible=mode_cible_enum,
                manoeuvre_en_cours=(self._manoeuvre is not None)
            )

            # 5. ENVOI COMMANDES TOR
            # CORRECTION-1 : Ne commander QUE si position différente (règle #2)
            if commandes_vannes.diaphragme is not None:
                # Vérifier position actuelle
                doit_commander_diaph = False
                if commandes_vannes.diaphragme == "iso":
                    doit_commander_diaph = (etat_vannes.diaphragme != PositionDiaphragme.ISO)
                elif commandes_vannes.diaphragme == "miller":
                    doit_commander_diaph = (etat_vannes.diaphragme != PositionDiaphragme.MILLER)

                if doit_commander_diaph:
                    log.info(f"Commande diaphragme: {commandes_vannes.diaphragme}")
                    self.commander_diaphragme(commandes_vannes.diaphragme)
                else:
                    log.debug(f"Diaphragme déjà sur {commandes_vannes.diaphragme}, ordre non envoyé")

            if commandes_vannes.bypass is not None:
                # Vérifier position actuelle
                doit_commander_bypass = False
                if commandes_vannes.bypass is True:
                    doit_commander_bypass = (etat_vannes.bypass != PositionBypass.OUVERT)
                elif commandes_vannes.bypass is False:
                    doit_commander_bypass = (etat_vannes.bypass != PositionBypass.FERME)

                if doit_commander_bypass:
                    log.info(f"Commande bypass: {'ouvert' if commandes_vannes.bypass else 'fermé'}")
                    self.commander_bypass(commandes_vannes.bypass)
                else:
                    log.debug(f"Bypass déjà {'ouvert' if commandes_vannes.bypass else 'fermé'}, ordre non envoyé")

            # Vanne PdC : vers sa position cible EN MÊME TEMPS que les vannes TOR
            # (écrite plus bas, dans l'attente des vannes) — grande ouverte dans
            # tous les modes, sauf si elle est déjà régulée en mode PdC.
            pdc_ouverte_visee = not (mode_cible == MODE_PDC_CONTROL
                                     and self._pdc_regulee)
            if mode_cible != MODE_PDC_CONTROL:
                # la PdC est rouverte pour un autre mode : un retour au mode
                # PdC repassera par l'ouverture à 100 % (scénario 3)
                self._pdc_regulee = False

            # 6. VÉRIFICATION : Toutes les vannes sont-elles en position ?
            # Utiliser les positions depuis etat_vannes (nouveau module)
            if mode == MODE_NORMAL_HIGH:
                diaphragme_ok = (etat_vannes.diaphragme == PositionDiaphragme.ISO)
                bypass_ok = (etat_vannes.bypass == PositionBypass.FERME)
                pdc_ok = (etat_vannes.pdc == PositionPDC.OUVERT)
            elif mode in (MODE_NORMAL_LOW, MODE_PDC_CONTROL):
                diaphragme_ok = (etat_vannes.diaphragme == PositionDiaphragme.MILLER)
                bypass_ok = (etat_vannes.bypass == PositionBypass.OUVERT)
                # PDC selon mode et transition :
                # - normal_low : PDC doit être à 100% (FC ouvert)
                # - pdc_control, ENTRÉE : PDC doit être à 100% d'abord (FC ouvert)
                # - pdc_control, DÉJÀ en mode : PDC peut être n'importe où (contrôlée par PI)
                # « déjà en mode PdC » = la PdC est DÉJÀ régulée, et non « la
                # préparation vient de choisir le mode PdC » (elle le fixe
                # elle-même : l'entrée en mode PdC n'attendait pas la PdC à 100 %)
                deja_en_mode_pdc = (mode == MODE_PDC_CONTROL and self._pdc_regulee)
                if deja_en_mode_pdc:
                    pdc_ok = True  # Déjà en mode, PDC contrôlée par PI
                else:
                    pdc_ok = (etat_vannes.pdc == PositionPDC.OUVERT)  # Attendre FC ouvert
            else:
                log.error(f"Mode de régulation invalide: {mode}")
                raise ValueError(f"Mode de régulation invalide: {mode}")

            vannes_pretes = diaphragme_ok and bypass_ok and pdc_ok

            # DEBUG : Logguer état vannes_pretes
            log.debug(f"VANNES: diaph_ok={diaphragme_ok} (attendu={PositionDiaphragme.ISO}, lu={etat_vannes.diaphragme}), "
                     f"bypass_ok={bypass_ok} (attendu={PositionBypass.FERME}, lu={etat_vannes.bypass}), "
                     f"pdc_ok={pdc_ok} (attendu={PositionPDC.OUVERT}, lu={etat_vannes.pdc}) → pretes={vannes_pretes}")

            # ---- Calcul de la commande selon le mode ----
            # consigne effective : bornée au maximum mesurable par le capteur
            re_vise = self._re_borne(re_cible)
            if not vannes_pretes:
                # CORRECTION-2 : Timeout si vannes bloquées
                self._cycles_attente_vannes += 1
                if self._cycles_attente_vannes * self.periode \
                        > self._timeout_attente_vannes_s():
                    # Timeout dépassé : arrêt sécurisé
                    self._repli_sur()

                    # Diagnostic détaillé
                    msg_timeout = []
                    if not diaphragme_ok:
                        msg_timeout.append(f"diaphragme (attendu {mode}, lu {etat_vannes.diaphragme.value})")
                    if not bypass_ok:
                        attendu_bypass = "ouvert" if mode in (MODE_NORMAL_LOW, MODE_PDC_CONTROL) else "fermé"
                        msg_timeout.append(f"bypass (attendu {attendu_bypass}, lu {etat_vannes.bypass.value})")
                    if not pdc_ok:
                        msg_timeout.append(f"PDC (attendu FC ouvert, lu {etat_vannes.pdc.value})")

                    self.etat.update({
                        "defaut_vanne": f"Timeout vannes ({self._cycles_attente_vannes * self.periode:.1f}s) : "
                                       f"{', '.join(msg_timeout)} — vérifier actionneurs et câblage, puis relancer",
                        "regulation": False, "u_variateur": 0.0, "convergee": False,
                    })
                    return

                # ATTENTE : aucune NOUVELLE consigne tant que les vannes ne
                # sont pas toutes en butée.
                # - vannes TOR en mouvement : variateur à 0 V ;
                # - seule la PdC rejoint sa butée (vannes TOR déjà en place,
                #   ex. sortie du mode PdC) : tension TENUE à sa dernière
                #   valeur — la couper arrêtait le ventilateur le temps de la
                #   course de la PdC.
                # Le biais de table appris (intégrale) est conservé et la
                # régulation repartira de la tension tenue, sans à-coup.
                u = (self.etat.get("u_variateur", 0.0) or 0.0) \
                    if (diaphragme_ok and bypass_ok) else 0.0
                # PdC vers sa cible, simultanément avec les vannes TOR
                pdc_v = 10.0 if pdc_ouverte_visee else self._pdc_consigne
                self.trim.reinitialiser(u, self.trim.integrale)
                self._u_ff_applique = max(0.0, u - self.trim.integrale)
                self._consigne_interne = None
                self._diag = {"phase": "attente vannes"}
            else:
                # Vannes OK : réinitialiser compteur
                self._cycles_attente_vannes = 0

            if vannes_pretes and mode_table == MODE_PDC_CONTROL:
                # très bas Re : tension fixe (u_pdc), le PI règle la PdC.
                # Le PI a été calé sur le feed-forward PdC par _preparer_pi
                # au changement de mode (plus haut).
                self._pdc_regulee = True
                # u_pdc rejoint sur la rampe du variateur : pas d'échelon à la
                # bascule depuis le mode variateur
                u_prec = self.etat.get("u_variateur", 0.0) or 0.0
                pas = self._rampe_nominale * self.periode
                u = u_prec + max(-pas, min(pas, self._u_pdc() - u_prec))

                consigne_pi = self._reference_filtree(re_vise, re_mesure)
                self.pi.u_max = 10.0          # sortie = vanne, pas variateur
                # feed-forward de la table PdC (Re conduite) : la sortie du PI
                # le contient (intégrale pré-chargée par _preparer_pi)
                v_ff_pdc = self._pdc_ff_v(re_vise * self._k_conversion)
                en_course = abs(self._pdc_consigne - self._pdc_position_modele) \
                    > self.ecart_course_pdc_v
                if v_ff_pdc is not None and en_course:
                    # vanne en course vers sa commande (≈ 16 s pleine course,
                    # p. ex. depuis grande ouverte à l'entrée en mode PdC) :
                    # le Re mesuré est celui d'une vanne pas encore arrivée.
                    # Le PI le poursuivait, vidait son intégrale, et la vanne
                    # arrivait bien trop fermée (Re à -30 %, 30 s de rattrapage).
                    # Vanne tenue sur la table, PI recalé dessus ; il reprend
                    # la main une fois la vanne arrivée.
                    self.pi.reinitialiser(v_ff_pdc)
                    self._consigne_interne = None
                    pdc_v = v_ff_pdc
                else:
                    pdc_v = min(max(self.pi.calculer(consigne_pi, re_mesure),
                                    0.0), 10.0)
                # démarrage du ventilateur (à l'arrêt : repos ou après une
                # manœuvre TOR) : seulement une fois la PdC à sa position, comme
                # les vannes TOR sur leurs fins de course. Lancé vanne encore
                # ouverte, il faisait dépasser la consigne de ~36 %. Ventilateur
                # déjà en marche : tension tenue pendant la course (inchangé).
                if u_prec <= 0.0 and abs(pdc_v - self._pdc_position_modele) \
                        > self.ecart_course_pdc_v:
                    self._pdc_attente_depuis = attente_pdc or maintenant
                    if maintenant - self._pdc_attente_depuis > \
                            self.facteur_chien_garde * self.t_vanne_pdc_course \
                            + self.marge_manoeuvre:
                        self._repli_sur()
                        self.etat.update({
                            "defaut_vanne": (
                                f"vanne PdC : position {pdc_v:.2f} V non atteinte "
                                f"(modèle {self._pdc_position_modele:.2f} V) — "
                                "vanne bloquée ou recopie, vérifier puis relancer"),
                            "regulation": False, "u_variateur": 0.0,
                            "convergee": False,
                        })
                        self._pdc_attente_depuis = None
                        return
                    u = 0.0
                else:
                    self._pdc_attente_depuis = None
                if self._pdc_attente_depuis is not None:
                    self._diag = {"phase": "positionnement PdC avant ventilateur",
                                  "pdc_v": round(pdc_v, 3)}
                elif v_ff_pdc is not None:
                    self._diag = {
                        "phase": "FF+PI vanne PdC",
                        "pdc_v": round(pdc_v, 3),
                        "v_ff_pdc": round(v_ff_pdc, 3),
                        "correction_pi": round(pdc_v - v_ff_pdc, 3)
                    }
                else:
                    self._diag = {"phase": "PI vanne PdC", "pdc_v": round(pdc_v, 3)}
                # consigne hors de portée : variateur figé à u_min, la vanne
                # PdC en butée ne peut plus rapprocher le Re de la consigne.
                # Sans alarme, la régulation attendait indéfiniment.
                # butée PHYSIQUE : commande en butée ET vanne arrivée (position
                # estimée, recalée sur la recopie) ; la commande seule y est
                # dès le début de la course, d'où une fausse alarme
                # fins de course de la PdC (indépendantes de l'échelle de la
                # recopie) ; à défaut, modèle de course
                pos = self._pdc_position_modele
                fc_o = mesures.get("fc_pdc_ouvert")
                fc_f = mesures.get("fc_pdc_ferme")
                en_butee_o = fc_o if fc_o is not None else pos >= 10.0 - self.ecart_course_pdc_v
                en_butee_f = fc_f if fc_f is not None else pos <= self.ecart_course_pdc_v
                ouverte = pdc_v >= 10.0 - 1e-6 and en_butee_o \
                    and re_mesure < re_vise * (1.0 - self.tolerance)
                fermee = pdc_v <= 1e-6 and en_butee_f \
                    and re_mesure > re_vise * (1.0 + self.tolerance)
                self._cycles_pdc_butee = self._cycles_pdc_butee + 1 \
                    if (ouverte or fermee) and self._pdc_attente_depuis is None \
                    else 0
                if self._cycles_pdc_butee > int(5.0 / self.periode):
                    self._diag["alerte_pdc"] = (
                        f"consigne {re_vise:.0f} hors de portée du mode PdC "
                        f"(vanne PdC en butée {'ouverte' if ouverte else 'fermée'}, "
                        f"variateur fixé à {u:.2f} V) — ajuster "
                        "strategie_bas_re (seuil, u_min) ou la consigne")
            elif vannes_pretes:
                self._pdc_regulee = False
                # modes normaux : PdC grande ouverte, variateur =
                # feed-forward + correction PI en volts
                # CORRECTION CRITIQUE-1 : Utiliser mode_table pour choisir table gains
                pdc_v = 10.0
                u = self._commande_variateur(mode_table, re_cible, re_vise,
                                             re_mesure, mesures, maintenant)

            # Appliquer les commandes
            self.es.ecrire_variateur(u)
            self.commander_vanne_perte_charge(pdc_v)

            # Garder trace pour compatibilité (TODO: supprimer à terme)
            # Utiliser mode_table (effectif) pour _mode_bas_re
            self._mode_bas_re = (mode_table != MODE_NORMAL_HIGH)
        else:
            u = 0.0
            self.es.ecrire_variateur(u)
            self._mode_bas_re = False
            self._diag = {}

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

        # jugée sur la consigne effectivement visée : bornée au maximum
        # mesurable, une consigne hors plage ne convergeait jamais (Re moyen
        # sur le plafond, mais comparé à la consigne saisie)
        re_effectif = self._re_borne(re_cible)
        tol = self.tolerance_sortie if self._convergee_depuis is not None \
            else self.tolerance
        # pas de convergence sur un transitoire : la moyenne d'une fenêtre
        # qui traverse la consigne peut tomber dessus (Re instantané à
        # +35 %). Chaque demi-fenêtre doit déjà être dans la bande de sortie.
        stable = False
        if fenetre_pleine and re_effectif > 0:
            moitie = len(fenetre) // 2
            valeurs = list(fenetre)
            stable = all(
                abs(fmean(part) - re_effectif) / re_effectif
                < self.tolerance_sortie
                for part in (valeurs[:moitie], valeurs[moitie:]))
        convergee = bool(
            regul and re_effectif > 0 and fenetre_pleine and stable
            and abs(re_moyen - re_effectif) / re_effectif < tol)
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
        if self.bas_re_actif:
            motifs = []
            if self._seuil_bas_re_config_invalide():
                motifs.append(f"seuil bas-Re de config ({self.seuil_bas_re:.0f}) "
                              f"au-delà du seuil Miller/ISO "
                              f"({self.banc.seuil_re:.0f}) : ignoré — corriger "
                              "strategie_bas_re")
            if not self.table_gains_bas_re.valide_ff():
                motifs.append("Re mapping Miller non fait : le mode PdC (très "
                              "bas Re) reste inactif — lancer l'apprentissage "
                              "« Re mapping Miller »")
            elif not self._mode_pdc_caracterise():
                plancher = self._re_plancher_miller()
                motifs.append("phase PdC du Re mapping Miller sans palier "
                              "exploitable : mode PdC (très bas Re) inactif"
                              + (f", consignes bornées à {plancher:.0f} Re "
                                 "conduite (variateur à u_min)" if plancher else ""))
            elif not seuil or self.u_min_ventilateur is None:
                motifs.append("seuil bas-Re inconnu : lancer l'apprentissage "
                              "« Re mapping » (ou fixer strategie_bas_re.seuil_re)")
            bornes = self._re_bornes_bas_re()
            if bornes and not self.seuil_bas_re and bornes[1] < 1.105 * bornes[0]:
                motifs.append("marge_ventilateur_pdc trop faible : le mode PdC "
                              "ne couvre plus la bande d'hystérésis du seuil")
            bas_re_motif = " ; ".join(motifs) or None

        # Calculer systématiquement rho, mu, nu (même si dP=0)
        # pour affichage dans l'IHM, en utilisant T et P_amont actuels
        P1_abs = self.banc.patm_pa + p_amont
        rho_actuel = self.banc.fluide.rho(P1_abs, T)
        mu_actuel = self.banc.fluide.mu(T)
        nu_actuel = self.banc.fluide.nu(P1_abs, T)

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
            "rho": rho_actuel,
            "mu": mu_actuel,
            "nu": nu_actuel,
            "C": resultat.get("C"), "u_variateur": u,
            "re_cible": re_cible, "reference": reference,
            "re_vise": re_effectif,
            "re_mesure": re_mesure, "convergee": convergee,
            "regulation": regul, "manuel": manuel,
            "pdc_consigne": self._pdc_consigne,
            "mode_bas_re": self._mode_bas_re,
            "seuil_bas_re_effectif": round(seuil) if seuil else None,
            "dp_sature": dp_sature,
            "u_ff": self._diag.get("u_ff"),
            "trim_v": self._diag.get("trim"),
            "integrale_v": self._diag.get("integrale"),
            "re_reference": self._diag.get("reference"),
            "phase_regulation": self._diag.get("phase"),
            "alerte_table_ff": self._diag.get("alerte_table_ff"),
            "alerte_pdc": self._diag.get("alerte_pdc"),
        })

        # ===== LOGGER DIAGNOSTIC RÉGULATION =====
        # Grandeurs internes du cycle (feed-forward, correction, phase), telles
        # que calculées par la commande — pas recalculées ici.
        if self.logger_regulation.actif and regul and re_cible > 0:
            d = self._diag
            self.logger_regulation.log_cycle({
                'consigne_re': re_cible,
                'mode_regulation': self._mode_regulation,
                'diaphragme': self.etat.get("diaphragme"),
                'bypass_ferme': bool(mesures.get("fc_bypass_ferme")),
                'pdc_position_v': round(self._pdc_position_estimee, 3),
                'u_ff_regression': None,
                'u_ff_interpolation': d.get("u_ff_cible"),
                'u_ff_utilise': d.get("u_ff"),
                'methode_ff': d.get("phase"),
                'u_pi_sortie': d.get("trim"),
                'u_variateur_final': round(u, 3),
                're_mesure': round(re_mesure, 1),
                'erreur_re': round(re_cible - re_mesure, 1),
                'erreur_relative_pct': round((re_cible - re_mesure)
                                             / re_cible * 100, 2),
                'alarme': (self.etat.get("consigne_limitee")
                           or d.get("alerte_table_ff")
                           or ("dP saturé" if dp_sature else None)),
            })

    def _determiner_mode_regulation(self, re_cond: float) -> str:
        """Mode de régulation pour un Re conduite visé, AVEC hystérésis.

        Sert à la PRÉPARATION d'une consigne (pré-positionnement de la PdC,
        calage des régulateurs). En régulation, c'est gestion_modes qui
        décide, avec les mêmes seuils (cf. _configuration_modes) ; un écart
        éventuel est rattrapé par un nouveau calage au cycle suivant.

        - MODE_NORMAL_HIGH : ISO, bypass fermé, PdC 100 %, variateur réglé
        - MODE_NORMAL_LOW  : Miller, bypass ouvert, PdC 100 %, variateur réglé
        - MODE_PDC_CONTROL : Miller, bypass ouvert, variateur à u_min, PdC réglée

        Seuil ISO/Miller et hystérésis : ceux du Banc (selection_diaphragme),
        les mêmes que choisir_diaphragme. Sans hystérésis, une séquence
        oscillant autour du seuil commutait diaphragme ET bypass à chaque
        palier (vannes de plusieurs secondes, variateur à 0 V pendant ce
        temps). Entre Miller et PdC : hystérésis de ±5 % sur Re min.
        """
        seuil = self.banc.seuil_re
        hyst = self.banc.hysteresis_re

        # même seuil bas-Re que la boucle (0 si la stratégie est désactivée)
        freq_min_re = self._seuil_bas_re()

        courant = self._mode_regulation
        if courant is None:
            # premier choix : partir de la position réelle du diaphragme,
            # comme choisir_diaphragme (sinon les deux se contredisent dans
            # la bande d'hystérésis)
            courant = MODE_NORMAL_HIGH if self.etat.get("diaphragme") == "iso" \
                else MODE_NORMAL_LOW

        if courant == MODE_NORMAL_HIGH:
            haut = re_cond >= seuil - hyst
        else:
            haut = re_cond > seuil + hyst
        if haut or self._miller_insuffisant(re_cond):
            return MODE_NORMAL_HIGH

        if courant == MODE_PDC_CONTROL:
            bas = re_cond < 1.05 * freq_min_re
        elif courant == MODE_NORMAL_LOW:
            bas = re_cond < 0.95 * freq_min_re
        else:
            bas = re_cond < freq_min_re
        return MODE_PDC_CONTROL if bas else MODE_NORMAL_LOW

    def _seuil_bas_re(self) -> float:
        """Re en dessous duquel on bascule en mode PdC. Valeur de config si
        fixée, sinon dérivée de u_min : à mi-chemin entre Re(u_min), le Re
        minimal du mode Miller à variateur réglé, et Re(u_pdc), le Re maximal
        du mode PdC (variateur à u_pdc, PdC grande ouverte). Les deux modes
        couvrent ainsi la bande d'hystérésis (±5 %) : avant, le seuil valait
        1,3 × Re(u_min) avec le variateur figé à u_min en mode PdC, et les
        consignes entre Re(u_min) et 1,3 × Re(u_min) étaient inatteignables.

        Un seuil configuré au-delà du seuil Miller/ISO est ignoré (cf.
        _seuil_bas_re_config_invalide) : toute consigne Miller partirait en
        mode PdC, variateur figé à u_min, et resterait inatteignable.

        Le seuil dérivé vient en priorité de la table Miller (bypass ouvert,
        configuration du mode PdC) ; la table ISO et le gain global ont été
        mesurés bypass fermé et surestiment le Re tenu à u_min en Miller."""
        if not self.bas_re_actif:
            return 0.0                     # stratégie désactivée : jamais de mode PdC
        if not self._mode_pdc_caracterise():
            return 0.0
        if self.seuil_bas_re and not self._seuil_bas_re_config_invalide():
            return self.seuil_bas_re
        bornes = self._re_bornes_bas_re()
        if not bornes:
            return 0.0
        if self.u_ventilateur_pdc and self.u_ventilateur_pdc > \
                (self.u_min_ventilateur or 0.0) * self.marge_ventilateur_pdc:
            # tension du mode PdC imposée : le mode PdC prend le relais dès
            # Re(u_pdc), le variateur ne descend pas (ou guère) en dessous.
            # / 1,06 : PdC ouverte, le mode PdC couvre encore le haut de la
            # bande d'hystérésis (seuil × 1,05).
            return bornes[1] / 1.06
        return 0.5 * (bornes[0] + bornes[1])

    def _mode_pdc_caracterise(self) -> bool:
        """Le mode PdC fixe le ventilateur à u_min × marge : il n'est sûr
        qu'avec un u_min MESURÉ sur ce banc, c.-à-d. après le Re mapping
        Miller (qui produit aussi la table Miller). Sans lui, le seuil
        retombait sur la table ISO ou le gain appris en marche, avec le
        u_min de la config : sur un banc non caractérisé, ventilateur à
        0,35 V, Re nul et alarme « hors de portée ».

        Il faut aussi la table PdC (phase PdC du Re mapping Miller) et une
        PdC minimale exploitable sous 100 % : sans elles (constaté au banc,
        aucun palier PdC mesurable), le PI fermait la vanne sans borne sous
        le plancher de mesure du Miller -> Re lu à 0, oscillation sans fin,
        sans alarme. Le mode PdC est alors inactif et les consignes sont
        bornées au Re tenu par le variateur à u_min (_re_plancher_miller)."""
        pdc_min = self.pdc_min_exploitable_pct
        return (self.table_gains_bas_re.valide_ff()
                and self.table_gains_pdc.valide_ff()
                and not (_nombre(pdc_min) and pdc_min >= 100.0))

    def _re_plancher_miller(self) -> float | None:
        """Re conduite minimal tenu en Miller par le variateur seul (à
        u_min, PdC ouverte), d'après la table Miller. None si inconnu."""
        if not self.u_min_ventilateur or not self.table_gains_bas_re.valide_ff():
            return None
        return self.table_gains_bas_re.re_pour_u_ff(self.u_min_ventilateur)

    def _u_pdc(self) -> float:
        """Tension du ventilateur en mode PdC : u_min × marge_ventilateur_pdc,
        ou la tension imposée (u_ventilateur_pdc_v) si elle est plus haute :
        ventilateur tenu à une vitesse plus franche, la PdC règle le débit."""
        u_min = self.u_min_ventilateur or _U_MIN_VENTILATEUR_FALLBACK
        return min(max(u_min * self.marge_ventilateur_pdc,
                       self.u_ventilateur_pdc or 0.0), 10.0)

    def _re_bornes_bas_re(self) -> tuple[float, float] | None:
        """(Re(u_min), Re(u_pdc)) en Miller, bypass ouvert, PdC ouverte :
        table Miller en priorité ; la table ISO et le gain global, mesurés
        bypass fermé, surestiment ces Re (repli faute de mieux)."""
        if not self.u_min_ventilateur:
            return None
        u0, u1 = self.u_min_ventilateur, self._u_pdc()
        if self.table_gains_bas_re.valide_ff():
            t = self.table_gains_bas_re
            return t.re_pour_u_ff(u0), t.re_pour_u_ff(u1)
        if self.table_gains.valide_ff():
            t = self.table_gains
            return t.re_pour_u_ff(u0), t.re_pour_u_ff(u1)
        if self._gain_re_par_volt:
            return u0 * self._gain_re_par_volt, u1 * self._gain_re_par_volt
        return None

    def _re_max_miller(self, T: float | None = None,
                       p_amont: float | None = None) -> float | None:
        """Re conduite maximal mesurable sur le Miller : ΔP Miller attendu à
        la limite des consignes (marge_consigne_dp × saturation_dp × pleine
        échelle), aux T et P amont données (sinon les dernières mesurées).
        Au-delà, le Miller saturerait : c'est l'ISO qu'il faut, quel que soit
        le seuil de bascule configuré (≈ 8 200 Re conduite à 20 °C, 7 900 à
        30 °C : le ΔP croît avec μ à Re donné)."""
        T = self.etat.get("temperature") if T is None else T
        p_amont = self.etat.get("p_amont") if p_amont is None else p_amont
        if not _nombre(T):
            return None
        p_amont = p_amont if _nombre(p_amont) else 0.0
        cle = (round(T, 1), round(p_amont, -1))
        if self._cache_re_max_miller[0] == cle:
            return self._cache_re_max_miller[1]
        dp_lim = self.marge_consigne_dp * self.saturation_dp \
            * self._dp_pleine_echelle[CLE_DP["miller"]]
        re_max = None
        try:
            qm = self.banc.re_conduite_vers_qm(self.banc.seuil_re, T, p_amont)
            for _ in range(6):            # ΔP ~ qm² : convergence en 2-3 pas
                dp = self.banc.dP_attendu("miller", qm, T, p_amont)
                if dp <= 0:
                    break
                qm *= (dp_lim / dp) ** 0.5
            else:
                re_max = self.banc.qm_vers_re(
                    qm, T, self.banc.patm_pa + p_amont)["re_conduite"]
        except (ValueError, ZeroDivisionError):
            re_max = None
        self._cache_re_max_miller = (cle, re_max)
        return re_max

    def _miller_insuffisant(self, re_cond: float, T: float | None = None,
                            p_amont: float | None = None) -> bool:
        """Consigne hors de portée du capteur Miller : ISO obligatoire."""
        re_max = self._re_max_miller(T, p_amont)
        return re_max is not None and re_cond > re_max

    def _mode_cible_courant(self, re_cible_cond: float) -> ModeRegulation:
        """Mode visé pour la consigne (gestion_modes), avec sa mémoire
        d'hystérésis. Seul point de décision : la boucle et la redirection
        des vannes en cours de manœuvre (_rediriger_vannes) l'utilisent tous
        deux, sans quoi elles pourraient se contredire dans les bandes
        d'hystérésis et faire osciller les vannes.

        Mémoire amorcée sur le mode préparé (lui-même parti de la position
        réelle de la vanne) : vide, la bande d'hystérésis était tranchée sur
        le seuil seul, contre la préparation."""
        # seuils relus : seuil bas-Re appris, Banc remplacé par une édition
        self._config_modes = self._configuration_modes()
        precedent = self._mode_cible_precedent
        if precedent is None and self._mode_regulation is not None:
            precedent = ModeRegulation(self._mode_regulation)
        mode = determiner_mode_cible(re_cible_cond, self._config_modes,
                                     precedent)
        if mode == ModeRegulation.NORMAL_LOW \
                and self._miller_insuffisant(re_cible_cond):
            mode = ModeRegulation.NORMAL_HIGH
        self._mode_cible_precedent = mode
        return mode

    def _rediriger_vannes(self, mode: ModeRegulation, mesures: dict) -> None:
        """Nouvelle consigne pendant un déplacement de vannes : chaque vanne
        rejoint tout de suite la position du nouveau mode — inversion en
        cours de course si elle allait ailleurs (la carte GF3 et les ER20
        acceptent l'ordre inverse à tout moment), commande immédiate si elle
        était immobile hors de sa cible. Toutes ensemble, chacune avec un
        chien de garde neuf. Sans cela, la course devenue inutile allait à
        son terme avant le demi-tour (~20 s perdues)."""
        vise_diaph = "iso" if mode == ModeRegulation.NORMAL_HIGH else "miller"
        vise_ouvert = mode != ModeRegulation.NORMAL_HIGH
        etat_v = lire_etat_vannes(mesures, self._diaphragme_par_angle)

        m = self._manoeuvres.get("3voies")
        if m is not None:
            if m["angle_attendu"] != self._angle_par_diaphragme[vise_diaph]:
                # sans entrée en cours, commander_diaphragme ne relance pas de
                # manœuvre si la vanne n'a pas encore quitté sa butée cible
                self._manoeuvres.pop("3voies", None)
                self.commander_diaphragme(vise_diaph)
        elif etat_v.diaphragme in (PositionDiaphragme.ISO, PositionDiaphragme.MILLER) \
                and etat_v.diaphragme.value != vise_diaph:
            self.commander_diaphragme(vise_diaph)

        fc_visee = "fc_bypass_ouvert" if vise_ouvert else "fc_bypass_ferme"
        m = self._manoeuvres.get("bypass")
        if m is not None:
            if m["fc_attendue"] != fc_visee:
                self.commander_bypass(vise_ouvert)
        elif etat_v.bypass in (PositionBypass.OUVERT, PositionBypass.FERME) \
                and (etat_v.bypass == PositionBypass.OUVERT) != vise_ouvert:
            self.commander_bypass(vise_ouvert)

    def _configuration_modes(self) -> ConfigurationModes:
        """Seuils de la gestion des modes, alignés sur ceux du Banc
        (selection_diaphragme) qu'utilise aussi la préparation de consigne.
        Banc.choisir_diaphragme bascule à seuil ± hystérésis ; gestion_modes
        applique seuil ± largeur/2 : la largeur vaut donc 2 × hystérésis. Avant,
        la boucle lisait une clé inexistante (seuil figé à 5000) et une demi-
        hystérésis : les deux logiques se contredisaient de 4500 à 4750."""
        return ConfigurationModes(
            seuil_diaphragme=self.banc.seuil_re,
            largeur_hysteresis_diaphragme=2.0 * self.banc.hysteresis_re,
            seuil_bas_re=self._seuil_bas_re(),
            largeur_hysteresis_bas_re=0.1 * self._seuil_bas_re(),  # ±5 % (gestion_modes)
            u_min=self.u_min_ventilateur or 0.0)

    def _seuil_bas_re_config_invalide(self) -> bool:
        """Seuil bas-Re de config (strategie_bas_re) au-delà du seuil
        Miller/ISO : incompatible avec le mode Miller à variateur réglé."""
        return bool(self.seuil_bas_re) and self.seuil_bas_re >= self.banc.seuil_re

    def _diaphragme_mesure(self, mesures: dict) -> str:
        """Diaphragme dont le capteur ΔP et la géométrie servent à la mesure :
        celui de la butée LUE sur les fins de course de la vanne 3 voies
        (ISO en butée ISO, Miller en butée Miller), jamais la position
        commandée ni visée. Vanne en course (aucune butée lue) : dernière
        butée lue. Sans fin de course 3 voies (banc sans retour de
        position) : diaphragme de l'état."""
        angle = self._angle_vanne_3voies(mesures)
        if angle in self._diaphragme_par_angle:
            self._diaphragme_mesure_dernier = self._diaphragme_par_angle[angle]
        return self._diaphragme_mesure_dernier or self.etat["diaphragme"]

    def _dp_corrige(self, mesures: dict, diaphragme: str) -> float:
        """ΔP du capteur correspondant au diaphragme actif, corrigé du zéro
        appris à débit nul (décisif en bas-Re : le ΔP utile ne vaut que
        quelques Pa, un offset capteur serait poursuivi par le PI)."""
        cle = CLE_DP.get(diaphragme, CLE_DP["miller"])
        return mesures.get(cle, 0.0) - self.zero_capteurs.get(cle, 0.0)

    def _table_ff(self, mode: str):
        """Table de feed-forward du mode : ISO (bypass fermé) en normal_high,
        Miller (bypass ouvert) sinon. None si la table n'est pas exploitable.
        La table suit le MODE (donc la configuration des vannes), pas un
        seuil de Re : dans la bande d'hystérésis, le Re visé peut être sous
        5000 avec l'ISO en ligne."""
        t = self.table_gains if mode == MODE_NORMAL_HIGH \
            else self.table_gains_bas_re
        return t if t.valide_ff() else None

    def _u_equilibre(self, re_cond_attendu: float,
                     mode: str | None = None) -> float | None:
        """Tension variateur d'équilibre estimée pour un Re conduite visé :
        interpolation de la table apprise du mode, sinon gain global appris
        (ISO seulement : il a été mesuré bypass fermé), sinon None."""
        if mode is None:
            mode = MODE_NORMAL_HIGH if re_cond_attendu >= self.banc.seuil_re \
                else MODE_NORMAL_LOW
        table = self._table_ff(mode)
        if table is not None:
            return table.u_pour_re_ff(re_cond_attendu)
        if mode == MODE_NORMAL_HIGH and self._gain_re_par_volt:
            return min(max(re_cond_attendu / self._gain_re_par_volt, 0.0), 10.0)
        return None

    def _re_borne(self, re_cible: float) -> float:
        """Consigne effectivement visée : bornée au maximum mesurable par le
        capteur ΔP et au minimum tenu en mode PdC."""
        if self._re_cible_max is not None:
            re_cible = min(re_cible, self._re_cible_max)
        if self._re_cible_min is not None:
            re_cible = max(re_cible, self._re_cible_min)
        return re_cible

    def _borner_consigne(self, diaph: str, qm_vise: float, re_cible: float,
                         T: float, p_amont: float,
                         reference: str = "conduite") -> None:
        """Consigne au-delà de la plage mesurable du capteur ΔP du
        diaphragme : la régulation vise le maximum mesurable (avec la marge
        marge_consigne_dp) et l'opérateur est prévenu.

        Sans cette borne, le Re calculé plafonne au capteur, l'erreur ne se
        résorbe jamais, et la commande part en butée."""
        self._re_cible_max = None
        self._re_cible_min = None
        self.etat["consigne_limitee"] = None
        cle = CLE_DP.get(diaph, CLE_DP["miller"])
        dp_lim = self.marge_consigne_dp * self.saturation_dp \
            * self._dp_pleine_echelle[cle]
        dp_att = self.etat.get("dp_attendu") or 0.0
        if qm_vise <= 0 or dp_lim <= 0 or dp_att <= dp_lim:
            return
        qm_max = qm_vise
        for _ in range(5):            # ΔP ~ qm² : convergence en 2-3 pas
            dp_q = self.banc.dP_attendu(diaph, qm_max, T, p_amont)
            if dp_q <= 0:
                return
            qm_max *= (dp_lim / dp_q) ** 0.5
        # Re ∝ qm à T et P donnés : le rapport vaut dans les deux références
        self._re_cible_max = re_cible * qm_max / qm_vise
        msg = (f"consigne {re_cible:.0f} Re {reference} hors plage du capteur ΔP "
               f"({dp_att:.0f} Pa attendus, {dp_lim:.0f} Pa max) : "
               f"régulation limitée à {self._re_cible_max:.0f} Re {reference}")
        self.etat["consigne_limitee"] = msg
        log.warning(msg)

    def _reference_filtree(self, re_vise: float, re_mesure: float) -> float:
        """Consigne filtrée au 1er ordre (tau_consigne), ancrée sur la mesure
        avec un plancher de 20 % de la cible. Sert aux PI à erreur relative
        (vanne PdC, repli sans table). tau = 0 : échelon brut."""
        if self.tau_consigne <= 0:
            self._consigne_interne = None
            return re_vise
        if self._consigne_interne is None:
            self._consigne_interne = max(
                re_mesure, _RATIO_ANCRAGE_CONSIGNE_INTERNE * re_vise)
        a = self.periode / (self.tau_consigne + self.periode)
        self._consigne_interne += a * (re_vise - self._consigne_interne)
        return self._consigne_interne

    def _variateur_en_route(self, mesures: dict, maintenant: float) -> bool:
        """Vrai tant que la recopie AM du 3G3MV n'a pas rejoint la dernière
        commande (le ventilateur suit encore la rampe interne du variateur).
        Faux si la recopie est absente, RUN coupé, ou si l'écart dure plus de
        suivi_variateur_max_s (recopie fausse : ne pas geler pour toujours)."""
        am = mesures.get("frequence_variateur")
        if am is None or not self._run_actif or self.suivi_variateur_max_s <= 0:
            self._t_ecart_variateur = None
            return False
        u_prec = self.etat.get("u_variateur", 0.0) or 0.0
        if abs(am - u_prec) <= self.tol_suivi_variateur:
            self._t_ecart_variateur = None
            return False
        if self._t_ecart_variateur is None:
            self._t_ecart_variateur = maintenant
        return maintenant - self._t_ecart_variateur < self.suivi_variateur_max_s

    def _commande_variateur(self, mode: str, re_cible: float, re_vise: float,
                            re_mesure: float, mesures: dict,
                            maintenant: float) -> float:
        """Tension variateur des modes normaux.

        1. feed-forward : tension d'équilibre de la table apprise pour la
           consigne, rejointe sur la rampe rampe_v_par_s ;
        2. modèle de référence : Re que produirait ce feed-forward, filtré à
           la constante de temps du procédé (tau_consigne_s). C'est CE Re que
           la correction poursuit, pas l'échelon : en transitoire, l'écart
           mesuré ne traduit que l'erreur du modèle -> pas de coup de bélier
           du terme P, pas d'intégrale enroulée, pas de dépassement ;
        3. correction PI en volts (RegulateurTrim), intégrale gelée pendant
           la rampe du feed-forward, P et I gelés tant que le variateur n'a pas rejoint
           la commande (recopie AM).

        Re_cible / re_vise sont dans l'unité de la référence (conduite ou
        veine) ; les tables sont en Re conduite : le rapport est constant
        (même débit, géométrie fixe), on convertit par k.
        """
        table = self._table_ff(mode)
        gain = self._gain_re_par_volt if mode == MODE_NORMAL_HIGH else None
        if table is None and not gain:
            # banc non caractérisé : PI à erreur relative (repli)
            consigne_pi = self._reference_filtree(re_vise, re_mesure)
            self.pi.u_max = 10.0 if self._plafond_u_sature is None \
                else self._plafond_u_sature
            u = min(max(self.pi.calculer(consigne_pi, re_mesure), 0.0), 10.0)
            self._diag = {"phase": "PI seul (banc non caractérisé)",
                          "reference": round(consigne_pi, 1)}
            return u

        # Utiliser le facteur de conversion k sauvegardé (calculé AVANT les limitations)
        # Cela garantit que le lookup de table utilise le bon facteur même si
        # _re_cible_conduite a été limité par re_max_iso ou _borner_consigne
        k = self._k_conversion

        def u_pour(re_ref):
            if table is not None:
                return table.u_pour_re_ff(re_ref * k)
            return min(max(re_ref * k / gain, 0.0), 10.0)

        def re_pour(v):
            return (table.re_pour_u_ff(v) if table is not None else gain * v) / k

        def pente(v):
            return (table.pente_ff(v) if table is not None else gain) / k

        # 1. feed-forward sur rampe
        u_cible = u_pour(re_vise)
        pas = self.trim.rampe * self.periode
        self._u_ff_applique += max(-pas, min(pas, u_cible - self._u_ff_applique))
        ff_rendu = abs(u_cible - self._u_ff_applique) < 1e-9
        # 2. modèle de référence (à l'arrivée : la consigne elle-même, pour
        #    que l'intégrale annule l'erreur statique même hors table)
        re_modele = re_vise if ff_rendu else re_pour(self._u_ff_applique)
        if self._consigne_interne is None:
            self._consigne_interne = re_mesure
        if self.tau_consigne > 0:
            a = self.periode / (self.tau_consigne + self.periode)
            self._consigne_interne += a * (re_modele - self._consigne_interne)
        else:
            self._consigne_interne = re_modele
        reference = self._consigne_interne

        # 3. correction
        en_route = self._variateur_en_route(mesures, maintenant)
        transitoire = not ff_rendu
        # séparation d'intégrale : grand écart ET mesure encore en mouvement
        # = le procédé n'a pas fini de répondre (tau_consigne sous-estimé,
        # rampe du variateur…) ; intégrer ce retard enroulerait l'intégrale
        # et produirait un dépassement. Le P agit seul. Dès que la mesure
        # est stable, on intègre quel que soit l'écart (biais de table fort).
        self._hist_re_court.append(re_mesure)
        h = self._hist_re_court
        stable = False
        if len(h) == h.maxlen and re_vise > 0:
            m = len(h) // 2
            debut = fmean(list(h)[:m])
            fin = fmean(list(h)[m:])
            stable = abs(fin - debut) / re_vise < self.seuil_stabilite
        ecart_rel = abs(reference - re_mesure) / reference if reference > 0 else 0.0
        separation = ecart_rel > self.bande_integration and not stable
        u = self.trim.calculer(reference, re_mesure, self._u_ff_applique,
                               pente(self._u_ff_applique),
                               u_max=self._plafond_u_sature,
                               geler_integrale=transitoire or en_route
                               or separation,
                               geler_p=en_route)

        # biais de table appris, rapporté au feed-forward : resservira à la
        # prochaine consigne du même mode (intégrale pré-chargée)
        sature = abs(self.trim.integrale) >= self.trim.trim_max - 1e-6
        if not (transitoire or en_route or sature) \
                and self._plafond_u_sature is None and self._u_ff_applique > 0.3:
            self._biais_ff[mode] = (id(table),
                                    self.trim.integrale / self._u_ff_applique)
        # correction en butée durablement : la table ne décrit plus le banc
        if sature and not transitoire:
            self._cycles_trim_sature += 1
        else:
            self._cycles_trim_sature = 0
        alerte = None
        if self._cycles_trim_sature > int(5.0 / self.periode):
            alerte = (f"correction en butée ({self.trim.integrale:+.2f} V) : "
                      "la table de gains ne décrit plus le banc, relancer "
                      "l'apprentissage « Re mapping »")

        self._diag = {
            "phase": ("suivi variateur" if en_route else
                      "transitoire" if transitoire else "régulation"),
            "u_ff_cible": round(u_cible, 3),
            "u_ff": round(self._u_ff_applique, 3),
            "trim": round(self.trim.trim, 3),
            "integrale": round(self.trim.integrale, 3),
            "reference": round(reference, 1),
            "alerte_table_ff": alerte,
        }
        return u

    def _preparer_pi(self, mode: str, re_cond_attendu: float) -> None:
        """Cale les régulateurs sur le régime visé.

        - normal_high / normal_low : variateur = feed-forward + correction.
          L'intégrale démarre sur le biais de table appris dans ce mode
          (rapporté au nouveau feed-forward) ; la sortie reste continue
          (le feed-forward appliqué repart de la commande courante).
        - pdc_control : variateur à u_pdc, PI relatif sur la vanne PdC,
          pré-positionné sur la table PdC si elle existe.
        Le PI relatif est aussi recalé sur le feed-forward dans les modes
        normaux : c'est le repli si la table devient inexploitable.
        """
        if mode == MODE_PDC_CONTROL:
            self.pi.gain_scale = self.bas_re_scale
            self.pi.rampe = self._rampe_bas_re()
            pdc_ff = self._pdc_ff_v(re_cond_attendu)
            if pdc_ff is None and self.table_gains_bas_re.valide_ff():
                # approximation : fraction de u_min atteinte -> ouverture PdC
                u_ff = self.table_gains_bas_re.u_pour_re_ff(re_cond_attendu)
                u_pdc = self._u_pdc()
                # Si u_ff >= u_pdc, la PdC ouverte suffit (10 V) ; sinon
                # interpoler entre l'ouverture minimale exploitable et 10 V
                if u_ff < u_pdc:
                    pdc_min_v = self._pdc_pct_vers_v(self.pdc_min_exploitable_pct)
                    ratio = max(0.0, min(1.0, u_ff / u_pdc))
                    pdc_ff = pdc_min_v + (10.0 - pdc_min_v) * ratio
                else:
                    pdc_ff = 10.0
            if pdc_ff is None:
                pdc_ff = 10.0     # repli : partir ouverte, le PI fermera
            # pas de borne basse sur le PI : la consigne est bornée au Re min
            # mesuré (préparation), atteint à la PdC du dernier palier stable,
            # un pas SOUS pdc_min_exploitable_pct
            self.pi.u_min = 0.0
            self.pi.reinitialiser(pdc_ff)
            self._consigne_interne = None

        elif mode in (MODE_NORMAL_HIGH, MODE_NORMAL_LOW):
            self.pi.gain_scale = 1.0
            self.pi.rampe = self._rampe_nominale
            self.pi.u_min = 0.0
            # ventilateur jamais commandé sous u_min (décrochage) en régime
            # réglé ; 0 V reste possible à l'arrêt (hors régulateur)
            self.trim.u_min = self.u_min_ventilateur or 0.0
            u_actuel = self.etat.get("u_variateur", 0.0) or 0.0
            u_ff = self._u_equilibre(re_cond_attendu, mode)

            integ = 0.0
            biais = self._biais_ff.get(mode)
            if biais and u_ff is not None and biais[0] == id(self._table_ff(mode)):
                integ = biais[1] * u_ff
            self.trim.reinitialiser(u_actuel, integ)
            # sortie continue : feed-forward appliqué = commande - correction
            self._u_ff_applique = max(0.0, u_actuel - self.trim.integrale)
            if mode != self._mode_regulation:
                self._consigne_interne = None   # ré-ancrage sur la mesure
            self._cycles_trim_sature = 0

            self.pi.reinitialiser(u_ff if u_ff is not None else u_actuel)

        else:
            log.error(f"Mode de régulation invalide dans _preparer_pi: {mode}")
            raise ValueError(f"Mode de régulation invalide dans _preparer_pi: {mode}. "
                             f"Valeurs attendues: 'normal_high', 'normal_low', 'pdc_control'")

        self._mode_regulation = mode

    def _rampe_bas_re(self) -> float:
        """Rampe du PI en mode PdC : la sortie EST la consigne de vanne
        (0-10 V), que l'actionneur parcourt en t_vanne_pdc_course s. Plus
        vite, la commande devance la vanne et l'intégrale s'enroule contre
        elle (l'anti-windup voit la commande, pas la position réelle) ->
        dépassement puis oscillation lente."""
        return min(self._rampe_nominale, 10.0 / self.t_vanne_pdc_course)

