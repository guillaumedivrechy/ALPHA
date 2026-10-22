# -*- coding: utf-8 -*-
"""Bout en bout sur le banc simulé à horloge accélérée.

1. Banc vierge : les dix apprentissages enchaînés, « Appliquer » après
   chacun, puis consignes sur toute la plage (Re veine). C'est ce parcours
   qui a révélé la remise au repos sans manœuvre déclarée (arrêt de
   sécurité sur la consigne suivante).
2. Redémarrage : un pilote neuf, construit sur le config.json et les
   apprentissages/*.json écrits, retrouve les mêmes tables et régule.
3. Démarrage du logiciel (position de repos) puis consigne pendant la
   course de la vanne 3 voies."""

import json
import time as _time

import pytest

from banc_simule import SimTrace, BancSimule
from core.apprentissage import Apprentissage
from core.banc import Banc
from core.pilote import Pilote

SEQUENCES = ["zero_capteurs", "verif_cablage", "calibration_pdc",
             "vanne_3voies", "vanne_bypass", "vanne_pdc", "re_mapping_miller",
             "re_mapping_iso", "table_pdc", "indiciel"]

# consignes en Re veine : PdC, Miller, ISO, retours vers le bas ; le banc
# simulé tient ~260 Re veine au plus bas (ventilateur à u_min × marge)
CONSIGNES_VEINE = (300, 1000, 3000, 6000, 10000, 14000, 4000, 400)


def _horloge_simulee(monkeypatch, b, app):
    """Les attentes de la séquence avancent l'horloge simulée (un seul
    thread fait avancer le temps : résultats reproductibles)."""
    monkeypatch.setattr(_time, "sleep",
                        lambda s: b.cycles(max(s, b.p.periode), verifier=False))
    monkeypatch.setattr(app._annule, "wait", lambda t=None: b.cycles(
        max(t or 0.0, b.p.periode), verifier=False) or False)


def _executer(app, nom):
    """Comme Apprentissage.demarrer, sans thread."""
    cible = getattr(app, f"_test_{nom}")
    app._arrets_debut = app.pilote.arrets
    app.etat.update(en_cours=True, test=nom, statut="démarrage…")
    app._executer(cible, {})


def _reguler(b, pilote, re_veine, max_s=400.0):
    pilote.definir_consigne(re_veine, "veine")
    if not pilote.regulation_active:
        pilote.demarrer_regulation()
    t0 = b.t[0]
    while b.t[0] - t0 < max_s:
        b.t[0] += pilote.periode
        pilote._cycle_protege()
        if pilote.etat.get("convergee"):
            return b.t[0] - t0
        assert not pilote.etat.get("arret_securite"), \
            (re_veine, pilote.etat.get("arret_motif"))
    return None


@pytest.fixture(scope="module")
def banc_appris(tmp_path_factory):
    """Banc vierge caractérisé par les dix apprentissages (une fois pour le
    module : ~2 s réelles, ~30 min simulées)."""
    mp = pytest.MonkeyPatch()
    racine = tmp_path_factory.mktemp("bout_en_bout")
    b = BancSimule(mp, racine, caracterise=False)
    app = Apprentissage(b.p, b.cfg, racine / "config.json")
    _horloge_simulee(mp, b, app)
    statuts = {}
    for nom in SEQUENCES:
        _executer(app, nom)
        statuts[nom] = app.etat["statut"]
        resume = app.appliquer() if nom in app.resultats else None
        statuts[nom + ":appliquer"] = resume
        b.cycles(1.0, verifier=False)
    yield b, app, racine, statuts
    mp.undo()


def test_chaine_complete_des_apprentissages(banc_appris):
    b, app, racine, statuts = banc_appris
    p = b.p
    for nom in SEQUENCES:
        assert nom in app.resultats, (nom, statuts[nom])
        # câblage conforme : contrôle seul, rien à écrire
        attendu = "Aucun" if nom == "verif_cablage" else "Appliqué"
        assert statuts[nom + ":appliquer"].startswith(attendu), \
            (nom, statuts[nom + ":appliquer"])
    assert not p.etat.get("arret_securite"), p.etat.get("arret_motif")
    assert app.resultats["verif_cablage"]["resume"] == "câblage conforme"
    # courses du simulateur (11 s / 11 s / ~24 s) retrouvées, marge appliquée
    assert app.resultats["vanne_3voies"]["max_s"] == pytest.approx(11.0, abs=0.5)
    assert p.t_vanne_3voies == pytest.approx(1.1 * app.resultats["vanne_3voies"]["max_s"], abs=0.1)
    assert app.resultats["vanne_pdc"]["max_s"] == pytest.approx(24.0, abs=1.5)
    # tables apprises et mode PdC caractérisé
    assert p.table_gains.valide_ff() and len(p.table_gains.points) >= 4
    assert p.table_gains_bas_re.valide_ff()
    assert p.table_gains_pdc.valide_ff() and p._mode_pdc_caracterise()
    assert p.u_min_ventilateur is not None
    assert app.resultats["indiciel"]["kp_propose"] == p.trim.kp
    for nom in ("iso", "miller"):
        f = json.loads((racine / "apprentissages" / f"{nom}.json")
                       .read_text(encoding="utf-8"))
        assert f["metadata"]["unite_re"] == "conduite"


def test_regulation_sur_toute_la_plage_apres_apprentissage(banc_appris):
    """Première consigne juste après la dernière séquence (vannes encore en
    retour au repos), puis toute la plage, montées et descentes."""
    b, _, _, _ = banc_appris
    p = b.p
    modes = set()
    for re_v in CONSIGNES_VEINE:
        t = _reguler(b, p, re_v)
        assert t is not None, (re_v, p.etat.get("consigne_limitee"))
        assert p.etat["re_mesure"] == pytest.approx(re_v, rel=0.02)
        modes.add(p._mode_regulation)
    assert modes == {"pdc_control", "normal_low", "normal_high"}
    assert not b.violations, b.violations[:5]
    p.arreter_regulation()
    b.cycles(5.0, verifier=False)


def test_redemarrage_relit_les_apprentissages(banc_appris, monkeypatch):
    """Logiciel relancé : un pilote neuf, sur les seuls fichiers écrits par
    « Appliquer », retrouve tables, réglages et courses, et régule."""
    import core.pilote
    b, app, racine, _ = banc_appris
    p = b.p
    # dossier de données du banc appris (conftest le redirige par test)
    monkeypatch.setattr(core.pilote, "RACINE", racine)
    cfg = json.loads((racine / "config.json").read_text(encoding="utf-8"))
    banc = Banc(cfg)
    neuf = Pilote(banc, SimTrace(banc, cfg["simulation"], cfg["modbus"],
                                 cfg["actionneurs"]), cfg)
    assert neuf.table_gains.points == p.table_gains.points
    assert neuf.table_gains_bas_re.points == p.table_gains_bas_re.points
    assert neuf.table_gains_pdc.points == p.table_gains_pdc.points
    assert neuf.u_min_ventilateur == p.u_min_ventilateur
    assert (neuf.trim.kp, neuf.trim.ki) == (p.trim.kp, p.trim.ki)
    assert neuf.t_vanne_3voies == p.t_vanne_3voies
    assert (neuf.pdc_recopie_min, neuf.pdc_recopie_max) \
        == (p.pdc_recopie_min, p.pdc_recopie_max)
    neuf._position_repos()
    for re_v in (500, 8000):
        assert _reguler(b, neuf, re_v) is not None, re_v
        assert neuf.etat["re_mesure"] == pytest.approx(re_v, rel=0.02)


@pytest.mark.parametrize("depart", ["iso", "miller"])
def test_consigne_pendant_la_mise_au_repos_du_demarrage(monkeypatch, tmp_path,
                                                        depart):
    """Pilote.demarrer met les vannes au repos (Miller, bypass et PdC
    ouverts) ; vanne 3 voies laissée sur l'ISO à la fermeture précédente,
    une consigne lancée pendant sa course ne doit pas finir en arrêt de
    sécurité « position inexploitable »."""
    b = BancSimule(monkeypatch, tmp_path, caracterise=True)
    p = b.p
    p.commander_diaphragme(depart)
    p.commander_bypass(False)
    b.cycles(20.0, verifier=False)
    p._position_repos()                       # comme Pilote.demarrer
    b.cycles(0.4, verifier=False)
    for re_v in (1500, 9000):
        assert _reguler(b, p, re_v) is not None, re_v
    assert p.etat["diaphragme_lu"] == "iso"
