# -*- coding: utf-8 -*-
"""Profil de séquence : suivi automatique d'une liste de paliers
(durée, Re cible).

Format CSV attendu (onglet Séquence de l'IHM) :
  - 2 colonnes : durée du palier [s] ; Reynolds cible
  - séparateur « ; », tabulation ou « , » ; virgule décimale acceptée
  - ligne d'en-tête optionnelle, lignes vides et commentaires (#) ignorés
  - durées strictement positives

Les paliers s'enchaînent dans l'ordre du fichier, chacun pendant sa durée,
le dernier compris ; à la fin du dernier palier, le banc s'arrête (arrêt
complet, comme le bouton ARRÊT VARIATEUR).

Jusqu'au 2026-10-29, la 1re colonne était l'INSTANT de début de chaque
consigne et la dernière ligne ne servait qu'à marquer la fin (son palier
était de durée nulle, puis maintenu sans fin). Un ancien profil commence
en général par une ligne « 0 ; Re », refusée ici avec un message clair.
"""

from __future__ import annotations
from pathlib import Path


FORMAT = "durée du palier [s] ; Re cible"
ANCIEN_FORMAT = ("la 1re colonne est la DURÉE de chaque palier (et non plus "
                 "l'instant de début, format modifié le 2026-10-29) : "
                 "convertir le profil en durées")


def charger_sequence_csv(chemin) -> list[tuple[float, float]]:
    """Lit et valide un profil CSV. Retourne [(durée_s, re_cible), ...].
    Lève ValueError avec un message en clair si le fichier est invalide."""
    lignes = Path(chemin).read_text(encoding="utf-8-sig").splitlines()
    points: list[tuple[float, float]] = []
    premiere = True                          # seule ligne pouvant être un en-tête
    for num, brut in enumerate(lignes, start=1):
        ligne = brut.strip()
        if not ligne or ligne.startswith("#"):
            continue
        for sep in (";", "\t", ","):
            if sep in ligne:
                champs = [c.strip() for c in ligne.split(sep) if c.strip()]
                break
        else:
            sep = None
            champs = [ligne]
        en_tete_possible, premiere = premiere, False
        if sep == "," and len(champs) > 2:
            # « 10,5,3000 » : 10,5 s puis 3000, ou 10 s puis 5 ? Ambigu avec
            # la virgule décimale — lu auparavant, en silence, t=10 et Re=5
            raise ValueError(
                f"ligne {num} : séparateur « , » ambigu avec la virgule "
                "décimale — séparer les colonnes par « ; » ou une tabulation")
        if len(champs) < 2:
            raise ValueError(
                f"ligne {num} : 2 colonnes attendues ({FORMAT})")
        try:
            duree = float(champs[0].replace(",", "."))
            re = float(champs[1].replace(",", "."))
        except ValueError:
            if en_tete_possible:
                entete = champs[0].lower()
                if ("temps" in entete or "instant" in entete
                        or entete.startswith("t_")) and "dur" not in entete:
                    raise ValueError(
                        f"ligne {num} : en-tête « {champs[0]} » d'un ancien "
                        f"profil — {ANCIEN_FORMAT}") from None
                continue                     # ligne d'en-tête
            raise ValueError(
                f"ligne {num} : valeurs non numériques (« {ligne} »)") \
                from None
        if duree < 0 or re < 0:
            raise ValueError(f"ligne {num} : valeurs négatives interdites")
        if duree == 0:
            raise ValueError(f"ligne {num} : durée nulle — {ANCIEN_FORMAT}")
        points.append((duree, re))
    if not points:
        raise ValueError("aucun point exploitable dans le fichier")
    return points


def debuts_paliers(paliers) -> tuple[list[float], float]:
    """Instants de début de chaque palier [s] et durée totale du profil."""
    debuts, t = [], 0.0
    for duree, _ in paliers:
        debuts.append(t)
        t += float(duree)
    return debuts, t
