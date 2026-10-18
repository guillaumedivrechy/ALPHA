# -*- coding: utf-8 -*-
"""Régulateur PI : saturation, rampe, anti-windup, pré-positionnement."""

import pytest

from core.regulation import RegulateurPI, RegulateurTrim


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


def test_preposition_survit_a_un_transitoire_clippe():
    """Régression : l'anti-windup par recalcul détruisait le feed-forward au
    premier clip de rampe -> la sortie retombait sous la tension d'équilibre
    pendant que l'erreur se résorbait (convergence retardée de ~15 s)."""
    pi = _pi()
    pi.reinitialiser(u_initial=4.0)
    pi.calculer(consigne=10000.0, mesure=0.0)          # transitoire clippé
    for mesure in (5000.0, 7500.0, 9000.0, 10000.0):   # erreur qui décroît
        u = pi.calculer(consigne=10000.0, mesure=mesure)
        assert u >= 4.0 - 1e-9   # jamais sous le feed-forward (erreur >= 0)


def test_changement_echelle_gains_sans_a_coup():
    """Gain scheduling en cours de route : la sortie à erreur nulle doit
    rester identique (l'intégrale est recalée sur la nouvelle échelle)."""
    pi = _pi()
    pi.reinitialiser(u_initial=5.0)
    pi.changer_gain_scale(3.0)
    u = pi.calculer(consigne=10000.0, mesure=10000.0)
    assert u == pytest.approx(5.0, abs=1e-9)


# ------------------------------------------------------------ RegulateurTrim

def _trim(**kw):
    defauts = dict(kp=0.5, ki=0.6, periode_s=0.2, rampe_v_par_s=100.0,
                   trim_max_v=1.5)
    defauts.update(kw)
    return RegulateurTrim(**defauts)


def test_trim_erreur_nulle_sortie_egale_feed_forward():
    t = _trim()
    u = t.calculer(10000.0, 10000.0, u_ff=4.0, pente=3000.0)
    assert u == pytest.approx(4.0)


def test_trim_erreur_en_volts_par_la_pente():
    """Erreur de 300 Re sur une pente de 3000 Re/V = 0,1 V : P = kp × 0,1."""
    t = _trim(ki=0.0)
    u = t.calculer(10000.0, 9700.0, u_ff=4.0, pente=3000.0)
    assert u == pytest.approx(4.0 + 0.5 * 0.1)


def test_trim_integrale_annule_un_biais():
    """Plante statique Re = 3000·u, table fausse de 5 % : l'intégrale
    ramène la mesure sur la consigne."""
    t = _trim()
    u = 3.0
    for _ in range(300):
        u = t.calculer(9000.0, 3000.0 * 0.95 * u, u_ff=3.0, pente=3000.0)
    assert 3000.0 * 0.95 * u == pytest.approx(9000.0, rel=1e-3)
    assert t.integrale > 0


def test_trim_correction_bornee():
    t = _trim(trim_max_v=0.5)
    for _ in range(500):
        u = t.calculer(10000.0, 0.0, u_ff=4.0, pente=3000.0)
    assert u == pytest.approx(4.5)
    assert t.integrale <= 0.5 + 1e-9


def test_trim_anti_windup_sur_plafond():
    """Plafond (saturation ΔP) : l'intégrale ne s'enroule pas contre lui ;
    dès que la mesure dépasse la consigne, la sortie redescend."""
    t = _trim()
    for _ in range(200):
        t.calculer(10000.0, 5000.0, u_ff=4.0, pente=3000.0, u_max=4.0)
    i_bloquee = t.integrale
    assert i_bloquee < 0.2
    u = t.calculer(10000.0, 11000.0, u_ff=4.0, pente=3000.0, u_max=4.0)
    assert u < 4.0


def test_trim_gels():
    t = _trim()
    u = t.calculer(10000.0, 7000.0, u_ff=4.0, pente=3000.0,
                   geler_integrale=True, geler_p=True)
    assert u == pytest.approx(4.0)
    assert t.integrale == 0.0
    u = t.calculer(10000.0, 7000.0, u_ff=4.0, pente=3000.0,
                   geler_integrale=True)
    assert u == pytest.approx(4.0 + 0.5 * 1.0)   # P seul
    assert t.integrale == 0.0


def test_trim_rampe_sur_la_sortie():
    t = _trim(rampe_v_par_s=1.0)                 # 0,2 V par pas
    t.reinitialiser(u_initial=0.0)
    u = t.calculer(10000.0, 10000.0, u_ff=4.0, pente=3000.0)
    assert u == pytest.approx(0.2)


def test_trim_bande_morte_sur_p_seulement():
    """Bande morte : pas de P sur le bruit, mais l'intégrale continue de
    moyenner (précision = mesure moyennée, pas largeur de bande)."""
    t = _trim(bande_morte_relative=0.01)
    u = t.calculer(10000.0, 9950.0, u_ff=4.0, pente=1000.0)
    assert u == pytest.approx(4.0 + 0.6 * 0.05 * 0.2)
