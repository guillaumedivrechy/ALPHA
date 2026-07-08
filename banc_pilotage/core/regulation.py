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
        # facteur d'échelle des gains (1 = nominal). Deux usages par le
        # pilote : réduit en régime bas-Re (la grandeur réglante est la vanne
        # PdC, actionneur lent), et gain scheduling en régime normal (échelle
        # = tension d'équilibre estimée, pour un gain de boucle homogène).
        # Changer d'échelle en cours de route : changer_gain_scale (sans à-coup).
        self.gain_scale = 1.0
        self._integrale = 0.0
        self._u_prec = 0.0

    def reinitialiser(self, u_initial: float = 0.0) -> None:
        """Réinitialise le régulateur (ex. après commutation de diaphragme).
        u_initial permet le pré-positionnement (feed-forward)."""
        ki_eff = self.ki * self.gain_scale
        self._integrale = u_initial / ki_eff if ki_eff != 0 else 0.0
        self._u_prec = u_initial

    def changer_gain_scale(self, scale: float) -> None:
        """Change l'échelle des gains sans à-coup sur la sortie : l'intégrale
        est recalée pour que sa contribution (ki·scale·I) soit conservée."""
        if scale == self.gain_scale:
            return
        if scale != 0 and self.gain_scale != 0:
            self._integrale *= self.gain_scale / scale
        self.gain_scale = scale

    def calculer(self, consigne: float, mesure: float) -> float:
        """Un pas de régulation. Erreur normalisée par la consigne pour que
        les gains restent valables de Re=1000 à Re=100000."""
        if consigne <= 0:
            return self.u_min
        erreur = (consigne - mesure) / consigne

        if abs(erreur) < self.bande_morte:
            erreur = 0.0

        kp = self.kp * self.gain_scale
        ki = self.ki * self.gain_scale
        integrale_candidate = self._integrale + erreur * self.dt
        u_brut = kp * erreur + ki * integrale_candidate

        # saturation 0..10 V
        u = min(max(u_brut, self.u_min), self.u_max)

        # limitation de rampe
        delta_max = self.rampe * self.dt
        if u > self._u_prec + delta_max:
            u = self._u_prec + delta_max
        elif u < self._u_prec - delta_max:
            u = self._u_prec - delta_max

        # anti-windup par intégration conditionnelle : sortie clippée
        # (saturation OU rampe), on n'intègre pas dans le sens qui aggrave le
        # clip. Contrairement au recalcul (I = (u - kp·e)/ki), l'intégrale —
        # qui porte le pré-positionnement (feed-forward) — n'est pas détruite
        # par le premier transitoire clippé : la sortie ne retombe pas sous
        # la tension d'équilibre pendant que l'erreur se résorbe.
        clip_haut = u < u_brut - 1e-9      # sortie retenue plus basse que demandée
        clip_bas = u > u_brut + 1e-9
        if not ((clip_haut and erreur > 0) or (clip_bas and erreur < 0)):
            self._integrale = integrale_candidate

        self._u_prec = u
        return u
