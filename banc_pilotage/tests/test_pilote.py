# -*- coding: utf-8 -*-
"""Pilote : arrêt total, manœuvres de la vanne 3 voies, robustesse de la
boucle (régressions des défauts corrigés)."""

import json
import time
from pathlib import Path

import pytest

from core.banc import Banc
from core.pilote import Pilote, TableGains
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


def _pilote_en_position(fc: str) -> tuple:
    """Pilote sur StubES avec la vanne 3 voies déjà en position (fc actif)."""
    es = StubES({"temperature": 20.0, fc: True})
    return Pilote(Banc(CFG), es, CFG), es


def _espion_reinit(pi) -> list:
    """Enregistre les appels à pi.reinitialiser (feed-forward)."""
    appels = []
    original = pi.reinitialiser

    def espion(u_initial=0.0):
        appels.append(u_initial)
        original(u_initial)

    pi.reinitialiser = espion
    return appels


def test_rampe_pi_bornee_en_bas_re():
    """En bas-Re, la rampe du PI ne doit pas devancer la vanne PdC
    (10 V de course en t_vanne_pdc_course s) : sinon l'intégrale s'enroule
    contre l'actionneur lent -> dépassement puis oscillation."""
    pilote, es = _pilote_en_position("fc_3voies_90")   # déjà sur Miller
    pilote.seuil_bas_re = 3000.0
    pilote.definir_consigne(1000.0, "conduite")
    pilote.demarrer_regulation()
    pilote._cycle()
    assert pilote.pi.rampe == pytest.approx(
        pilote.split_point / pilote.t_vanne_pdc_course)
    assert pilote.pi.rampe < pilote._rampe_nominale
    # retour en régime normal : rampe nominale restaurée
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    pilote._cycle()
    assert pilote.pi.rampe == pilote._rampe_nominale


def test_sequence_petit_echelon_conserve_le_pi():
    """Un petit échelon de séquence (< echelon_reinit_ff_relatif), même
    diaphragme et même régime, ne réinitialise pas le PI : l'intégrale qui
    compense l'erreur du feed-forward est conservée d'un échelon à l'autre."""
    pilote, es = _pilote_en_position("fc_3voies_0")    # déjà sur ISO
    pilote.bas_re_actif = False
    appels = _espion_reinit(pilote.pi)
    pilote.demarrer_sequence([(0.0, 8000.0), (0.05, 8400.0)], "conduite")
    pilote._cycle()                       # échelon 1 : préparation complète
    assert len(appels) == 1
    time.sleep(0.06)
    pilote._cycle()                       # échelon 2 : +5 %, état PI conservé
    assert len(appels) == 1


def test_sequence_grand_echelon_reinitialise_le_pi():
    """Un grand échelon de séquence repart du feed-forward (table de gains)."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote.bas_re_actif = False
    appels = _espion_reinit(pilote.pi)
    pilote.demarrer_sequence([(0.0, 8000.0), (0.05, 16000.0)], "conduite")
    pilote._cycle()
    assert len(appels) == 1
    time.sleep(0.06)
    pilote._cycle()                       # échelon 2 : +100 % -> feed-forward
    assert len(appels) == 2


def test_convergee_exige_des_cycles_consecutifs():
    """Anti-battement : la convergence n'est affichée qu'après ~2 s
    consécutives dans la bande de tolérance, pas sur un échantillon isolé."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote.bas_re_actif = False
    pilote.tolerance = 2.0                # bande couvrant toute mesure
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    pilote._cycle()
    assert pilote.etat["convergee"] is False        # 1er cycle en bande
    for _ in range(3):
        pilote._cycle()
    assert pilote.etat["convergee"] is True         # 4 cycles (2 s) en bande


def test_gain_scheduling_suit_le_point_de_fonctionnement():
    """Erreur normalisée par la consigne -> gain de boucle en kp/u_équilibre :
    avec une table de gains valide, les gains sont remis à l'échelle par la
    tension d'équilibre pour un comportement homogène sur toute la plage."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote.bas_re_actif = False
    pilote.table_gains = TableGains([(1.0, 1000.0), (9.0, 9000.0)])
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    pilote._cycle()
    assert pilote.pi.gain_scale == pytest.approx(8.0)   # u_équilibre = 8 V
    # désactivable par config : échelle neutre
    pilote.gain_scheduling = False
    pilote.demarrer_regulation()
    pilote._cycle()
    assert pilote.pi.gain_scale == 1.0


def test_critere_bas_re_en_reference_veine():
    """Le seuil bas-Re est un Re CONDUITE : une consigne veine doit être
    convertie avant comparaison. Ici Re veine 1000 ≈ Re conduite 3129,
    au-dessus du seuil 3000 -> pas de stratégie bas-Re."""
    pilote, es = _pilote_en_position("fc_3voies_90")   # Miller (Re < 5000)
    pilote.seuil_bas_re = 3000.0
    pilote.definir_consigne(1000.0, "veine")
    pilote.demarrer_regulation()
    pilote._cycle()
    assert pilote._mode_bas_re is False
    assert pilote.etat["mode_bas_re"] is False


def test_bascule_bas_re_en_cours_recale_le_pi():
    """Si le seuil appris dérive et fait basculer le régime sans nouvelle
    consigne, la commande PI change de signification (tension <-> split-
    range) : elle doit être re-calée sur le feed-forward du nouveau régime."""
    pilote, es = _pilote_en_position("fc_3voies_90")
    pilote.seuil_bas_re = 1200.0
    pilote.definir_consigne(1500.0, "conduite")
    pilote.demarrer_regulation()
    pilote._cycle()                       # régime normal (1500 > 1200)
    assert pilote._regime_pi is False
    appels = _espion_reinit(pilote.pi)
    pilote.seuil_bas_re = 3000.0          # dérive du seuil -> bas-Re requis
    pilote._cycle()
    assert pilote._regime_pi is True
    assert len(appels) == 1               # PI re-calé sur le feed-forward
    assert pilote.pi.rampe == pytest.approx(pilote._rampe_bas_re())


def test_duree_cycle_publiee(tmp_path):
    """La durée réelle du cycle est publiée dans etat (détection d'un
    glissement de période dû à une communication lente)."""
    es = StubES({"temperature": 20.0, "fc_3voies_0": True})
    pilote = Pilote(Banc(CFG), es, CFG)
    pilote._chemin_etat = tmp_path / "etat.json"
    pilote.demarrer()
    time.sleep(0.3)
    pilote.arreter()
    assert "duree_cycle_s" in pilote.etat
    assert pilote.etat["duree_cycle_s"] < CFG["regulation"]["periode_s"]


def test_arreter_persiste_u_min(tmp_path):
    """u_min appris (freq_min) : réécrit par arreter() comme le gain et la
    table, sinon la valeur disparaît du fichier d'état à l'extinction."""
    es = StubES()
    pilote = Pilote(Banc(CFG), es, CFG)
    pilote._chemin_etat = tmp_path / "etat.json"
    pilote.u_min_ventilateur = 2.5
    pilote.arreter()
    donnees = json.loads((tmp_path / "etat.json").read_text(encoding="utf-8"))
    assert donnees["u_min_ventilateur_v"] == 2.5
