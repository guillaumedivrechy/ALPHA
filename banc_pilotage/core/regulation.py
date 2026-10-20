# -*- coding: utf-8 -*-
"""Régulateurs pour l'asservissement du Reynolds.

Grandeur réglée   : Re calculé (depuis ΔP, P_amont, T)
Grandeur réglante : tension 0-10 V du variateur (fréquence ventilateur),
                    ou position 0-10 V de la vanne perte de charge (très bas Re)

Deux régulateurs :
  - RegulateurTrim : feed-forward (régression apprise Re = a·U + b) + PI de
    correction en volts. C'est le régulateur des modes normaux (variateur).
  - RegulateurPI : PI classique à erreur relative, avec pré-positionnement.
    Sert à la vanne perte de charge (mode très bas Re) et de repli quand le
    banc n'est pas caractérisé (pas de table de gains).

Commun aux deux :
  - anti-windup par intégration conditionnelle (on n'intègre pas dans le
    sens qui aggrave une saturation ou un clip de rampe)
  - limitation de la vitesse de variation de la sortie (rampe)
  - gel de l'intégrale commandé par l'appelant : tant que l'actionneur n'a
    pas rejoint sa commande (recopie variateur / vanne), l'erreur ne dit
    rien du réglage et l'intégrer ne ferait que préparer un dépassement
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
        u_initial permet le pré-positionnement (feed-forward).

        OPTIMISATION: _u_prec = u_initial (au lieu de 0.0) pour que le feed-forward
        soit appliqué DIRECTEMENT au premier cycle, sans attendre la rampe.
        Gain de temps: ~2-3s au démarrage."""
        ki_eff = self.ki * self.gain_scale
        self._integrale = u_initial / ki_eff if ki_eff != 0 else 0.0
        self._u_prec = u_initial  # Applique feed-forward immédiatement (était 0.0)

    def changer_gain_scale(self, scale: float) -> None:
        """Change l'échelle des gains sans à-coup sur la sortie : l'intégrale
        est recalée pour que sa contribution (ki·scale·I) soit conservée."""
        if scale == self.gain_scale:
            return
        if scale != 0 and self.gain_scale != 0:
            self._integrale *= self.gain_scale / scale
        self.gain_scale = scale

    def calculer(self, consigne: float, mesure: float,
                 geler_integrale: bool = False) -> float:
        """Un pas de régulation. Erreur normalisée par la consigne pour que
        les gains restent valables de Re=1000 à Re=100000.

        geler_integrale : l'actionneur n'a pas encore rejoint sa commande
        (vanne lente) -> seul le terme P agit, l'intégrale est tenue."""
        if consigne <= 0:
            return self.u_min
        erreur = (consigne - mesure) / consigne

        if abs(erreur) < self.bande_morte:
            erreur = 0.0

        kp = self.kp * self.gain_scale
        ki = self.ki * self.gain_scale
        integrale_candidate = self._integrale if geler_integrale \
            else self._integrale + erreur * self.dt
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


class RegulateurTrim:
    """Feed-forward + PI de correction en volts (modes normaux, variateur).

        u = u_ff(consigne) + kp·e_v + ki·∫e_v·dt      e_v = (consigne − mesure) / pente

    Le feed-forward (régression Re = a·U + b apprise sur le banc) fait
    l'essentiel du chemin ; le PI ne corrige que le résidu : biais de table,
    dérive de rho et mu avec la température, bruit. L'erreur est ramenée en
    volts par la pente a de la régression : le gain de boucle vaut kp quel
    que soit le point de fonctionnement, et les kp/ki IMC proposés par
    l'essai indiciel (plante normalisée) s'appliquent tels quels.

    - bande morte relative sur le terme P seulement : le P ne poursuit pas
      le bruit capteur ; l'intégrale, qui le moyenne, résorbe le biais en
      entier (précision = celle de la mesure moyennée, pas de la bande)
    - correction bornée à ±trim_max (intégrale comprise) : le feed-forward
      reste le maître ; une table fausse de plus de trim_max se voit sur
      l'IHM au lieu d'être silencieusement rattrapée
    - gels commandés par l'appelant : tant que la consigne filtrée bouge ou
      que le variateur n'a pas rejoint sa commande (recopie AM), l'erreur ne
      renseigne pas sur le réglage -> intégrale tenue, et P TENU à sa
      dernière valeur tant que l'actionneur est en route (il ne pousse pas
      une commande que le variateur n'a pas encore atteinte). Tenu et non
      remis à zéro : un P annulé fait chuter la commande, le variateur la
      rejoint, le P revient, la commande remonte, le variateur décroche à
      nouveau -> cycle limite en paliers (constaté sur le 3G3MV, rampe
      d'accélération interne de 10 s)
    - anti-windup par intégration conditionnelle sur toute saturation de la
      sortie totale (u_min, u_max, plafond de saturation ΔP, rampe)
    - rampe sur la sortie totale
    """

    def __init__(self, kp: float, ki: float, periode_s: float,
                 u_min: float = 0.0, u_max: float = 10.0,
                 rampe_v_par_s: float = 2.0,
                 bande_morte_relative: float = 0.0,
                 trim_max_v: float = 2.0):
        self.kp = kp
        self.ki = ki
        self.dt = periode_s
        self.u_min = u_min
        self.u_max = u_max
        self.rampe = rampe_v_par_s
        self.bande_morte = bande_morte_relative
        self.trim_max = trim_max_v
        self._integrale = 0.0
        self._u_prec = 0.0
        self._trim = 0.0
        self._p = 0.0                      # dernier terme P appliqué [V]

    @property
    def integrale(self) -> float:
        """Part intégrale de la correction [V] (biais de table appris)."""
        return self._integrale

    @property
    def trim(self) -> float:
        """Dernière correction totale appliquée au feed-forward [V]."""
        return self._trim

    def reinitialiser(self, u_initial: float = 0.0,
                      integrale: float = 0.0) -> None:
        """u_initial : sortie courante (origine de la rampe). integrale :
        correction à conserver (0 = confiance totale au feed-forward)."""
        self._integrale = min(max(integrale, -self.trim_max), self.trim_max)
        self._u_prec = u_initial
        self._trim = self._integrale
        self._p = 0.0

    def calculer(self, consigne: float, mesure: float, u_ff: float,
                 pente: float, u_max: float | None = None,
                 geler_integrale: bool = False,
                 geler_p: bool = False) -> float:
        """Un pas. pente = dRe/dU au point visé (a de la régression) [Re/V].
        u_max : plafond ponctuel (saturation ΔP), en plus de self.u_max."""
        u_max_eff = self.u_max if u_max is None else min(self.u_max, u_max)
        if consigne <= 0 or pente <= 0:
            u = min(max(u_ff, self.u_min), u_max_eff)
            self._u_prec = u
            self._trim = 0.0
            self._p = 0.0
            return u

        erreur_rel = (consigne - mesure) / consigne
        erreur_v = (consigne - mesure) / pente

        if geler_p:
            p = self._p
        else:
            p = 0.0 if abs(erreur_rel) < self.bande_morte \
                else self.kp * erreur_v
            self._p = p
        i_candidate = self._integrale if geler_integrale \
            else self._integrale + self.ki * erreur_v * self.dt
        i_candidate = min(max(i_candidate, -self.trim_max), self.trim_max)
        trim = min(max(p + i_candidate, -self.trim_max), self.trim_max)
        u_brut = u_ff + trim

        u = min(max(u_brut, self.u_min), u_max_eff)

        delta_max = self.rampe * self.dt
        if u > self._u_prec + delta_max:
            u = self._u_prec + delta_max
        elif u < self._u_prec - delta_max:
            u = self._u_prec - delta_max

        clip_haut = u < u_brut - 1e-9
        clip_bas = u > u_brut + 1e-9
        if not ((clip_haut and erreur_v > 0) or (clip_bas and erreur_v < 0)):
            self._integrale = i_candidate

        self._u_prec = u
        self._trim = trim
        return u
