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
    @classmethod
    def r(cls):
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
    @classmethod
    def r(cls):
        return miller_qm_depuis_dP(GEO_MILLER, dP=18.3, T_celsius=20.0,
                                   P1_abs=101325.0, rho=cls.RHO_CLASSEUR,
                                   mu=cls.MU_CLASSEUR, gamma=GAMMA)

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


# ── cas de référence intégraux, relus dans les classeurs ──────────────────
#
# Les classes ci-dessus épinglent les classeurs avec des entrées arrondies
# (Miller y est évalué à 20 °C alors que son exemple est à 17,7 °C, et ses
# ρ/μ y étaient rétro-déduits). Les deux classes suivantes reprennent les
# feuilles de calcul TELLES QUELLES — toutes les entrées et toutes les
# sorties intermédiaires — ce qui permet une tolérance bien plus serrée et
# localise immédiatement une divergence (β, ε, C, puis qm).

class TestISOCasDeReferenceComplet:
    """Feuille « Calcul de qm » du classeur ISO V12, colonne « n itérations ».

    Entrées : ΔP = 145 Pa · T = 20 °C · Pabs = 101349 Pa · ρ = 1,20507 ·
    ν = 1,49867e-5 · D = 81,4 mm · d = 41,0184 mm · α = 2,394e-5 ·
    prises d'angle (L1 = L2' = 0).
    """

    GEO = GeometrieDiaphragme(D20=0.0814, d20=0.0410184,
                              alpha_conduite=2.394e-5,
                              alpha_diaphragme=2.394e-5)

    @pytest.fixture(scope="class")
    @classmethod
    def r(cls):
        return iso_qm_depuis_dP(cls.GEO, dP=145.0, T_celsius=20.0,
                                P1_abs=101349.0, rho=1.20507, nu=1.49867e-5,
                                gamma=GAMMA, formule_C="RHG")

    def test_beta(self):
        D, d = self.GEO.diametres_a_T(20.0)
        assert d / D == pytest.approx(0.503912, abs=1e-6)     # F17

    def test_epsilon(self, r):
        assert r["epsilon"] == pytest.approx(0.999559, abs=1e-6)   # D23

    def test_C_reader_harris_gallagher(self, r):
        assert r["C"] == pytest.approx(0.616115, abs=1e-6)         # I29

    def test_vitesse_conduite(self, r):
        assert r["v"] == pytest.approx(2.50811, rel=1e-5)          # I26

    def test_reynolds_conduite(self, r):
        assert r["ReD"] == pytest.approx(13622.7, rel=1e-5)        # I27

    def test_qm(self, r):
        assert r["qm"] == pytest.approx(0.0157288, rel=1e-5)       # I31

    def test_stolz_au_reynolds_du_classeur(self):
        """Le classeur itère UN seul débit (avec RHG) mais évalue les deux
        coefficients à ce Reynolds convergé. On compare donc la formule de
        Stolz à ReD = 13622,7, et non le résultat d'une itération Stolz —
        celle-ci converge vers un point légèrement différent (0,6156497)."""
        from core.diaphragmes import _C_stolz
        D, d = self.GEO.diametres_a_T(20.0)
        C = _C_stolz(beta=d / D, ReD=13622.7)
        assert C == pytest.approx(0.615642, abs=2e-6)              # I28
        assert abs(C - 0.616115) > 1e-4        # bien distinct de RHG


class TestMillerCasDeReferenceComplet:
    """Feuilles « Interface utilisateur - débit » et « Calcul » du classeur
    Miller V1-13.

    Entrées : ΔP = 18,3 Pa · T = 17,7 °C · Pabs = 101332 Pa ·
    ρ = 1,44229 · μ = 1,845e-5 · d = 21,11306 mm · D = 81,4 mm ·
    α_diaph = 1,692e-5 · α_conduite = 8e-5.
    """

    GEO = GeometrieDiaphragme(D20=0.0814, d20=0.02111306,
                              alpha_conduite=8e-5, alpha_diaphragme=1.692e-5)

    @pytest.fixture(scope="class")
    @classmethod
    def r(cls):
        return miller_qm_depuis_dP(cls.GEO, dP=18.3, T_celsius=17.7,
                                   P1_abs=101332.0, rho=1.44229,
                                   mu=1.845e-5, gamma=GAMMA)

    def test_diametres_a_T(self):
        D, d = self.GEO.diametres_a_T(17.7)
        assert d == pytest.approx(0.02111224, abs=1e-8)        # D4
        assert D == pytest.approx(0.08138502, abs=1e-8)        # D5

    def test_beta(self):
        D, d = self.GEO.diametres_a_T(17.7)
        assert d / D == pytest.approx(0.2594118, abs=1e-7)     # D11

    def test_C(self, r):
        assert r["C"] == pytest.approx(0.7738392, abs=1e-7)    # I4

    def test_Y1(self, r):
        assert r["Y1"] == pytest.approx(0.999947, abs=1e-6)    # I5

    def test_reynolds(self, r):
        assert r["ReD"] == pytest.approx(1672.661, rel=1e-6)   # I6

    def test_qm(self, r):
        assert r["qm"] == pytest.approx(0.001972598, rel=1e-6)  # I3


def test_pas_de_discontinuite_a_la_bascule():
    """Le banc bascule Miller -> ISO à Re conduite 5000. Pour un même débit
    physique, les deux diaphragmes doivent annoncer le même Reynolds, sinon
    la mesure sauterait au changement de vanne — ce que l'opérateur lirait
    comme une perturbation du banc."""
    import json
    from pathlib import Path
    from core.banc import Banc

    cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json")
                     .read_text(encoding="utf-8"))
    b = Banc(cfg)
    for qm in (0.005, 0.0058, 0.007):          # de part et d'autre du seuil
        dp_iso = b.dP_attendu("iso", qm, 20.0, 25.0)
        dp_mil = b.dP_attendu("miller", qm, 20.0, 25.0)
        re_iso = b.qm_depuis_mesures("iso", dp_iso, 20.0, 25.0)["re_conduite"]
        re_mil = b.qm_depuis_mesures("miller", dp_mil, 20.0, 25.0)["re_conduite"]
        assert re_mil == pytest.approx(re_iso, rel=1e-6)
