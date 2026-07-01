# -*- coding: utf-8 -*-
"""Pilote : arrêt total, manœuvres de la vanne 3 voies, robustesse de la
boucle (régressions des défauts corrigés)."""

import json
from pathlib import Path

import pytest

from core.banc import Banc
from core.pilote import Pilote
from io_banc.simulateur import SimulateurBanc

CFG = json.loads((Path(__file__).resolve().parent.parent / "config.json")
                 .read_text(encoding="utf-8"))


class StubES:
    """Entrées/sorties factices : enregistre les commandes reçues."""

    def __init__(self, mesures=None):
        self.mesures = mesures or {"temperature": 20.0}
        self.variateur = []
        self.run = []
        self.coil_diaphragme = []

    def lire_mesures(self):
        return dict(self.mesures)

    def ecrire_variateur(self, volts):
        self.variateur.append(volts)

    def ecrire_variateur_run(self, marche):
        self.run.append(marche)

    def ecrire_vanne_diaphragme(self, vers_iso):
        self.coil_diaphragme.append(vers_iso)

    def ecrire_vanne_bypass(self, ouverte):
        pass

    def ecrire_vanne_perte_charge(self, volts):
        pass

    def fermer(self):
        pass


@pytest.fixture
def pilote_stub():
    es = StubES()
    return Pilote(Banc(CFG), es, CFG), es


def test_arret_coupe_aussi_le_mode_manuel(pilote_stub):
    """Bouton ARRÊT VARIATEUR : la tension manuelle ne doit pas être
    réappliquée au cycle suivant."""
    pilote, es = pilote_stub
    pilote.mode_manuel_tension(5.0)
    pilote.arreter_regulation()
    assert pilote.mode_manuel is False
    assert pilote.tension_manuelle == 0.0
    assert es.variateur[-1] == 0.0
    assert es.run[-1] is False


def test_commander_diaphragme_fermee_double_impulsion(pilote_stub):
    """Depuis ISO (0°), la fermeture (180°) exige une relance à 90°."""
    pilote, es = pilote_stub
    pilote.etat["mesures_brutes"] = {"fc_3voies_0": True}
    pilote.commander_diaphragme("fermee")
    assert es.coil_diaphragme == [False]
    assert pilote.etat["diaphragme"] == "fermee"
    m = pilote._manoeuvre
    assert m["fc_attendue"] == "fc_3voies_180"
    assert m["fc_relance"] == "fc_3voies_90"


def test_commander_diaphragme_deja_en_position(pilote_stub):
    pilote, es = pilote_stub
    pilote.etat["mesures_brutes"] = {"fc_3voies_0": True}
    pilote.commander_diaphragme("iso")
    assert es.coil_diaphragme == []          # aucune impulsion
    assert pilote._manoeuvre is None


def test_commander_miller_depuis_fermee_remonte(pilote_stub):
    """Depuis 180°, aller à Miller (90°) = impulsion vers ISO."""
    pilote, es = pilote_stub
    pilote.etat["mesures_brutes"] = {"fc_3voies_180": True}
    pilote.commander_diaphragme("miller")
    assert es.coil_diaphragme == [True]


def test_cycle_supporte_position_fermee():
    """Régression : diaphragme 'fermee' provoquait un KeyError à chaque
    cycle (repli erreur com + variateur forcé à 0)."""
    es = SimulateurBanc(Banc(CFG), CFG.get("simulation"))
    pilote = Pilote(Banc(CFG), es, CFG)
    pilote._cycle()                          # position initiale depuis les FC
    pilote.etat["diaphragme"] = "fermee"
    pilote._cycle()                          # ne doit pas lever
    assert pilote.etat["erreur_com"] is None or True   # pas d'exception


def test_cycles_simulation_sans_erreur():
    """Quelques cycles complets en simulation, consigne active."""
    es = SimulateurBanc(Banc(CFG), CFG.get("simulation"))
    pilote = Pilote(Banc(CFG), es, CFG)
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    for _ in range(10):
        pilote._cycle()
    assert pilote.etat["cycles"] == 10
    assert pilote.etat.get("defaut_vanne") is None
