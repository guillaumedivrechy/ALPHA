# -*- coding: utf-8 -*-
"""Propriétés de l'air calculées en continu à partir des capteurs P et T.

Remplace les valeurs tabulées des classeurs Excel : le logiciel actualise
rho et mu à chaque cycle avec la pression absolue et la PT100, ce qui est
plus précis qu'une lecture de table figée.
"""


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
    (pour retrouver exactement les résultats des classeurs Excel).

    Supporte également des paramètres de Sutherland configurables pour permettre
    le rechargement à chaud depuis l'IHM.

    Si humidite_relative > 0, utilise les formules air humide au lieu de air sec."""

    def __init__(self, rho_force: float | None = None,
                 mu_force: float | None = None,
                 gamma: float = GAMMA_AIR,
                 sutherland_mu_ref: float = _MU_REF,
                 sutherland_t_ref: float = _T_REF,
                 sutherland_s: float = _S,
                 humidite_relative: float = 0.0):
        self.rho_force = rho_force
        self.mu_force = mu_force
        self.gamma = gamma
        self.sutherland_mu_ref = sutherland_mu_ref
        self.sutherland_t_ref = sutherland_t_ref
        self.sutherland_s = sutherland_s
        self.humidite_relative = humidite_relative  # en % (0-100)

    def rho(self, P_abs: float, T_celsius: float) -> float:
        if self.rho_force is not None:
            return self.rho_force

        # Si humidité > 0, utiliser les formules air humide
        if self.humidite_relative > 0:
            from .air_humide import calculer_proprietes_air_humide
            try:
                props = calculer_proprietes_air_humide(P_abs, T_celsius,
                                                       self.humidite_relative / 100.0)
                return props["rho"]
            except ValueError:
                # Si erreur, retomber sur air sec
                pass

        # Air sec
        return rho_air(P_abs, T_celsius)

    def mu(self, T_celsius: float) -> float:
        if self.mu_force is not None:
            return self.mu_force

        # Si humidité > 0, utiliser les formules air humide
        if self.humidite_relative > 0:
            from .air_humide import calculer_proprietes_air_humide
            try:
                # Pour mu, on a besoin d'une pression (utiliser pression atmosphérique standard)
                # Note: mu dans air_humide ne dépend que de T, comme Sutherland
                props = calculer_proprietes_air_humide(101325.0, T_celsius,
                                                       self.humidite_relative / 100.0)
                return props["mu"]
            except ValueError:
                # Si erreur, retomber sur air sec
                pass

        # Air sec : Loi de Sutherland avec paramètres d'instance
        T = T_celsius + 273.15
        return (self.sutherland_mu_ref * (T / self.sutherland_t_ref) ** 1.5 *
                (self.sutherland_t_ref + self.sutherland_s) / (T + self.sutherland_s))

    def nu(self, P_abs: float, T_celsius: float) -> float:
        return self.mu(T_celsius) / self.rho(P_abs, T_celsius)
