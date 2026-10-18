# -*- coding: utf-8 -*-
"""Matrice d'usage : chaque consigne depuis chaque état de départ, sur le
banc simulé caractérisé (tables de la répétition générale), à horloge
accélérée.

Pour chaque cas : bon mode (ISO / Miller / PdC selon les seuils et
l'hystérésis), convergence rapide sur la consigne visée (bornée au maximum
mesurable si besoin), aucune alerte autre que « consigne limitée », pas de
battement de vannes, et les invariants de banc_simule à chaque cycle
(capteur ΔP de la butée lue, 0 V pendant la course d'une vanne TOR, aucun
défaut ni arrêt de sécurité)."""

import random

import pytest

from banc_simule import BancSimule

DEPARTS = {"repos": None, "ISO": (12000.0, "veine"),
           "Miller": (2000.0, "veine"), "PdC": (300.0, "veine")}
CONSIGNES = [(300, "veine"), (600, "veine"), (1000, "veine"), (2000, "veine"),
             (2600, "veine"), (3000, "veine"), (6000, "veine"),
             (10000, "veine"), (13000, "veine"), (15000, "veine"),
             (500, "conduite"), (1000, "conduite"), (4800, "conduite"),
             (5200, "conduite"), (8000, "conduite"), (20000, "conduite"),
             (26000, "conduite")]
K_VEINE = 1.870                 # Re conduite / Re veine (géométrie du banc)


def _banc(monkeypatch, tmp_path, depart, caracterise=True):
    b = BancSimule(monkeypatch, tmp_path, caracterise=caracterise)
    if DEPARTS[depart]:
        b.consigne(*DEPARTS[depart])
        assert b.jusqu_a_convergence(150) is not None, "départ non établi"
        b.cycles(10)
    b.violations.clear()
    return b


def _mode_attendu(b, depart, re_cond):
    seuil, hyst = b.p.banc.seuil_re, b.p.banc.hysteresis_re
    depart_iso = depart == "ISO"
    if re_cond > seuil + hyst or (depart_iso and re_cond >= seuil - hyst):
        return "normal_high"
    # bande ±5 % autour du seuil bas-Re : on garde le mode bas-Re courant ;
    # venant de l'ISO (aucun des deux), décision au seuil exact
    s_bas = b.p._seuil_bas_re()
    facteur = {"PdC": 1.05, "ISO": 1.0}.get(depart, 0.95)
    return "pdc_control" if re_cond < facteur * s_bas else "normal_low"


@pytest.mark.parametrize("re,ref", CONSIGNES)
@pytest.mark.parametrize("depart", list(DEPARTS))
def test_consigne_depuis_chaque_etat(monkeypatch, tmp_path, depart, re, ref):
    b = _banc(monkeypatch, tmp_path, depart)
    b.consigne(float(re), ref)
    motifs, t_conv, faux_regule = [], None, []
    for i in range(int(100.0 / b.p.periode)):
        motifs += b.suivre(b.p.periode)
        e = b.p.etat
        if e.get("convergee"):
            t_conv = t_conv or (i + 1) * b.p.periode
            # « RÉGULÉ ✓ » sur un transitoire : la moyenne glissante
            # traversait la consigne alors que le Re en était à ±35 %
            if abs(e["re_mesure"] - e["re_vise"]) > 0.10 * e["re_vise"]:
                faux_regule.append(round((i + 1) * b.p.periode, 1))
    assert not faux_regule, faux_regule[:5]
    e = b.p.etat
    re_cond = re * (K_VEINE if ref == "veine" else 1.0)
    mode = _mode_attendu(b, depart, re_cond)
    assert b.p._mode_regulation == mode
    assert not b.violations, b.violations[:3]
    # temps mesurés sur ce banc : ≤ 28 s variateur, 31-68 s vanne PdC
    assert t_conv is not None and t_conv <= (80.0 if mode == "pdc_control"
                                             else 40.0), t_conv
    assert e["convergee"], (e.get("re_moyen"), e.get("re_vise"))
    assert e["re_moyen"] == pytest.approx(e["re_vise"], rel=0.01)
    limitee = e["re_vise"] < re - 0.5
    assert bool(e.get("consigne_limitee")) == limitee
    if limitee:                          # plafond capteur ΔP ISO (451 Pa)
        assert e["re_vise"] > 0.93 * (25600 if ref == "conduite" else 13690)
    for cle in ("alerte_pdc", "alerte_table_ff", "dp_sature"):
        assert not e.get(cle), (cle, e.get(cle))
    # au plus un changement de configuration de vannes, sans battement
    assert b.nb_manoeuvres_tor(motifs) <= 2


def test_frontiere_iso_miller_sans_battement(monkeypatch, tmp_path):
    """Consignes alternées de part et d'autre du seuil ISO/Miller, dans la
    bande d'hystérésis : aucune manœuvre de vanne."""
    b = _banc(monkeypatch, tmp_path, "Miller")
    b.consigne(5000.0, "conduite")
    b.cycles(30)
    motifs = []
    for re in (5400.0, 4600.0, 5400.0, 4600.0, 5400.0):
        b.consigne(re, "conduite")
        motifs += b.suivre(30.0)
    assert b.nb_manoeuvres_tor(motifs) == 0
    assert b.p._mode_regulation == "normal_low"
    assert not b.violations, b.violations[:3]


def test_frontiere_bas_re_sans_battement(monkeypatch, tmp_path):
    """Consignes alternées à ±3 % du seuil bas-Re (hystérésis ±5 %) : le
    mode ne change pas, quel que soit le côté de départ."""
    for depart, mode in (("Miller", "normal_low"), ("PdC", "pdc_control")):
        b = _banc(monkeypatch, tmp_path / depart, depart)
        s = b.p._seuil_bas_re()
        modes = set()
        for f in (1.03, 0.97, 1.03, 0.97):
            b.consigne(s * f, "conduite")
            for _ in range(int(25 / b.p.periode)):
                b.cycles(b.p.periode)
                modes.add(b.p._mode_regulation)
        assert modes == {mode}, (depart, modes)
        assert not b.violations, b.violations[:3]


def test_sequence_traverse_tous_les_modes(monkeypatch, tmp_path):
    """Profil ISO -> Miller -> PdC -> Miller -> ISO : chaque échelon converge
    avant le suivant, sans défaut ; « Arrêter » fige ensuite la consigne."""
    b = _banc(monkeypatch, tmp_path, "repos")
    profil = [(0.0, 12000.0), (60.0, 2000.0), (120.0, 300.0),
              (180.0, 1500.0), (240.0, 8000.0), (300.0, 8000.0)]
    assert b.p.demarrer_sequence(profil, "veine") is None
    modes, convergee_par_etape = [], {}
    t_fin = b.t[0] + 400.0
    while (b.p.etat.get("sequence") is None
           or not b.p.etat["sequence"].get("terminee")) and b.t[0] < t_fin:
        b.cycles(b.p.periode)
        seq = b.p.etat.get("sequence") or {}
        if b.p.etat.get("convergee"):
            convergee_par_etape[seq.get("etape")] = True
        if not modes or modes[-1] != b.p._mode_regulation:
            modes.append(b.p._mode_regulation)
    assert modes[:5] == ["normal_high", "normal_low", "pdc_control",
                         "normal_low", "normal_high"][:len(modes[:5])]
    assert set(convergee_par_etape) >= {1, 2, 3, 4, 5}
    assert b.p.figer_sequence() is None           # profil terminé
    assert b.p.regulation_active and b.p.re_cible == 8000.0
    assert not b.violations, b.violations[:3]


def test_endurance_consignes_aleatoires(monkeypatch, tmp_path):
    """40 consignes tirées au hasard sur toute la plage (≈ 1 h de banc) :
    aucune violation ; chaque consigne atteignable converge dans sa
    fenêtre, et une consigne sous le Re minimal du mode PdC (vanne en
    butée fermée) est signalée « hors de portée »."""
    b = _banc(monkeypatch, tmp_path, "repos")
    re_min = b.cfg["strategie_bas_re"]["re_min_exploitable"]   # Re conduite
    rnd = random.Random(7)
    echecs = []
    for i in range(40):
        ref = rnd.choice(("veine", "conduite"))
        re = round(10 ** rnd.uniform(2.5, 4.4 if ref == "conduite" else 4.15))
        b.consigne(float(re), ref)
        re_cond = re * (K_VEINE if ref == "veine" else 1.0)
        if re_cond < re_min:
            # près du Re minimal appris (borne approchée) : convergence ou
            # alarme « hors de portée », jamais une attente silencieuse
            atteint = b.jusqu_a_convergence(90.0) is not None
            if not atteint and "hors de portée" not in (
                    b.p.etat.get("alerte_pdc") or ""):
                echecs.append((i, re, ref, "ni convergence ni alarme"))
        elif b.jusqu_a_convergence(90.0) is None:
            echecs.append((i, re, ref, b.p._mode_regulation,
                           b.p.etat.get("re_moyen"), b.p.etat.get("re_vise")))
        b.cycles(rnd.uniform(0.0, 20.0))
    assert not echecs, echecs
    assert not b.violations, b.violations[:3]


@pytest.mark.parametrize("re,ref", [r for r in CONSIGNES if r[0] >= 1000])
def test_banc_vierge_sans_violation(monkeypatch, tmp_path, re, ref):
    """Banc non caractérisé (aucune table) : règles respectées, et
    convergence pour les Re que le ventilateur tient seul."""
    b = _banc(monkeypatch, tmp_path, "repos", caracterise=False)
    b.consigne(float(re), ref)
    assert b.jusqu_a_convergence(80.0) is not None
    assert not b.violations, b.violations[:3]
