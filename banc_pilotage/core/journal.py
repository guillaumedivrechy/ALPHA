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

# colonne interne -> en-tête CSV. L'ordre fixe l'ordre des colonnes.
COLONNES = [
    ("horodatage", "horodatage"),
    ("t_s", "t [s]"),
    ("re_cible", "Re consigne"),
    ("re_mesure", "Re calculé"),
    ("re_moyen", "Re moyen"),
    ("re_ecart_type", "Re ecart-type"),
    ("convergee", "convergee"),
    ("dp", "dP [Pa]"),
    ("temperature", "T [degC]"),
    ("p_amont", "P amont [Pa]"),
    ("rho", "rho [kg/m3]"),
    ("qm", "qm [kg/s]"),
    ("u_variateur", "U variateur [V]"),
    ("pdc_position", "vanne PdC [V]"),
    ("diaphragme", "diaphragme"),
    ("reference", "reference"),
    ("mode_bas_re", "bas-Re"),
    ("dp_sature", "dP sature"),
    ("defauts", "defauts"),
]

# grandeurs déduites du ΔP : sans objet pendant la course de la vanne 3 voies
_MESURES_DEBIT = ("re_mesure", "re_moyen", "re_ecart_type", "dp", "qm")

# drapeaux de défaut recopiés dans la colonne « defauts »
_DEFAUTS = ("erreur_com", "defaut_capteur", "defaut_vanne",
            "defaut_variateur", "defaut_pdc", "incoherence_vanne")


# colonnes arrondies à l'unité. Un Reynolds est un nombre sans dimension de
# quelques milliers : ses décimales n'ont aucun sens physique et encombrent le
# dépouillement. L'écart-type en fait partie, il s'exprime dans la même unité.
# L'arrondi est une pure mise en forme : l'état du pilote et la boucle
# continuent de travailler en flottant.
COLONNES_ENTIERES = frozenset((
    "re_cible", "re_mesure", "re_moyen", "re_ecart_type",
))


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

    def __init__(self, dossier, periode_s: float = 1.0):
        self.dossier = Path(dossier)
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
                f.write(";".join(entete for _, entete in COLONNES) + "\n")
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

    def enregistrer(self, etat: dict, t_monotonic: float) -> None:
        """Écrit une ligne si la période est écoulée. Silencieux sinon."""
        if self._f is None:
            return
        if self._t_derniere is not None \
                and t_monotonic - self._t_derniere < self.periode:
            return
        self._t_derniere = t_monotonic

        defauts = "; ".join(
            str(etat[c]) if isinstance(etat.get(c), str) else c
            for c in _DEFAUTS if etat.get(c))
        valeurs = dict(etat)
        # diaphragme dont le capteur ΔP est exploité (butée lue)
        valeurs["diaphragme"] = etat.get("diaphragme_mesure",
                                         etat.get("diaphragme"))
        if etat.get("diaphragme_lu") == "en_transit":
            # vanne 3 voies en course : le débit passe par les deux voies,
            # aucun capteur ne le mesure — pas de valeur plutôt qu'une fausse
            # (l'IHM affiche « en cours »)
            for cle in _MESURES_DEBIT:
                valeurs[cle] = None
            valeurs["diaphragme"] = "en cours"
        valeurs["horodatage"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        valeurs["t_s"] = round(t_monotonic - (self._t0 or t_monotonic), 1)
        valeurs["defauts"] = defauts
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
