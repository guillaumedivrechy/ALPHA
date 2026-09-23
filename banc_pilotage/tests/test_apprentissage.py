# -*- coding: utf-8 -*-
"""Apprentissages : zéro capteurs, contrôle de câblage (mapping ISO/Miller),
garde-fous d'ordre (indiciel après courbe de gain)."""

import json
from pathlib import Path

import pytest

from core.apprentissage import Apprentissage
from core.banc import Banc
from core.pilote import Pilote, TableGains

CHEMIN_CFG = Path(__file__).resolve().parent.parent / "config.json"


def _cfg():
    return json.loads(CHEMIN_CFG.read_text(encoding="utf-8"))


class StubES:
    def lire_mesures(self):
        return {"temperature": 20.0}

    def ecrire_variateur(self, volts):
        pass

    def ecrire_variateur_run(self, marche):
        pass

    def ecrire_vanne_diaphragme(self, vers_0deg):
        pass

    def ecrire_vanne_bypass(self, ouverte):
        pass

    def ecrire_vanne_perte_charge(self, volts):
        pass

    def fermer(self):
        pass


def _app(tmp_path, brutes=None):
    cfg = _cfg()
    pilote = Pilote(Banc(cfg), StubES(), cfg)
    pilote._chemin_etat = tmp_path / "etat.json"
    pilote.periode = 0.02                  # accélère les boucles de mesure
    if brutes is not None:
        pilote.etat["mesures_brutes"] = brutes
    return Apprentissage(pilote, cfg, tmp_path / "config.json"), pilote


def test_zero_capteurs_mesure_et_applique(tmp_path):
    app, pilote = _app(tmp_path, {"dp_diaph_500": 1.2, "dp_iso_500": -0.4})
    app._test_zero_capteurs(attente_s=0.0, duree_s=0.2)
    z = app.resultats["zero_capteurs"]
    assert z["dp_diaph_500"] == pytest.approx(1.2)
    assert z["dp_iso_500"] == pytest.approx(-0.4)
    app.appliquer()
    assert pilote.zero_capteurs["dp_diaph_500"] == pytest.approx(1.2)
    cfg_relu = json.loads((tmp_path / "config.json")
                          .read_text(encoding="utf-8"))
    assert cfg_relu["zero_capteurs"]["dp_iso_500"] == pytest.approx(-0.4)


def test_zero_capteurs_refuse_offset_aberrant(tmp_path):
    """Un 'zéro' de dizaines de Pa signale un écoulement résiduel ou un
    capteur en défaut : ne pas l'enregistrer comme correction."""
    app, _ = _app(tmp_path, {"dp_diaph_500": 60.0, "dp_iso_500": 0.1})
    app._test_zero_capteurs(attente_s=0.0, duree_s=0.2)
    assert "zero_capteurs" not in app.resultats
    assert "anormalement" in app.etat["statut"]


def test_appliquer_intervertit_mapping_iso_miller(tmp_path):
    app, pilote = _app(tmp_path)
    app.resultats["verif_cablage"] = {"mapping_inverse": True,
                                      "resume": "mapping inversé"}
    app.appliquer()
    # l'ambiguite ne porte plus sur les fins de course (Miller n'en a plus
    # en propre) mais sur la plomberie : quelle butee alimente quel diaphragme
    pos = app.cfg["modbus"]["positions_vanne_3voies"]
    assert pos["diaphragme_a_0deg"] == "miller"
    assert pos["diaphragme_a_90deg"] == "iso"
    assert pilote._angle_par_diaphragme["iso"] == "90"
    assert pilote._diaphragme_par_angle["0"] == "miller"
    assert pilote._position_initiale_lue is False   # position relue aux FC


def test_indiciel_exige_une_table_de_gains(tmp_path):
    """Les kp/ki IMC supposent le gain scheduling actif : sans table de
    gains, ils ne vaudraient qu'au point d'essai -> refus explicite."""
    app, pilote = _app(tmp_path)
    pilote.gain_scheduling = True
    pilote.table_gains = TableGains()      # invalide
    err = app.demarrer("indiciel")
    assert err is not None and "courbe de gain" in err


def test_capteur_dominant():
    assert Apprentissage._capteur_dominant(40.0, 2.0) == "iso"
    assert Apprentissage._capteur_dominant(1.5, 30.0) == "miller"
    assert Apprentissage._capteur_dominant(1.0, 2.0) is None      # trop faible
    assert Apprentissage._capteur_dominant(20.0, 15.0) is None    # ambigu
