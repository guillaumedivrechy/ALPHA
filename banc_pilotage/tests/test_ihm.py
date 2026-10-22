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


def test_curseur_maintenance_ne_relance_pas_apres_arret(ihm):
    """Revue : RUN depuis Maintenance, puis ARRÊT VARIATEUR du rail (ou un
    repli) ; bouger ensuite le curseur relançait le ventilateur sans
    confirmation. Le curseur est désarmé dès que le manuel n'est plus actif."""
    app = ihm.app
    app._nav(app.PAGE_MAINTENANCE)
    app._mes_run_arme = True
    ihm.b.p.mode_manuel_tension(0.0)
    ihm.avancer(0.5)
    app._mes_slider.setValue(300)
    assert ihm.b.p.tension_manuelle == pytest.approx(3.0)
    ihm.b.p.arreter_regulation()          # ARRÊT VARIATEUR du rail
    ihm.avancer(0.5)
    assert not app._mes_run_arme
    app._mes_slider.setValue(600)
    ihm.avancer(0.5)
    assert ihm.b.sim.u_ecrite == 0.0 and not ihm.b.p.mode_manuel


def test_commandes_refusees_pendant_un_apprentissage(ihm):
    """Revue : « Appliquer » en asservi démarrait la régulation par-dessus
    un Re mapping ; un bouton de vanne bougeait la vanne 3 voies en pleine
    mesure ; la configuration pouvait être déverrouillée et enregistrée."""
    app = ihm.app
    app.apprentissage.etat["en_cours"] = True     # séquence en cours
    try:
        app.inp_re.setText("8000")
        app._appliquer()
        assert not ihm.b.p.regulation_active
        app._cmd_diaph("iso")
        assert not ihm.b.p._manoeuvres
        app._choisir_mode("manuel")
        assert app._mode != "manuel"
        app.chk_unlock.setChecked(True)
        assert not app.chk_unlock.isChecked()
        refus = [d for d in ihm.dialogues if d[1] == "Apprentissage en cours"]
        assert len(refus) == 4
    finally:
        app.apprentissage.etat["en_cours"] = False


# ---- revue, lot C ------------------------------------------------------------

def test_rho_de_la_carte_pt100_a_la_pression_absolue(ihm):
    """La carte PT100 (Maintenance) prenait p_amont, RELATIF, pour la
    pression absolue : ρ affiché ~100 fois trop faible."""
    app = ihm.app
    app._nav(app.PAGE_MAINTENANCE)
    ihm.avancer(1.0)
    rho = float(app._mes_lbl["pt100_rho"].text().split()[0].replace(",", "."))
    assert rho == pytest.approx(ihm.b.p.etat["rho"], rel=0.02)
    assert 1.0 < rho < 1.4


def _lecture(app, chemin):
    return app._champs_lecture[chemin][0].text()


def test_onglet_configuration_formats_et_valeurs_derivees(ihm):
    """Tolérance de convergence affichée « 0.01 » sous l'unité %, S et Dh
    restés sur la valeur du démarrage après un enregistrement de H et W,
    titre de la bascule figé sur l'ancien seuil."""
    app = ihm.app
    assert _lecture(app, ("regulation", "tolerance_convergence_relative")) == "1"
    app.chk_unlock.setChecked(True)
    app.champs[("veine", "hauteur_m")][0].setText("0.300")
    app.champs[("veine", "largeur_m")][0].setText("0.200")
    app.champs[("selection_diaphragme", "seuil_re")][0].setText("9000")
    app._enregistrer_configuration()
    assert _lecture(app, ("veine", "section_m2")) == "0.06000"
    assert _lecture(app, ("veine", "longueur_caracteristique_m")) == "0.240000"
    titre = app._carte_bascule.titre.text()
    assert titre == app._titre_bascule().upper()
    seuil = 9000, app.cfg["selection_diaphragme"]["hysteresis_re"]
    k = app._calculer_k()
    assert f"{(seuil[0] - seuil[1]) / k:.0f}" in titre


def test_enregistrer_ne_reecrit_pas_les_valeurs_arrondies(ihm):
    """Enregistrer réécrivait chaque valeur non touchée au format affiché
    (μ référence 1,716e-05 -> 1,72e-05, α 1,15e-05 -> 1,1e-05…)."""
    app = ihm.app
    app.cfg["fluide"]["sutherland_mu_ref"] = 1.716e-05
    app._remplir_champs_config()
    assert app.champs[("fluide", "sutherland_mu_ref")][0].text() == "1.72e-05"
    app.chk_unlock.setChecked(True)
    app.champs[("patm_pa",)][0].setText("100000")
    app._enregistrer_configuration()
    cfg = json.loads(app.chemin_cfg.read_text(encoding="utf-8"))
    assert cfg["patm_pa"] == 100000.0
    assert cfg["fluide"]["sutherland_mu_ref"] == 1.716e-05


def test_config_ancienne_sans_hauteur_largeur_enregistrable(monkeypatch,
                                                            tmp_path):
    """config.json antérieur à veine.hauteur_m / largeur_m : les champs
    affichaient « — » et rendaient TOUT enregistrement de l'onglet
    impossible (« — » invalide). Ils restent absents, S et Dh intacts."""
    i = IHMSimulee.__new__(IHMSimulee)
    i.dialogues = []
    i._intercepter(monkeypatch)
    i.qa = QApplication.instance() or QApplication([])
    i.b = BancSimule(monkeypatch, tmp_path, caracterise=True)
    for cle in ("hauteur_m", "largeur_m"):
        i.b.cfg["veine"].pop(cle, None)
    i.b.cfg["fluide"].pop("humidite_relative", None)
    import gui.app as gapp
    chemin = tmp_path / "config.json"
    chemin.write_text(json.dumps(i.b.cfg), encoding="utf-8")
    app = gapp.Application(i.b.p, "simulation", i.b.cfg, chemin)
    app._timer.stop()
    try:
        assert app.champs[("veine", "hauteur_m")][0].text() == "—"
        section = i.b.cfg["veine"]["section_m2"]
        app.chk_unlock.setChecked(True)
        app.champs[("patm_pa",)][0].setText("100500")
        app._enregistrer_configuration()
        assert not [d for d in i.dialogues if d[0] == "critical"], i.dialogues
        cfg = json.loads(chemin.read_text(encoding="utf-8"))
        assert cfg["patm_pa"] == 100500.0
        assert "hauteur_m" not in cfg["veine"]
        assert cfg["veine"]["section_m2"] == section
    finally:
        app.deleteLater()


def test_maintenance_ne_borne_pas_les_reglages_appris(ihm):
    """Les spinbox de Maintenance (tau 0-2 s, ki 0-2…) écrasaient à
    « Appliquer » un réglage appris hors de leurs bornes ou plus fin que
    leur pas, même non touché."""
    app = ihm.app
    app.cfg["regulation"]["tau_consigne_s"] = 3.5
    app.cfg["regulation"]["ki"] = 0.6234
    app._remplir_widgets_regulation()
    app._mes_reg_widgets["kp"].setValue(0.8)
    app._mes_reg_appliquer()
    cfg = json.loads(app.chemin_cfg.read_text(encoding="utf-8"))
    assert cfg["regulation"]["kp"] == 0.8
    assert cfg["regulation"]["tau_consigne_s"] == 3.5
    assert cfg["regulation"]["ki"] == 0.6234


def test_mode_affiche_suit_le_pilote(ihm, tmp_path, monkeypatch):
    """Séquence arrêtée par ARRÊT VARIATEUR : l'IHM restait en « séquence »
    et refusait toute consigne ; « Séquence » sans profil passait en mode
    séquence sans rien à suivre ; RUN depuis Maintenance laissait
    « asservi » affiché."""
    a = ihm.app
    a._choisir_mode("sequence")                    # sans profil
    assert a._mode == "asservi" and a._stack.currentIndex() == 2
    csv = tmp_path / "profil.csv"
    csv.write_text("10;3000\n60;6000\n", encoding="utf-8")
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName",
                        lambda *x, **k: (str(csv), ""))
    a._charger_sequence()
    a._btn_seq_start.click()
    ihm.avancer(5)
    assert a._mode == "sequence"
    a._arreter()                                   # ARRÊT VARIATEUR
    ihm.avancer(1)
    assert a._mode == "asservi"
    _saisir(a.inp_re, "4000")
    a._btn_apply.click()
    assert ihm.b.p.regulation_active
    ihm.b.p.mode_manuel_tension(2.0)               # RUN depuis Maintenance
    ihm.avancer(1)
    assert a._mode == "manuel" and a._slider_u.isEnabled()


def test_passage_en_manuel_tension_preremplie_a_zero(ihm):
    """En passant de la régulation au manuel, la tension de régulation
    était pré-remplie : « Appliquer » relançait le ventilateur alors que le
    variateur venait d'être mis à 0 V."""
    a = ihm.app
    a._mode_btns[1].click()
    _saisir(a.inp_re, "8000")
    a._btn_apply.click()
    ihm.avancer(30)
    a._mode_btns[0].click()
    assert a._inp_vman.text() == "0.0"
    a._btn_apply.click()
    ihm.avancer(1)
    assert ihm.b.p.tension_manuelle == 0.0


def test_arreter_sequence_sans_sequence_garde_la_saisie(ihm):
    a = ihm.app
    a._mode_btns[1].click()
    _saisir(a.inp_re, "8000")
    a._btn_apply.click()
    ihm.avancer(2)
    _saisir(a.inp_re, "9500")
    a._arreter_sequence()
    assert a.inp_re.text() == "9500"


def test_voies_et_pied_de_page_vivants(ihm):
    """Onglet Voies : GAIN « 1,000 » / OFFSET « 0,00 » en dur et « Actif »
    permanent ; pied de page « Fréq : — » et « 0 s » jamais mis à jour."""
    a = ihm.app
    a._mode_btns[1].click()
    _saisir(a.inp_re, "8000")
    a._btn_apply.click()
    ihm.avancer(30)
    assert all(l.text() == "● Active" for l in a.lbl_entrees.values())
    assert a._foot_freq.text() != "Fréq : —"
    assert "min" in a._foot_elapsed.text()
    ihm.b.sim.lire_mesures = lambda: (_ for _ in ()).throw(OSError("coupé"))
    ihm.avancer(1)
    assert all(l.text() == "● Liaison perdue" for l in a.lbl_entrees.values())


def test_validateurs_tres_bas_re():
    import gui.app as gapp
    assert gapp._tension_ventilateur_pdc(12.0, {})
    assert gapp._tension_ventilateur_pdc(1.0, {}) is None
    plancher = {("mesure_dp", "dp_min_mesurable_pa"): 0.15}
    assert gapp._seuil_debit_nul(0.2, plancher)
    assert gapp._seuil_debit_nul(0.05, plancher) is None


def test_offset_pt100_dans_la_configuration(ihm):
    """Champ « Offset PT100 » : 0 par défaut, écrit dans config.json et
    appliqué au pilote ; hors ±10 °C refusé."""
    app = ihm.app
    champ = app.champs[("temperature", "offset_pt100_c")][0]
    assert champ.text() == "0.00"
    app.chk_unlock.setChecked(True)
    champ.setText("-0,8")
    app._enregistrer_configuration()
    assert ihm.b.p.offset_pt100 == -0.8
    cfg = json.loads(app.chemin_cfg.read_text(encoding="utf-8"))
    assert cfg["temperature"]["offset_pt100_c"] == -0.8
    ihm.avancer(1.0)
    e = ihm.b.p.etat
    assert e["temperature"] == pytest.approx(e["mesures_brutes"]["temperature"] - 0.8)
    app.chk_unlock.setChecked(True)
    champ.setText("15")
    app._enregistrer_configuration()
    assert ihm.dialogues[-1][1] == "Saisie invalide"
    assert ihm.b.p.offset_pt100 == -0.8


# ---- parcours complets (tests de non-régression d'usage) --------------------

class _ThreadSynchrone:
    """Remplace threading.Thread dans core.apprentissage : la séquence
    s'exécute dans le clic, sur l'horloge simulée (un seul thread fait
    avancer le temps : résultats reproductibles)."""

    def __init__(self, target, args=(), kwargs=None, daemon=None, name=None):
        self._cible, self._args, self._kw = target, args, kwargs or {}

    def start(self):
        self._cible(*self._args, **self._kw)

    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass


def _apprentissage_synchrone(ihm, monkeypatch):
    import time as _time
    import core.apprentissage as ca
    b, app = ihm.b, ihm.app
    monkeypatch.setattr(ca.threading, "Thread", _ThreadSynchrone)
    monkeypatch.setattr(_time, "sleep",
                        lambda s: b.cycles(max(s, b.p.periode), verifier=False))
    monkeypatch.setattr(app.apprentissage._annule, "wait", lambda t=None: b.cycles(
        max(t or 0.0, b.p.periode), verifier=False) or False)


def test_parcours_apprentissage_par_l_ihm_puis_consigne(ihm, monkeypatch):
    """Boutons d'apprentissage, aperçu, « Appliquer », puis consigne
    aussitôt : la dernière séquence (Re mapping ISO) laisse la vanne 3
    voies en retour vers le Miller ; la consigne Miller qui suit ne doit
    pas finir en arrêt de sécurité (régression trouvée au test complet)."""
    _apprentissage_synchrone(ihm, monkeypatch)
    a, p = ihm.app, ihm.b.p
    a._nav(3)
    for test in ("calibration_pdc", "vanne_3voies", "vanne_bypass"):
        a._btns_vanne[test].click()
        ihm.avancer(0.4)
        assert test in a.apprentissage.resultats, a.apprentissage.etat["statut"]
        assert a._lbls_vanne[test][0].text() != "—"
    a.inp_u1.setText("2.0")
    a.inp_u2.setText("4.0")
    a._btn_indiciel.click()
    a._btn_re_mapping_iso.click()
    ihm.avancer(0.4)
    assert {"indiciel", "re_mapping_iso"} <= set(a.apprentissage.resultats), \
        a.apprentissage.etat["statut"]
    assert a.btn_appliquer_app.isEnabled()
    assert a.lbl_app_resultats.text().startswith("À appliquer :")
    a.btn_appliquer_app.click()
    assert a.lbl_app_statut.text().startswith("Appliqué"), a.lbl_app_statut.text()
    assert ihm.dialogues[-1][0] == "question"
    cfg = json.loads(a.chemin_cfg.read_text(encoding="utf-8"))
    assert cfg["actionneurs"]["vanne_3voies_temps_s"] == p.t_vanne_3voies
    assert cfg["regulation"]["kp"] == p.trim.kp
    ihm.avancer(0.4)
    assert not a.btn_appliquer_app.isEnabled()
    # consigne aussitôt, depuis l'écran principal
    a._nav(0)
    a._mode_btns[1].click()
    _saisir(a.inp_re, "1500")
    a._btn_apply.click()
    ihm.avancer(60)
    assert not p.etat.get("arret_securite"), p.etat.get("arret_motif")
    assert p.etat["convergee"] and p.etat["diaphragme_lu"] == "miller"
    assert not ihm.sections_en_defaut(), ihm.sections_en_defaut()
    assert not [d for d in ihm.dialogues if d[0] in ("critical", "warning")], \
        ihm.dialogues


def _boucle_active(ihm):
    """Le pilote du banc simulé tourne sans thread : on le déclare actif
    pour l'initialisation (qui sinon s'annule, « pilote non démarré »)."""
    ihm.b.p._thread = type("BoucleActive", (), {"is_alive": lambda self: True})()


def _initialisation(ihm, max_s=60.0):
    """Initialisation des vannes au démarrage, timer de suivi piloté par le
    test (un appel par cycle de 0,2 s)."""
    a = ihm.app
    _boucle_active(ihm)
    a._lancer_initialisation_vannes()
    a._timer_verif_init.stop()
    for _ in range(int(max_s / 0.2)):
        ihm.avancer(0.2)
        if a._dialog_init.isHidden():
            return
        a._verifier_vannes_init()


def _hors_repos(ihm):
    p = ihm.b.p
    p.commander_diaphragme("iso")
    p.commander_bypass(False)
    p.commander_vanne_perte_charge_pct(0.0)
    ihm.avancer(40)
    br = p.etat["mesures_brutes"]
    assert p.etat["diaphragme_lu"] == "iso" and br["fc_bypass_ferme"] \
        and br["fc_pdc_ferme"]


def test_initialisation_des_vannes_au_demarrage(ihm):
    _hors_repos(ihm)
    _initialisation(ihm)
    p, br = ihm.b.p, ihm.b.p.etat["mesures_brutes"]
    assert ihm.app._dialog_init.isHidden()
    assert ihm.journal[0].startswith("✓ Vannes initialisées"), ihm.journal[:3]
    assert p.etat["diaphragme_lu"] == "miller"
    assert br["fc_bypass_ouvert"] and br["fc_pdc_ouvert"]
    assert not p.etat.get("defaut_vanne") and not p.etat.get("arret_securite")


def test_initialisation_vanne_bloquee_signalee(ihm):
    """PdC qui n'atteint jamais sa butée : la fenêtre se ferme au délai et
    le journal le dit (pas d'attente sans fin)."""
    _hors_repos(ihm)
    sim = ihm.b.sim
    lire = sim.lire_mesures
    sim.lire_mesures = lambda: {**lire(), "fc_pdc_ouvert": False}
    _initialisation(ihm, max_s=90.0)
    assert ihm.app._dialog_init.isHidden()
    assert "timeout" in ihm.journal[0], ihm.journal[:3]


def test_initialisation_ne_plus_attendre(ihm):
    """« Ne plus attendre » ferme le suivi ; les vannes finissent leur
    course."""
    _hors_repos(ihm)
    a = ihm.app
    _boucle_active(ihm)
    a._lancer_initialisation_vannes()
    ihm.avancer(1.0)
    a._dialog_init.canceled.emit()                 # bouton « Ne plus attendre »
    assert not a._timer_verif_init.isActive()
    assert "suivi interrompu" in ihm.journal[0]
    ihm.avancer(40)
    assert ihm.b.p.etat["diaphragme_lu"] == "miller"


def test_fermeture_pendant_la_regulation(ihm):
    """Fermeture de la fenêtre en pleine régulation : ventilateur coupé
    (0 V, RUN retiré) AVANT toute manœuvre de vanne, puis vannes au repos ;
    l'apprentissage éventuel est annulé."""
    a, p, sim = ihm.app, ihm.b.p, ihm.b.sim
    a._mode_btns[1].click()
    _saisir(a.inp_re, "10000")
    a._btn_apply.click()
    ihm.avancer(40)
    assert p.etat["convergee"] and p.etat["diaphragme_lu"] == "iso"
    ordres = []
    for nom in ("ecrire_variateur", "ecrire_variateur_run",
                "ecrire_vanne_diaphragme", "ecrire_vanne_bypass",
                "ecrire_vanne_perte_charge"):
        orig = getattr(sim, nom)
        setattr(sim, nom, lambda v, n=nom, o=orig: (ordres.append((n, v)), o(v))[1])
    a.close()
    assert ordres, "aucun ordre à la fermeture"
    premiere_vanne = next(i for i, (n, _) in enumerate(ordres)
                          if n.startswith("ecrire_vanne"))
    avant = ordres[:premiere_vanne]
    assert ("ecrire_variateur", 0.0) in avant
    assert ("ecrire_variateur_run", False) in avant
    angle_miller = p._angle_par_diaphragme["miller"]
    assert ("ecrire_vanne_diaphragme", angle_miller == "0") in ordres
    assert ("ecrire_vanne_bypass", True) in ordres
    assert not p.regulation_active and a.apprentissage._annule.is_set()


def test_configuration_relue_au_redemarrage(ihm):
    """Réglages enregistrés depuis l'onglet Configuration (offset PT100,
    température saisie, seuil de bascule) : un pilote neuf construit sur le
    config.json écrit les retrouve."""
    from banc_simule import SimTrace
    from core.banc import Banc
    from core.pilote import Pilote
    a = ihm.app
    a.chk_unlock.setChecked(True)
    a.champs[("temperature", "offset_pt100_c")][0].setText("-0,4")
    a.champs[("temperature", "valeur_manuelle_c")][0].setText("23,5")
    a.chk_t_manuelle.setChecked(True)
    a.champs[("selection_diaphragme", "seuil_re")][0].setText("8500")
    a._enregistrer_configuration()
    assert not [d for d in ihm.dialogues if d[0] == "critical"], ihm.dialogues
    cfg = json.loads(a.chemin_cfg.read_text(encoding="utf-8"))
    banc = Banc(cfg)
    neuf = Pilote(banc, SimTrace(banc, cfg["simulation"], cfg["modbus"],
                                 cfg["actionneurs"]), cfg)
    assert neuf.offset_pt100 == -0.4
    assert neuf.t_manuelle == 23.5
    assert neuf.banc.seuil_re == 8500.0
