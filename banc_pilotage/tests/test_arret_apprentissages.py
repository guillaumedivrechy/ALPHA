# -*- coding: utf-8 -*-
"""ARRÊT pendant chaque séquence d'apprentissage, sur le banc simulé avec la
vraie boucle (thread) en temps réel : ventilateur coupé, jamais relancé,
séquence terminée sans résultat partiel, vannes rendues à la boucle.

Deux voies d'arrêt : le bouton de l'IHM (annulation + arrêt total) et un
arrêt du variateur seul (repli, page de mise en service). Toutes les
séquences tournent en parallèle (≈ 15 s au total)."""

import json
import shutil
import threading
import time

import pytest

import banc_simule as BS
from core.apprentissage import Apprentissage
from core.banc import Banc
from core.pilote import Pilote

TESTS = ["zero_capteurs", "verif_cablage", "calibration_pdc", "vanne_3voies",
         "vanne_bypass", "vanne_pdc", "re_mapping_miller", "re_mapping_iso",
         "indiciel"]
# deux instants d'arrêt par séquence : tôt (positionnement) et plus tard
# (ventilateur en marche pour les séquences qui l'utilisent)
DELAIS = (2.0, 6.0)


def _derouler(test, delai, voie, racine, sortie):
    cfg = json.loads(json.dumps(BS.CFG_DEPOT))
    BS._fusion(cfg, json.loads((BS.DONNEES / "config_appris.json")
                               .read_text(encoding="utf-8")))
    cfg["journal"]["actif"] = False
    banc = Banc(cfg)
    sim = BS.SimTrace(banc, cfg["simulation"], cfg["modbus"], cfg["actionneurs"])
    p = Pilote(banc, sim, cfg)
    p.demarrer()
    try:
        time.sleep(1.0)
        a = Apprentissage(p, cfg, racine / f"cfg_{test}_{voie}_{delai}.json")
        kw = {"u1": 2.0, "u2": 4.0} if test == "indiciel" else {}
        err = a.demarrer(test, **kw)
        if err:
            sortie[(test, voie, delai)] = f"refusé : {err}"
            return
        time.sleep(delai)
        if voie == "bouton":
            a.annuler()
        p.arreter_regulation()
        t0 = time.monotonic()
        relance, fin = [], None
        while time.monotonic() - t0 < 6.0:
            time.sleep(0.05)
            if time.monotonic() - t0 > 0.5 and (sim._u_variateur > 0 or sim._run):
                relance.append(round(sim._u_variateur, 2))
            if fin is None and not a.etat["en_cours"]:
                fin = time.monotonic() - t0
        sortie[(test, voie, delai)] = {
            "relance": relance, "fin_s": fin, "statut": a.etat["statut"],
            "resultat": test in a.resultats,
            "maintien_suspendu": p._maintien_tor_suspendu,
        }
    finally:
        p.arreter()


@pytest.fixture(scope="module")
def deroulements(tmp_path_factory):
    import core.pilote
    racine = tmp_path_factory.mktemp("racine_arrets")
    shutil.copytree(BS.DONNEES / "apprentissages", racine / "apprentissages")
    shutil.copy(BS.DONNEES / "etat_banc.json", racine / "etat_banc.json")
    ancienne = core.pilote.RACINE
    core.pilote.RACINE = racine
    sortie = {}
    try:
        fils = [threading.Thread(target=_derouler,
                                 args=(t, d, v, racine, sortie))
                for t in TESTS for v in ("bouton", "arret_variateur")
                for d in DELAIS]
        for f in fils:
            f.start()
        for f in fils:
            f.join(120)
    finally:
        core.pilote.RACINE = ancienne
    return sortie


@pytest.mark.parametrize("delai", DELAIS)
@pytest.mark.parametrize("voie", ["bouton", "arret_variateur"])
@pytest.mark.parametrize("test", TESTS)
def test_arret_pendant_apprentissage(deroulements, test, voie, delai):
    r = deroulements[(test, voie, delai)]
    assert isinstance(r, dict), r
    assert r["relance"] == [], "ventilateur relancé après l'arrêt"
    assert r["fin_s"] is not None and r["fin_s"] < 2.0
    assert r["statut"].startswith(("annulé", "interrompu")), r["statut"]
    assert not r["resultat"], "résultat partiel enregistré"
    assert not r["maintien_suspendu"], "vannes non rendues à la boucle"
