# -*- coding: utf-8 -*-
"""Mode séquence : parsing du CSV et suivi du profil par le pilote."""

import json
from pathlib import Path

import pytest

from core.banc import Banc
from core.pilote import Pilote
from core.sequence import charger_sequence_csv, debuts_paliers

CFG = json.loads((Path(__file__).resolve().parent.parent / "config.json")
                 .read_text(encoding="utf-8"))


# ------------------------------------------------------------- parsing CSV

def _csv(tmp_path, contenu):
    f = tmp_path / "seq.csv"
    f.write_text(contenu, encoding="utf-8")
    return f


def test_csv_point_virgule_et_virgule_decimale(tmp_path):
    pts = charger_sequence_csv(_csv(
        tmp_path, "duree_s;re_cible\n60;3000\n60,5;8000,5\n"))
    assert pts == [(60.0, 3000.0), (60.5, 8000.5)]


def test_csv_separateur_virgule(tmp_path):
    pts = charger_sequence_csv(_csv(tmp_path, "60,3000\n60,8000\n"))
    assert pts == [(60.0, 3000.0), (60.0, 8000.0)]


def test_csv_tabulation(tmp_path):
    pts = charger_sequence_csv(_csv(tmp_path, "60\t3000\n60\t8000\n"))
    assert pts == [(60.0, 3000.0), (60.0, 8000.0)]


def test_csv_bom_commentaires_lignes_vides(tmp_path):
    f = tmp_path / "seq.csv"
    f.write_text("# profil de test\n\n10;100\n10;200\n", encoding="utf-8-sig")
    assert charger_sequence_csv(f) == [(10.0, 100.0), (10.0, 200.0)]


def test_csv_durees_dans_l_ordre_du_fichier(tmp_path):
    """Les durées ne sont pas des instants : elles n'ont pas à croître."""
    pts = charger_sequence_csv(_csv(tmp_path, "120;100\n10;200\n60;300\n"))
    assert pts == [(120.0, 100.0), (10.0, 200.0), (60.0, 300.0)]
    assert debuts_paliers(pts) == ([0.0, 120.0, 130.0], 190.0)


def test_csv_duree_nulle_refusee(tmp_path):
    """« 0 ; Re » : durée nulle, typique d'un ancien profil (instants)."""
    with pytest.raises(ValueError, match="ligne 1 : durée nulle.*DURÉE"):
        charger_sequence_csv(_csv(tmp_path, "0;3000\n60;6000\n"))


def test_csv_ancien_en_tete_temps_refuse(tmp_path):
    with pytest.raises(ValueError, match="ancien profil"):
        charger_sequence_csv(_csv(tmp_path, "temps_s;re_cible\n10;3000\n"))


def test_csv_une_seule_colonne(tmp_path):
    with pytest.raises(ValueError, match="2 colonnes"):
        charger_sequence_csv(_csv(tmp_path, "10\n20\n"))


def test_csv_valeur_negative(tmp_path):
    with pytest.raises(ValueError, match="négatives"):
        charger_sequence_csv(_csv(tmp_path, "10;-100\n"))


def test_csv_vide(tmp_path):
    with pytest.raises(ValueError, match="aucun point"):
        charger_sequence_csv(_csv(tmp_path, "duree;re\n"))


def test_csv_non_numerique_apres_donnees(tmp_path):
    with pytest.raises(ValueError, match="non numériques"):
        charger_sequence_csv(_csv(tmp_path, "10;100\nabc;def\n"))


# ------------------------------------------------------- suivi par le pilote

class _ES:
    def __init__(self):
        self.mesures = {"temperature": 20.0}

    def lire_mesures(self):
        return dict(self.mesures)

    def ecrire_variateur(self, volts):
        pass

    def ecrire_variateur_run(self, marche):
        pass

    def ecrire_vanne_diaphragme(self, vers_iso):
        pass

    def ecrire_vanne_bypass(self, ouverte):
        pass

    def ecrire_vanne_perte_charge(self, volts):
        pass

    def fermer(self):
        pass


PROFIL = [(60.0, 3000.0), (60.0, 8000.0)]        # (durée, Re)


@pytest.fixture
def pilote():
    return Pilote(Banc(CFG), _ES(), CFG)


def test_sequence_vide(pilote):
    assert pilote.demarrer_sequence([], "conduite") is not None


def test_sequence_duree_nulle_refusee(pilote):
    assert "invalide" in pilote.demarrer_sequence([(0.0, 3000.0)], "conduite")
    assert not pilote.regulation_active


def test_sequence_applique_les_echelons(pilote):
    assert pilote.demarrer_sequence(PROFIL, "conduite") is None
    pilote._cycle()
    assert pilote.re_cible == 3000.0
    assert pilote.etat["sequence"]["etape"] == 1
    assert pilote.etat["sequence"]["terminee"] is False

    pilote._sequence["t0"] -= 61.0        # simule 61 s écoulées
    pilote._cycle()
    assert pilote.re_cible == 8000.0
    assert pilote.etat["sequence"]["etape"] == 2


def test_sequence_dernier_palier_dure_puis_arret_complet(pilote):
    """Constaté au banc : le dernier palier commençait à la fin de la
    séquence (durée nulle, absent du tracé), puis était maintenu sans fin.
    Il dure désormais sa durée, puis le banc s'arrête (comme ARRÊT)."""
    pilote.demarrer_sequence(PROFIL, "conduite")
    pilote._sequence["t0"] -= 100.0       # dans le dernier palier
    pilote._cycle()
    assert pilote.re_cible == 8000.0 and pilote.regulation_active
    assert pilote.etat["sequence"]["etape"] == 2
    assert pilote.etat["sequence"]["terminee"] is False
    arrets = pilote.arrets
    pilote._sequence["t0"] -= 21.0        # 121 s : fin du dernier palier
    pilote._cycle()
    assert pilote.etat["sequence"]["terminee"] is True
    assert pilote.etat["sequence"]["t_fin"] == 120.0
    assert pilote._sequence is None
    assert not pilote.regulation_active   # arrêt complet
    assert pilote.arrets == arrets + 1
    assert pilote.etat["u_variateur"] == 0.0


def test_arret_annule_la_sequence(pilote):
    pilote.demarrer_sequence(PROFIL, "conduite")
    pilote.arreter_regulation()
    assert pilote._sequence is None
    assert pilote.etat["sequence"] is None
    assert pilote.regulation_active is False


def test_mode_manuel_annule_la_sequence(pilote):
    pilote.demarrer_sequence(PROFIL, "conduite")
    pilote.mode_manuel_tension(2.0)
    assert pilote._sequence is None


def test_consigne_fixe_annule_la_sequence(pilote):
    pilote.demarrer_sequence(PROFIL, "conduite")
    pilote.definir_consigne(5000.0, "conduite")
    pilote.demarrer_regulation()
    assert pilote._sequence is None
    assert pilote.re_cible == 5000.0


def test_sequence_utilise_la_reference(pilote):
    pilote.demarrer_sequence(PROFIL, "veine")
    assert pilote.reference == "veine"


def test_virgule_separateur_et_decimale_refusee(tmp_path):
    """« 10,5,3000 » était lu en silence t = 10 s, Re = 5."""
    f = tmp_path / "s.csv"
    f.write_text("0,5,3000\n10,5,4000\n", encoding="utf-8")
    with pytest.raises(ValueError, match="ambigu"):
        charger_sequence_csv(f)


def test_seule_la_premiere_ligne_peut_etre_un_en_tete(tmp_path):
    """Une faute de frappe sur la première ligne de DONNÉES après l'en-tête
    était ignorée comme un second en-tête, et le profil commençait plus tard."""
    f = tmp_path / "s.csv"
    f.write_text("duree;Re\n10;3O00\n10;4000\n", encoding="utf-8")
    with pytest.raises(ValueError, match="ligne 2"):
        charger_sequence_csv(f)


def test_arreter_la_sequence_fige_la_consigne(pilote):
    """« ■ Arrêter » de l'onglet Séquence : le profil ne défile plus, mais
    la consigne courante reste régulée (ventilateur non coupé)."""
    pilote.demarrer_sequence([(60.0, 3000.0), (60.0, 6000.0)], "conduite")
    assert pilote.figer_sequence() == 3000.0
    assert pilote.regulation_active
    assert pilote.re_cible == 3000.0
    assert pilote.etat["sequence"] is None
    assert pilote.figer_sequence() is None          # plus de séquence


def test_exemple_livre_est_valide():
    chemin = Path(__file__).resolve().parent.parent / "exemple_sequence.csv"
    paliers = charger_sequence_csv(chemin)
    assert paliers == [(60.0, 3000.0), (60.0, 6000.0), (180.0, 10000.0),
                       (60.0, 4000.0)]
    assert debuts_paliers(paliers)[1] == 360.0
