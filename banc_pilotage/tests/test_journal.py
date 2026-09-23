# -*- coding: utf-8 -*-
"""Journal CSV des conditions d'essai.

L'opérateur pose une consigne, attend la stabilisation, puis quitte l'IHM
pour mesurer sur le banc. Le journal est ce qui lui permet, au dépouillement,
de dire dans quelles conditions un point a été relevé et si elles ont tenu.
"""

import json
from pathlib import Path

import pytest

from core.banc import Banc
from core.journal import COLONNES, JournalEssai
from core.pilote import Pilote

CFG = json.loads((Path(__file__).resolve().parent.parent / "config.json")
                 .read_text(encoding="utf-8"))


def _etat(**kw) -> dict:
    base = {"re_cible": 6000.0, "re_mesure": 5987.3, "re_moyen": 5998.1,
            "re_ecart_type": 28.4, "convergee": True, "dp": 204.15,
            "temperature": 21.7, "p_amont": 25.0, "rho": 1.1982,
            "qm": 0.0218, "u_variateur": 5.16, "pdc_position": 10.0,
            "diaphragme": "iso", "reference": "veine", "mode_bas_re": False,
            "dp_sature": False}
    base.update(kw)
    return base


def test_entete_et_ligne(tmp_path):
    j = JournalEssai(tmp_path, periode_s=0.0)
    j.ouvrir(100.0)
    j.enregistrer(_etat(), 100.0)
    j.fermer()

    lignes = j.chemin.read_text(encoding="utf-8-sig").splitlines()
    assert len(lignes) == 2
    entete = lignes[0].split(";")
    assert entete == [e for _, e in COLONNES]
    champs = dict(zip(entete, lignes[1].split(";")))
    # virgule décimale et séparateur « ; » : lecture directe dans Excel FR,
    # même convention qu'en lecture de séquence (core/sequence.py)
    assert champs["T [degC]"] == "21,7"
    assert champs["convergee"] == "1"
    # Reynolds arrondis a l'unite : les decimales d'un nombre sans dimension
    # de quelques milliers n'ont pas de sens physique. Le pilote, lui, garde
    # ses flottants.
    assert champs["Re consigne"] == "6000"
    assert champs["Re mesure"] == "5987"        # 5987,3
    assert champs["Re moyen"] == "5998"         # 5998,1
    assert champs["Re ecart-type"] == "28"      # 28,4
    for col in ("Re consigne", "Re mesure", "Re moyen", "Re ecart-type"):
        assert "," not in champs[col], col
    # les autres colonnes gardent leurs decimales
    assert champs["dP [Pa]"] == "204,15"
    assert champs["diaphragme"] == "iso"
    assert champs["defauts"] == ""


def test_periode_respectee(tmp_path):
    j = JournalEssai(tmp_path, periode_s=1.0)
    j.ouvrir(0.0)
    for t in (0.0, 0.2, 0.4, 0.6, 0.8):     # une seule ligne attendue
        j.enregistrer(_etat(), t)
    assert j.lignes == 1
    j.enregistrer(_etat(), 1.0)             # période écoulée
    assert j.lignes == 2
    j.fermer()


def test_defauts_recopies(tmp_path):
    j = JournalEssai(tmp_path, periode_s=0.0)
    j.ouvrir(0.0)
    j.enregistrer(_etat(defaut_capteur="PT100 coupée", dp_sature=True), 0.0)
    j.fermer()
    ligne = j.chemin.read_text(encoding="utf-8-sig").splitlines()[1]
    assert "PT100 coupée" in ligne
    assert ligne.split(";")[-1] != ""


def test_une_session_un_fichier(tmp_path):
    j = JournalEssai(tmp_path, periode_s=0.0)
    j.ouvrir(0.0)
    premier = j.chemin
    j.enregistrer(_etat(), 0.0)
    j.ouvrir(0.0)                           # sans effet tant qu'ouvert
    assert j.chemin == premier
    j.fermer()
    assert not j.ouvert


def test_erreur_disque_desarme_sans_lever(tmp_path):
    """Un disque plein ne doit jamais interrompre la régulation : le journal
    se désarme et publie son erreur, la boucle continue."""
    j = JournalEssai(tmp_path, periode_s=0.0)
    j.ouvrir(0.0)

    class Casse:
        def write(self, _):
            raise OSError("disque plein")

        def flush(self):
            pass

        def close(self):
            pass

    j._f = Casse()
    j.enregistrer(_etat(), 0.0)             # ne doit pas lever
    assert j.erreur is not None
    assert not j.ouvert


def test_dossier_indisponible_ne_leve_pas(tmp_path):
    fichier = tmp_path / "pas_un_dossier"
    fichier.write_text("x", encoding="utf-8")
    j = JournalEssai(fichier / "sous", periode_s=0.0)
    j.ouvrir(0.0)                           # ne doit pas lever
    assert j.erreur is not None
    assert not j.ouvert
    j.enregistrer(_etat(), 0.0)             # silencieux


class _StubES:
    def __init__(self):
        self.mesures = {"temperature": 20.0, "fc_3voies_0": True,
                        "dp_iso_500": 200.0}

    def lire_mesures(self):
        return dict(self.mesures)

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


def test_pilote_ouvre_et_ferme_le_journal(tmp_path):
    """Une session de régulation = un fichier, ouvert au démarrage et fermé
    à l'arrêt, avec le chemin publié dans l'état pour l'IHM."""
    cfg = json.loads(json.dumps(CFG))
    cfg["journal"] = {"actif": True, "dossier": str(tmp_path),
                      "periode_s": 0.0}
    pilote = Pilote(Banc(cfg), _StubES(), cfg)
    pilote.bas_re_actif = False
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    for _ in range(5):
        pilote._cycle()

    chemin = Path(pilote.etat["journal"])
    assert chemin.exists()
    assert pilote.etat["journal_lignes"] >= 5
    assert pilote.etat["journal_erreur"] is None

    pilote.arreter_regulation()
    assert not pilote.journal.ouvert
    lignes = chemin.read_text(encoding="utf-8-sig").splitlines()
    assert len(lignes) >= 6                          # en-tête + cycles
    assert "Re mesure" in lignes[0]

    # une seconde session écrit un AUTRE fichier
    pilote.demarrer_regulation()
    pilote._cycle()
    assert Path(pilote.etat["journal"]) != chemin


def test_journal_desactivable(tmp_path):
    cfg = json.loads(json.dumps(CFG))
    cfg["journal"] = {"actif": False, "dossier": str(tmp_path)}
    pilote = Pilote(Banc(cfg), _StubES(), cfg)
    assert pilote.journal is None
    pilote.bas_re_actif = False
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    pilote._cycle()                                  # ne doit pas lever
    assert not any(tmp_path.iterdir())
