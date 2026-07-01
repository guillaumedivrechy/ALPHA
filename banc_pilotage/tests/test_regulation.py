# -*- coding: utf-8 -*-
"""Régulateur PI : saturation, rampe, anti-windup, pré-positionnement."""

import pytest

from core.regulation import RegulateurPI


def _pi(**kw):
    defauts = dict(kp=1.5, ki=0.5, periode_s=0.5, rampe_v_par_s=1.0)
    defauts.update(kw)
    return RegulateurPI(**defauts)


def test_sortie_bornee():
    pi = _pi(rampe_v_par_s=1000.0)      # rampe inactive
    for _ in range(200):
        u = pi.calculer(consigne=10000.0, mesure=0.0)
    assert 0.0 <= u <= 10.0


def test_rampe_limite_la_variation():
    pi = _pi()                           # 1 V/s, dt 0,5 s -> 0,5 V max/cycle
    u_prec = 0.0
    for _ in range(20):
        u = pi.calculer(consigne=10000.0, mesure=0.0)
        assert u - u_prec <= 0.5 + 1e-9
        u_prec = u


def test_anti_windup():
    """Après une longue saturation haute, la sortie doit redescendre dès que
    la mesure dépasse la consigne (pas d'intégrale accumulée à purger)."""
    pi = _pi(rampe_v_par_s=1000.0)
    for _ in range(500):                 # saturation prolongée à 10 V
        u_sat = pi.calculer(consigne=10000.0, mesure=0.0)
    assert u_sat == pytest.approx(10.0)
    u = pi.calculer(consigne=10000.0, mesure=12000.0)   # dépassement 20 %
    assert u < 10.0 - 0.1


def test_preposition_feed_forward():
    pi = _pi()
    pi.reinitialiser(u_initial=4.0)
    u = pi.calculer(consigne=10000.0, mesure=10000.0)   # erreur nulle
    assert u == pytest.approx(4.0, abs=1e-9)


def test_bande_morte():
    pi = _pi(bande_morte_relative=0.01)
    pi.reinitialiser(u_initial=5.0)
    u = pi.calculer(consigne=10000.0, mesure=10050.0)   # +0,5 % < bande
    assert u == pytest.approx(5.0, abs=1e-9)


def test_consigne_nulle():
    pi = _pi()
    assert pi.calculer(consigne=0.0, mesure=5000.0) == 0.0
