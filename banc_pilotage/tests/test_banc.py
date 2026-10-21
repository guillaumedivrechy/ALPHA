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


# bornes dérivées de la config, pas écrites en dur : le seuil de bascule
# sera réglé à la mise en service, ces tests doivent le suivre sans casser.
_SEL = CFG["selection_diaphragme"]
_SEUIL = _SEL["seuil_re"]
_HYST = _SEL.get("hysteresis_re", 500)


def test_selection_sans_historique(banc):
    assert banc.choisir_diaphragme(_SEUIL * 0.6) == "miller"
    assert banc.choisir_diaphragme(_SEUIL * 1.6) == "iso"


def test_hysteresis_commutation(banc):
    # depuis Miller : bascule seulement au-delà de seuil + hystérésis
    assert banc.choisir_diaphragme(_SEUIL + _HYST - 1, "miller") == "miller"
    assert banc.choisir_diaphragme(_SEUIL + _HYST + 1, "miller") == "iso"
    # depuis ISO : bascule seulement en dessous de seuil - hystérésis
    assert banc.choisir_diaphragme(_SEUIL - _HYST + 1, "iso") == "iso"
    assert banc.choisir_diaphragme(_SEUIL - _HYST - 1, "iso") == "miller"


def test_mesure_coherente_avec_inverse(banc):
    """dP_attendu (inverse) puis qm_depuis_mesures (direct) doivent
    reboucler sur le débit visé, pour les deux diaphragmes."""
    for diaph, qm in (("iso", 0.015), ("miller", 0.002)):
        dp = banc.dP_attendu(diaph, qm, T=20.0, P1_eff=25.0)
        r = banc.qm_depuis_mesures(diaph, dp, T=20.0, P1_eff=25.0)
        assert r["qm"] == pytest.approx(qm, rel=1e-6)


def test_tres_bas_debit_mesure_sous_0_3_pa():
    """Constaté au banc : le Re stagnait à ~200 Re veine. 100 Re veine
    donnent 0,24 Pa sur le Miller, lus comme un débit nul sous l'ancien
    seuil de 0,3 Pa. Seuil réglable (mesure_dp.seuil_debit_nul_pa, 0,05)."""
    import json
    from pathlib import Path
    from core.banc import Banc
    cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json")
                     .read_text(encoding="utf-8"))
    banc = Banc(cfg)
    r = banc.qm_depuis_mesures("miller", 0.241, 24.0, 0.0)
    assert r["re_veine"] == pytest.approx(100.0, rel=0.02)
    assert banc.qm_depuis_mesures("miller", 0.04, 24.0, 0.0)["qm"] == 0.0
    cfg["mesure_dp"]["seuil_debit_nul_pa"] = 0.3            # ancien réglage
    assert Banc(cfg).qm_depuis_mesures("miller", 0.241, 24.0, 0.0)["qm"] == 0.0


def test_veine_sans_hauteur_largeur_si_section_et_dh_fournis():
    """config.json antérieur à veine.hauteur_m / largeur_m : le Banc levait
    KeyError et le logiciel ne démarrait plus. S et Dh suffisent."""
    import copy
    cfg = copy.deepcopy(CFG)
    for cle in ("hauteur_m", "largeur_m"):
        cfg["veine"].pop(cle, None)
    banc = Banc(cfg)
    assert banc.veine.section_m2 == cfg["veine"]["section_m2"]
    cfg["veine"].pop("section_m2")
    with pytest.raises(ValueError):
        Banc(cfg)
