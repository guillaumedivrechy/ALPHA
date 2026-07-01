# -*- coding: utf-8 -*-
"""Propriétés de l'air calculées en continu à partir des capteurs P et T.

Remplace les valeurs tabulées des classeurs Excel : le logiciel actualise
rho et mu à chaque cycle avec la pression absolue et la PT100, ce qui est
plus précis qu'une lecture de table figée.
"""

import math

R_AIR = 287.058          # constante spécifique de l'air [J/(kg.K)]
GAMMA_AIR = 1.40273      # coefficient de détente isentropique (valeur des classeurs)

# Loi de Sutherland pour la viscosité dynamique de l'air
_MU_REF = 1.716e-5       # [Pa.s] à T_REF
_T_REF = 273.15          # [K]
_S = 110.4               # constante de Sutherland [K]


def rho_air(P_abs: float, T_celsius: float) -> float:
    """Masse volumique de l'air [kg/m3] — gaz parfait."""
    return P_abs / (R_AIR * (T_celsius + 273.15))


def mu_air(T_celsius: float) -> float:
    """Viscosité dynamique de l'air [Pa.s] — loi de Sutherland."""
    T = T_celsius + 273.15
    return _MU_REF * (T / _T_REF) ** 1.5 * (_T_REF + _S) / (T + _S)


def nu_air(P_abs: float, T_celsius: float) -> float:
    """Viscosité cinématique de l'air [m2/s]."""
    return mu_air(T_celsius) / rho_air(P_abs, T_celsius)


class ProprietesFluide:
    """Propriétés du fluide, avec possibilité de forcer des valeurs fixes
    (pour retrouver exactement les résultats des classeurs Excel)."""

    def __init__(self, rho_force: float | None = None,
                 mu_force: float | None = None,
                 gamma: float = GAMMA_AIR):
        self.rho_force = rho_force
        self.mu_force = mu_force
        self.gamma = gamma

    def rho(self, P_abs: float, T_celsius: float) -> float:
        return self.rho_force if self.rho_force else rho_air(P_abs, T_celsius)

    def mu(self, T_celsius: float) -> float:
        return self.mu_force if self.mu_force else mu_air(T_celsius)

    def nu(self, P_abs: float, T_celsius: float) -> float:
        return self.mu(T_celsius) / self.rho(P_abs, T_celsius)
