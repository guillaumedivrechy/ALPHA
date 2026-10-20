# -*- coding: utf-8 -*-
"""Injection de défauts sur le banc simulé caractérisé, en régulation.

Défaut de sécurité : repli sûr (variateur 0 V écrit, RUN retiré, régulation
coupée), cause publiée, arrêt de sécurité à acquitter ; le défaut disparu,
rien ne repart seul ; après acquittement, une consigne reconverge.
Défaut signalé seulement (défaut actionneur, recopie PdC) : alerte, la
régulation continue."""

import json

import pytest

from banc_simule import BancSimule


def _banc(monkeypatch, tmp_path, consigne=(12000.0, "veine")):
    b = BancSimule(monkeypatch, tmp_path)
    b.consigne(*consigne)
    assert b.jusqu_a_convergence(150) is not None
    b.cycles(5)
    return b


def _alterer(b, modif):
    """Mesures du simulateur modifiées par modif(m) ; renvoie le rétablisseur."""
    orig = b.sim.lire_mesures

    def lire():
        m = orig()
        modif(m)
        return m
    b.sim.lire_mesures = lire
    return lambda: setattr(b.sim, "lire_mesures", orig)


def _assert_repli(b, cle, texte):
    e = b.p.etat
    assert not b.p.regulation_active and not b.p.mode_manuel
    assert b.sim.u_ecrite == 0.0 and b.sim._run is False
    assert e["arret_securite"]
    assert texte in str(e.get(cle) or ""), (cle, e.get(cle))


def _assert_reprise_apres_acquittement(b, consigne=(12000.0, "veine")):
    motif = b.p.etat.get("arret_motif")
    assert motif                                      # cause mémorisée
    b.cycles(10, verifier=False)                     # défaut disparu
    assert not b.p.regulation_active and b.sim.u_ecrite == 0.0
    assert b.p.etat["arret_securite"]                 # toujours à acquitter
    assert b.p.etat["arret_motif"] == motif           # cause toujours lisible
    b.p.acquitter_arret_securite()
    assert b.p.etat["arret_securite"] is None and b.p.etat["arret_motif"] is None
    b.consigne(*consigne)
    assert b.jusqu_a_convergence(150) is not None


def test_perte_liaison_modbus(monkeypatch, tmp_path):
    b = _banc(monkeypatch, tmp_path)
    orig = b.sim.lire_mesures

    def coupure():
        raise IOError("Modbus : pas de réponse du coupleur")
    b.sim.lire_mesures = coupure
    b.cycles(3, verifier=False)
    _assert_repli(b, "erreur_com", "pas de réponse")
    b.sim.lire_mesures = orig
    _assert_reprise_apres_acquittement(b)


def test_erreur_interne_du_cycle(monkeypatch, tmp_path):
    """Bug ou pilote d'E/S renvoyant une valeur inattendue : repli, pas de
    thread mort laissant le ventilateur tourner."""
    b = _banc(monkeypatch, tmp_path)
    retablir = _alterer(b, lambda m: m.update(temperature=None))
    b.cycles(1, verifier=False)
    _assert_repli(b, "erreur_com", "erreur interne")
    retablir()
    _assert_reprise_apres_acquittement(b)


@pytest.mark.parametrize("t_c", [150.0, 0.0])
def test_pt100_invraisemblable(monkeypatch, tmp_path, t_c):
    b = _banc(monkeypatch, tmp_path)
    retablir = _alterer(b, lambda m: m.update(temperature=t_c))
    b.cycles(10, verifier=False)
    _assert_repli(b, "defaut_capteur", "température invraisemblable")
    retablir()
    _assert_reprise_apres_acquittement(b)


def test_pression_amont_invraisemblable(monkeypatch, tmp_path):
    b = _banc(monkeypatch, tmp_path)
    retablir = _alterer(b, lambda m: m.update(p_amont_500=-40000.0))
    b.cycles(3, verifier=False)
    _assert_repli(b, "defaut_capteur", "pression amont")
    retablir()
    _assert_reprise_apres_acquittement(b)


@pytest.mark.parametrize("valeur", [0.0, 200.0, 450.0])
def test_capteur_dp_fige(monkeypatch, tmp_path, valeur):
    """Capteur ΔP figé (débranché ou bloqué) en régulation : détecté sur
    une fenêtre de 10 s."""
    b = _banc(monkeypatch, tmp_path)
    retablir = _alterer(b, lambda m: m.update(dp_iso_500=valeur))
    b.cycles(15, verifier=False)
    _assert_repli(b, "defaut_capteur", "signal ΔP figé")
    retablir()
    _assert_reprise_apres_acquittement(b)


def test_defaut_variateur(monkeypatch, tmp_path):
    b = _banc(monkeypatch, tmp_path)
    b.sim.defaut_variateur = True
    b.cycles(2, verifier=False)
    _assert_repli(b, "defaut_variateur", "3G3MV")
    b.sim.defaut_variateur = False
    _assert_reprise_apres_acquittement(b)


@pytest.mark.parametrize("vanne", ["3voies", "bypass"])
def test_vanne_bloquee(monkeypatch, tmp_path, vanne):
    """Miller -> ISO, une vanne ne bouge pas : défaut au chien de garde,
    ventilateur jamais relancé entre-temps."""
    b = _banc(monkeypatch, tmp_path, (2000.0, "veine"))
    ecrire = {"3voies": "ecrire_vanne_diaphragme", "bypass": "ecrire_vanne_bypass"}[vanne]
    vraie = getattr(b.sim, ecrire)
    setattr(b.sim, ecrire, lambda v: None)
    b.consigne(12000.0, "veine")
    b.cycles(40, verifier=False)
    _assert_repli(b, "defaut_vanne", "non atteinte")
    setattr(b.sim, ecrire, vraie)
    _assert_reprise_apres_acquittement(b)


@pytest.mark.parametrize("fc,texte", [
    ({"fc_3voies_0": True, "fc_3voies_90": True}, "incohérents"),
    ({"fc_3voies_0": False, "fc_3voies_90": False, "fc_3voies_180": False},
     "aucune butée franche"),
    ({"fc_bypass_ouvert": False, "fc_bypass_ferme": False}, "bypass sans fin de course"),
])
def test_fins_de_course_aberrantes(monkeypatch, tmp_path, fc, texte):
    b = _banc(monkeypatch, tmp_path)
    retablir = _alterer(b, lambda m: m.update(fc))
    b.cycles(30, verifier=False)
    _assert_repli(b, "defaut_vanne", texte)
    retablir()
    _assert_reprise_apres_acquittement(b)


def test_defaut_actionneur_alerte_sans_repli(monkeypatch, tmp_path):
    """Choix d'exploitation : un défaut actionneur est signalé (IHM), il
    n'arrête pas le banc."""
    b = _banc(monkeypatch, tmp_path)
    _alterer(b, lambda m: m.update(defaut_act_bypass=True))
    b.cycles(10, verifier=False)
    assert b.p.regulation_active and b.sim.u_ecrite > 0.0
    assert b.p.etat["mesures_brutes"]["defaut_act_bypass"] is True
    assert not b.p.etat.get("arret_securite")


def test_recopie_pdc_figee_signalee(monkeypatch, tmp_path):
    """Recopie PdC qui ne suit plus la commande : défaut PdC signalé ; la
    régulation (sur le Re mesuré) continue, ventilateur au plus u_pdc."""
    b = _banc(monkeypatch, tmp_path, (300.0, "veine"))
    _alterer(b, lambda m: m.update(pdc_recopie=7.0))
    b.consigne(600.0, "veine")
    b.cycles(30, verifier=False)
    assert b.p.etat["defaut_pdc"]
    assert b.p.regulation_active
    assert b.sim.u_ecrite <= b.p._u_pdc() + 1e-6


def test_vanne_plus_lente_que_la_course_configuree(monkeypatch, tmp_path):
    """Constaté au banc : vanne 3 voies ER20 (11 s) avec une course
    configurée de 3,9 s -> défaut au chien de garde, puis la vanne arrive
    en butée. Le message donne les durées et l'issue (refaire
    l'apprentissage de la course)."""
    b = _banc(monkeypatch, tmp_path, (2000.0, "veine"))
    b.sim._v3._vitesse = 90.0 / 11.0
    b.consigne(12000.0, "veine")
    b.cycles(15, verifier=False)
    _assert_repli(b, "defaut_vanne", "non atteinte en")
    assert "course configurée 3.9 s" in b.p.etat["defaut_vanne"]
    assert "apprentissage" in b.p.etat["defaut_vanne"]
    b.cycles(10, verifier=False)
    assert b.p.etat["diaphragme_lu"] == "iso"      # arrivée après le défaut


def test_pas_de_faux_signal_fige_au_demarrage_lent(monkeypatch, tmp_path):
    """Démarrage depuis l'arrêt avec un débit lent à s'établir (rampe
    interne du 3G3MV, inertie) : la fenêtre de 10 s du détecteur de signal
    figé était pleine des zéros d'avant la relance -> faux « signal ΔP
    figé » et arrêt de sécurité. Le détecteur n'arme qu'une fois le
    ventilateur en marche depuis toute la fenêtre."""
    b = BancSimule(monkeypatch, tmp_path, cfg_sur={
        "simulation": {"constante_temps_s": 8.0}})
    sim, st = b.sim, {"u": 0.0, "cmd": 0.0, "t": b.t[0]}

    def ecrire(v):
        sim.u_ecrite = v
        st["cmd"] = min(max(v, 0.0), 10.0)
    avancer = sim._avancer

    def avancer_avec_rampe():                       # 3G3MV : 1 V/s
        dt, st["t"] = b.t[0] - st["t"], b.t[0]
        e = st["cmd"] - st["u"]
        st["u"] += max(-dt, min(dt, e))
        sim._u_variateur = st["u"]
        avancer()
    monkeypatch.setattr(sim, "ecrire_variateur", ecrire)
    monkeypatch.setattr(sim, "_avancer", avancer_avec_rampe)
    b.consigne(10000.0, "veine")
    assert b.jusqu_a_convergence(90) is not None
    assert not b.p.etat.get("arret_securite"), b.p.etat.get("arret_motif")


def test_vanne_pdc_bloquee_signalee(monkeypatch, tmp_path):
    """Mode PdC : l'actionneur PdC ne suit plus sa commande (bloqué grande
    ouverte). Défaut PdC signalé (alerte), la régulation continue."""
    b = _banc(monkeypatch, tmp_path, (12000.0, "veine"))
    monkeypatch.setattr(b.sim, "ecrire_vanne_perte_charge", lambda v: None)
    b.consigne(300.0, "veine")              # mode PdC : la vanne doit fermer
    vu = False
    for _ in range(int(90 / b.p.periode)):
        b.cycles(b.p.periode, verifier=False)
        vu = vu or bool(b.p.etat["defaut_pdc"])
    assert b.p._mode_regulation == "pdc_control"
    assert vu


def test_temperature_saisie_remplace_la_pt100(monkeypatch, tmp_path):
    """Choix de l'opérateur : température saisie à la main. Les calculs
    l'utilisent, la PT100 est ignorée (y compris une lecture aberrante, qui
    sinon arrêterait le banc) ; repassée en PT100, la mesure revient."""
    b = _banc(monkeypatch, tmp_path)
    cfg = json.loads(json.dumps(b.cfg))
    cfg["temperature"] = {"source": "manuelle", "valeur_manuelle_c": 30.0}
    b.p.appliquer_config(cfg)
    _alterer(b, lambda m: m.update(temperature=150.0))     # PT100 aberrante
    b.cycles(10, verifier=False)
    e = b.p.etat
    assert e["temperature"] == 30.0 and e["temperature_source"] == "manuelle"
    assert not e.get("arret_securite") and b.p.regulation_active
    assert e["mesures_brutes"]["temperature"] == 150.0       # lecture visible
    assert e["mu"] == pytest.approx(b.p.banc.fluide.mu(30.0))
    cfg["temperature"]["source"] = "pt100"
    b.p.appliquer_config(cfg)
    b.cycles(10, verifier=False)
    _assert_repli(b, "defaut_capteur", "température invraisemblable")
