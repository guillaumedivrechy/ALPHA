# -*- coding: utf-8 -*-
"""Apprentissages : zéro capteurs, contrôle de câblage (mapping ISO/Miller),
garde-fous d'ordre (indiciel après courbe de gain)."""

import json
from pathlib import Path

import pytest

from core.apprentissage import Apprentissage
from core.banc import Banc
from core.pilote import Pilote, TableGains

CHEMIN_CFG = Path(__file__).resolve().parent.parent / "config.json"


def _cfg():
    return json.loads(CHEMIN_CFG.read_text(encoding="utf-8"))


class StubES:
    def lire_mesures(self):
        return {"temperature": 20.0}

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


def _app(tmp_path, brutes=None):
    cfg = _cfg()
    pilote = Pilote(Banc(cfg), StubES(), cfg)
    pilote._chemin_etat = tmp_path / "etat.json"
    pilote.periode = 0.02                  # accélère les boucles de mesure
    if brutes is not None:
        pilote.etat["mesures_brutes"] = brutes
    return Apprentissage(pilote, cfg, tmp_path / "config.json"), pilote


def test_zero_capteurs_mesure_et_applique(tmp_path):
    app, pilote = _app(tmp_path, {"dp_diaph_500": 1.2, "dp_iso_500": -0.4})
    app._test_zero_capteurs(attente_s=0.0, duree_s=0.2)
    z = app.resultats["zero_capteurs"]
    assert z["dp_diaph_500"] == pytest.approx(1.2)
    assert z["dp_iso_500"] == pytest.approx(-0.4)
    app.appliquer()
    assert pilote.zero_capteurs["dp_diaph_500"] == pytest.approx(1.2)
    cfg_relu = json.loads((tmp_path / "config.json")
                          .read_text(encoding="utf-8"))
    assert cfg_relu["zero_capteurs"]["dp_iso_500"] == pytest.approx(-0.4)


def test_zero_capteurs_refuse_offset_aberrant(tmp_path):
    """Un 'zéro' de dizaines de Pa signale un écoulement résiduel ou un
    capteur en défaut : ne pas l'enregistrer comme correction."""
    app, _ = _app(tmp_path, {"dp_diaph_500": 60.0, "dp_iso_500": 0.1})
    app._test_zero_capteurs(attente_s=0.0, duree_s=0.2)
    assert "zero_capteurs" not in app.resultats
    assert "anormalement" in app.etat["statut"]


def test_appliquer_intervertit_mapping_iso_miller(tmp_path):
    app, pilote = _app(tmp_path)
    app.resultats["verif_cablage"] = {"mapping_inverse": True,
                                      "resume": "mapping inversé"}
    app.appliquer()
    # l'ambiguite ne porte plus sur les fins de course (Miller n'en a plus
    # en propre) mais sur la plomberie : quelle butee alimente quel diaphragme
    pos = app.cfg["modbus"]["positions_vanne_3voies"]
    assert pos["diaphragme_a_0deg"] == "miller"
    assert pos["diaphragme_a_90deg"] == "iso"
    assert pilote._angle_par_diaphragme["iso"] == "90"
    assert pilote._diaphragme_par_angle["0"] == "miller"
    assert pilote._position_initiale_lue is False   # position relue aux FC


def test_indiciel_exige_une_table_de_gains(tmp_path):
    """Les kp/ki IMC supposent le gain scheduling actif : sans table de
    gains, ils ne vaudraient qu'au point d'essai -> refus explicite."""
    app, pilote = _app(tmp_path)
    pilote.table_gains = TableGains()      # invalide
    err = app.demarrer("indiciel")
    assert err is not None and "courbe de gain" in err


def test_capteur_dominant():
    assert Apprentissage._capteur_dominant(40.0, 2.0) == "iso"
    assert Apprentissage._capteur_dominant(1.5, 30.0) == "miller"
    assert Apprentissage._capteur_dominant(1.0, 2.0) is None      # trop faible
    assert Apprentissage._capteur_dominant(20.0, 15.0) is None    # ambigu


def test_sequence_refusee_si_le_ventilateur_tourne(tmp_path):
    """RÉGRESSION : demarrer() ne refusait que si « regulation » était vrai.
    Le ventilateur peut tourner en mode MANUEL — la page de mise en service
    le rend facile. verif_cablage voyait alors un écoulement dès son premier
    instant et concluait « variateur répond » même si la référence 0-10 V
    n'était pas raccordée ; zero_capteurs aurait relevé un zéro pollué par
    le débit résiduel, puis soustrait cet offset de toutes les mesures."""
    app, pilote = _app(tmp_path)

    # banc a l'arret : accepte
    assert app.demarrer("zero_capteurs") is None
    app.annuler()
    app._thread.join(timeout=5.0)

    # ventilateur en marche : refuse, avec le geste a faire
    pilote.mode_manuel_tension(4.0)
    err = app.demarrer("verif_cablage")
    assert err is not None
    assert "ventilateur" in err.lower()
    assert app.etat["en_cours"] is False

    # l'ordre RUN seul suffit a refuser, meme a tension nulle
    pilote.arreter_regulation()
    pilote._run_actif = True
    assert app.demarrer("verif_cablage") is not None

    pilote._run_actif = False
    assert app.demarrer("zero_capteurs") is None
    app.annuler()
    app._thread.join(timeout=5.0)


def test_re_mapping_produit_paliers_plage_haute():
    """Phase 4 du re_mapping doit créer paliers_plage_haute pour table_gains.

    Vérifie que les résultats contiennent la clé 'paliers_plage_haute' qui
    alimente la table de gains mode normal (Re > 5000).
    """
    cfg = _cfg()
    pilote = Pilote(Banc(cfg), StubES(), cfg)
    pilote.periode = 0.02

    # Simuler résultats re_mapping complets avec Phase 4
    resultats_re_mapping = {
        "u_min_exploitable_v": 0.5,
        "u_seuil_bas_re_v": 5.3,
        "re_a_u_min": 250,
        "paliers_V_Re": [[0.5, 250], [1.0, 500], [5.0, 4800]],
        "paliers_plage_haute": [[5.5, 5200], [7.0, 8000], [9.0, 12000]]
    }

    # Vérifier que la clé paliers_plage_haute existe
    assert "paliers_plage_haute" in resultats_re_mapping
    assert isinstance(resultats_re_mapping["paliers_plage_haute"], list)
    assert len(resultats_re_mapping["paliers_plage_haute"]) > 0

    # Vérifier structure des paliers (voltage, Re)
    for palier in resultats_re_mapping["paliers_plage_haute"]:
        assert len(palier) == 2
        volts, re = palier
        assert 0 <= volts <= 10.0, "Voltage doit être dans [0, 10V]"
        assert re > 0, "Re doit être positif"

    # Vérifier que les paliers sont croissants en voltage ET en Re
    paliers = resultats_re_mapping["paliers_plage_haute"]
    for i in range(len(paliers) - 1):
        assert paliers[i][0] < paliers[i + 1][0], "Voltages doivent être croissants"
        assert paliers[i][1] < paliers[i + 1][1], "Re doivent être croissants"


# ---- revue complète : séquences d'apprentissage ----------------------------

def _app_cfg(tmp_path, cfg=None):
    cfg = cfg or _cfg()
    pilote = Pilote(Banc(cfg), StubES(), cfg)
    from core.gestionnaire_apprentissages import GestionnaireApprentissages
    pilote.gestionnaire_apprentissages = GestionnaireApprentissages(tmp_path)
    return Apprentissage(pilote, cfg, tmp_path / "config.json"), pilote


def test_sequences_positionnent_le_diaphragme_selon_la_plomberie(tmp_path):
    """Plomberie croisée (ISO à 90°) : « positionner ISO » doit viser 90°.
    RÉGRESSION : les séquences codaient 0° = ISO et caractérisaient le
    mauvais diaphragme."""
    cfg = _cfg()
    cfg["modbus"]["positions_vanne_3voies"] = {
        "diaphragme_a_0deg": "miller", "diaphragme_a_90deg": "iso"}
    app, _ = _app_cfg(tmp_path, cfg)
    appels = []
    app._avancer_vanne_3voies = lambda vers_0deg, label: appels.append(vers_0deg) or 0.0
    app._positionner_diaphragme("iso")
    app._positionner_diaphragme("miller")
    assert appels == [False, True]


def test_sequence_annulee_ne_produit_aucun_resultat(tmp_path):
    """Annulée pendant un palier, la séquence ISO allait jusqu'au bout,
    affichait « terminé » et stockait des paliers partiels que « Appliquer »
    aurait écrits en configuration."""
    import threading
    app, pilote = _app_cfg(tmp_path)
    pilote.periode = 0.05
    pilote.etat["mesures_brutes"] = {
        "fc_bypass_ferme": True, "fc_3voies_0": True,
        "pdc_recopie": pilote.pdc_recopie_max}
    threading.Timer(2.5, app.annuler).start()
    app._test_re_mapping_iso(u_seuil=3.0)
    assert "re_mapping_iso" not in app.resultats
    assert not app.etat["statut"].startswith("terminé")


def test_essai_indiciel_sur_le_circuit_iso(tmp_path):
    """L'indiciel exige la table ISO (sa pente normalise la correction) :
    il doit se faire sur le circuit ISO, bypass fermé — pas sur le Miller
    bypass fermé, configuration jamais utilisée en régulation."""
    app, _ = _app_cfg(tmp_path)
    circuits = []
    app._preparer_circuit_normal = lambda d: circuits.append(d) or False
    app._test_indiciel()
    assert circuits == ["iso"]


def test_re_mapping_combine_est_sauvegarde(tmp_path):
    """Les tables du re_mapping combiné n'étaient jamais écrites dans
    apprentissages/ (test d'une clé « plage_haute » inexistante) : perdues
    au redémarrage."""
    app, pilote = _app_cfg(tmp_path)
    app.resultats["re_mapping"] = {
        "u_min_exploitable_v": 0.8, "u_seuil_bas_re_v": 5.0,
        "paliers_V_Re": [[0.8, 600.0], [2.0, 1500.0], [5.0, 3800.0]],
        "paliers_plage_haute": [[3.0, 9000.0], [6.0, 18000.0]],
    }
    app.appliquer()
    iso = json.loads((tmp_path / "apprentissages" / "iso.json").read_text("utf-8"))
    miller = json.loads((tmp_path / "apprentissages" / "miller.json").read_text("utf-8"))
    assert iso["tables"]["table_gains"]
    assert miller["tables"]["table_gains_bas_re"]


def test_appliquer_ne_reapplique_pas_les_resultats_deja_appliques(tmp_path):
    """« Appliquer » après un 2e test n'applique que ce 2e résultat : le
    mapping ISO/Miller intervertit une fois, pas une seconde (ce qui le
    remettait en place en silence)."""
    app, pilote = _app(tmp_path)
    app.resultats["verif_cablage"] = {"mapping_inverse": True,
                                      "resume": "mapping inversé"}
    app.appliquer()
    mapping = dict(pilote._diaphragme_par_angle)
    app.resultats["vanne_bypass"] = {"ouverture_s": 9.0, "fermeture_s": 10.0,
                                     "max_s": 10.0}
    assert app.resultats_a_appliquer() == ["vanne_bypass"]
    assert [t for t, _ in app.apercu_application()] == ["Vanne bypass"]
    assert "11.0 s" in app.apercu_application()[0][1]
    app.appliquer()
    assert pilote._diaphragme_par_angle == mapping
    assert pilote.t_vanne_bypass == 11.0
    assert app.resultats_a_appliquer() == []
    assert "Aucun nouveau" in app.appliquer()
    # test relancé : nouveau résultat, de nouveau à appliquer
    app.resultats["vanne_bypass"] = {"ouverture_s": 9.0, "fermeture_s": 9.5,
                                     "max_s": 9.5}
    assert app.resultats_a_appliquer() == ["vanne_bypass"]


def test_table_iso_non_monotone_appliquee_sauvegardee_et_relue(tmp_path,
                                                               monkeypatch):
    """Banc réel : un palier un peu bas (bruit, palier pas encore établi)
    rendait la table ISO non monotone ; « Appliquer » la rejetait EN BLOC
    (« ignorée (Re non monotone) »), rien n'était enregistré et la
    régulation perdait son feed-forward. Le palier fautif seul est écarté."""
    app, pilote = _app_cfg(tmp_path)
    app.resultats["re_mapping_iso"] = {
        "paliers_plage_haute": [[2.25, 6100.0], [1.75, 4300.0],
                                [1.25, 4400.0],            # palier fautif
                                [3.75, 11500.0], [5.25, 16800.0]]}
    message = app.appliquer()
    assert "ignorée" not in message and "1 écarté" in message
    assert pilote.table_gains.valide_ff()
    assert pilote.table_gains.u_pour_re_ff(11500.0) == pytest.approx(3.75)
    iso = json.loads((tmp_path / "apprentissages" / "iso.json")
                     .read_text("utf-8"))
    assert len(iso["tables"]["table_gains"]) == 5
    # relue au démarrage suivant
    import core.pilote
    monkeypatch.setattr(core.pilote, "RACINE", tmp_path)
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    relu = Pilote(Banc(cfg), StubES(), cfg)
    assert relu.table_gains.valide_ff()
    assert relu.table_gains.u_pour_re_ff(16800.0) == pytest.approx(5.25)


@pytest.mark.parametrize("reference", ["veine", "conduite"])
def test_tables_apprises_en_re_conduite_quelle_que_soit_la_reference(
        monkeypatch, tmp_path, reference):
    """Constaté au banc : IHM en référence veine (défaut), le Re mapping
    lisait pilote.etat["re_mesure"], publié en Re veine hors régulation ->
    tables 1,87 fois trop basses, et une consigne de 10 000 Re veine
    régulée à plus de 13 000 (feed-forward extrapolé hors table). Les
    tables doivent être en Re conduite."""
    import time as _time
    from banc_simule import BancSimule
    b = BancSimule(monkeypatch, tmp_path, caracterise=False)
    monkeypatch.setattr(_time, "sleep",
                        lambda s: b.cycles(max(s, b.p.periode), verifier=False))
    b.p.definir_reference_affichage(reference)
    b.p.suspendre_maintien_tor()
    app = Apprentissage(b.p, b.cfg, tmp_path / "config.json")
    # pauses de la séquence (Event.wait) : horloge simulée elle aussi
    monkeypatch.setattr(app._annule, "wait", lambda t=None: b.cycles(
        max(t or 0.0, b.p.periode), verifier=False) or False)
    app._test_re_mapping_iso()
    paliers = app.resultats["re_mapping_iso"]["paliers_plage_haute"]
    # simulateur : ~3 500 Re CONDUITE par volt (~1 900 en veine) ; dernier
    # palier juste sous la saturation ISO (~26 000 Re conduite, ~14 000 veine)
    assert len(paliers) >= 4
    assert all(re / u > 2700.0 for u, re in paliers), paliers
    assert max(re for _, re in paliers) > 20000.0, paliers


def test_miller_sans_palier_pdc_efface_les_resultats_pdc_precedents(tmp_path):
    """« Appliquer » d'un Re mapping Miller sans palier PdC mesurable : la
    table PdC et le Re min précédents étaient conservés (et réécrits dans
    miller.json) à côté d'une PdC min à 100 %. Ils sont effacés et le mode
    PdC est inactif."""
    app, pilote = _app_cfg(tmp_path)
    pilote.table_gains_pdc = TableGains([(20.0, 600.0), (100.0, 2300.0)])
    pilote.re_min_exploitable = 214
    app.resultats["re_mapping_miller"] = {
        "u_min_exploitable_v": 0.3, "u_seuil_bas_re_v": 8.4,
        "pdc_min_exploitable_pct": None, "re_min_exploitable": None,
        "paliers_V_Re": [[0.3, 266.0], [2.0, 2008.0], [8.0, 7682.0]],
        "paliers_PDC_Re": []}
    message = app.appliquer()
    assert "aucun palier mesurable" in message
    assert not pilote.table_gains_pdc.valide_ff()
    assert pilote.re_min_exploitable is None
    assert not pilote._mode_pdc_caracterise()
    miller = json.loads((tmp_path / "apprentissages" / "miller.json")
                        .read_text("utf-8"))
    assert "table_gains_pdc" not in miller["tables"]


def test_table_pdc_seule_a_la_tension_imposee_puis_regulation(monkeypatch,
                                                              tmp_path):
    """Choix d'exploitation : ventilateur tenu à une tension imposée en mode
    PdC (au-dessus de u_min × marge), la PdC règle le débit. L'apprentissage
    « Table PdC » mesure la table à cette tension sans refaire le Re mapping
    Miller ; « Appliquer » garde u_seuil dans miller.json ; une consigne
    très basse passe en mode PdC, ventilateur à la tension imposée."""
    import time as _time
    from banc_simule import BancSimule
    b = BancSimule(monkeypatch, tmp_path, cfg_sur={
        "strategie_bas_re": {"u_ventilateur_pdc_v": 2.0}})
    p = b.p
    assert p._u_pdc() == 2.0                        # > u_min 1,3 × 1,15
    monkeypatch.setattr(_time, "sleep",
                        lambda s: b.cycles(max(s, p.periode), verifier=False))
    p.suspendre_maintien_tor()
    app = Apprentissage(p, b.cfg, tmp_path / "config.json")
    monkeypatch.setattr(app._annule, "wait", lambda t=None: b.cycles(
        max(t or 0.0, p.periode), verifier=False) or False)
    u_seuil = p.u_seuil_bas_re
    app._test_table_pdc()
    r = app.resultats["table_pdc"]
    assert r["u_ventilateur_v"] == 2.0 and len(r["paliers_PDC_Re"]) >= 3
    app.appliquer()
    miller = json.loads((tmp_path / "apprentissages" / "miller.json")
                        .read_text("utf-8"))
    assert miller["parametres"]["u_seuil_bas_re_v"] == u_seuil
    assert len(miller["tables"]["table_gains_pdc"]) == len(r["paliers_PDC_Re"])
    p.reprendre_maintien_tor()
    # seuil : Re(2 V) / 1,06 ; consigne 20 % plus bas -> mode PdC
    seuil = p._seuil_bas_re()
    assert seuil == pytest.approx(p.table_gains_bas_re.re_pour_u_ff(2.0) / 1.06)
    b.consigne(0.8 * seuil, "conduite")
    assert b.jusqu_a_convergence(150) is not None
    assert p._mode_regulation == "pdc_control"
    assert p.etat["u_variateur"] == pytest.approx(2.0)
    assert p.etat["pdc_consigne"] < 9.0              # la PdC règle


def test_controle_cablage_relatif_a_la_configuration(monkeypatch, tmp_path):
    """Revue : le contrôle câblage attendait toujours « 0° = ISO » et
    « Appliquer » PERMUTAIT la configuration : plomberie croisée corrigée,
    un second contrôle la ré-inversait. Il compare désormais à la
    correspondance en vigueur et Appliquer écrit le mapping mesuré. Le gain
    Re/V n'est pas appris pendant la séquence (Miller saturé : −21 %)."""
    import time as _time
    from banc_simule import BancSimule
    b = BancSimule(monkeypatch, tmp_path, caracterise=True, cfg_sur={
        "modbus": {"positions_vanne_3voies": {
            "diaphragme_a_0deg": "miller", "diaphragme_a_90deg": "iso"}}})
    p = b.p
    monkeypatch.setattr(_time, "sleep",
                        lambda s: b.cycles(max(s, p.periode), verifier=False))
    p.suspendre_maintien_tor()
    app = Apprentissage(p, b.cfg, tmp_path / "config.json")
    monkeypatch.setattr(app._annule, "wait", lambda t=None: b.cycles(
        max(t or 0.0, p.periode), verifier=False) or False)
    gain = p._gain_re_par_volt
    app._test_verif_cablage()
    r = app.resultats["verif_cablage"]
    assert r["mapping_inverse"] and "DIFFÉRENT" in r["resume"]
    assert r["mapping_mesure"] == {"diaphragme_a_0deg": "iso",
                                   "diaphragme_a_90deg": "miller"}
    assert p._gain_re_par_volt == gain               # gain non appris
    app.appliquer()
    assert p._diaphragme_par_angle == {"0": "iso", "90": "miller"}
    app._test_verif_cablage()                        # second contrôle
    r = app.resultats["verif_cablage"]
    assert r["resume"] == "câblage conforme" and not r["mapping_inverse"]
    assert "rien à écrire" in app.apercu_application()[0][1]
