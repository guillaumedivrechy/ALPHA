# -*- coding: utf-8 -*-
"""Démarrage sur fichiers corrompus ou absents.

config.json invalide : main.py s'arrête avec un message clair, sans trace
Python (lancé par double-clic, la console se refermait sur un
JSONDecodeError incompréhensible). Fichiers d'apprentissage ou d'état
abîmés : démarrage quand même, la table concernée ignorée et signalée."""

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import banc_simule as BS
from core.banc import Banc
from core.pilote import Pilote
from io_banc.simulateur import SimulateurBanc

MAIN = Path(__file__).resolve().parent.parent / "main.py"


def _lancer(cfg_texte: str, tmp_path) -> subprocess.CompletedProcess:
    chemin = tmp_path / "config.json"
    chemin.write_text(cfg_texte, encoding="utf-8")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    return subprocess.run([sys.executable, str(MAIN), "--simulation",
                           "--config", str(chemin)],
                          capture_output=True, text=True, timeout=60, env=env)


def _cfg_modifiee(modif) -> str:
    cfg = json.loads(json.dumps(BS.CFG_DEPOT))
    modif(cfg)
    return json.dumps(cfg)


@pytest.mark.parametrize("nom,texte,attendu", [
    ("tronqué", json.dumps(BS.CFG_DEPOT)[:500], "illisible"),
    ("vide", "", "illisible"),
    ("clé absente",
     _cfg_modifiee(lambda c: c["regulation"].pop("kp")), "paramètre obligatoire absent"),
    ("géométrie impossible",
     _cfg_modifiee(lambda c: c["diaphragme_iso"].update(d20_mm=999)), "valeur invalide"),
])
def test_config_invalide_message_clair(tmp_path, nom, texte, attendu):
    r = _lancer(texte, tmp_path)
    assert r.returncode != 0
    assert "Démarrage impossible" in r.stderr and attendu in r.stderr, r.stderr
    assert "Traceback" not in r.stderr, r.stderr


def test_config_introuvable(tmp_path):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    r = subprocess.run([sys.executable, str(MAIN), "--simulation", "--config",
                        str(tmp_path / "absent.json")],
                       capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode != 0 and "introuvable" in r.stderr
    assert "Traceback" not in r.stderr


def _pilote(tmp_path, monkeypatch, abimer):
    import core.pilote
    monkeypatch.setattr(core.pilote, "RACINE", tmp_path)
    shutil.copytree(BS.DONNEES / "apprentissages", tmp_path / "apprentissages")
    shutil.copy(BS.DONNEES / "etat_banc.json", tmp_path / "etat_banc.json")
    abimer(tmp_path)
    cfg = json.loads(json.dumps(BS.CFG_DEPOT))
    cfg["journal"]["actif"] = False
    banc = Banc(cfg)
    return Pilote(banc, SimulateurBanc(banc, cfg["simulation"], cfg["modbus"],
                                       cfg["actionneurs"]), cfg)


@pytest.mark.parametrize("nom,abimer,iso,miller", [
    ("iso.json tronqué",
     lambda d: (d / "apprentissages/iso.json").write_text('{"tables": {"t', encoding="utf-8"),
     False, True),
    ("miller.json vide",
     lambda d: (d / "apprentissages/miller.json").write_text("", encoding="utf-8"),
     True, False),
    ("table ISO mal formée",
     lambda d: (d / "apprentissages/iso.json").write_text(json.dumps(
         {"tables": {"table_gains": [[1, "x"], [2]]}}), encoding="utf-8"),
     False, True),
    ("etat_banc.json tronqué",
     lambda d: (d / "etat_banc.json").write_text('{"gain', encoding="utf-8"),
     True, True),
    ("dossier apprentissages absent",
     lambda d: shutil.rmtree(d / "apprentissages"), True, False),
])
def test_fichiers_apprentissage_abimes(tmp_path, monkeypatch, caplog, nom,
                                       abimer, iso, miller):
    """Le pilote démarre toujours ; seule la table abîmée est perdue (la
    table ISO subsiste aussi dans etat_banc.json, d'où iso=True quand seul
    le dossier d'apprentissages manque)."""
    with caplog.at_level(logging.ERROR):
        p = _pilote(tmp_path, monkeypatch, abimer)
    assert p.table_gains.est_valide() is iso
    assert p.table_gains_bas_re.est_valide() is miller
    if nom != "dossier apprentissages absent" and nom != "etat_banc.json tronqué":
        assert caplog.records, "fichier abîmé non signalé"
