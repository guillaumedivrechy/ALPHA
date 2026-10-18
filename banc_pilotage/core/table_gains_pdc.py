# -*- coding: utf-8 -*-
"""Table de gains PDC : relation Re = f(V_pdc) en mode bas-Re.

Ce module fournit un feed-forward pour la vanne perte de charge (PDC) en mode
bas-Re, similaire à la table gains variateur utilisée en modes normaux.

La table est construite par apprentissage (séquence dédiée) et stockée dans
etat_banc.json sous la clé "table_gains_pdc".

Utilisation :
    table = TableGainsPDC(points)  # points = [(V_pdc, Re), ...]
    v_pdc = table.v_pour_re(re_cible)  # Feed-forward
"""

from __future__ import annotations
import logging
from typing import Tuple

log = logging.getLogger(__name__)


class TableGainsPDC:
    """Table de correspondance tension PDC → Reynolds en mode bas-Re.

    La vanne PDC module le débit en étranglant, tandis que le variateur reste
    fixé à u_min. La table fournit un feed-forward (pré-positionnement) pour
    accélérer la convergence du PI.

    Interpolation linéaire entre les points, extrapolation constante aux bornes.
    """

    def __init__(self, points: list[Tuple[float, float]] | None = None):
        """Construit une table depuis une liste de points (V_pdc, Re).

        Args:
            points: Liste de tuples (V_pdc [V], Re calculé [sans dimension]).
                    Ex: [(0.5, 100), (2.0, 500), (5.0, 1500), (10.0, 2000)]
                    Si None ou vide, la table est vide (pas de feed-forward).
        """
        self.points = sorted(points or [], key=lambda p: p[0])  # Tri par V_pdc

    def v_pour_re(self, re_cible: float) -> float:
        """Retourne la tension PDC (V) correspondant au Re cible.

        Interpolation linéaire entre les points de la table. Si Re cible hors
        bornes, extrapole en gardant la tension limite (0 ou 10V).

        Args:
            re_cible: Reynolds visé (sans dimension)

        Returns:
            Tension PDC en volts (0-10 V). Si table vide, retourne 5.0V (milieu).
        """
        if not self.points:
            # Table vide : retourner tension médiane (PI partira de là)
            return 5.0

        # Chercher les deux points encadrant re_cible
        for i in range(len(self.points) - 1):
            v1, re1 = self.points[i]
            v2, re2 = self.points[i + 1]

            # Interpolation linéaire
            if re1 <= re_cible <= re2:
                if re2 == re1:  # Éviter division par zéro
                    return v1
                return v1 + (re_cible - re1) / (re2 - re1) * (v2 - v1)

        # Hors bornes : extrapolation constante
        if re_cible < self.points[0][1]:  # En dessous de la table
            return self.points[0][0]  # Tension minimale
        else:  # Au-dessus de la table
            return self.points[-1][0]  # Tension maximale

    def re_pour_v(self, v_pdc: float) -> float:
        """Retourne le Re correspondant à une tension PDC donnée.

        Fonction inverse de v_pour_re(), utile pour vérifier la table.

        Args:
            v_pdc: Tension PDC en volts (0-10 V)

        Returns:
            Reynolds calculé (sans dimension). Si table vide, retourne 0.
        """
        if not self.points:
            return 0.0

        # Chercher les deux points encadrant v_pdc
        for i in range(len(self.points) - 1):
            v1, re1 = self.points[i]
            v2, re2 = self.points[i + 1]

            # Interpolation linéaire
            if v1 <= v_pdc <= v2:
                if v2 == v1:
                    return re1
                return re1 + (v_pdc - v1) / (v2 - v1) * (re2 - re1)

        # Hors bornes
        if v_pdc < self.points[0][0]:
            return self.points[0][1]
        else:
            return self.points[-1][1]

    def vide(self) -> bool:
        """Retourne True si la table est vide (pas de feed-forward disponible)."""
        return len(self.points) == 0

    def __repr__(self):
        if not self.points:
            return "TableGainsPDC(vide)"
        v_min, re_min = self.points[0]
        v_max, re_max = self.points[-1]
        return (f"TableGainsPDC({len(self.points)} points, "
                f"V={v_min:.1f}-{v_max:.1f}V, Re={re_min:.0f}-{re_max:.0f})")
