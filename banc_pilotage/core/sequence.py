# -*- coding: utf-8 -*-
"""Profil de séquence : suivi automatique d'une liste (temps, Re cible).

Format CSV attendu (onglet Séquence de l'IHM) :
  - 2 colonnes : temps [s] ; Reynolds cible
  - séparateur « ; », tabulation ou « , » ; virgule décimale acceptée
  - ligne d'en-tête optionnelle, lignes vides et commentaires (#) ignorés
  - temps strictement croissants

La consigne évolue par échelons : la valeur d'une ligne s'applique de son
échéance jusqu'à la suivante ; le temps de la dernière ligne marque la fin
de la séquence, sa consigne est ensuite maintenue (mettre Re = 0 en
dernière ligne pour arrêter la soufflerie en fin d'essai).
"""

from __future__ import annotations
from pathlib import Path


def charger_sequence_csv(chemin) -> list[tuple[float, float]]:
    """Lit et valide un profil CSV. Retourne [(t_s, re_cible), ...].
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
                f"ligne {num} : 2 colonnes attendues (temps [s] ; Re cible)")
        try:
            t = float(champs[0].replace(",", "."))
            re = float(champs[1].replace(",", "."))
        except ValueError:
            if en_tete_possible:
                continue                     # ligne d'en-tête
            raise ValueError(
                f"ligne {num} : valeurs non numériques (« {ligne} »)") \
                from None
        if t < 0 or re < 0:
            raise ValueError(f"ligne {num} : valeurs négatives interdites")
        if points and t <= points[-1][0]:
            raise ValueError(
                f"ligne {num} : les temps doivent être strictement croissants")
        points.append((t, re))
    if not points:
        raise ValueError("aucun point exploitable dans le fichier")
    return points
