# -*- coding: utf-8 -*-
"""IHM réelle (Qt hors écran) branchée sur le banc simulé à horloge
accélérée : parcours opérateur scriptés et clics aléatoires.

Chaque pas avance la boucle du pilote puis rafraîchit l'IHM (_tick), comme
le QTimer. Les boîtes de dialogue sont interceptées et enregistrées. Échoue
sur : exception dans un gestionnaire de clic, section d'affichage en défaut
(« Affichage … indisponible »), violation des invariants du banc simulé."""

import json
import os
import random

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from banc_simule import BancSimule              # noqa: E402

QApplication, QMessageBox = QtWidgets.QApplication, QtWidgets.QMessageBox


class IHMSimulee:
    def __init__(self, monkeypatch, tmp_path, caracterise=True):
        self.dialogues: list[tuple] = []
        self._intercepter(monkeypatch)
        self.qa = QApplication.instance() or QApplication([])
        self.b = BancSimule(monkeypatch, tmp_path, caracterise=caracterise)
        import json
        import gui.app as gapp
        chemin = tmp_path / "config.json"
        chemin.write_text(json.dumps(self.b.cfg), encoding="utf-8")
        self.app = gapp.Application(self.b.p, "simulation", self.b.cfg, chemin)
        self.app._timer.stop()                  # rafraîchi par avancer()
        self.qa.processEvents()

    def _intercepter(self, monkeypatch):
        def capt(genre, retour=None):
            def f(parent, titre, texte, *a, **k):
                self.dialogues.append((genre, titre, texte))
                return retour
            return f
        for genre in ("critical", "warning", "information"):
            monkeypatch.setattr(QMessageBox, genre, capt(genre))
        monkeypatch.setattr(QMessageBox, "question",
                            capt("question", QMessageBox.StandardButton.Yes))

    def avancer(self, duree_s: float) -> None:
        p = self.b.p
        n = max(1, int(round(duree_s / p.periode)))
        for i in range(n):
            self.b.cycles(p.periode)
            if i % 1 == 0:
                self.app._tick()
        self.qa.processEvents()

    def sections_en_defaut(self) -> list:
        return sorted(self.app._sections_en_defaut)

    @property
    def journal(self) -> list[str]:
        return [e["msg"] for e in self.app._action_log]


@pytest.fixture
def ihm(monkeypatch, tmp_path):
    i = IHMSimulee(monkeypatch, tmp_path)
    yield i
    i.app._timer.stop()
    i.app.deleteLater()


def _saisir(champ, texte):
    champ.setText(texte)
    champ.textEdited.emit(texte)


def test_demarrage_propre(ihm):
    ihm.avancer(2.0)
    a = ihm.app
    assert "(" not in a._btn_apply.text()           # rien « à appliquer »
    assert a._bandeau_arret.isHidden()
    assert a._lbl_cnx.text() == "Simulateur"
    assert not ihm.sections_en_defaut()
    assert not ihm.dialogues


def test_parcours_asservi_iso_puis_pdc(ihm):
    a = ihm.app
    a._mode_btns[1].click()
    _saisir(a.inp_re, "12000")
    a._btn_apply.click()
    ihm.avancer(40)
    assert ihm.b.p.etat["convergee"]
    assert a._lbl_re_state.text() == "RÉGULÉ ✓"
    assert a._lbl_re_hdr.text() == "REYNOLDS Re VEINE — MESURÉ"
    _saisir(a.inp_re, "300")
    a._btn_apply.click()
    ihm.avancer(2.0)
    assert a._lbl_re_big.text() == "en cours"         # vanne 3 voies en course
    ihm.avancer(80)
    assert ihm.b.p._mode_regulation == "pdc_control"
    assert a._lbl_re_state.text() == "RÉGULÉ ✓"
    assert not ihm.sections_en_defaut() and not ihm.b.violations


def test_saisies_invalides_signalees(ihm):
    a = ihm.app
    a._mode_btns[1].click()
    for texte in ("abc", "-5", "0", ""):
        _saisir(a.inp_re, texte)
        a._btn_apply.click()
        assert "refusée" in ihm.journal[0], texte
        assert "border" in a.inp_re.styleSheet()
    assert not ihm.b.p.regulation_active


def test_manuel_puis_arret(ihm):
    a = ihm.app
    a._mode_btns[0].click()
    _saisir(a._inp_vman, "4")
    a._btn_apply.click()
    ihm.avancer(5)
    assert ihm.b.sim.u_ecrite == pytest.approx(4.0)
    a._diaph_btns[0].click()                          # ISO, en manuel
    ihm.avancer(1)
    assert a._act_vals["vanne3"].text() == "Manœuvre…"
    ihm.avancer(10)
    assert ihm.b.p.etat["diaphragme_mesure"] == "iso"
    a._arreter()
    ihm.avancer(1)
    assert ihm.b.sim.u_ecrite == 0.0 and not ihm.b.p.mode_manuel


def test_arret_de_securite_bandeau_et_acquittement(ihm):
    a = ihm.app
    a._mode_btns[1].click()
    _saisir(a.inp_re, "6000")
    a._btn_apply.click()
    ihm.avancer(30)
    ihm.b.sim.defaut_variateur = True
    ihm.avancer(1)
    assert not a._bandeau_arret.isHidden()
    assert "3G3MV" in a._lbl_arret.text()
    ihm.b.sim.defaut_variateur = False
    ihm.avancer(2)
    a._btn_apply.click()                              # relance refusée
    assert ihm.dialogues[-1][1] == "Arrêt de sécurité"
    assert not ihm.b.p.regulation_active
    a._acquitter_arret()
    ihm.avancer(1)
    assert a._bandeau_arret.isHidden()
    a._btn_apply.click()
    ihm.avancer(40)
    assert ihm.b.p.etat["convergee"]


def test_sequence_chargee_demarree_figee(ihm, tmp_path, monkeypatch):
    a = ihm.app
    csv = tmp_path / "profil.csv"
    csv.write_text("10;3000\n60;6000\n", encoding="utf-8")
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName",
                        lambda *x, **k: (str(csv), ""))
    a._charger_sequence()
    assert a._seq_points[0] == (0.0, 3000.0)          # profil exécuté dès t=0
    a._btn_seq_start.click()
    ihm.avancer(30)
    a._btn_apply.click()                              # en séquence : expliqué
    assert ihm.dialogues[-1][1] == "Mode séquence"
    a._arreter_sequence()
    ihm.avancer(30)
    assert a._mode == "asservi" and ihm.b.p.regulation_active
    assert ihm.b.p.re_cible == 3000.0 and ihm.b.p.etat["convergee"]


def test_clics_aleatoires(ihm):
    """≈ 400 actions opérateur tirées au hasard (modes, saisies valides et
    invalides, références, vannes en manuel, ARRÊT, pages, acquittement),
    entre lesquelles le banc tourne : aucune exception, aucun affichage en
    défaut, aucun invariant violé."""
    a = ihm.app
    rnd = random.Random(11)
    # consignes de toutes les plages (PdC, Miller, bande, ISO, au-delà du
    # capteur), tensions manuelles, et saisies invalides
    textes = ["300", "800", "1500", "2000", "2000", "2600", "3000", "4800",
              "6000", "12000", "15000", "26000", "abc", "", "-3", "2,5", "7", "0"]
    actions = [
        lambda: a._mode_btns[rnd.randrange(3)].click(),
        lambda: (_saisir(a.inp_re, rnd.choice(textes)), a._btn_apply.click()),
        lambda: (_saisir(a._inp_vman, rnd.choice(textes)), a._btn_apply.click()),
        lambda: (_saisir(a._inp_pdc, rnd.choice(["0", "50", "100", "x"])),
                 a._btn_apply.click()),
        lambda: a._ref_btns[rnd.randrange(2)].click(),
        lambda: a._diaph_btns[rnd.randrange(2)].click(),
        lambda: a._bypass_btns[rnd.randrange(2)].click(),
        lambda: a._arreter(),
        lambda: a._nav(rnd.randrange(len(a._nav_btns))),
        lambda: a._acquitter_arret(),
        lambda: a._arreter_sequence(),
    ]
    erreurs, vu = [], set()
    for n in range(400):
        action = rnd.choice(actions)
        try:
            action()
        except Exception as exc:                      # noqa: BLE001
            erreurs.append((n, repr(exc)))
        ihm.avancer(rnd.choice((0.2, 1.0, 3.0, 8.0)))
        e = ihm.b.p.etat
        vu |= {f"ihm:{a._mode}", f"mode:{ihm.b.p._mode_regulation}"}
        vu |= {k for k in ("convergee", "manuel", "arret_securite",
                           "consigne_limitee") if e.get(k)}
        if e.get("diaphragme_lu") == "en_transit":
            vu.add("course 3 voies")
    assert not erreurs, erreurs[:5]
    # le hasard a bien parcouru les situations utiles
    assert {"ihm:manuel", "ihm:asservi", "mode:normal_high", "mode:normal_low",
            "convergee", "manuel", "course 3 voies"} <= vu, sorted(vu)
    assert not ihm.sections_en_defaut(), ihm.sections_en_defaut()
    assert not ihm.b.violations, ihm.b.violations[:5]


def test_ordres_pendant_une_coupure_du_coupleur(ihm):
    """Liaison coupleur perdue : les ordres manuels (vannes, PdC, ARRÊT) ne
    lèvent pas dans l'IHM, et l'opérateur voit « ordre non transmis »."""
    a = ihm.app
    a._mode_btns[0].click()
    ihm.avancer(1)

    def coupure(*_):
        raise ConnectionError("liaison Modbus perdue : test")
    for nom in ("ecrire_vanne_diaphragme", "ecrire_vanne_bypass",
                "ecrire_vanne_perte_charge", "ecrire_variateur",
                "ecrire_variateur_run"):
        setattr(ihm.b.sim, nom, coupure)
    a._diaph_btns[0].click()
    a._bypass_btns[1].click()
    _saisir(a._inp_pdc, "50")
    a._btn_apply.click()
    a._mes_diaphragme("iso")
    a._mes_pdc(5.0)
    a._arreter()
    non_transmis = [m for m in ihm.journal if "ordre non transmis" in m]
    assert len(non_transmis) >= 4, ihm.journal[:8]


def test_bascule_iso_miller_validee_et_affichee_en_veine(ihm):
    """Onglet Configuration : la bascule ISO / Miller est en Re conduite,
    son équivalent veine est rappelé, et seuil − hystérésis ne peut pas
    descendre sous 5000 Re conduite (validité ISO 5167)."""
    import gui.app as gapp
    from PySide6.QtWidgets import QLabel
    cle_h = ("selection_diaphragme", "hysteresis_re")
    assert gapp._seuil_bascule(8000.0, {cle_h: 500.0}) is None
    assert "5000" in gapp._seuil_bascule(5200.0, {cle_h: 500.0})
    textes = " ".join(w.text() for w in ihm.app.findChildren(QLabel))
    assert "MILLER SOUS 4011 / ISO AU-DESSUS DE 4546 RE VEINE" in textes


def test_changement_de_reference_convertit_la_consigne_saisie(ihm):
    """8000 saisi en conduite devenait 8000 veine (1,87 fois le débit) au
    passage en veine ; la courbe mêlait les deux unités."""
    app = ihm.app
    app._set_ref_re("conduite")
    app.inp_re.setText("8000")
    app._set_ref_re("veine")
    assert int(app.inp_re.text()) == round(8000 / app._calculer_k())   # 4278
    app._set_ref_re("conduite")
    assert abs(int(app.inp_re.text()) - 8000) <= 1
    app._hist_re.extend([5000.0, 5100.0])
    app._hist_cible.extend([5000.0, 5000.0])
    app._ref_courbe = "conduite"
    app._historiser({"re_mesure": 2700.0, "reference": "veine",
                     "regulation": False, "re_vise": 2700.0})
    assert list(app._hist_re) == [2700.0] and list(app._hist_cible) == [0.0]


def test_temperature_saisie_choisie_dans_la_configuration(ihm):
    """Onglet Configuration : case « température saisie » et valeur, écrites
    dans config.json et appliquées au pilote à l'enregistrement."""
    app = ihm.app
    app.chk_unlock.setChecked(True)
    assert app.chk_t_manuelle.isEnabled()
    app.chk_t_manuelle.setChecked(True)
    app.champs[("temperature", "valeur_manuelle_c")][0].setText("27,5")
    app._enregistrer_configuration()
    assert ihm.b.p.t_manuelle == 27.5
    cfg = json.loads(app.chemin_cfg.read_text(encoding="utf-8"))
    assert cfg["temperature"] == {**cfg["temperature"], "source": "manuelle",
                                  "valeur_manuelle_c": 27.5}
    ihm.avancer(1.0)
    assert ihm.b.p.etat["temperature"] == 27.5
