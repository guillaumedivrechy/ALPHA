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


# ---- revue, lot B ------------------------------------------------------------

def _app_sans_banc(tmp_path, chemin_cfg=None):
    """Apprentissage sur StubES, vannes et ventilateur neutralisés : la
    logique de séquence seule."""
    app, pilote = _app_cfg(tmp_path)
    if chemin_cfg is not None:
        app.chemin_cfg = chemin_cfg
    app._positionner_diaphragme = lambda d: 0.0
    app._chronometrer_vanne = lambda *a, **k: 1.0
    app._variateur = lambda u: None
    return app, pilote


def test_table_pdc_echec_de_vanne_conserve_la_table(tmp_path):
    """PdC n'atteignant pas sa butée : la phase rendait (None, None, [])
    comme « aucun palier mesurable », et « Appliquer » effaçait la table PdC
    existante. Désormais un échec, sans résultat à appliquer."""
    app, pilote = _app_sans_banc(tmp_path)
    app._attendre_pdc_extreme = lambda *a, **k: None
    app._test_table_pdc()
    assert "table_pdc" not in app.resultats
    assert "conservée" in app.etat["statut"]


def test_echec_d_ecriture_signale_et_non_marque_applique(tmp_path):
    """Écriture refusée (dossier absent, disque plein, droits) : l'erreur
    était avalée (apprentissages) ou levée dans l'IHM (config), et le
    résultat pouvait passer pour enregistré. Désormais « ÉCHEC… », et le
    résultat reste à appliquer pour une nouvelle tentative."""
    app, pilote = _app_cfg(tmp_path)
    app.chemin_cfg = tmp_path / "absent" / "config.json"
    app.resultats["vanne_bypass"] = {"ouverture_s": 9.0, "fermeture_s": 10.0,
                                     "max_s": 10.0}
    message = app.appliquer()
    assert message.startswith("ÉCHEC D'ENREGISTREMENT")
    assert app.resultats_a_appliquer() == ["vanne_bypass"]


def test_relance_retire_le_resultat_precedent_et_remet_les_vannes_au_repos(
        tmp_path):
    """Un test relancé puis en échec laissait le résultat précédent affiché
    et « à appliquer » ; la séquence finie laissait les vannes dans l'état
    de sa dernière phase (PdC fermée après un Re mapping Miller)."""
    app, pilote = _app_cfg(tmp_path)
    app.resultats["vanne_bypass"] = {"ouverture_s": 9.0, "fermeture_s": 10.0,
                                     "max_s": 10.0}
    app.etat["paliers_pdc"] = [(100.0, 900)]
    repos = []
    pilote._position_repos = lambda: repos.append(True)

    def echec():
        app.etat["statut"] = "échec : fin de course jamais atteinte"
    app._test_vanne_bypass = echec
    assert app.demarrer("vanne_bypass") is None
    app._thread.join(5.0)
    assert "vanne_bypass" not in app.resultats
    assert "paliers_pdc" not in app.etat
    assert repos


def test_apprentissage_enregistre_l_unite_de_re(tmp_path, caplog):
    """Les tables sont en Re conduite ; une version les a enregistrées en Re
    veine. L'unité est désormais déclarée, et un fichier sans unité est
    signalé au chargement."""
    import logging
    from core.gestionnaire_apprentissages import GestionnaireApprentissages
    g = GestionnaireApprentissages(tmp_path)
    g.sauvegarder_iso({"table_gains": [[1.0, 3500.0], [2.0, 7000.0]]}, {})
    iso = json.loads((tmp_path / "apprentissages" / "iso.json")
                     .read_text("utf-8"))
    assert iso["metadata"]["unite_re"] == "conduite"
    del iso["metadata"]["unite_re"]
    (tmp_path / "apprentissages" / "iso.json").write_text(
        json.dumps(iso), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        g.charger_tout()
    assert "sans unité de Re" in caplog.text


def test_simulateur_recopie_pdc_a_l_echelle_configuree():
    """La recopie simulée était à l'échelle de la commande (0-10 V) quelles
    que soient les bornes pdc_recopie_min_v/max_v : écart avec la recopie
    attendue, faux défaut PdC en simulation."""
    from io_banc.simulateur import SimulateurBanc
    cfg = _cfg()
    act = dict(cfg.get("actionneurs", {}), pdc_recopie_min_v=2.0,
               pdc_recopie_max_v=8.0)
    es = SimulateurBanc(Banc(cfg), cfg.get("simulation"), cfg.get("modbus"),
                        act)
    assert es.lire_mesures()["pdc_recopie"] == pytest.approx(8.0, abs=0.2)


def test_apercu_sans_none_et_tension_pdc_plafonnee(tmp_path):
    """Aperçu d'Appliquer : « PdC min None → … » quand la valeur en
    configuration valait None ; tension du ventilateur en mode PdC non
    bornée à la commande 0-10 V."""
    app, pilote = _app_cfg(tmp_path)
    app.cfg.setdefault("strategie_bas_re", {})["pdc_min_exploitable_pct"] = None
    app.resultats["table_pdc"] = {
        "u_ventilateur_v": 1.0, "pdc_min_exploitable_pct": 30.0,
        "re_min_exploitable": 180, "paliers_PDC_Re": [[100.0, 900], [40.0, 300]]}
    texte = app.apercu_application()[0][1]
    assert "None" not in texte and "PdC min — → 30.0 %" in texte
    pilote.u_ventilateur_pdc = 12.0
    assert app._u_ventilateur_pdc(0.3) == 10.0


@pytest.mark.parametrize("croise", [False, True])
def test_consigne_juste_apres_un_apprentissage_fini_sur_l_iso(
        monkeypatch, tmp_path, croise):
    """Test de non-régression complet : la remise au repos de fin
    d'apprentissage (vanne 3 voies -> Miller, ~11 s) écrivait l'ordre sans
    déclarer de manœuvre. Une consigne lancée pendant la course voyait une
    vanne « sans butée » hors manœuvre : arrêt de sécurité. Et la relecture
    de position initiale pouvait réaffirmer la butée ISO de départ."""
    import time as _time
    from banc_simule import BancSimule
    sur = {"modbus": {"positions_vanne_3voies": {
        "diaphragme_a_0deg": "miller", "diaphragme_a_90deg": "iso"}}} \
        if croise else None
    b = BancSimule(monkeypatch, tmp_path, caracterise=True, cfg_sur=sur)
    p = b.p
    monkeypatch.setattr(_time, "sleep",
                        lambda s: b.cycles(max(s, p.periode), verifier=False))
    app = Apprentissage(p, b.cfg, tmp_path / "config.json")
    monkeypatch.setattr(app._annule, "wait", lambda t=None: b.cycles(
        max(t or 0.0, p.periode), verifier=False) or False)
    app._arrets_debut = p.arrets
    app._executer(lambda: app._positionner_diaphragme("iso"), {})
    assert p._angle_vanne_3voies(p.etat["mesures_brutes"]) \
        == p._angle_par_diaphragme["iso"]
    b.cycles(0.2, verifier=False)
    b.consigne(1500, "veine")                       # Miller : vanne en course
    if croise:
        # plomberie du simulateur fixe (0° = ISO) : on ne vérifie que la
        # manœuvre, la régulation tournerait sur le mauvais capteur
        b.cycles(30.0, verifier=False)
        assert not p.etat.get("arret_securite"), p.etat.get("arret_motif")
        assert p._angle_vanne_3voies(p.etat["mesures_brutes"]) \
            == p._angle_par_diaphragme["miller"]
        return
    assert b.jusqu_a_convergence(120) is not None, p.etat.get("arret_motif")
    assert not p.etat.get("arret_securite")
    assert p.etat["diaphragme_lu"] == "miller"


def test_apprentissages_enchaines_sans_attendre_la_mise_au_repos(
        monkeypatch, tmp_path):
    """Test de non-régression (parcours IHM) : la remise au repos de fin de
    séquence déclare ses manœuvres (bypass vers l'ouverture) ; un
    apprentissage lancé aussitôt referme le bypass, et le chien de garde de
    cette manœuvre caduque coupait tout (« bypass non ouvert »)."""
    import time as _time
    from banc_simule import BancSimule
    b = BancSimule(monkeypatch, tmp_path, caracterise=True)
    p = b.p
    monkeypatch.setattr(_time, "sleep",
                        lambda s: b.cycles(max(s, p.periode), verifier=False))
    app = Apprentissage(p, b.cfg, tmp_path / "config.json")
    monkeypatch.setattr(app._annule, "wait", lambda t=None: b.cycles(
        max(t or 0.0, p.periode), verifier=False) or False)
    for cible in (app._test_vanne_bypass, app._test_indiciel):
        app._arrets_debut = p.arrets
        app._executer(cible, {})                  # sans pause entre les deux
    assert "indiciel" in app.resultats, app.etat["statut"]
    assert not p.etat.get("arret_securite"), p.etat.get("arret_motif")


def test_zero_capteurs_inclut_la_p_amont(tmp_path):
    """Constaté au banc : le zéro de la P amont ne se faisait pas (seuls
    les deux ΔP étaient mesurés), son offset restait dans ρ et le Re. Il
    est mesuré à débit nul, appliqué, puis soustrait par la boucle."""
    app, pilote = _app(tmp_path, {"dp_diaph_500": 0.4, "dp_iso_500": -0.2,
                                  "p_amont_500": 7.5})
    app._test_zero_capteurs(attente_s=0.0, duree_s=0.2)
    z = app.resultats["zero_capteurs"]
    assert z["p_amont_500"] == pytest.approx(7.5)
    assert "p_amont_500 = 7.5 Pa" in app.apercu_application()[0][1]
    app.appliquer()
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["zero_capteurs"]["p_amont_500"] == pytest.approx(7.5)
    assert pilote.zero_capteurs["p_amont_500"] == pytest.approx(7.5)


def test_zero_p_amont_aberrant_refuse(tmp_path):
    app, _ = _app(tmp_path, {"dp_diaph_500": 0.0, "dp_iso_500": 0.0,
                             "p_amont_500": 60.0})
    app._test_zero_capteurs(attente_s=0.0, duree_s=0.2)
    assert "zero_capteurs" not in app.resultats
    assert "p_amont_500 = 60.0 Pa" in app.etat["statut"]


def test_boucle_soustrait_le_zero_de_la_p_amont(monkeypatch, tmp_path):
    from banc_simule import BancSimule
    b = BancSimule(monkeypatch, tmp_path, caracterise=True)
    p = b.p
    b.cycles(1.0, verifier=False)
    brute = p.etat["mesures_brutes"]["p_amont_500"]
    p.zero_capteurs["p_amont_500"] = 3.0
    b.cycles(0.2, verifier=False)
    assert p.etat["p_amont"] == pytest.approx(
        p.etat["mesures_brutes"]["p_amont_500"] - 3.0)
    assert abs(p.etat["mesures_brutes"]["p_amont_500"] - brute) < 1.0


def _apprentissage_3voies(monkeypatch, tmp_path):
    import time as _time
    from banc_simule import BancSimule
    b = BancSimule(monkeypatch, tmp_path, caracterise=True)
    p = b.p
    monkeypatch.setattr(_time, "sleep",
                        lambda s: b.cycles(max(s, p.periode), verifier=False))
    app = Apprentissage(p, b.cfg, tmp_path / "config.json")
    monkeypatch.setattr(app._annule, "wait", lambda t=None: b.cycles(
        max(t or 0.0, p.periode), verifier=False) or False)
    app._arrets_debut = p.arrets
    app._executer(app._test_vanne_3voies, {})
    return app, b


def test_vanne_3voies_chronologie_des_fins_de_course(monkeypatch, tmp_path):
    """Constaté au banc : course mesurée ~3 s, ~8 s au chronomètre.
    L'apprentissage relève l'état des trois fins de course (DI) pendant
    chaque course, et observe encore après l'arrivée déclarée."""
    app, b = _apprentissage_3voies(monkeypatch, tmp_path)
    r = app.resultats["vanne_3voies"]
    course = 90.0 / b.sim._v3._vitesse
    assert r["T1_0_vers_90_s"] == pytest.approx(course, abs=0.4)
    # T1 : 0° (DI5) -> aucune butée -> 90° (DI7) ; [t, fc0, fc90, fc180]
    assert [e[1:] for e in r["chronologie_T1"]] == [[1, 0, 0], [0, 0, 0],
                                                    [0, 1, 0]]
    assert r["chronologie_T1"][1][0] < 0.5                 # départ
    assert r["chronologie_T1"][2][0] == pytest.approx(course, abs=0.4)
    assert [e[1:] for e in r["chronologie_T2"]] == [[0, 1, 0], [0, 0, 0],
                                                    [1, 0, 0]]
    texte = r["chronologie_texte"]
    assert "DI5=1  DI7=0  DI6=0  (butée 0°)" in texte
    assert "(butée 90°)  ← arrivée déclarée" in texte
    assert "⚠" not in texte
    assert texte in app.etat["statut"]


def test_vanne_3voies_bascule_avant_la_butee_signalee(monkeypatch, tmp_path):
    """Came réglée trop tôt : le contact 90° se ferme brièvement vers 30°,
    se rouvre, puis se referme en butée. L'arrivée est déclarée trop tôt
    (course sous-estimée, ce qui ressemble au constat du banc) ; la
    bascule tardive est relevée et signalée."""
    from io_banc.simulateur import _Vanne3Voies
    monkeypatch.setattr(_Vanne3Voies, "fc_90", property(
        lambda self: 30.0 < self._angle < 40.0 or abs(self._angle - 90.0) < 1.0))
    app, b = _apprentissage_3voies(monkeypatch, tmp_path)
    r = app.resultats["vanne_3voies"]
    course = 90.0 / b.sim._v3._vitesse
    assert r["T1_0_vers_90_s"] < 0.5 * course
    assert r["chronologie_T1"][-1][0] == pytest.approx(course, abs=0.4)
    assert "⚠ une fin de course a encore basculé APRÈS" in r["chronologie_texte"]
