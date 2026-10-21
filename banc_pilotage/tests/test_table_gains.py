# -*- coding: utf-8 -*-
"""Table de gains tension <-> Re (pré-positionnement feed-forward)."""

import pytest

from core.pilote import TableGains

POINTS = [[2.0, 4000.0], [4.0, 8500.0], [6.0, 13500.0], [8.0, 19000.0]]


def test_validite():
    assert TableGains(POINTS).est_valide()
    assert not TableGains([]).est_valide()
    assert not TableGains([[2.0, 4000.0]]).est_valide()
    # Re non monotone -> invalide
    assert not TableGains([[2.0, 4000.0], [4.0, 3000.0]]).est_valide()
    # tensions dupliquées -> invalide
    assert not TableGains([[2.0, 4000.0], [2.0, 8000.0]]).est_valide()


def test_interpolation():
    t = TableGains(POINTS)
    assert t.re_pour_tension(4.0) == pytest.approx(8500.0)
    assert t.re_pour_tension(5.0) == pytest.approx(11000.0)   # milieu segment


def test_interpolation_inverse():
    t = TableGains(POINTS)
    assert t.tension_pour_re(8500.0) == pytest.approx(4.0)
    assert t.tension_pour_re(11000.0) == pytest.approx(5.0)


def test_extrapolation_bornee():
    t = TableGains(POINTS)
    assert 0.0 <= t.tension_pour_re(100.0) <= 10.0
    assert 0.0 <= t.tension_pour_re(1e6) <= 10.0


def test_aller_retour():
    t = TableGains(POINTS)
    for re in (4000.0, 6000.0, 15000.0):
        assert t.re_pour_tension(t.tension_pour_re(re)) == \
            pytest.approx(re, rel=1e-9)


def test_palier_remesure_a_la_meme_tension():
    """Deux paliers à la même tension (palier remesuré) : le feed-forward
    divisait par zéro (re_pour_u_ff) et pente_ff levait AttributeError
    (_gain_re_par_volt n'existe pas sur TableGains)."""
    t = TableGains([(1.0, 3000.0), (2.0, 6000.0), (2.0, 6500.0), (3.0, 9000.0)])
    assert t.valide_ff() and t.nb_points_ecartes() == 1
    assert t.re_pour_u_ff(2.0) == pytest.approx(6000.0)
    assert t.pente_ff(2.0) == pytest.approx(3000.0)
    assert 1.0 < t.u_pour_re_ff(7000.0) < 3.0
