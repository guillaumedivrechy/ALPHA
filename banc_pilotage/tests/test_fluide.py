# -*- coding: utf-8 -*-
"""Propriétés de l'air : valeurs physiques connues + valeurs forcées."""

import pytest

from core.fluide import rho_air, mu_air, nu_air, ProprietesFluide


def test_rho_conditions_normales():
    # air à 101325 Pa / 20 °C : ~1,204 kg/m³
    assert rho_air(101325.0, 20.0) == pytest.approx(1.2041, abs=1e-3)


def test_mu_sutherland():
    # ~1,81e-5 Pa.s à 20 °C ; référence exacte à 0 °C
    assert mu_air(20.0) == pytest.approx(1.813e-5, rel=1e-2)
    assert mu_air(0.0) == pytest.approx(1.716e-5, rel=1e-9)


def test_nu_coherence():
    assert nu_air(101325.0, 20.0) == pytest.approx(
        mu_air(20.0) / rho_air(101325.0, 20.0))


def test_valeurs_forcees():
    f = ProprietesFluide(rho_force=1.205, mu_force=1.5e-5)
    assert f.rho(90000.0, 35.0) == 1.205
    assert f.mu(35.0) == 1.5e-5
    assert f.nu(90000.0, 35.0) == pytest.approx(1.5e-5 / 1.205)


def test_sans_forcage():
    f = ProprietesFluide()
    assert f.rho(101325.0, 20.0) == rho_air(101325.0, 20.0)
    assert f.mu(20.0) == mu_air(20.0)
