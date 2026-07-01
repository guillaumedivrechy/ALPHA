# -*- coding: utf-8 -*-
"""Non-régression du moteur de calcul contre les classeurs Excel de
référence (tableau « Validation » du README) :

  - Calcul_diaphragme_ISO_V12     (NFX 10-102 / ISO 5167-1, RHG)
  - Calcul_diaphragme_Miller_V1-13

ρ et ν/μ sont forcés aux valeurs lues dans les classeurs (le logiciel les
recalcule normalement en continu depuis P et T).
"""

import math
import pytest

from core.diaphragmes import (GeometrieDiaphragme,
                              iso_qm_depuis_dP, iso_dP_depuis_qm,
                              miller_qm_depuis_dP, miller_dP_depuis_qm)

GAMMA = 1.40273
GEO_ISO = GeometrieDiaphragme(D20=0.0814, d20=0.04101843,
                              alpha_conduite=2.4e-5, alpha_diaphragme=2.4e-5)
GEO_MILLER = GeometrieDiaphragme(D20=0.0814, d20=0.02111306,
                                 alpha_conduite=8e-5, alpha_diaphragme=1.7e-5)


# ---------------------------------------------------------------- ISO 5167

class TestISOContreExcel:
    """ΔP = 145 Pa, T = 20 °C, P1 = 101325 Pa, ρ = 1,205 kg/m³,
    ν = 1,5e-5 m²/s (valeurs du classeur ISO V12)."""

    @pytest.fixture(scope="class")
    def r(self):
        return iso_qm_depuis_dP(GEO_ISO, dP=145.0, T_celsius=20.0,
                                P1_abs=101325.0, rho=1.205, nu=1.5e-5,
                                gamma=GAMMA, formule_C="RHG")

    def test_qm(self, r):
        assert r["qm"] == pytest.approx(0.015729, rel=1e-4)

    def test_epsilon(self, r):
        assert r["epsilon"] == pytest.approx(0.999559, abs=1e-6)

    def test_C_rhg(self, r):
        assert r["C"] == pytest.approx(0.616122, abs=1e-5)

    def test_convergence(self, r):
        assert r["iterations"] < 50


def test_iso_aller_retour():
    """qm -> ΔP (inverse) -> qm (direct) doit reboucler."""
    qm = 0.0157
    inv = iso_dP_depuis_qm(GEO_ISO, qm, 20.0, 101325.0, 1.205, 1.5e-5, GAMMA)
    dir_ = iso_qm_depuis_dP(GEO_ISO, inv["dP"], 20.0, 101325.0,
                            1.205, 1.5e-5, GAMMA)
    assert dir_["qm"] == pytest.approx(qm, rel=1e-6)


def test_iso_dp_nul():
    r = iso_qm_depuis_dP(GEO_ISO, 0.0, 20.0, 101325.0, 1.205, 1.5e-5, GAMMA)
    assert r["qm"] == 0.0 and r["ReD"] == 0.0


def test_iso_dilatation_thermique():
    D, d = GEO_ISO.diametres_a_T(120.0)
    assert D == pytest.approx(0.0814 * (1 + 2.4e-5 * 100.0))
    assert d == pytest.approx(0.04101843 * (1 + 2.4e-5 * 100.0))


# ---------------------------------------------------------------- Miller

class TestMillerContreExcel:
    """ΔP = 18,3 Pa, T = 20 °C, P1 = 101325 Pa. C et Y1 ne dépendent que de
    la géométrie et de ΔP/P1 : comparaison directe au classeur. qm et Re
    sont épinglés avec les ρ/μ retrouvés depuis l'exemple du classeur."""

    RHO_CLASSEUR = 1.442660
    MU_CLASSEUR = 1.845036e-5

    @pytest.fixture(scope="class")
    def r(self):
        return miller_qm_depuis_dP(GEO_MILLER, dP=18.3, T_celsius=20.0,
                                   P1_abs=101325.0, rho=self.RHO_CLASSEUR,
                                   mu=self.MU_CLASSEUR, gamma=GAMMA)

    def test_C(self, r):
        assert r["C"] == pytest.approx(0.773839, abs=2e-6)

    def test_Y1(self, r):
        assert r["Y1"] == pytest.approx(0.999947, abs=1e-6)

    def test_qm(self, r):
        assert r["qm"] == pytest.approx(0.001973, rel=1e-4)

    def test_Re(self, r):
        assert r["ReD"] == pytest.approx(1672.661, rel=1e-4)


def test_miller_aller_retour():
    qm = 0.002
    inv = miller_dP_depuis_qm(GEO_MILLER, qm, 20.0, 101325.0,
                              1.205, 1.81e-5, GAMMA)
    dir_ = miller_qm_depuis_dP(GEO_MILLER, inv["dP"], 20.0, 101325.0,
                               1.205, 1.81e-5, GAMMA)
    assert dir_["qm"] == pytest.approx(qm, rel=1e-6)


def test_miller_dp_nul():
    r = miller_qm_depuis_dP(GEO_MILLER, 0.0, 20.0, 101325.0,
                            1.205, 1.81e-5, GAMMA)
    assert r["qm"] == 0.0


def test_beta_coherent():
    """β = d/D doit rester dans les plages de validité des corrélations."""
    D, d = GEO_ISO.diametres_a_T(20.0)
    assert 0.10 <= d / D <= 0.75          # plage ISO 5167
    D, d = GEO_MILLER.diametres_a_T(20.0)
    assert 0.0 < d / D < 1.0
