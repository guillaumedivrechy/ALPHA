# -*- coding: utf-8 -*-
"""Journal CSV des conditions d'essai.

Un fichier par session de régulation, écrit par le thread du pilote. Sert à
répondre après coup à « dans quelles conditions ce point a-t-il été relevé,
et ont-elles tenu pendant la mesure ? » — l'opérateur quitte l'IHM pour aller
mesurer sur le banc, il n'a pas les valeurs sous les yeux au moment utile.

Format aligné sur core/sequence.py : séparateur « ; », virgule décimale
(lecture directe dans Excel francophone).

Écriture volontairement défensive : un disque plein ou un fichier verrouillé
ne doit jamais interrompre la régulation. En cas d'échec, le journal se
désarme et publie son erreur ; la boucle continue.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

# colonne interne -> en-tête CSV. L'ordre fixe l'ordre des colonnes
# (format demandé par l'exploitation, 2026-11-02).
COLONNES = [
    ("horodatage", "horodatage"),
    ("t_s", "t [s]"),
    ("re_cible", "Re consigne"),
    ("re_mesure", "Re calculé"),
    ("convergee", "Convergence"),
    ("phase_regulation", "phase regulation"),
    ("qv", "Qv [m3/s]"),
    ("rho", "ρ [kg/m3]"),
    ("mu", "µ [Pa.s]"),
    ("nu", "ν [m2/s]"),
    ("dp_iso", "dP ISO [Pa]"),
    ("dp_miller", "dP Miller [Pa]"),
    ("p_amont", "P Amont [Pa]"),
    ("pdc_v", "Vanne PDC [V]"),
    ("pdc_fermeture_pct", "Vanne PDC [% ferm]"),
    ("vanne_iso", "Vanne ISO"),
    ("vanne_miller", "Vanne Miller"),
    ("temperature", "T [degC]"),
    ("temperature_source", "source T"),
    ("humidite_relative", "H [%HR]"),
    # source de l'humidité et lectures des deux sondes (corrigées de leur
    # offset), pour les comparer quelle que soit la source retenue
    ("humidite_source", "source H"),
    ("temperature_pt100", "T PT100 [degC]"),
    ("temperature_sht45", "T SHT45 [degC]"),
    ("defauts", "Defaut"),
    # fins de course, en dernières colonnes : 1 = contact actif, 0 = non
    # (vanne 3 voies : diaphragme de chaque butée ajouté à l'ouverture du
    # fichier, d'après modbus.positions_vanne_3voies)
    ("fc_3voies_0", "FC 3 voies 0°"),
    ("fc_3voies_90", "FC 3 voies 90°"),
    ("fc_3voies_180", "FC 3 voies 180°"),
    ("fc_bypass_ouvert", "FC Bypass ouvert"),
    ("fc_bypass_ferme", "FC Bypass fermé"),
    ("fc_pdc_ouvert", "FC PdC ouverte"),
    ("fc_pdc_ferme", "FC PdC fermée"),
]

# fins de course recopiées telles que lues (après inversion des contacts NF)
_FINS_DE_COURSE = tuple(cle for cle, _ in COLONNES if cle.startswith("fc_"))


def entetes(diaphragme_par_angle: dict | None = None) -> list[str]:
    """En-têtes du fichier ; les butées 0° et 90° de la vanne 3 voies
    portent le diaphragme qu'elles mettent en ligne (« FC 3 voies 0° (ISO) »)."""
    noms = {"iso": "ISO", "miller": "Miller"}
    angles = diaphragme_par_angle or {}
    resultat = []
    for cle, entete in COLONNES:
        diaph = None
        if cle.startswith("fc_3voies_"):
            diaph = angles.get(cle.removeprefix("fc_3voies_"))
        if diaph:
            entete = f"{entete} ({noms.get(diaph, diaph)})"
        resultat.append(entete)
    return resultat


# grandeurs déduites du débit : sans objet pendant la course de la vanne
# 3 voies (le débit passe par les deux voies, aucun capteur ne le mesure)
_MESURES_DEBIT = ("re_mesure", "qv")

# drapeaux de défaut recopiés dans la colonne « defauts »
_DEFAUTS = ("erreur_com", "defaut_capteur", "defaut_vanne",
            "defaut_variateur", "defaut_pdc", "incoherence_vanne",
            # SHT45 perdu : humidité saisie utilisée à sa place
            "alerte_sht45")


# colonnes arrondies à l'unité. Un Reynolds est un nombre sans dimension de
# quelques milliers : ses décimales n'ont aucun sens physique et encombrent le
# dépouillement. L'arrondi est une pure mise en forme : l'état du pilote et
# la boucle continuent de travailler en flottant.
COLONNES_ENTIERES = frozenset(("re_cible", "re_mesure"))


def _fmt(valeur, entier: bool = False) -> str:
    """Rend une valeur en texte CSV francophone."""
    if valeur is None:
        return ""
    if isinstance(valeur, bool):
        return "1" if valeur else "0"
    if entier and isinstance(valeur, (int, float)):
        return f"{valeur:.0f}"
    if isinstance(valeur, float):
        return f"{valeur:.6g}".replace(".", ",")
    return str(valeur).replace(";", ",")


class JournalEssai:
    """Journal d'une session. Ouvert au démarrage de la régulation, fermé à
    l'arrêt. Réouvrir crée un nouveau fichier : une session = un fichier."""

    def __init__(self, dossier, periode_s: float = 1.0,
                 diaphragme_par_angle: dict | None = None):
        self.dossier = Path(dossier)
        # butée -> diaphragme (en-têtes des fins de course 3 voies)
        self.diaphragme_par_angle = diaphragme_par_angle
        self.periode = max(periode_s, 0.0)
        self.chemin: Path | None = None
        self.erreur: str | None = None
        self.lignes = 0
        self._f = None
        self._t0 = None
        self._t_derniere = None

    # ----- cycle de vie ---------------------------------------------------

    def ouvrir(self, t_monotonic: float) -> None:
        """Ouvre un fichier horodaté. Sans effet si déjà ouvert."""
        if self._f is not None:
            return
        try:
            self.dossier.mkdir(parents=True, exist_ok=True)
            base = datetime.now().strftime("essai_%Y-%m-%d_%Hh%Mm%S")
            # l'horodatage est à la seconde : deux sessions rapprochées
            # (arrêt puis relance immédiate) tomberaient sur le même nom et
            # la seconde écraserait la première, ouverte en « w ».
            chemin = self.dossier / f"{base}.csv"
            n = 2
            while chemin.exists():
                chemin = self.dossier / f"{base}_{n}.csv"
                n += 1

            f = None
            try:
                f = chemin.open("w", encoding="utf-8-sig", newline="")
                f.write(";".join(entetes(self.diaphragme_par_angle)) + "\n")
                f.flush()
                self._f = f
                self.chemin = chemin
            except OSError:
                # Fermer le fichier en cas d'erreur après ouverture
                if f is not None:
                    try:
                        f.close()
                    except OSError:  # Spécifier exception système attendue
                        pass  # Best-effort cleanup, acceptable
                raise
        except OSError as exc:
            self.erreur = f"journal indisponible : {exc}"
            return
        self.erreur = None
        self.lignes = 0
        self._t0 = t_monotonic
        self._t_derniere = None

    def fermer(self) -> None:
        if self._f is None:
            return
        try:
            self._f.close()
        except OSError:
            pass
        self._f = None
        self._t0 = None
        self._t_derniere = None

    @property
    def ouvert(self) -> bool:
        return self._f is not None

    # ----- écriture -------------------------------------------------------

    def enregistrer(self, etat: dict, t_monotonic: float,
                    zero_capteurs: dict | None = None,
                    humidite_relative: float | None = None,
                    recopie_pdc: tuple[float, float] | None = None) -> None:
        """Écrit une ligne si la période est écoulée. Silencieux sinon.
        zero_capteurs : zéros appris, ôtés des ΔP bruts des deux capteurs ;
        humidite_relative : humidité de la configuration [%], si l'état
        ne publie pas l'humidité utilisée (etat["humidite"]) ;
        recopie_pdc : tensions de recopie de la vanne PdC fermée / ouverte
        (apprentissage « calibration PdC »)."""
        if self._f is None:
            return
        # tolérance de 10 % : la boucle tourne à la même période que le
        # journal (0,2 s) ; un cycle arrivé à 0,199 s aurait sinon été sauté,
        # une ligne sur deux
        if self._t_derniere is not None \
                and t_monotonic - self._t_derniere < 0.9 * self.periode:
            return
        self._t_derniere = t_monotonic

        valeurs = dict(etat)
        brutes = etat.get("mesures_brutes") or {}
        zero = zero_capteurs or {}
        # les deux capteurs ΔP, corrigés de leur zéro (celui du diaphragme
        # hors ligne lit ~0)
        for cle, voie in (("dp_iso", "dp_iso_500"),
                          ("dp_miller", "dp_diaph_500")):
            v = brutes.get(voie)
            valeurs[cle] = None if v is None else v - zero.get(voie, 0.0)
        # vanne de perte de charge : tension de RECOPIE (position réelle),
        # sur l'échelle apprise par la calibration (fermée -> ouverte), et
        # fermeture entre ces deux bornes. Sans recopie lue, la commande
        # 0-10 V est reportée sur cette même échelle.
        v_ferme, v_ouvert = recopie_pdc or (0.0, 10.0)
        plage = v_ouvert - v_ferme
        pdc = brutes.get("pdc_recopie")
        if pdc is None and etat.get("pdc_consigne") is not None:
            pdc = v_ferme + etat["pdc_consigne"] / 10.0 * plage
        valeurs["pdc_v"] = pdc
        if pdc is None or plage <= 0:
            valeurs["pdc_fermeture_pct"] = None
        else:
            fermeture = (v_ouvert - pdc) / plage * 100.0
            valeurs["pdc_fermeture_pct"] = round(
                min(max(fermeture, 0.0), 100.0), 1)
        # vanne 3 voies : 1 sur la butée lue de ce diaphragme ; 0 et 0 en
        # course
        lu = etat.get("diaphragme_lu")
        valeurs["vanne_iso"] = lu == "iso"
        valeurs["vanne_miller"] = lu == "miller"
        valeurs["humidite_relative"] = etat.get("humidite",
                                                humidite_relative)
        for cle in _FINS_DE_COURSE:
            v = brutes.get(cle)
            valeurs[cle] = None if v is None else bool(v)
        if lu == "en_transit":
            # vanne 3 voies en course : pas de valeur plutôt qu'une fausse
            # (l'IHM affiche « en cours »)
            for cle in _MESURES_DEBIT:
                valeurs[cle] = None
        # horodatage à la milliseconde (%f donne les microsecondes)
        valeurs["horodatage"] = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S.%f")[:-3]
        valeurs["t_s"] = round(t_monotonic - (self._t0 or t_monotonic), 2)
        valeurs["defauts"] = "; ".join(
            str(etat[c]) if isinstance(etat.get(c), str) else c
            for c in _DEFAUTS if etat.get(c))
        try:
            self._f.write(
                ";".join(_fmt(valeurs.get(cle), cle in COLONNES_ENTIERES)
                         for cle, _ in COLONNES) + "\n")
            self._f.flush()          # un arrêt brutal ne doit rien perdre
        except OSError as exc:
            # ne jamais faire tomber la boucle de régulation pour un disque
            self.erreur = f"ecriture journal interrompue : {exc}"
            self.fermer()
            return
        self.lignes += 1
