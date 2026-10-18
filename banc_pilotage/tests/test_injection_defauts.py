# -*- coding: utf-8 -*-
"""Injection de défauts sur le banc simulé caractérisé, en régulation.

Défaut de sécurité : repli sûr (variateur 0 V écrit, RUN retiré, régulation
coupée), cause publiée, arrêt de sécurité à acquitter ; le défaut disparu,
rien ne repart seul ; après acquittement, une consigne reconverge.
Défaut signalé seulement (défaut actionneur, recopie PdC) : alerte, la
régulation continue."""

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
    b.cycles(10, verifier=False)                     # défaut disparu
    assert not b.p.regulation_active and b.sim.u_ecrite == 0.0
    assert b.p.etat["arret_securite"]                 # toujours à acquitter
    b.p.acquitter_arret_securite()
    assert b.p.etat["arret_securite"] is None
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
