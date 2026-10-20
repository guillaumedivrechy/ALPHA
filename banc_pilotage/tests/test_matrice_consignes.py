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
    # temps mesurés sur ce banc : ≤ 28 s variateur, ≤ 37 s vanne PdC
    assert t_conv is not None and t_conv <= (50.0 if mode == "pdc_control"
                                             else 40.0), t_conv
    assert e["convergee"], (e.get("re_moyen"), e.get("re_vise"))
    assert e["re_moyen"] == pytest.approx(e["re_vise"], rel=0.01)
    limitee = abs(e["re_vise"] - re) > 0.5
    assert bool(e.get("consigne_limitee")) == limitee
    if limitee and e["re_vise"] < re:    # plafond capteur ΔP ISO (451 Pa)
        assert e["re_vise"] > 0.93 * (25600 if ref == "conduite" else 13690)
    elif limitee:                        # plancher du mode PdC (Re mapping)
        re_min = b.cfg["strategie_bas_re"]["re_min_exploitable"]
        assert e["re_vise"] * (K_VEINE if ref == "veine" else 1.0) \
            == pytest.approx(re_min, rel=0.02)
    for cle in ("alerte_pdc", "alerte_table_ff", "dp_sature"):
        assert not e.get(cle), (cle, e.get(cle))
    # au plus un changement de configuration de vannes, sans battement
    assert b.nb_manoeuvres_tor(motifs) <= 2


def test_frontiere_iso_miller_sans_battement(monkeypatch, tmp_path):
    """Consignes alternées de part et d'autre du seuil ISO/Miller, dans la
    bande d'hystérésis : aucune manœuvre de vanne. Côté haut, la bande est
    bornée par ce que le capteur Miller mesure (au-delà, ISO forcé)."""
    b = _banc(monkeypatch, tmp_path, "Miller")
    seuil, hyst = b.p.banc.seuil_re, b.p.banc.hysteresis_re
    haut = min(seuil + 0.6 * hyst, 0.98 * b.p._re_max_miller())
    bas = seuil - 0.6 * hyst
    assert haut > seuil
    b.consigne(seuil, "conduite")
    b.cycles(30)
    motifs = []
    for re in (haut, bas, haut, bas, haut):
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


@pytest.mark.parametrize("re", [300.0, 600.0])
def test_banc_vierge_pas_de_mode_pdc_sans_re_mapping_miller(monkeypatch, tmp_path, re):
    """Banc non caractérisé, après une marche en ISO (gain appris en
    marche) : très bas Re au ventilateur, jamais en mode PdC avec un u_min
    non mesuré (avant : ventilateur à 0,35 V, Re nul, alarme)."""
    b = _banc(monkeypatch, tmp_path, "ISO", caracterise=False)
    b.consigne(re, "veine")
    modes = set()
    for _ in range(int(80 / b.p.periode)):
        b.cycles(b.p.periode)
        modes.add(b.p._mode_regulation)
    e = b.p.etat
    assert "pdc_control" not in modes
    assert not e.get("alerte_pdc")
    assert e["re_moyen"] == pytest.approx(re, rel=0.10)
    assert "Re mapping Miller non fait" in (e.get("bas_re_motif") or "")
    assert not b.violations, b.violations[:3]


@pytest.mark.parametrize("re,ref", [(150.0, "veine"), (349.0, "conduite")])
def test_consigne_sous_le_minimum_du_mode_pdc_limitee(monkeypatch, tmp_path, re, ref):
    """Sous le Re minimal tenu en mode PdC (Re mapping Miller) : consigne
    limitée dès la saisie, convergée au minimum, sans alarme « hors de
    portée » (avant : ~30 s d'attente puis alarme)."""
    b = _banc(monkeypatch, tmp_path, "Miller")
    b.consigne(re, ref)
    assert b.jusqu_a_convergence(90.0) is not None
    e = b.p.etat
    re_min = b.cfg["strategie_bas_re"]["re_min_exploitable"]
    assert "sous le minimum" in (e.get("consigne_limitee") or "")
    assert e["re_vise"] * (K_VEINE if ref == "veine" else 1.0) == pytest.approx(re_min, rel=0.02)
    assert not e.get("alerte_pdc")
    assert not b.violations, b.violations[:3]


@pytest.mark.parametrize("depart,re,ref", [("ISO", 600.0, "veine"),
                                           ("Miller", 600.0, "veine"),
                                           ("Miller", 1000.0, "conduite")])
def test_entree_en_mode_pdc_par_le_haut_sans_creux(monkeypatch, tmp_path,
                                                   depart, re, ref):
    """Entrée en mode PdC depuis l'ISO ou le Miller : la vanne part grande
    ouverte (≈ 12 s de course). Le PI poursuivait le Re de la vanne pas
    encore arrivée et la vanne arrivait trop fermée : Re à -20/-30 % sous la
    consigne puis 30 s de rattrapage. Désormais tenue sur la table PdC
    pendant sa course."""
    b = _banc(monkeypatch, tmp_path, depart)
    b.consigne(re, ref)
    # creux mesuré après le premier passage à la consigne (depuis l'ISO, le
    # ventilateur coupé pendant la manœuvre fait repartir le Re de zéro)
    signe, traverse, re_min = None, False, None
    for _ in range(int(60 / b.p.periode)):
        b.cycles(b.p.periode)
        e = b.p.etat
        # ventilateur arrivé à u_pdc : sous ~1,2 V le simulateur fait
        # décrocher le ventilateur (débit erratique pendant sa montée)
        if b.p._mode_regulation != "pdc_control" or e.get("manoeuvre") \
                or abs(e["u_variateur"] - b.p._u_pdc()) > 0.02:
            continue
        s_ = e["re_mesure"] >= re
        traverse = traverse or (signe is not None and s_ != signe)
        signe = s_
        if traverse:
            re_min = e["re_mesure"] if re_min is None else min(re_min, e["re_mesure"])
    assert re_min is not None and re_min > 0.95 * re, re_min
    assert e["convergee"]


@pytest.mark.parametrize("re", [300.0, 600.0])
def test_mode_pdc_ventilateur_lance_apres_la_pdc(monkeypatch, tmp_path, re):
    """Démarrage en mode PdC depuis le repos : le ventilateur n'est lancé
    qu'une fois la PdC à sa position, comme après les fins de course des
    vannes TOR. Lancé vanne encore ouverte, le Re dépassait de ~36 %."""
    b = _banc(monkeypatch, tmp_path, "repos")
    b.consigne(re, "veine")
    re_max, t_conv = 0.0, None
    for i in range(int(60 / b.p.periode)):
        b.cycles(b.p.periode)
        e = b.p.etat
        if b.sim.u_ecrite > 0:
            assert abs(b.p._pdc_consigne - b.p._pdc_position_estimee) \
                <= b.p.ecart_course_pdc_v + 0.3, "ventilateur lancé PdC en course"
            re_max = max(re_max, e["re_mesure"] or 0.0)
        if t_conv is None and e.get("convergee"):
            t_conv = (i + 1) * b.p.periode
    assert re_max < 1.10 * re, re_max
    assert t_conv is not None and t_conv <= 40.0, t_conv
    assert not b.violations, b.violations[:3]


def test_pdc_bloquee_au_demarrage_defaut_sans_lancer_le_ventilateur(monkeypatch, tmp_path):
    b = _banc(monkeypatch, tmp_path, "repos")
    b.sim.ecrire_vanne_perte_charge = lambda v: None     # vanne PdC bloquée
    b.consigne(300.0, "veine")
    lance = False
    for _ in range(int(60 / b.p.periode)):
        b.cycles(b.p.periode, verifier=False)
        lance = lance or b.sim.u_ecrite > 0
    assert not lance
    assert "vanne PdC" in (b.p.etat.get("defaut_vanne") or "")
    assert b.p.etat["arret_securite"]


def test_attente_pdc_interrompue_ne_compte_pas(monkeypatch, tmp_path):
    """Attente « PdC avant ventilateur » interrompue (ARRÊT), reprise bien
    plus tard : pas de faux défaut (vu en clics aléatoires : l'horodatage
    d'un épisode ancien déclenchait le chien de garde)."""
    b = _banc(monkeypatch, tmp_path, "repos")
    b.consigne(300.0, "veine")
    b.cycles(2)
    assert b.p.etat["phase_regulation"] == "positionnement PdC avant ventilateur"
    b.p.arreter_regulation()
    b.cycles(60)                                     # bien au-delà du chien de garde
    b.consigne(300.0, "veine")
    assert b.jusqu_a_convergence(60) is not None
    assert not b.violations, b.violations[:3]


@pytest.mark.parametrize("depart,re", [("repos", 300.0), ("Miller", 600.0)])
def test_mode_pdc_avec_recopie_a_une_autre_echelle(monkeypatch, tmp_path, depart, re):
    """Carte P6 dont la recopie ne vaut pas la commande (2,4-9,2 V pour
    0-10 V commandés, valeurs de config.json) : l'arrivée de la vanne est
    jugée sur le modèle de course et les fins de course, pas sur l'égalité
    recopie = commande (avant : ventilateur jamais lancé, faux défaut vanne
    PdC, ou PI figé sur la table sans converger)."""
    vmin, vmax = 2.389, 9.153
    b = _banc(monkeypatch, tmp_path, depart)
    orig = b.sim.lire_mesures

    def lire():
        m = orig()
        m["pdc_recopie"] = vmin + b.sim._pdc_position / 10.0 * (vmax - vmin)
        return m
    b.sim.lire_mesures = lire
    b.p.pdc_recopie_min, b.p.pdc_recopie_max = vmin, vmax
    b.consigne(re, "veine")
    assert b.jusqu_a_convergence(80.0) is not None
    assert not b.p.etat.get("arret_securite")
    assert b.p.etat["re_moyen"] == pytest.approx(re, rel=0.01)


def test_3000_re_veine_depuis_l_iso_passe_en_miller(monkeypatch, tmp_path):
    """Constaté au banc avec l'ancien seuil (5000 Re conduite) : 3000 Re
    veine (5610 conduite) restait en ISO, à ~20 Pa sur un capteur de
    500 Pa. Bascule à 8000 ± 500 : Miller (~210 Pa), bypass ouvert."""
    b = _banc(monkeypatch, tmp_path, "ISO")
    b.consigne(3000.0, "veine")
    assert b.jusqu_a_convergence(90) is not None
    e = b.p.etat
    assert b.p._mode_regulation == "normal_low"
    assert e["diaphragme_lu"] == "miller"
    assert e["mesures_brutes"]["fc_bypass_ouvert"]
    assert e["dp"] > 150.0
    assert not b.violations, b.violations[:3]


# capteur Miller borné à 490 Pa (0,99 × 0,99) : garde-fou actif au-dessus de
# ~21 °C (à 20 °C le Miller mesure jusqu'à ~8 550, au-delà de la bande)
@pytest.mark.parametrize("t_c", [25.0, 30.0])
def test_miller_hors_capteur_bascule_en_iso(monkeypatch, tmp_path, t_c):
    """Depuis le Miller, consigne dans la bande d'hystérésis mais au-delà de
    ce que le capteur Miller mesure (ΔP croît avec μ, donc avec T) : ISO,
    sans saturation ni consigne limitée. Sans ce garde-fou, 8000 ± 500
    saturait le Miller (481 Pa à 8500 Re conduite et 20 °C)."""
    b = BancSimule(monkeypatch, tmp_path, cfg_sur={
        "simulation": {"temperature_c": t_c}})
    b.consigne(2000.0, "veine")
    assert b.jusqu_a_convergence(150) is not None
    b.cycles(10)
    re_max = b.p._re_max_miller()
    seuil, hyst = b.p.banc.seuil_re, b.p.banc.hysteresis_re
    assert re_max < seuil + hyst                 # garde-fou utile ici
    consigne = 0.5 * (re_max + seuil + hyst)     # dans la bande, Miller saturé
    b.consigne(consigne, "conduite")
    assert b.jusqu_a_convergence(90) is not None
    e = b.p.etat
    assert b.p._mode_regulation == "normal_high"
    assert e["diaphragme_lu"] == "iso"
    assert not e.get("consigne_limitee") and not e.get("dp_sature")
    assert e["re_moyen"] == pytest.approx(consigne, rel=0.01)


@pytest.mark.parametrize("de,vers", [("conduite", "veine"), ("veine", "conduite")])
def test_changement_de_reference_sans_coup(monkeypatch, tmp_path, de, vers):
    """Même débit, référence changée en régulation (15 000 Re conduite ->
    8 021 Re veine) : le modèle de référence restait dans l'ancienne
    unité -> correction en butée, +20 % de Re (ou un creux en sens
    inverse). Le débit doit rester tenu."""
    b = BancSimule(monkeypatch, tmp_path)
    re_c = 15000.0
    b.consigne(re_c if de == "conduite" else re_c / K_VEINE, de)
    assert b.jusqu_a_convergence(150) is not None
    b.cycles(10)
    b.consigne(re_c if vers == "conduite" else re_c / K_VEINE, vers)
    vus = []
    for _ in range(int(15.0 / b.p.periode)):
        b.cycles(b.p.periode)
        vus.append(b.p.etat["re_conduite"])
    assert max(vus) < 1.05 * re_c and min(vus) > 0.95 * re_c, (min(vus), max(vus))
    assert not b.violations, b.violations[:3]


def test_14000_re_veine_a_24_degres(monkeypatch, tmp_path):
    """Constaté au banc (24 °C) : 14 000 Re veine bornés à ~13 450 alors que
    le capteur ISO n'était pas saturé (consigne limitée à 0,95 × 0,95 ×
    500 = 451 Pa). Il en faut ~488 : consigne bornée à 490 Pa (0,99 ×
    0,99), atteinte sans « consigne limitée »."""
    b = BancSimule(monkeypatch, tmp_path, cfg_sur={
        "simulation": {"temperature_c": 24.0}})
    b.consigne(14000.0, "veine")
    assert b.jusqu_a_convergence(120) is not None
    e = b.p.etat
    assert not e.get("consigne_limitee") and not e.get("dp_sature")
    assert e["re_moyen"] == pytest.approx(14000.0, rel=0.01)
    assert 470.0 < e["dp"] < 495.0
