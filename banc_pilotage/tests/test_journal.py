# -*- coding: utf-8 -*-
"""Journal CSV des conditions d'essai.

L'opérateur pose une consigne, attend la stabilisation, puis quitte l'IHM
pour mesurer sur le banc. Le journal est ce qui lui permet, au dépouillement,
de dire dans quelles conditions un point a été relevé et si elles ont tenu.
"""

import json
import time
from pathlib import Path


from core.banc import Banc
from core.journal import COLONNES, JournalEssai, entetes
from core.pilote import Pilote
from banc_simule import sans_sht45  # noqa: E402

CFG = sans_sht45(json.loads(
    (Path(__file__).resolve().parent.parent / "config.json")
    .read_text(encoding="utf-8")))


def _etat(**kw) -> dict:
    base = {"re_cible": 6000.0, "re_mesure": 5987.3, "re_moyen": 5998.1,
            "re_ecart_type": 28.4, "convergee": True, "dp": 204.15,
            "temperature": 21.7, "temperature_source": "pt100",
            "p_amont": 25.0, "rho": 1.1982, "mu": 1.81e-05, "nu": 1.51e-05,
            "qm": 0.0218, "qv": 0.0182, "u_variateur": 5.16,
            "pdc_consigne": 10.0, "diaphragme_lu": "iso",
            "phase_regulation": "suivi consigne",
            "mesures_brutes": {"dp_iso_500": 204.45, "dp_diaph_500": 0.12},
            "reference": "veine", "mode_bas_re": False, "dp_sature": False}
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
    assert champs["Convergence"] == "1"
    # Reynolds arrondis a l'unite : les decimales d'un nombre sans dimension
    # de quelques milliers n'ont pas de sens physique. Le pilote, lui, garde
    # ses flottants.
    assert champs["Re consigne"] == "6000"
    assert champs["Re calculé"] == "5987"        # 5987,3
    # les autres colonnes gardent leurs decimales
    assert champs["Qv [m3/s]"] == "0,0182"
    assert champs["ρ [kg/m3]"] == "1,1982"
    assert champs["µ [Pa.s]"] == "1,81e-05"
    assert champs["phase regulation"] == "suivi consigne"
    assert champs["source T"] == "pt100"
    assert champs["Vanne ISO"] == "1" and champs["Vanne Miller"] == "0"
    assert champs["Defaut"] == ""


def test_format_demande_par_l_exploitation(tmp_path):
    """Colonnes et ordre demandés (2026-11-02), ΔP des deux capteurs
    corrigés de leur zéro, vanne PdC en V et % de fermeture, humidité de la
    configuration, horodatage à la milliseconde."""
    assert [e for _, e in COLONNES] == [
        "horodatage", "t [s]", "Re consigne", "Re calculé", "Convergence",
        "phase regulation", "Qv [m3/s]", "ρ [kg/m3]", "µ [Pa.s]",
        "ν [m2/s]", "dP ISO [Pa]", "dP Miller [Pa]", "P Amont [Pa]",
        "Vanne PDC [V]", "Vanne PDC [% ferm]", "Vanne ISO", "Vanne Miller",
        "T [degC]", "source T", "H [%HR]", "source H", "T PT100 [degC]",
        "T SHT45 [degC]", "Defaut",
        "FC 3 voies 0°", "FC 3 voies 90°", "FC 3 voies 180°",
        "FC Bypass ouvert", "FC Bypass fermé", "FC PdC ouverte",
        "FC PdC fermée"]
    j = JournalEssai(tmp_path, periode_s=0.0)
    j.ouvrir(100.0)
    brutes = {"dp_iso_500": 204.45, "dp_diaph_500": 0.12, "pdc_recopie": 7.462}
    j.enregistrer(_etat(mesures_brutes=brutes, pdc_consigne=7.5), 100.4,
                  zero_capteurs={"dp_iso_500": 0.3, "dp_diaph_500": 0.1},
                  humidite_relative=50.0, recopie_pdc=(2.389, 9.153))
    j.fermer()
    entete, ligne = (l.split(";") for l in
                     j.chemin.read_text(encoding="utf-8-sig").splitlines())
    c = dict(zip(entete, ligne))
    assert c["dP ISO [Pa]"] == "204,15"          # 204,45 − 0,3
    assert c["dP Miller [Pa]"] == "0,02"         # 0,12 − 0,1
    # recopie mesurée, sur l'échelle apprise (2,389 V fermée, 9,153 V
    # ouverte) : (9,153 − 7,462) / 6,764 = 25 % de fermeture
    assert c["Vanne PDC [V]"] == "7,462"
    assert c["Vanne PDC [% ferm]"] == "25"
    assert c["H [%HR]"] == "50"
    assert c["t [s]"] == "0,4"
    assert len(c["horodatage"].split(".")[-1]) == 3   # millisecondes


def test_une_ligne_par_cycle_a_200_ms(tmp_path):
    """Période du journal égale à celle de la boucle (0,2 s) : un cycle
    arrivé à 0,199 s n'est pas sauté (avant : une ligne sur deux)."""
    j = JournalEssai(tmp_path, periode_s=0.2)
    j.ouvrir(0.0)
    t = 0.0
    for dt in (0.0, 0.199, 0.201, 0.199, 0.2, 0.198):
        t += dt
        j.enregistrer(_etat(), t)
    assert j.lignes == 6
    j.fermer()


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
    entete, ligne = j.chemin.read_text(encoding="utf-8-sig").splitlines()
    assert "PT100 coupée" in ligne
    assert dict(zip(entete.split(";"), ligne.split(";")))["Defaut"] != ""


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


def _pilote_journalisant(tmp_path, periode_journal=0.0):
    cfg = json.loads(json.dumps(CFG))
    cfg["journal"] = {"actif": True, "dossier": str(tmp_path),
                      "periode_s": periode_journal}
    cfg["regulation"]["periode_s"] = 0.05           # essais rapides
    pilote = Pilote(Banc(cfg), _StubES(), cfg)
    pilote.bas_re_actif = False
    return pilote


def test_pilote_ouvre_et_ferme_le_journal(tmp_path):
    """Une session de régulation = un fichier, ouvert au démarrage et fermé
    à l'arrêt, avec le chemin publié dans l'état pour l'IHM.

    Passe par la vraie boucle : le journal est écrit dans _boucle, pas dans
    _cycle, précisément pour survivre aux sorties anticipées de celui-ci."""
    pilote = _pilote_journalisant(tmp_path)
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    pilote.demarrer()
    try:
        time.sleep(0.6)
        chemin = Path(pilote.etat["journal"])
        assert chemin.exists()
        assert pilote.etat["journal_lignes"] >= 5
        assert pilote.etat["journal_erreur"] is None

        pilote.arreter_regulation()
        assert not pilote.journal.ouvert
        lignes = chemin.read_text(encoding="utf-8-sig").splitlines()
        assert len(lignes) >= 6                      # en-tête + cycles
        assert "Re calculé" in lignes[0]

        # une seconde session écrit un AUTRE fichier
        pilote.demarrer_regulation()
        time.sleep(0.3)
        assert Path(pilote.etat["journal"]) != chemin
    finally:
        pilote.arreter()


def test_journal_enregistre_pendant_manoeuvre_et_sur_defaut(tmp_path):
    """RÉGRESSION : le journal était écrit en fin de _cycle, donc après les
    cinq sorties anticipées. Il se taisait pendant les manœuvres de vannes
    et sur défaut — exactement les moments que l'opérateur cherchera au
    dépouillement — et sa colonne « defauts » ne pouvait jamais se remplir."""
    pilote = _pilote_journalisant(tmp_path)
    es = pilote.es
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    pilote.demarrer()
    try:
        time.sleep(0.3)
        # une manœuvre gèle la boucle et fait sortir _cycle par le haut
        pilote._demarrer_manoeuvre("essai", t_course=1.0, angle_attendu="90")
        n_avant = pilote.etat["journal_lignes"]
        time.sleep(0.5)
        assert pilote.etat["journal_lignes"] > n_avant,             "rien n'a été journalisé pendant la manœuvre"

        # puis un défaut capteur, autre sortie anticipée. Le défaut n'est
        # levé qu'après ~2 s de persistance (garde anti-transitoire), et le
        # chien de garde de la manœuvre ci-dessus a lui aussi pu poser un
        # défaut vanne : les deux sont des sorties anticipées de _cycle.
        es.mesures["temperature"] = 3276.7
        time.sleep(3.0)
        chemin = Path(pilote.etat["journal"])
        lignes = chemin.read_text(encoding="utf-8-sig").splitlines()
        colonnes = lignes[0].split(";")
        i_def = colonnes.index("Defaut")
        motifs = [l.split(";")[i_def] for l in lignes[1:]
                  if l.split(";")[i_def]]
        assert motifs, "la colonne « defauts » n'est jamais renseignée"
        assert any("température" in m or "vanne" in m or "butée" in m
                   for m in motifs), motifs[:3]
    finally:
        pilote.arreter()


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


def test_mesures_vides_pendant_la_course_de_la_vanne_3_voies(tmp_path):
    """Vanne 3 voies en course : le débit passe par les deux voies, aucun
    capteur ne le mesure — colonnes de mesure vides, diaphragme « en cours »
    (avant : le Re du capteur de la dernière butée, sans valeur physique)."""
    j = JournalEssai(tmp_path, periode_s=0.0)
    j.ouvrir(100.0)
    j.enregistrer(dict(_etat(), diaphragme_lu="en_transit"), 100.0)
    j.enregistrer(dict(_etat(), diaphragme_lu="miller",
                       diaphragme_mesure="miller"), 101.0)
    j.fermer()
    entete, en_course, arrivee = (l.split(";") for l in
                                  j.chemin.read_text(encoding="utf-8-sig").splitlines())
    c, a = dict(zip(entete, en_course)), dict(zip(entete, arrivee))
    for col in ("Re calculé", "Qv [m3/s]"):
        assert c[col] == "", col
    assert c["Vanne ISO"] == "0" and c["Vanne Miller"] == "0"
    assert c["Re consigne"] == "6000"
    assert a["Re calculé"] == "5987" and a["Vanne Miller"] == "1"


def test_vanne_pdc_sans_recopie_sur_l_echelle_apprise(tmp_path):
    """Recopie non lue : la commande 0-10 V est reportée sur l'échelle de
    la calibration, pas écrite telle quelle (0 et 10 V)."""
    j = JournalEssai(tmp_path, periode_s=0.0)
    j.ouvrir(0.0)
    for commande in (10.0, 0.0):
        j.enregistrer(_etat(pdc_consigne=commande), 0.0,
                      recopie_pdc=(2.389, 9.153))
    j.fermer()
    entete, ouverte, fermee = (l.split(";") for l in
                               j.chemin.read_text(encoding="utf-8-sig").splitlines())
    o, f = dict(zip(entete, ouverte)), dict(zip(entete, fermee))
    assert o["Vanne PDC [V]"] == "9,153" and o["Vanne PDC [% ferm]"] == "0"
    assert f["Vanne PDC [V]"] == "2,389" and f["Vanne PDC [% ferm]"] == "100"


def test_fins_de_course_en_dernieres_colonnes(tmp_path):
    """Les 7 fins de course, 1 = contact actif ; les butées de la vanne 3
    voies nomment le diaphragme qu'elles mettent en ligne, d'après la
    configuration (plomberie croisée comprise)."""
    j = JournalEssai(tmp_path, periode_s=0.0,
                     diaphragme_par_angle={"0": "miller", "90": "iso"})
    j.ouvrir(0.0)
    brutes = {"fc_3voies_0": True, "fc_3voies_90": False,
              "fc_3voies_180": False, "fc_bypass_ouvert": True,
              "fc_bypass_ferme": False, "fc_pdc_ouvert": False,
              "fc_pdc_ferme": True}
    j.enregistrer(_etat(mesures_brutes=brutes), 0.0)
    j.fermer()
    entete, ligne = (l.split(";") for l in
                     j.chemin.read_text(encoding="utf-8-sig").splitlines())
    assert entete[-7:] == ["FC 3 voies 0° (Miller)", "FC 3 voies 90° (ISO)",
                           "FC 3 voies 180°", "FC Bypass ouvert",
                           "FC Bypass fermé", "FC PdC ouverte",
                           "FC PdC fermée"]
    assert ligne[-7:] == ["1", "0", "0", "1", "0", "0", "1"]
    assert entetes({"0": "iso", "90": "miller"})[-7:-5] == [
        "FC 3 voies 0° (ISO)", "FC 3 voies 90° (Miller)"]


def test_colonnes_du_capteur_d_air(tmp_path):
    """SHT45 : humidité utilisée et sa source, lectures des deux sondes ;
    SHT45 perdu, l'alerte figure dans la colonne Defaut."""
    j = JournalEssai(tmp_path, periode_s=0.0)
    j.ouvrir(0.0)
    j.enregistrer(_etat(humidite=37.4, humidite_source="sht45",
                        temperature_pt100=26.1, temperature_sht45=21.3), 0.2,
                  humidite_relative=50.0)
    j.enregistrer(_etat(humidite=50.0, humidite_source="manuelle",
                        temperature_pt100=26.1, temperature_sht45=None,
                        alerte_sht45="SHT45 indisponible"), 0.4)
    j.fermer()
    entete, l1, l2 = (l.split(";") for l in
                      j.chemin.read_text(encoding="utf-8-sig").splitlines())
    c1, c2 = dict(zip(entete, l1)), dict(zip(entete, l2))
    assert (c1["H [%HR]"], c1["source H"]) == ("37,4", "sht45")
    assert (c1["T PT100 [degC]"], c1["T SHT45 [degC]"]) == ("26,1", "21,3")
    assert c1["Defaut"] == ""
    assert (c2["source H"], c2["T SHT45 [degC]"]) == ("manuelle", "")
    assert c2["Defaut"] == "SHT45 indisponible"
