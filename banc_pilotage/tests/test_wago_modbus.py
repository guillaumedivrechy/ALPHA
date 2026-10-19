# -*- coding: utf-8 -*-
"""Le VRAI code de communication (io_banc/wago.py, client pymodbus) contre un
faux coupleur Modbus TCP local : mapping de config.json, entrées inversées,
mise à l'échelle, réponses d'erreur, coupure et retour du coupleur, latence
— et le pilote complet (thread) branché dessus."""

import json
import time
from pathlib import Path

import pytest

pytest.importorskip("pymodbus")

from faux_coupleur import FauxCoupleur          # noqa: E402
from core.banc import Banc                      # noqa: E402
from core.pilote import Pilote                  # noqa: E402
from io_banc.wago import WagoModbus             # noqa: E402

CFG = json.loads((Path(__file__).resolve().parent.parent / "config.json")
                 .read_text(encoding="utf-8"))
PE = 32767                                      # 10 V sur les modules 0-10 V


def _voies(section):
    return {k: v for k, v in CFG["modbus"][section].items()
            if not k.startswith("_")}


@pytest.fixture
def coupleur():
    fc = FauxCoupleur()
    yield fc
    fc.arreter()


def _cfg_modbus(fc):
    mb = json.loads(json.dumps(CFG["modbus"]))
    mb["ip"], mb["port"] = "127.0.0.1", fc.port
    return mb


def _banc_au_repos(fc):
    """Entrées d'un banc au repos : Miller, bypass et PdC ouverts, 20,5 °C,
    contact NF du variateur fermé (pas de défaut)."""
    e, t = _voies("entrees"), _voies("entrees_tor")
    fc.ir[e["temperature"]["adresse"]] = 205
    fc.ir[e["pdc_recopie"]["adresse"]] = PE
    for nom in ("fc_3voies_90", "fc_bypass_ouvert", "fc_pdc_ouvert"):
        fc.di[t[nom]["adresse"]] = True
    fc.di[t["defaut_variateur"]["adresse"]] = True    # NF fermé = sain


def test_lecture_mapping_et_echelles(coupleur):
    e, t = _voies("entrees"), _voies("entrees_tor")
    for nom, v in e.items():
        coupleur.ir[v["adresse"]] = 205 if nom == "temperature" else PE // 2
    actifs = {"fc_bypass_ferme", "fc_3voies_0", "defaut_act_pdc"}
    for nom in actifs:
        coupleur.di[t[nom]["adresse"]] = True
    w = WagoModbus(_cfg_modbus(coupleur))
    m = w.lire_mesures()
    assert m["temperature"] == pytest.approx(20.5)
    for nom in ("dp_iso_500", "dp_diaph_500", "p_amont_500"):
        assert m[nom] == pytest.approx(250.0, abs=0.1)        # 5 V = 250 Pa
    for nom in ("pdc_recopie", "frequence_variateur"):
        assert m[nom] == pytest.approx(5.0, abs=0.01)         # brut en volts
    for nom, v in t.items():
        attendu = (nom in actifs) != v.get("inverse", False)
        assert m[nom] is attendu, nom
    # bit à 0 sur une entrée NF (fil coupé) = défaut signalé
    assert m["defaut_variateur"] is True
    # lectures groupées : AI 0-4, RTD, DI = 3 transactions
    assert coupleur.transactions == 3


def test_ecritures_sorties(coupleur):
    s = _voies("sorties")
    w = WagoModbus(_cfg_modbus(coupleur))
    w.ecrire_variateur(5.0)
    w.ecrire_vanne_perte_charge(2.5)
    w.ecrire_variateur_run(True)
    w.ecrire_vanne_bypass(True)
    assert coupleur.hr[s["variateur"]["adresse"]] == round(0.5 * PE)
    assert coupleur.hr[s["vanne_perte_charge"]["adresse"]] == round(0.25 * PE)
    assert coupleur.co[s["variateur_run"]["adresse"]] is True
    assert coupleur.co[s["vanne_bypass"]["adresse"]] is True
    # vanne 3 voies : jamais les deux ordres ensemble, l'ancien relâché d'abord
    a0, a90 = s["vanne_diaphragme_0deg"]["adresse"], s["vanne_diaphragme_90deg"]["adresse"]
    etat = {a0: False, a90: False}
    for cible in (True, False, True):
        n = len(coupleur.ecritures)
        w.ecrire_vanne_diaphragme(cible)
        for fc_, adr, val in coupleur.ecritures[n:]:
            if adr in etat and fc_ == 5:
                etat[adr] = val
                assert not (etat[a0] and etat[a90]), "deux ordres 3 voies actifs"
        assert etat[a0] is cible and etat[a90] is (not cible)


def test_reponse_d_erreur_modbus(coupleur):
    w = WagoModbus(_cfg_modbus(coupleur))
    coupleur.erreurs_restantes = 1
    with pytest.raises(OSError):
        w.lire_mesures()
    coupleur.erreurs_restantes = 1
    with pytest.raises(OSError):
        w.ecrire_variateur(3.0)
    w.ecrire_variateur(3.0)                     # refusée -> non mise en cache


def test_coupure_puis_retour_du_coupleur(coupleur):
    """Coupure : OSError (et non une exception pymodbus qui traversait les
    replis sûrs). Retour : reconnexion automatique."""
    w = WagoModbus(_cfg_modbus(coupleur))
    w.lire_mesures()
    coupleur.arreter()
    with pytest.raises(ConnectionError, match="liaison Modbus perdue"):
        w.lire_mesures()
    with pytest.raises(OSError):
        w.ecrire_variateur(0.0)
    coupleur.demarrer()
    time.sleep(0.2)
    w.lire_mesures()
    w.ecrire_variateur(0.0)


def test_coupleur_absent_au_demarrage():
    fc = FauxCoupleur()
    port = fc.port
    fc.arreter()
    mb = json.loads(json.dumps(CFG["modbus"]))
    mb["ip"], mb["port"], mb["timeout_s"] = "127.0.0.1", port, 0.2
    with pytest.raises(ConnectionError, match="Connexion impossible"):
        WagoModbus(mb)


def _pilote(fc, tmp_path, monkeypatch):
    import core.pilote
    monkeypatch.setattr(core.pilote, "RACINE", tmp_path)
    cfg = json.loads(json.dumps(CFG))
    cfg["journal"]["actif"] = False
    cfg["modbus"]["ip"], cfg["modbus"]["port"] = "127.0.0.1", fc.port
    _banc_au_repos(fc)
    p = Pilote(Banc(cfg), WagoModbus(cfg["modbus"]), cfg)
    p.demarrer()
    return p, cfg["modbus"]["sorties"]


def test_pilote_survit_a_une_coupure_du_coupleur(coupleur, tmp_path, monkeypatch):
    """Ventilateur en marche, le coupleur disparaît : la boucle reste en vie
    (avant : thread mort, consigne 3 V et RUN laissés au coupleur), l'arrêt
    est latché et à acquitter ; au retour, 0 V et RUN off sont réécrits et
    rien ne redémarre seul."""
    p, s = _pilote(coupleur, tmp_path, monkeypatch)
    try:
        time.sleep(0.8)
        p.mode_manuel_tension(3.0)
        time.sleep(0.8)
        assert coupleur.hr[s["variateur"]["adresse"]] == round(0.3 * PE)
        coupleur.arreter()
        time.sleep(1.5)
        assert p._thread.is_alive()
        assert "liaison Modbus perdue" in (p.etat["erreur_com"] or "")
        assert not p.mode_manuel and p.etat["arret_securite"]
        coupleur.demarrer()
        time.sleep(1.5)
        assert p._thread.is_alive()
        assert p.etat["erreur_com"] is None
        cycles = p.etat["cycles"]
        time.sleep(0.6)
        assert p.etat["cycles"] > cycles                 # la boucle tourne
        assert coupleur.hr[s["variateur"]["adresse"]] == 0
        assert coupleur.co[s["variateur_run"]["adresse"]] is False
        assert not p.mode_manuel and p.etat["arret_securite"]
        p.arreter_regulation()          # bouton ARRÊT : jamais d'exception
    finally:
        p.arreter()


def test_bouton_arret_pendant_la_coupure(coupleur, tmp_path, monkeypatch):
    p, _ = _pilote(coupleur, tmp_path, monkeypatch)
    try:
        time.sleep(0.5)
        coupleur.arreter()
        time.sleep(0.5)
        p.arreter_regulation()          # ne lève pas
        assert p._thread.is_alive()
    finally:
        coupleur.demarrer()
        p.arreter()


def test_latence_publiee_dans_la_duree_de_cycle(coupleur, tmp_path, monkeypatch):
    """Coupleur lent (80 ms par transaction, 3 transactions par lecture) :
    la durée réelle du cycle publiée dépasse la période — c'est ce que
    l'IHM signale (« cycle … ms ⚠ »)."""
    p, _ = _pilote(coupleur, tmp_path, monkeypatch)
    try:
        time.sleep(0.6)
        coupleur.latence_s = 0.08
        time.sleep(1.5)
        assert p.etat["duree_cycle_s"] > p.periode
    finally:
        coupleur.latence_s = 0.0
        p.arreter()


def test_coupleur_muet_cause_publiee_sans_attendre_le_repli(coupleur, tmp_path,
                                                             monkeypatch):
    """Câble coupé côté coupleur (connexion ouverte, plus aucune réponse) :
    arrêt de sécurité après délai × tentatives, cause publiée tout de suite
    — avant : affichée seulement après les deux écritures de repli, elles
    aussi bloquées (≈ 8 s sans cause au bandeau)."""
    p, _ = _pilote(coupleur, tmp_path, monkeypatch)
    try:
        time.sleep(0.8)
        p.mode_manuel_tension(3.0)
        time.sleep(0.8)
        coupleur.latence_s = 120.0
        t0 = time.monotonic()
        while time.monotonic() - t0 < 20 and not p.etat.get("arret_securite"):
            time.sleep(0.05)
        assert p.etat.get("arret_securite")
        time.sleep(0.3)
        assert "liaison Modbus perdue" in (p.etat.get("arret_motif") or "")
    finally:
        coupleur.latence_s = 0.0
        p.arreter()
