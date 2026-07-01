# -*- coding: utf-8 -*-
"""Logique métier : conversions Re <-> qm, sélection de diaphragme."""

import json
from pathlib import Path

import pytest

from core.banc import Banc

CFG = json.loads((Path(__file__).resolve().parent.parent / "config.json")
                 .read_text(encoding="utf-8"))


@pytest.fixture
def banc():
    return Banc(CFG)


def test_re_conduite_aller_retour(banc):
    qm = banc.re_conduite_vers_qm(8000.0, T=20.0, P1_abs=101350.0)
    r = banc.qm_vers_re(qm, T=20.0, P1_abs=101350.0)
    assert r["re_conduite"] == pytest.approx(8000.0, rel=1e-9)


def test_re_veine_aller_retour(banc):
    qm = banc.re_veine_vers_qm(30000.0, T=20.0, P1_abs=101350.0)
    r = banc.qm_vers_re(qm, T=20.0, P1_abs=101350.0)
    assert r["re_veine"] == pytest.approx(30000.0, rel=1e-9)


def test_selection_sans_historique(banc):
    assert banc.choisir_diaphragme(3000.0) == "miller"
    assert banc.choisir_diaphragme(8000.0) == "iso"


def test_hysteresis_commutation(banc):
    # depuis Miller : bascule seulement au-delà de seuil + hystérésis
    assert banc.choisir_diaphragme(5200.0, "miller") == "miller"
    assert banc.choisir_diaphragme(5600.0, "miller") == "iso"
    # depuis ISO : bascule seulement en dessous de seuil - hystérésis
    assert banc.choisir_diaphragme(4800.0, "iso") == "iso"
    assert banc.choisir_diaphragme(4400.0, "iso") == "miller"


def test_mesure_coherente_avec_inverse(banc):
    """dP_attendu (inverse) puis qm_depuis_mesures (direct) doivent
    reboucler sur le débit visé, pour les deux diaphragmes."""
    for diaph, qm in (("iso", 0.015), ("miller", 0.002)):
        dp = banc.dP_attendu(diaph, qm, T=20.0, P1_eff=25.0)
        r = banc.qm_depuis_mesures(diaph, dp, T=20.0, P1_eff=25.0)
        assert r["qm"] == pytest.approx(qm, rel=1e-6)
