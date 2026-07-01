# -*- coding: utf-8 -*-
"""Régulateur PI pour l'asservissement du Reynolds.

Grandeur réglée   : Re mesuré (calculé depuis ΔP, P_amont, T)
Grandeur réglante : tension 0-10 V du variateur (fréquence ventilateur)

Particularités :
  - anti-windup par écrêtage conditionnel de l'intégrale
  - limitation de la vitesse de variation de la sortie (rampe), pour
    ménager le ventilateur et laisser l'écoulement s'établir
  - bande morte optionnelle autour de la consigne
  - pré-positionnement : une estimation initiale de la tension peut être
    injectée (feed-forward) pour converger plus vite
"""

from __future__ import annotations


class RegulateurPI:
    def __init__(self, kp: float, ki: float, periode_s: float,
                 u_min: float = 0.0, u_max: float = 10.0,
                 rampe_v_par_s: float = 0.5,
                 bande_morte_relative: float = 0.0):
        self.kp = kp
        self.ki = ki
        self.dt = periode_s
        self.u_min = u_min
        self.u_max = u_max
        self.rampe = rampe_v_par_s
        self.bande_morte = bande_morte_relative
        self._integrale = 0.0
        self._u_prec = 0.0

    def reinitialiser(self, u_initial: float = 0.0) -> None:
        """Réinitialise le régulateur (ex. après commutation de diaphragme).
        u_initial permet le pré-positionnement (feed-forward)."""
        self._integrale = u_initial / self.ki if self.ki != 0 else 0.0
        self._u_prec = u_initial

    def calculer(self, consigne: float, mesure: float) -> float:
        """Un pas de régulation. Erreur normalisée par la consigne pour que
        les gains restent valables de Re=1000 à Re=100000."""
        if consigne <= 0:
            return self.u_min
        erreur = (consigne - mesure) / consigne

        if abs(erreur) < self.bande_morte:
            erreur = 0.0

        integrale_candidate = self._integrale + erreur * self.dt
        u_brut = self.kp * erreur + self.ki * integrale_candidate

        # saturation 0..10 V
        u = min(max(u_brut, self.u_min), self.u_max)

        # limitation de rampe
        delta_max = self.rampe * self.dt
        if u > self._u_prec + delta_max:
            u = self._u_prec + delta_max
        elif u < self._u_prec - delta_max:
            u = self._u_prec - delta_max

        # anti-windup unifié : si la sortie a été clippée (saturation OU rampe),
        # on recale l'intégrale sur la sortie réelle pour éviter l'accumulation
        # silencieuse pendant les transitoires de grande amplitude.
        if abs(u - u_brut) > 1e-9 and self.ki != 0:
            self._integrale = (u - self.kp * erreur) / self.ki
        else:
            self._integrale = integrale_candidate

        self._u_prec = u
        return u
