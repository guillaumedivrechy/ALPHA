# -*- coding: utf-8 -*-
"""
Calcul des diaphragmes — reproduit fidèlement les classeurs Excel :
  - "Calcul_diaphragme_ISO_V12"    : norme NFX 10-102 / ISO 5167-1, prises d'angle
  - "Calcul_diaphragme_Miller_V1-13" : corrélation Miller (petit orifice, faible Re)

Deux sens de calcul pour chaque diaphragme :
  - direct  : ΔP mesuré  -> débit massique qm  (lecture capteurs)
  - inverse : qm visé    -> ΔP attendu          (pré-positionnement / consigne)
"""

from __future__ import annotations
import math
from dataclasses import dataclass


# ----------------------------------------------------------------------------
# Géométrie commune
# ----------------------------------------------------------------------------

@dataclass
class GeometrieDiaphragme:
    """Géométrie d'un diaphragme et de sa conduite (diamètres à 20 °C, en m)."""
    D20: float                 # diamètre interne conduite à 20 °C [m]
    d20: float                 # diamètre orifice à 20 °C [m]
    alpha_conduite: float      # coeff. dilatation thermique conduite [°C^-1]
    alpha_diaphragme: float    # coeff. dilatation thermique diaphragme [°C^-1]

    def diametres_a_T(self, T_celsius: float) -> tuple[float, float]:
        """Diamètres corrigés de la dilatation thermique (réf. 20 °C)."""
        D = self.D20 * (1.0 + self.alpha_conduite * (T_celsius - 20.0))
        d = self.d20 * (1.0 + self.alpha_diaphragme * (T_celsius - 20.0))
        return D, d


# ----------------------------------------------------------------------------
# Diaphragme ISO 5167-1 (fichier ISO V12)
# ----------------------------------------------------------------------------

def _C_stolz(beta: float, ReD: float, L1: float = 0.0, L2p: float = 0.0,
             Co: float = 0.0) -> float:
    """Coefficient de décharge, formule de Stolz (cellules D28/F28 du classeur)."""
    return (0.5959 + 0.0312 * beta**2.1 - 0.184 * beta**8
            + 0.0029 * beta**2.5 * (1e6 / ReD) ** 0.75
            + Co * beta**4 / (1.0 - beta**4)
            - 0.0337 * L2p * beta**3)


def _C_reader_harris_gallagher(beta: float, ReD: float,
                               L1: float = 0.0, L2p: float = 0.0) -> float:
    """Coefficient de décharge Reader-Harris-Gallagher (cellule F29 du classeur).

    Prises d'angle : L1 = L2' = 0.
    """
    A = (19000.0 * beta / ReD) ** 0.8
    M2 = 2.0 * L2p / (1.0 - beta)
    return (0.5961 + 0.0261 * beta**2 - 0.216 * beta**8
            + 0.000521 * (1e6 * beta / ReD) ** 0.7
            + (0.0188 + 0.0063 * A) * beta**3.5 * (1e6 / ReD) ** 0.3
            + (0.043 + 0.080 * math.exp(-10.0 * L1)
               - 0.123 * math.exp(-7.0 * L1)) * (1.0 - 0.11 * A)
            * beta**4 / (1.0 - beta**4)
            - 0.031 * (M2 - 0.8 * M2**1.1) * beta**1.3)


def _epsilon_iso(beta: float, dP: float, P1_abs: float, gamma: float) -> float:
    """Coefficient de détente ε (cellule D23 du classeur)."""
    return 1.0 - (0.41 + 0.35 * beta**4) * dP / (gamma * P1_abs)


def iso_qm_depuis_dP(geo: GeometrieDiaphragme, dP: float, T_celsius: float,
                     P1_abs: float, rho: float, nu: float, gamma: float,
                     formule_C: str = "RHG", L1: float = 0.0, L2p: float = 0.0,
                     Co: float = 0.0, qm_init: float = 0.1,
                     tol: float = 1e-9, max_iter: int = 50) -> dict:
    """Calcul direct ISO : ΔP mesuré [Pa] -> débit massique qm [kg/s].

    Itératif (le coefficient de décharge dépend de Re, qui dépend de qm),
    identique aux colonnes "1ère itér. / 2è itér. / 3è itér." du classeur.
    """
    if dP <= 0:
        return {"qm": 0.0, "ReD": 0.0, "C": float("nan"), "epsilon": 1.0,
                "v": 0.0, "iterations": 0}
    D, d = geo.diametres_a_T(T_celsius)
    beta = d / D
    E = 1.0 / math.sqrt(1.0 - beta**4)
    eps = _epsilon_iso(beta, dP, P1_abs, gamma)
    aire_orifice = math.pi * d**2 / 4.0

    qm = qm_init
    C = float("nan")
    n = 0
    for n in range(1, max_iter + 1):
        v = 4.0 * qm / (rho * math.pi * D**2)          # vitesse conduite amont
        ReD = max(v * D / nu, 100.0)                    # Reynolds conduite
        if formule_C.upper() == "STOLZ":
            C = _C_stolz(beta, ReD, L1, L2p, Co)
        else:
            C = _C_reader_harris_gallagher(beta, ReD, L1, L2p)
        qm_new = C * E * eps * aire_orifice * math.sqrt(2.0 * dP * rho)
        if abs(qm_new - qm) < tol:
            qm = qm_new
            break
        qm = qm_new

    v = 4.0 * qm / (rho * math.pi * D**2)
    ReD = v * D / nu
    return {"qm": qm, "ReD": ReD, "C": C, "epsilon": eps, "v": v,
            "beta": beta, "iterations": n}


def iso_dP_depuis_qm(geo: GeometrieDiaphragme, qm: float, T_celsius: float,
                     P1_abs: float, rho: float, nu: float, gamma: float,
                     formule_C: str = "RHG", L1: float = 0.0, L2p: float = 0.0,
                     Co: float = 0.0, tol: float = 1e-9,
                     max_iter: int = 50) -> dict:
    """Calcul inverse ISO : qm visé [kg/s] -> ΔP attendu [Pa].

    Re est connu (fixé par qm), seule ε dépend de ΔP -> itération rapide,
    identique à la feuille "Calcul de dP" du classeur.
    """
    if qm <= 0:
        return {"dP": 0.0, "ReD": 0.0, "C": float("nan"), "epsilon": 1.0}
    D, d = geo.diametres_a_T(T_celsius)
    beta = d / D
    E = 1.0 / math.sqrt(1.0 - beta**4)
    v = 4.0 * qm / (rho * math.pi * D**2)
    ReD = v * D / nu
    if formule_C.upper() == "STOLZ":
        C = _C_stolz(beta, ReD, L1, L2p, Co)
    else:
        C = _C_reader_harris_gallagher(beta, ReD, L1, L2p)
    aire_orifice = math.pi * d**2 / 4.0

    eps = 1.0
    dP = 0.0
    for _ in range(max_iter):
        dP_new = (qm / (C * E * eps * aire_orifice)) ** 2 / (2.0 * rho)
        eps = _epsilon_iso(beta, dP_new, P1_abs, gamma)
        if abs(dP_new - dP) < tol:
            dP = dP_new
            break
        dP = dP_new
    return {"dP": dP, "ReD": ReD, "C": C, "epsilon": eps, "v": v, "beta": beta}


# ----------------------------------------------------------------------------
# Diaphragme Miller (fichier Miller V1-13)
# ----------------------------------------------------------------------------

def _C_miller(beta: float) -> float:
    """Coefficient de décharge Miller (cellule I4 du classeur) — indépendant de Re."""
    return (0.7746 - 0.1334 * beta**2.1 + 1.4098 * beta**8
            + 0.0675 * beta**4 / (1.0 - beta**4) + 0.3865 * beta**3)


def _Y1_miller(beta: float, dP: float, P1_abs: float, gamma: float) -> float:
    """Facteur d'expansion du gaz amont Y1 (cellule I5 du classeur)."""
    return 1.0 - (0.41 + 0.35 * beta**4) / gamma * (dP / P1_abs)


def miller_qm_depuis_dP(geo: GeometrieDiaphragme, dP: float, T_celsius: float,
                        P1_abs: float, rho: float, mu: float,
                        gamma: float) -> dict:
    """Calcul direct Miller : ΔP mesuré [Pa] -> débit massique q [kg/s].

    Formule I3 du classeur :
      q = (π·√2/4) · C·dT²·Y1·√(ΔP·ρ) / √(1-β⁴)

    NB : la dilatation thermique du classeur Miller est référencée à 293,15 K.
    """
    if dP <= 0:
        return {"qm": 0.0, "ReD": 0.0, "C": float("nan"), "Y1": 1.0}
    TK = T_celsius + 273.15
    D = geo.D20 * (1.0 + geo.alpha_conduite * (TK - 293.15))
    d = geo.d20 * (1.0 + geo.alpha_diaphragme * (TK - 293.15))
    beta = d / D
    C = _C_miller(beta)
    Y1 = _Y1_miller(beta, dP, P1_abs, gamma)
    qm = (math.pi * math.sqrt(2.0) / 4.0) * C * d**2 * Y1 \
        * math.sqrt(dP) * math.sqrt(rho) / math.sqrt(1.0 - beta**4)
    ReD = 4.0 * qm / (math.pi * D * mu)
    return {"qm": qm, "ReD": ReD, "C": C, "Y1": Y1, "beta": beta}


def miller_dP_depuis_qm(geo: GeometrieDiaphragme, qm: float, T_celsius: float,
                        P1_abs: float, rho: float, mu: float, gamma: float,
                        tol: float = 1e-9, max_iter: int = 50) -> dict:
    """Calcul inverse Miller : q visé [kg/s] -> ΔP attendu [Pa]."""
    if qm <= 0:
        return {"dP": 0.0, "ReD": 0.0, "C": float("nan"), "Y1": 1.0}
    TK = T_celsius + 273.15
    D = geo.D20 * (1.0 + geo.alpha_conduite * (TK - 293.15))
    d = geo.d20 * (1.0 + geo.alpha_diaphragme * (TK - 293.15))
    beta = d / D
    C = _C_miller(beta)
    K = (math.pi * math.sqrt(2.0) / 4.0) * C * d**2 / math.sqrt(1.0 - beta**4)

    Y1 = 1.0
    dP = 0.0
    for _ in range(max_iter):
        dP_new = (qm / (K * Y1)) ** 2 / rho
        Y1 = _Y1_miller(beta, dP_new, P1_abs, gamma)
        if abs(dP_new - dP) < tol:
            dP = dP_new
            break
        dP = dP_new
    ReD = 4.0 * qm / (math.pi * D * mu)
    return {"dP": dP, "ReD": ReD, "C": C, "Y1": Y1, "beta": beta}
