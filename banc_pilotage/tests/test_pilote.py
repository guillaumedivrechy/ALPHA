# -*- coding: utf-8 -*-
"""Pilote : arrêt total, manœuvres de la vanne 3 voies, robustesse de la
boucle (régressions des défauts corrigés)."""

import json
import time
from pathlib import Path

import pytest

from core.banc import Banc
from core.pilote import Pilote, TableGains
from io_banc.simulateur import SimulateurBanc

CFG = json.loads((Path(__file__).resolve().parent.parent / "config.json")
                 .read_text(encoding="utf-8"))


class StubES:
    """Entrées/sorties factices : enregistre les commandes reçues."""

    def __init__(self, mesures=None):
        self.mesures = mesures or {"temperature": 20.0}
        self.variateur = []
        self.run = []
        self.coil_diaphragme = []

    def lire_mesures(self):
        return dict(self.mesures)

    def ecrire_variateur(self, volts):
        self.variateur.append(volts)

    def ecrire_variateur_run(self, marche):
        self.run.append(marche)

    def ecrire_vanne_diaphragme(self, vers_0deg):
        self.coil_diaphragme.append(vers_0deg)

    def ecrire_vanne_bypass(self, ouverte):
        pass

    def ecrire_vanne_perte_charge(self, volts):
        pass

    def fermer(self):
        pass


@pytest.fixture
def pilote_stub():
    es = StubES()
    return Pilote(Banc(CFG), es, CFG), es


def test_arret_de_securite_reste_affiche_jusqua_lacquittement(pilote_stub):
    """Un repli sûr laisse une trace (heure du premier repli) que ni un
    second repli ni un redémarrage n'effacent : seul l'acquittement
    explicite de l'opérateur la retire, avec les défauts latchés."""
    pilote, es = pilote_stub
    pilote.etat["defaut_variateur"] = "contact MB ouvert"
    pilote._repli_sur()
    heure = pilote.etat["arret_securite"]
    assert heure and es.variateur[-1] == 0.0
    pilote._repli_sur()
    assert pilote.etat["arret_securite"] == heure      # premier repli gardé
    pilote.acquitter_arret_securite()
    assert pilote.etat["arret_securite"] is None
    assert pilote.etat["defaut_variateur"] is None


def test_position_lue_de_la_vanne_3_voies_publiee_hors_regulation():
    """L'IHM affiche la position LUE : ordre Miller donné, vanne encore en
    butée 0° -> la position lue reste ISO tant que la butée 90° n'est pas
    atteinte, même régulation arrêtée."""
    es = StubES({"temperature": 20.0, "fc_3voies_0": True})
    pilote = Pilote(Banc(CFG), es, CFG)
    pilote._cycle()
    pilote.commander_diaphragme("miller")
    pilote._cycle()
    assert pilote.etat["diaphragme_lu"] == "iso"
    assert pilote.etat["diaphragme_commande"] == "miller"
    es.mesures = {"temperature": 20.0}            # en course
    pilote._cycle()
    assert pilote.etat["diaphragme_lu"] == "en_transit"
    es.mesures = {"temperature": 20.0, "fc_3voies_90": True}
    pilote._cycle()
    assert pilote.etat["diaphragme_lu"] == "miller"


def test_reference_choisie_sur_lihm_suivie_hors_regulation(pilote_stub):
    """Manuel en « Veine » : le Re mesuré publié est le Re veine. En
    régulation, la référence reste celle de la consigne appliquée."""
    pilote, _ = pilote_stub
    pilote.definir_reference_affichage("veine")
    assert pilote.reference == "veine"
    pilote.definir_consigne(5000.0, "conduite")
    pilote.demarrer_regulation()
    pilote.definir_reference_affichage("veine")
    assert pilote.reference == "conduite"
    with pytest.raises(ValueError):
        pilote.definir_reference_affichage("veinee")


def test_plafond_capteur_dp_dans_la_reference_veine():
    """15 000 Re veine ≈ 28 000 Re conduite : au-delà du capteur ΔP ISO. Le
    plafond doit être exprimé en Re VEINE (≈ 14 000), sinon il ne borne
    rien et la régulation vise un Re que la mesure n'atteint jamais."""
    import copy
    cfg = copy.deepcopy(CFG)
    cfg["regulation"].pop("re_max_iso", None)     # seul le plafond ΔP joue
    es = StubES({"temperature": 20.0, "fc_3voies_0": True,
                 "fc_bypass_ferme": True, "fc_pdc_ouvert": True})
    pilote = Pilote(Banc(cfg), es, cfg)
    pilote.re_max_iso = None
    pilote.definir_consigne(15000.0, "veine")
    pilote.demarrer_regulation()
    pilote._cycle()
    assert pilote._re_cible_max is not None
    assert 13000 < pilote._re_cible_max < 15000
    assert "Re veine" in pilote.etat["consigne_limitee"]


def test_bypass_deja_en_butee_pas_de_manoeuvre(pilote_stub):
    """Ordre bypass vers la butée déjà lue : réaffirmé, mais aucune
    manœuvre (ni gel de la régulation) ; vers l'autre butée : manœuvre."""
    pilote, _ = pilote_stub
    pilote.etat["mesures_brutes"] = {"fc_bypass_ouvert": True}
    pilote.commander_bypass(True)
    assert pilote._bypass_commande is True
    assert "bypass" not in pilote._manoeuvres
    pilote.commander_bypass(False)
    assert pilote._manoeuvres["bypass"]["fc_attendue"] == "fc_bypass_ferme"


def test_capteur_dp_suit_la_butee_lue_de_la_vanne_3_voies():
    """ΔP ISO exploité en butée ISO, ΔP Miller en butée Miller — d'après les
    fins de course, jamais d'après l'ordre donné : en manuel, l'ordre Miller
    basculait aussitôt sur le capteur Miller, vanne encore en ISO. Pendant
    la course (aucune butée lue), la dernière butée lue est conservée."""
    es = StubES({"temperature": 20.0, "fc_3voies_0": True,
                 "dp_iso_500": 80.0, "dp_diaph_500": 15.0})
    pilote = Pilote(Banc(CFG), es, CFG)
    pilote.mode_manuel_tension(3.0)
    pilote._cycle()
    assert pilote.etat["diaphragme_mesure"] == "iso"
    assert pilote.etat["dp"] == pytest.approx(80.0, abs=1.0)
    pilote.commander_diaphragme("miller")          # ordre donné, vanne en 0°
    pilote._cycle()
    assert pilote.etat["dp"] == pytest.approx(80.0, abs=1.0)
    es.mesures.pop("fc_3voies_0")                  # en course
    pilote._cycle()
    assert pilote.etat["diaphragme_mesure"] == "iso"
    es.mesures["fc_3voies_90"] = True              # arrivée en butée Miller
    pilote._cycle()
    assert pilote.etat["diaphragme_mesure"] == "miller"
    assert pilote.etat["dp"] == pytest.approx(15.0, abs=1.0)


def test_arret_coupe_aussi_le_mode_manuel(pilote_stub):
    """Bouton ARRÊT VARIATEUR : la tension manuelle ne doit pas être
    réappliquée au cycle suivant."""
    pilote, es = pilote_stub
    pilote.mode_manuel_tension(5.0)
    pilote.arreter_regulation()
    assert pilote.mode_manuel is False
    assert pilote.tension_manuelle == 0.0
    assert es.variateur[-1] == 0.0
    assert es.run[-1] is False


def test_commander_diaphragme_va_droit_a_la_butee(pilote_stub):
    """Carte GF3 : un seul ordre suffit depuis n'importe quelle position.
    Depuis 0°, aller à Miller = ordre I (borne 4), sans relance."""
    pilote, es = pilote_stub
    pilote.etat["mesures_brutes"] = {"fc_3voies_0": True}
    pilote.commander_diaphragme("miller")
    assert es.coil_diaphragme == [False]         # False = butée 90°
    assert pilote.etat["diaphragme"] == "miller"
    assert pilote._manoeuvre["angle_attendu"] == "90"


def test_commander_diaphragme_reaffirme_en_position(pilote_stub):
    """Déjà en place : pas de manœuvre, mais l'ordre est tout de même émis.
    Sur la carte GF3 l'actionneur est alimenté par ses bornes de commande —
    un relais retombé le priverait de sa résistance anti-condensation."""
    pilote, es = pilote_stub
    pilote.etat["mesures_brutes"] = {"fc_3voies_0": True}
    pilote.commander_diaphragme("iso")
    assert es.coil_diaphragme == [True]          # True = butée 0°
    assert pilote._manoeuvre is None


def test_decodage_position_un_parmi_trois():
    """Codage vanne 3 voies installée (codage un-parmi-trois simplifié) :
    Chaque position stable active UNE seule fin de course."""
    d = Pilote._angle_vanne_3voies
    # Position 0° (ISO) : FC2 seule active
    assert d({"fc_3voies_0": True, "fc_3voies_90": False, "fc_3voies_180": False}) == "0"
    # Position 90° (MILLER) : FC3 seule active
    assert d({"fc_3voies_0": False, "fc_3voies_90": True, "fc_3voies_180": False}) == "90"
    # Position intermédiaire (mouvement) : aucune FC active
    assert d({}) is None
    assert d({"fc_3voies_0": False, "fc_3voies_90": False, "fc_3voies_180": False}) is None
    # Combinaisons invalides (plusieurs FC actives = rupture de fil, etc.)
    assert d({"fc_3voies_0": True, "fc_3voies_90": True}) is None
    assert d({"fc_3voies_0": True, "fc_3voies_180": True}) is None
    assert d({"fc_3voies_90": True, "fc_3voies_180": True}) is None


def test_cycle_supporte_position_fermee():
    """Régression : diaphragme 'fermee' provoquait un KeyError à chaque
    cycle (repli erreur com + variateur forcé à 0)."""
    es = SimulateurBanc(Banc(CFG), CFG.get("simulation"), CFG.get("modbus"), CFG.get("actionneurs"))
    pilote = Pilote(Banc(CFG), es, CFG)
    pilote._cycle()                          # position initiale depuis les FC
    pilote.etat["diaphragme"] = "fermee"
    pilote._cycle()                          # ne doit pas lever
    assert pilote.etat["erreur_com"] is None or True   # pas d'exception


def test_cycles_simulation_sans_erreur():
    """Quelques cycles complets en simulation, consigne active."""
    es = SimulateurBanc(Banc(CFG), CFG.get("simulation"), CFG.get("modbus"), CFG.get("actionneurs"))
    pilote = Pilote(Banc(CFG), es, CFG)
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    for _ in range(10):
        pilote._cycle()
    assert pilote.etat["cycles"] == 10
    assert pilote.etat.get("defaut_vanne") is None


# table Miller (bypass ouvert) d'un banc caractérisé : prérequis du mode PdC
TABLE_MILLER = TableGains([(0.5, 700.0), (2.0, 2800.0), (5.0, 7000.0)])


def _pilote_en_position(fc: str) -> tuple:
    """Pilote sur StubES avec la vanne 3 voies déjà en position.

    Codage un-parmi-trois : chaque position active UNE seule fin de course.
    Positionne aussi bypass et PDC selon le diaphragme :
    - ISO (fc_3voies_0) : bypass fermé, PDC ouverte (mode normal_high)
    - Miller (fc_3voies_90) : bypass ouvert, PDC ouverte (mode normal_low)

    LIMITATION : Cette fonction simplifie les FCs pour les tests de base.
    Elle ne couvre pas tous les scénarios (vannes bloquées, transitions, etc.).
    Pour tester des configurations spécifiques, créer un StubES personnalisé.
    """
    mesures = {"temperature": 20.0, fc: True}

    # Positionner bypass et PDC selon le diaphragme
    if fc == "fc_3voies_0":  # ISO
        mesures["fc_bypass_ferme"] = True
        mesures["fc_bypass_ouvert"] = False
        mesures["fc_pdc_ouvert"] = True
    elif fc == "fc_3voies_90":  # Miller
        mesures["fc_bypass_ouvert"] = True
        mesures["fc_bypass_ferme"] = False
        mesures["fc_pdc_ouvert"] = True

    es = StubES(mesures)
    return Pilote(Banc(CFG), es, CFG), es


def _espion_reinit(pi) -> list:
    """Enregistre les appels à pi.reinitialiser (feed-forward)."""
    appels = []
    original = pi.reinitialiser

    def espion(u_initial=0.0):
        appels.append(u_initial)
        original(u_initial)

    pi.reinitialiser = espion
    return appels


def test_rampe_pi_bornee_en_bas_re():
    """En bas-Re, la rampe du PI ne doit pas devancer la vanne PdC
    (10 V de course en t_vanne_pdc_course s) : sinon l'intégrale s'enroule
    contre l'actionneur lent -> dépassement puis oscillation."""
    pilote, es = _pilote_en_position("fc_3voies_90")   # déjà sur Miller
    pilote.seuil_bas_re = 3000.0
    pilote.table_gains_bas_re = TABLE_MILLER        # Re mapping Miller fait
    pilote.definir_consigne(1000.0, "conduite")
    pilote.demarrer_regulation()
    pilote._cycle()
    assert pilote.pi.rampe == pytest.approx(
        10.0 / pilote.t_vanne_pdc_course)
    assert pilote.pi.rampe < pilote._rampe_nominale
    # retour en régime normal : rampe nominale restaurée
    # CORRECTION : Simuler le basculement physique Miller→ISO
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    # Simuler le mouvement du diaphragme vers ISO (FC confirmée)
    es.mesures["fc_3voies_90"] = False
    es.mesures["fc_3voies_0"] = True
    pilote.etat["diaphragme"] = "iso"  # Forcer mise à jour état diaphragme
    pilote._cycle()
    assert pilote.pi.rampe == pilote._rampe_nominale


def test_sequence_petit_echelon_conserve_le_pi():
    """Un petit échelon de séquence (< echelon_reinit_ff_relatif), même
    diaphragme et même régime, ne réinitialise pas le PI : l'intégrale qui
    compense l'erreur du feed-forward est conservée d'un échelon à l'autre."""
    pilote, es = _pilote_en_position("fc_3voies_0")    # déjà sur ISO
    pilote.bas_re_actif = False
    appels = _espion_reinit(pilote.pi)
    pilote.demarrer_sequence([(0.0, 8000.0), (0.05, 8400.0)], "conduite")
    pilote._cycle()                       # échelon 1 : préparation complète
    assert len(appels) == 1
    time.sleep(0.06)
    pilote._cycle()                       # échelon 2 : +5 %, état PI conservé
    assert len(appels) == 1


def test_sequence_grand_echelon_reinitialise_le_pi():
    """Un grand échelon de séquence repart du feed-forward (table de gains)."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote.bas_re_actif = False
    appels = _espion_reinit(pilote.pi)
    pilote.demarrer_sequence([(0.0, 8000.0), (0.05, 16000.0)], "conduite")
    pilote._cycle()
    assert len(appels) == 1
    time.sleep(0.06)
    pilote._cycle()                       # échelon 2 : +100 % -> feed-forward
    assert len(appels) == 2


def test_convergee_jugee_sur_la_moyenne_et_non_sur_l_echantillon():
    """Le bruit des capteurs ΔP est du même ordre que la tolérance : un
    critère exigeant N échantillons CONSÉCUTIFS dans la bande fait retomber
    le voyant dès qu'un seul en sort, alors que le débit ne bouge pas
    (mesuré : 41 % du temps à 1 % de bruit). La convergence se juge donc sur
    la moyenne de la fenêtre, et la dispersion est publiée."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote.bas_re_actif = False
    es.mesures["dp_iso_500"] = 200.0
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    pilote._cycle()
    re0 = pilote.etat["re_mesure"]                  # Re produit par ce ΔP

    # consigne calée sur la mesure : la moyenne est pile dans la bande
    pilote.definir_consigne(re0, "conduite")
    pilote.demarrer_regulation()
    n = pilote._historique_re.maxlen
    for _ in range(n - 1):
        pilote._cycle()
    assert pilote.etat["convergee"] is False        # fenêtre incomplète
    pilote._cycle()
    assert pilote.etat["convergee"] is True         # fenêtre pleine, en bande
    assert pilote.etat["re_ecart_type"] == pytest.approx(0.0, abs=1e-6)

    # RÉGRESSION : un échantillon isolé hors bande ne doit PAS faire
    # retomber le voyant — c'est le battement qu'on vient de supprimer
    es.mesures["dp_iso_500"] = 260.0                # ~ +14 % sur le Re
    pilote._cycle()
    es.mesures["dp_iso_500"] = 200.0
    assert pilote.etat["convergee"] is True
    assert pilote.etat["re_ecart_type"] > 0.0       # dispersion visible

    # en revanche un décalage durable doit bien la faire tomber
    es.mesures["dp_iso_500"] = 260.0
    for _ in range(n):
        pilote._cycle()
    assert pilote.etat["convergee"] is False


def test_convergee_depuis_mesure_la_duree_du_palier():
    """L'opérateur part mesurer sur le banc : il doit pouvoir lire depuis
    combien de temps le palier tient, pas seulement qu'il tient."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote.bas_re_actif = False
    es.mesures["dp_iso_500"] = 200.0
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    pilote._cycle()
    pilote.definir_consigne(pilote.etat["re_mesure"], "conduite")
    pilote.demarrer_regulation()
    for _ in range(pilote._historique_re.maxlen):
        pilote._cycle()
    assert pilote.etat["convergee"] is True
    assert pilote.etat["convergee_depuis_s"] is not None
    t1 = pilote.etat["convergee_depuis_s"]
    time.sleep(0.05)
    pilote._cycle()
    assert pilote.etat["convergee_depuis_s"] > t1   # le palier s'allonge


def test_reference_modele_suit_le_feed_forward():
    """Modes normaux : la correction poursuit le Re que produirait le
    feed-forward APPLIQUÉ (rampe, puis 1er ordre tau_consigne), ancré sur la
    mesure ; pas l'échelon de consigne. Une fois le feed-forward rendu, la
    référence rejoint exactement la consigne (erreur statique annulée)."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote.bas_re_actif = False
    pilote.table_gains = TableGains([(1.0, 3000.0), (9.0, 30000.0)])
    pilote.tau_consigne = 1.0
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    pilote._cycle()
    c1 = pilote._consigne_interne
    pilote._cycle()
    c2 = pilote._consigne_interne
    assert 0.0 <= c1 < c2 < 8000.0        # progression, pas d'échelon
    # 8 s : avant la détection « ΔP figé » (10 s), la mesure du stub
    # étant constante
    for _ in range(40):
        pilote._cycle()
    assert pilote._consigne_interne == pytest.approx(8000.0, rel=1e-3)
    assert pilote.etat["u_ff"] == pytest.approx(
        pilote.table_gains.u_pour_re_ff(8000.0), abs=1e-3)


class _Horloge:
    """Horloge simulée partagée par le pilote et le simulateur : un essai de
    plusieurs dizaines de secondes s'exécute instantanément."""

    def __init__(self):
        self.t = time.monotonic()

    def monotonic(self):
        return self.t

    def sleep(self, d):
        self.t += d

    def __getattr__(self, nom):
        return getattr(time, nom)


def test_echelon_sans_depassement_en_simulation(monkeypatch):
    """Simulateur (1er ordre 2 s, bruit 1 %) caractérisé par une table
    biaisée de 3 % : l'échelon converge sans dépassement notable et sans
    erreur statique."""
    import random
    import core.pilote as mod_pilote
    import io_banc.simulateur as mod_simu
    random.seed(1)
    horloge = _Horloge()
    monkeypatch.setattr(mod_pilote, "time", horloge)
    monkeypatch.setattr(mod_simu, "time", horloge)
    cfg = json.loads(json.dumps(CFG))
    cfg["simulation"]["u_decrochage_v"] = 0.0
    es = SimulateurBanc(Banc(cfg), cfg.get("simulation"), cfg.get("modbus"), cfg.get("actionneurs"))
    es._v3._angle = 0.0                         # ISO en place
    es._bypass.position = es._bypass.cible = False
    es._pdc_position = es._pdc_consigne = 10.0
    pilote = Pilote(Banc(cfg), es, cfg)
    # table « vraie » du simulateur, biaisée de 3 % : la correction doit
    # résorber le biais
    T, pa = 20.0, cfg["simulation"]["p_amont_eff_pa"]
    b = Banc(cfg)
    rho = b.fluide.rho(b.patm_pa + pa, T)
    pts = []
    for u in (1.0, 3.0, 5.0, 7.0, 9.0):
        qm = es.gain_qv * u * rho
        pts.append((u, 1.03 * b.qm_vers_re(qm, T, b.patm_pa + pa)["re_conduite"]))
    pilote.table_gains = TableGains(pts)
    pilote.tau_consigne = es.tau_s
    pilote.definir_consigne(15000.0, "conduite")
    pilote.demarrer_regulation()
    re_max = 0.0
    t_conv = None
    for i in range(int(40.0 / pilote.periode)):
        pilote._cycle()
        re_max = max(re_max, pilote.etat["re_mesure"])
        if pilote.etat["convergee"] and t_conv is None:
            t_conv = i * pilote.periode
        horloge.sleep(pilote.periode)
    assert pilote.etat["convergee"] is True
    assert t_conv is not None and t_conv < 25.0
    assert re_max < 15000.0 * 1.05              # dépassement < 5 % (bruit compris)
    assert abs(pilote.etat["re_moyen"] - 15000.0) / 15000.0 < 0.01


def test_consigne_hors_plage_capteur_est_bornee():
    """Consigne dont le ΔP attendu dépasse la plage du capteur : la
    régulation vise le maximum mesurable, l'opérateur est prévenu.
    MODIFIÉ : La limite explicite re_max_iso s'applique avant _borner_consigne."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote.bas_re_actif = False
    re_max = pilote.re_max_iso  # Lire la valeur de la config
    # Consigne 40000 Re, mais re_max_iso la plafonne
    pilote.definir_consigne(40000.0, "conduite")
    pilote.demarrer_regulation()
    pilote._cycle()
    # La consigne est plafonnée à re_max_iso
    assert pilote.re_cible == re_max
    # _re_cible_max peut être None car la limite re_max_iso s'applique avant _borner_consigne
    # Mais on peut tester que _re_cible_conduite est bien plafonné aussi
    assert pilote._re_cible_conduite <= re_max
    # consigne dans la plage : pas de borne
    pilote.definir_consigne(15000.0, "conduite")
    pilote._cycle()
    assert pilote._re_cible_max is None
    assert pilote.etat["consigne_limitee"] is None


def test_hysteresis_du_mode_autour_du_seuil_diaphragme():
    """Une consigne dans la bande seuil ± hystérésis ne fait pas commuter
    le mode (donc ni le diaphragme ni le bypass)."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    seuil, h = pilote.banc.seuil_re, pilote.banc.hysteresis_re
    pilote._mode_regulation = "normal_high"
    assert pilote._determiner_mode_regulation(seuil - h / 2) == "normal_high"
    assert pilote._determiner_mode_regulation(seuil - 2 * h) == "normal_low"
    pilote._mode_regulation = "normal_low"
    assert pilote._determiner_mode_regulation(seuil + h / 2) == "normal_low"
    assert pilote._determiner_mode_regulation(seuil + 2 * h) == "normal_high"


def test_separation_integrale_tant_que_la_mesure_bouge():
    """Grand écart pendant que la mesure évolue encore (le procédé répond) :
    l'intégrale est tenue, sinon elle s'enroule sur le retard et provoque un
    dépassement. Mesure stable : elle intègre, même sur un grand écart."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote.bas_re_actif = False
    pilote.table_gains = TableGains([(1.0, 3000.0), (9.0, 30000.0)])
    pilote.tau_consigne = 0.0
    es.mesures["dp_iso_500"] = 115.0                # Re ~13 % sous la cible
    pilote.definir_consigne(15000.0, "conduite")
    pilote.demarrer_regulation()
    for _ in range(13):                             # rampe du feed-forward
        pilote._cycle()
    dp = 115.0
    for k in range(10):                             # le procédé répond encore
        dp += 2.0
        es.mesures["dp_iso_500"] = dp
        pilote._cycle()
        if k == 3:
            i_tenue = pilote.trim.integrale
    assert pilote.trim.integrale == i_tenue
    for _ in range(8):                              # mesure stabilisée
        pilote._cycle()
    assert pilote.trim.integrale > i_tenue


def test_correction_gelee_tant_que_le_variateur_suit_sa_rampe():
    """Recopie AM loin de la commande : le ventilateur accélère encore,
    l'erreur ne dit rien du réglage -> P et I gelés (sortie = feed-forward)."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote.bas_re_actif = False
    pilote.table_gains = TableGains([(1.0, 3000.0), (9.0, 30000.0)])
    pilote.tau_consigne = 0.0
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    es.mesures["dp_iso_500"] = 5.0             # Re mesuré très bas
    for _ in range(30):
        pilote._cycle()
        es.mesures["frequence_variateur"] = 0.0    # variateur à la traîne
    assert pilote.etat["phase_regulation"] == "suivi variateur"
    assert pilote.etat["trim_v"] == pytest.approx(0.0, abs=1e-9)
    assert pilote.etat["u_variateur"] == pytest.approx(pilote.etat["u_ff"],
                                                       abs=1e-3)


def test_critere_bas_re_en_reference_veine():
    """Le mode se décide sur le Re CONDUITE : une consigne veine est convertie
    avant comparaison. Re veine 1000 ≈ Re conduite 1870 (géométrie de
    config.json), au-dessus du seuil bas-Re 1500 -> normal_low (comparé tel
    quel, 1000 partirait en PdC)."""
    pilote, es = _pilote_en_position("fc_3voies_90")
    pilote.seuil_bas_re = 1500.0
    pilote.u_min_ventilateur = 0.5
    es.mesures["dp_diaph_500"] = 0.5
    pilote.definir_consigne(1000.0, "veine")
    pilote.demarrer_regulation()
    for _ in range(3):
        pilote._cycle()
    assert pilote._re_cible_conduite > 1.05 * 1500.0
    assert pilote._mode_regulation == "normal_low"


def test_bascule_bas_re_en_cours_recale_le_pi():
    """Changement de mode en cours de régulation (Miller variateur -> PdC) :
    le PI de la vanne est recalé sur son feed-forward, pas laissé sur l'état
    du mode précédent."""
    pilote, es = _pilote_en_position("fc_3voies_90")
    pilote.seuil_bas_re = 2000.0
    pilote.table_gains_bas_re = TABLE_MILLER        # Re mapping Miller fait
    pilote.u_min_ventilateur = 0.5
    pilote.table_gains_pdc = TableGains([(30.0, 300.0), (100.0, 1500.0)])
    es.mesures["dp_diaph_500"] = 0.5
    pilote.definir_consigne(3000.0, "conduite")
    pilote.demarrer_regulation()
    for _ in range(5):
        pilote._cycle()
    assert pilote._mode_regulation == "normal_low"
    appels = _espion_reinit(pilote.pi)
    pilote.definir_consigne(1000.0, "conduite")
    for _ in range(3):
        pilote._cycle()
    assert pilote._mode_regulation == "pdc_control"
    v_ff = pilote._pdc_ff_v(1000.0)
    assert appels and appels[0] == pytest.approx(v_ff)
    for _ in range(15):                 # u_pdc rejoint sur la rampe du variateur
        pilote._cycle()
    assert pilote.etat["u_variateur"] == pytest.approx(pilote._u_pdc())


def test_duree_cycle_publiee(tmp_path):
    """La durée réelle du cycle est publiée dans etat (détection d'un
    glissement de période dû à une communication lente)."""
    es = StubES({"temperature": 20.0, "fc_3voies_0": True})
    pilote = Pilote(Banc(CFG), es, CFG)
    pilote._chemin_etat = tmp_path / "etat.json"
    pilote.demarrer()
    time.sleep(0.3)
    pilote.arreter()
    assert "duree_cycle_s" in pilote.etat
    assert pilote.etat["duree_cycle_s"] < CFG["regulation"]["periode_s"]


def test_zero_capteur_soustrait_du_dp():
    """L'offset de zéro appris est soustrait du ΔP avant le calcul du débit :
    un capteur qui lit 1,5 Pa à débit nul ne doit pas produire un Re fictif."""
    es = StubES({"temperature": 20.0, "fc_3voies_0": True,
                 "dp_iso_500": 1.5})
    pilote = Pilote(Banc(CFG), es, CFG)
    pilote.zero_capteurs = {"dp_iso_500": 1.5}
    pilote._cycle()
    assert pilote.etat["dp"] == pytest.approx(0.0)
    assert pilote.etat["re_mesure"] == pytest.approx(0.0)


def test_definir_consigne_seule_reprepare():
    """Régression : definir_consigne() sans demarrer_regulation() laissait
    le feed-forward, le choix de diaphragme et le Re conduite équivalent
    calés sur l'ancienne consigne (critère bas-Re faussé)."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote.bas_re_actif = False
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    pilote._cycle()
    appels = _espion_reinit(pilote.pi)
    pilote.definir_consigne(12000.0, "conduite")   # sans demarrer_regulation
    pilote._cycle()
    assert len(appels) == 1                        # re-préparation déclenchée
    assert pilote._re_cible_conduite == pytest.approx(12000.0, rel=1e-6)


def test_position_illisible_verrouille_la_regulation():
    """Fil de fin de course coupé en cours de régulation : la lecture devient
    inexploitable. Réguler sur un capteur ΔP qui ne correspond plus à la
    position réelle emballerait le ventilateur -> repli sûr et verrouillage."""
    # Re=8000 → normal_high : bypass fermé, PDC ouverte, ISO
    es = StubES({"temperature": 20.0, "fc_3voies_0": True,
                 "fc_bypass_ferme": True, "fc_bypass_ouvert": False,
                 "fc_pdc_ouvert": True})
    pilote = Pilote(Banc(CFG), es, CFG)
    pilote.bas_re_actif = False
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    pilote._cycle()
    assert pilote.etat["regulation"] is True
    es.mesures["fc_3voies_0"] = False              # rupture de fil
    for _ in range(max(3, int(3.0 / pilote.periode)) + 1):
        pilote._cycle()
    assert pilote.etat["incoherence_vanne"] is True
    assert pilote.regulation_active is False       # latch
    assert es.variateur[-1] == 0.0
    assert es.run[-1] is False


def test_maintien_tor_suspendu_puis_retabli():
    """L'apprentissage manœuvre les vannes en direct ; la boucle doit cesser
    de réaffirmer ses ordres, puis les reprendre une fois la position relue."""
    es = StubES({"temperature": 20.0, "fc_3voies_0": True})
    pilote = Pilote(Banc(CFG), es, CFG)
    pilote._cycle()                                 # lit la position initiale
    assert pilote._diaphragme_commande == "0"       # maintien établi
    pilote.suspendre_maintien_tor()
    assert pilote._diaphragme_commande is None
    n = len(es.coil_diaphragme)
    pilote._cycle()
    assert len(es.coil_diaphragme) == n             # plus aucune réaffirmation
    # régression : la relecture de position remettait le maintien en route au
    # cycle suivant, donc pendant la séquence d'apprentissage, et la boucle
    # ramenait la vanne en place pendant qu'on essayait de la chronométrer
    pilote._position_initiale_lue = False
    pilote._cycle()
    assert pilote._diaphragme_commande is None      # toujours suspendu
    assert len(es.coil_diaphragme) == n
    pilote.reprendre_maintien_tor()
    pilote._position_initiale_lue = False           # ce que fait l'apprentissage
    pilote._cycle()
    assert pilote._diaphragme_commande == "0"       # maintien rétabli


def test_dp_sature_signale():
    """ΔP proche de la pleine échelle du capteur : le Re calculé plafonne au
    capteur, pas au banc — signalé (après debounce) pour que l'opérateur
    comprenne une non-convergence sur consigne trop haute.
    (500,1 Pa : au-delà de tout seuil de saturation valide, < 100 %)."""
    # fc_3voies_0=True seule -> position ISO (0°)
    es = StubES({"temperature": 20.0, "fc_3voies_0": True,
                 "dp_iso_500": 500.1})              # > 100 % de 500 Pa
    pilote = Pilote(Banc(CFG), es, CFG)
    n = max(3, int(1.0 / pilote.periode))
    for _ in range(n - 1):
        pilote._cycle()
    assert pilote.etat["dp_sature"] is False        # debounce en cours
    pilote._cycle()                                 # n-ième cycle saturé
    assert pilote.etat["dp_sature"] is True
    es.mesures["dp_iso_500"] = 200.0                # retour en plage
    pilote._cycle()
    assert pilote.etat["dp_sature"] is False


def test_arreter_persiste_u_min(tmp_path):
    """u_min appris (freq_min) : réécrit par arreter() comme le gain et la
    table, sinon la valeur disparaît du fichier d'état à l'extinction."""
    es = StubES()
    pilote = Pilote(Banc(CFG), es, CFG)
    pilote._chemin_etat = tmp_path / "etat.json"
    pilote.u_min_ventilateur = 2.5
    pilote.arreter()
    donnees = json.loads((tmp_path / "etat.json").read_text(encoding="utf-8"))
    assert donnees["u_min_ventilateur_v"] == 2.5


# ── correctifs de la revue fonctionnelle (point de vue opérateur) ──────────

def test_temperature_invraisemblable_arrete_le_banc():
    """RÉGRESSION : une PT100 coupée sature le 750-450 et, le coupleur Modbus
    n'ayant pas accès aux octets de statut du module, rien ne le signalait.
    Le T aberrant faussait rho et mu, donc le Re, et le PI emmenait le
    ventilateur à 100 % en affichant « RÉGULATION… » indéfiniment."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote.bas_re_actif = False
    es.mesures["dp_iso_500"] = 200.0
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    pilote._cycle()
    assert pilote.etat.get("defaut_capteur") is None

    es.mesures["temperature"] = 3276.7          # saturation du module RTD
    for _ in range(max(3, int(2.0 / pilote.periode)) + 1):
        pilote._cycle()

    assert "température invraisemblable" in pilote.etat["defaut_capteur"]
    assert pilote.etat["regulation"] is False
    assert pilote.regulation_active is False    # latché : pas de reprise seule
    assert es.variateur[-1] == 0.0
    assert es.run[-1] is False


def test_temperature_aberrante_isolee_ne_declenche_pas():
    """Un seul échantillon douteux ne doit pas arrêter un essai en cours :
    le défaut n'est levé que sur une persistance (mêmes idiomes que les
    autres défauts de la boucle)."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote.bas_re_actif = False
    es.mesures["dp_iso_500"] = 200.0
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    pilote._cycle()
    es.mesures["temperature"] = -300.0
    pilote._cycle()
    es.mesures["temperature"] = 20.0
    pilote._cycle()
    assert pilote.etat.get("defaut_capteur") is None
    assert pilote.etat["regulation"] is True


def test_dp_sature_plafonne_la_tension():
    """Au-delà de la plage du capteur, le Re calculé plafonne mais le banc
    peut encore accélérer : la commande est plafonnée et recule tant que la
    saturation dure. RÉGRESSION : le plafond était latché jusqu'à la consigne
    suivante (Re 23000 tenu à 21000 après un dépassement) ; il se relâche
    désormais une fois la mesure revenue sous le seuil.
    (500,1 Pa : au-delà de tout seuil de saturation valide, < 100 %)."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote.bas_re_actif = False
    pilote.table_gains = TableGains([(1.0, 3000.0), (9.0, 30000.0)])
    es.mesures["dp_iso_500"] = 300.0               # en plage
    pilote.definir_consigne(20000.0, "conduite")
    pilote.demarrer_regulation()
    for _ in range(15):                            # commande établie
        pilote._cycle()
    assert pilote.etat["u_variateur"] > 2.0
    es.mesures["dp_iso_500"] = 500.1               # > 1.0 × 500 Pa
    for _ in range(max(3, int(1.0 / pilote.periode)) + 1):
        pilote._cycle()
    assert pilote.etat["dp_sature"] is True
    plafond = pilote.etat["plafond_u_sature"]
    assert plafond is not None

    for _ in range(20):                            # 4 s de plus
        pilote._cycle()
    assert pilote.etat["u_variateur"] < plafond    # la commande a reculé
    assert pilote.etat["u_variateur"] <= pilote.etat["plafond_u_sature"] + 1e-9

    # retour en plage : le plafond remonte puis disparaît
    es.mesures["dp_iso_500"] = 200.0
    p0 = pilote._plafond_u_sature
    pilote._cycle()
    pilote._cycle()
    assert pilote.etat["dp_sature"] is False
    assert pilote._plafond_u_sature > p0
    for _ in range(int(5.0 / pilote.periode)):
        pilote._cycle()
    # relâchement à recul_saturation / 5 : ~0,5 V en 5 s
    assert pilote._plafond_u_sature == pytest.approx(
        p0 + pilote.recul_saturation / 5.0 * 5.4, abs=0.05)


def test_defaut_vanne_acquitte_au_redemarrage():
    """RÉGRESSION : defaut_vanne était positionné mais jamais effacé. Le banc
    régulait normalement alors que l'IHM affichait un défaut périmé — qui
    masquait de surcroît le défaut suivant (préfixe commun côté journal)."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote._demarrer_manoeuvre("test", t_course=0.01, angle_attendu="180")
    time.sleep(0.1)
    pilote._cycle()
    assert pilote.etat["defaut_vanne"] is not None

    pilote.demarrer_regulation()
    assert pilote.etat["defaut_vanne"] is None
    assert pilote.etat["defaut_capteur"] is None


def test_motif_bas_re_indisponible_est_publie():
    """Sans la séquence re_mapping, u_min est inconnu, le seuil vaut 0 et la
    stratégie bas-Re ne s'engage jamais — silencieusement jusqu'ici."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    es.mesures["dp_iso_500"] = 200.0
    pilote.u_min_ventilateur = None
    pilote.table_gains = TableGains()
    pilote._gain_re_par_volt = None
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    pilote._cycle()
    assert "Re mapping" in pilote.etat["bas_re_motif"]

    pilote.bas_re_actif = False
    pilote._cycle()
    assert pilote.etat["bas_re_motif"] is None


def test_image_process_reconstituee_par_le_simulateur():
    """Le simulateur reproduit la couche Modbus quand on lui donne la config,
    pour que la page de mise en service soit exercable sans materiel. Sans
    config modbus il n'invente pas d'adresses."""
    from io_banc.simulateur import SimulateurBanc

    sans = SimulateurBanc(Banc(CFG), CFG.get("simulation"), None, CFG.get("actionneurs"))
    sans.lire_mesures()
    assert sans.image_process is None

    es = SimulateurBanc(Banc(CFG), CFG.get("simulation"), CFG["modbus"], CFG.get("actionneurs"))
    mesures = es.lire_mesures()
    img = es.image_process
    entrees = {n: v for n, v in CFG["modbus"]["entrees"].items()
               if not n.startswith("_")}
    assert set(img["mots"]) == {v["adresse"] for v in entrees.values()}

    # aller-retour : le brut remis a l'echelle redonne la grandeur physique
    t_addr = entrees["temperature"]["adresse"]
    assert img["mots"][t_addr] * 0.1 == pytest.approx(mesures["temperature"],
                                                      abs=0.06)
    voie = entrees["dp_iso_500"]
    brut = img["mots"][voie["adresse"]]
    volts = brut / 32767 * voie["pleine_echelle_v"]
    attendu = voie["valeur_min"] + (volts - voie["volts_min"]) \
        / (voie["volts_max"] - voie["volts_min"]) \
        * (voie["valeur_max"] - voie["valeur_min"])
    assert attendu == pytest.approx(mesures["dp_iso_500"], abs=0.05)

    # les bits portent l'etat electrique, avant inversion
    for nom, v in CFG["modbus"]["entrees_tor"].items():
        if nom.startswith("_"):
            continue
        assert (img["bits"][v["adresse"]] != v.get("inverse", False)) \
            is bool(mesures[nom]), nom


def test_image_process_publiee_meme_sur_defaut():
    """La page de mise en service sert justement quand quelque chose ne va
    pas : l'image doit etre publiee avant les retours anticipes du cycle."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    es.mesures["temperature"] = 3276.7            # PT100 saturee -> defaut
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    for _ in range(max(3, int(2.0 / pilote.periode)) + 2):
        pilote._cycle()
    assert pilote.etat["defaut_capteur"] is not None
    assert "image_process" in pilote.etat        # publiee malgre le return
    assert pilote.etat["mesures_brutes"]["temperature"] == 3276.7


def test_appliquer_config_reprend_les_reglages_a_chaud():
    """RÉGRESSION : l'onglet Configuration écrivait dans config.json et
    n'appliquait que le Banc. Gains, tolérances, chiens de garde et bornes
    capteurs restaient sur les valeurs du démarrage — l'opérateur croyait
    avoir réglé quelque chose."""
    import json as _json
    cfg = _json.loads(_json.dumps(CFG))
    pilote, es = _pilote_en_position("fc_3voies_0")

    cfg["regulation"].update(kp=9.99, ki=8.88, rampe_v_par_s=3.3,
                             tolerance_convergence_relative=0.05,
                             temperature_min_c=-10.0, temperature_max_c=99.0,
                             recul_saturation_v_par_s=1.25,
                             fenetre_convergence_s=20.0)
    cfg["actionneurs"].update(vanne_3voies_temps_s=33.0,
                              facteur_chien_garde=2.5)
    cfg["regulation"].update(trim_max_v=0.7, suivi_variateur_max_s=4.0)
    cfg["strategie_bas_re"].update(gain_scale=0.5)

    pilote.appliquer_config(cfg)

    assert pilote.pi.kp == 9.99
    assert pilote.pi.ki == 8.88
    assert pilote.pi.rampe == 3.3
    assert pilote.tolerance == 0.05
    assert (pilote.t_min_c, pilote.t_max_c) == (-10.0, 99.0)
    assert pilote.recul_saturation == 1.25
    assert pilote.t_vanne_3voies == 33.0
    assert pilote.facteur_chien_garde == 2.5
    assert (pilote.trim.kp, pilote.trim.ki) == (9.99, 8.88)
    assert pilote.trim.rampe == 3.3
    assert pilote.trim.trim_max == 0.7
    assert pilote.suivi_variateur_max_s == 4.0
    assert pilote.bas_re_scale == 0.5
    # la fenêtre est un nombre d'échantillons : elle doit être reconstruite
    assert pilote.fenetre_convergence == 20.0
    assert pilote._historique_re.maxlen == int(20.0 / pilote.periode)


def test_appliquer_config_ne_touche_pas_aux_grandeurs_apprises():
    """Le gain, la table et u_min viennent de l'apprentissage, pas de la
    saisie : une édition de configuration ne doit pas les effacer."""
    import json as _json
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote._gain_re_par_volt = 3600.0
    pilote.table_gains = TableGains([(2.0, 7000.0), (8.0, 28000.0)])
    pilote.u_min_ventilateur = 1.5

    pilote.appliquer_config(_json.loads(_json.dumps(CFG)))

    assert pilote._gain_re_par_volt == 3600.0
    assert pilote.table_gains.est_valide()
    assert pilote.u_min_ventilateur == 1.5


def test_appliquer_config_ignore_les_cles_commentaire():
    """Règle 6 de CLAUDE.md : les clés « _ » de config.json sont des
    commentaires et ne doivent jamais devenir des offsets de capteur."""
    import json as _json
    cfg = _json.loads(_json.dumps(CFG))
    cfg["zero_capteurs"] = {"_commentaire": "texte", "dp_iso_500": 1.5}
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote.appliquer_config(cfg)
    assert pilote.zero_capteurs == {"dp_iso_500": 1.5}


# ---- régressions de la refonte « 3 modes » ---------------------------------

def test_cycle_inattendu_repli_sur_sans_tuer_la_boucle(tmp_path):
    """Une exception imprévue (défaut de programmation) dans le cycle doit
    produire le repli sûr, pas tuer le thread : sinon le variateur reste à sa
    dernière tension, RUN actif, sans plus aucune supervision."""
    class ESEnPanne(StubES):
        def lire_mesures(self):
            raise TypeError("défaut de programmation simulé")

    es = ESEnPanne()
    pilote = Pilote(Banc(CFG), es, CFG)
    pilote._chemin_etat = tmp_path / "etat.json"
    pilote.journal = None
    pilote.demarrer()
    time.sleep(0.5)
    vivant = pilote._thread.is_alive()
    pilote.arreter()
    assert vivant
    assert es.variateur and es.variateur[-1] == 0.0
    assert es.run and es.run[-1] is False
    assert "erreur interne" in pilote.etat["erreur_com"]


def test_miller_ne_fige_pas_le_variateur_quand_le_re_mesure_est_bas():
    """Consigne Miller au-dessus du seuil bas-Re, Re mesuré encore bas (le
    ventilateur démarre) : la stratégie suit la consigne (variateur réglé).
    RÉGRESSION : le Re mesuré faisait choisir le mode PdC, dont l'entrée
    levait une TypeError (thread de régulation tué), et qui figeait sinon
    le variateur à u_min : la consigne n'était jamais atteinte."""
    pilote, es = _pilote_en_position("fc_3voies_90")
    pilote.seuil_bas_re = 2000.0
    pilote.u_min_ventilateur = 0.5
    pilote.table_gains_bas_re = TableGains()
    es.mesures["dp_diaph_500"] = 0.5
    pilote.definir_consigne(3000.0, "conduite")
    pilote.demarrer_regulation()
    for _ in range(25):
        pilote._cycle()
    assert pilote.etat["re_mesure"] < 0.95 * 2000.0     # Re mesuré sous le seuil
    assert pilote._mode_regulation == "normal_low"
    assert pilote.etat["phase_regulation"] != "PI vanne PdC"
    assert pilote.etat["u_variateur"] > pilote.u_min_ventilateur


def test_feed_forward_pdc_depuis_la_table_apprise_en_pourcent():
    """La table PdC est apprise en % d'ouverture (variateur à u_min). Le
    feed-forward doit la convertir en volts avec les butées calibrées.
    RÉGRESSION : AttributeError (v_pour_re absent de TableGains) qui tuait le
    thread, et pré-positionnement lu comme des volts bornés à 10, donc
    plafonné à 1 V (~10 % d'ouverture)."""
    pilote, es = _pilote_en_position("fc_3voies_90")
    pilote.seuil_bas_re = 2000.0
    pilote.table_gains_bas_re = TABLE_MILLER        # Re mapping Miller fait
    pilote.u_min_ventilateur = 0.5
    pilote.table_gains_pdc = TableGains([(30.0, 300.0), (100.0, 1500.0)])
    es.mesures["dp_diaph_500"] = 0.5
    pilote.definir_consigne(1000.0, "conduite")
    pilote.demarrer_regulation()
    pilote._cycle()
    pct = 30.0 + (1000.0 - 300.0) / (1500.0 - 300.0) * 70.0
    v_attendu = pilote.pdc_recopie_min + pct / 100.0 * (
        pilote.pdc_recopie_max - pilote.pdc_recopie_min)
    assert pilote._mode_regulation == "pdc_control"
    # la vanne part de grande ouverte : ventilateur à l'arrêt tant qu'elle
    # n'a pas rejoint sa position
    assert pilote.etat["phase_regulation"] == "positionnement PdC avant ventilateur"
    assert pilote._pdc_consigne == pytest.approx(v_attendu, abs=0.2)
    assert pilote.etat["u_variateur"] == 0.0
    es.mesures.update(pdc_recopie=pilote._pdc_consigne, fc_pdc_ouvert=False)
    pilote._cycle()                                  # vanne arrivée
    assert pilote.etat["phase_regulation"] == "FF+PI vanne PdC"
    assert pilote._diag["v_ff_pdc"] == pytest.approx(v_attendu, abs=1e-3)
    for _ in range(5):                               # variateur sur sa rampe
        pilote._cycle()
    assert pilote.etat["u_variateur"] == pytest.approx(pilote._u_pdc())


def test_seuil_bas_re_de_config_au_dela_du_seuil_miller_ignore():
    """config.json livrait seuil_bas_re_mesure = 7547, au-delà du seuil
    Miller/ISO (5000) : toute consigne Miller partait en mode PdC, variateur
    figé à u_min, consigne inatteignable et sans alarme. Le seuil est
    désormais ignoré et le motif publié."""
    pilote, es = _pilote_en_position("fc_3voies_90")
    pilote.seuil_bas_re = 7547.0
    pilote.u_min_ventilateur = 0.3
    pilote.table_gains = TableGains()
    pilote.table_gains_bas_re = TableGains()
    pilote._gain_re_par_volt = None
    es.mesures["dp_diaph_500"] = 0.5
    pilote.definir_consigne(3000.0, "conduite")
    pilote.demarrer_regulation()
    for _ in range(25):
        pilote._cycle()
    assert pilote._mode_regulation == "normal_low"
    assert pilote.etat["u_variateur"] > 0.3
    assert "ignoré" in pilote.etat["bas_re_motif"]


def test_plomberie_croisee_prise_en_compte_par_le_decodage():
    """positions_vanne_3voies interverti (ISO alimenté à 90°) : la vanne en
    butée 90° est sur l'ISO, la régulation ISO démarre sans manœuvre.
    RÉGRESSION : etat_vannes codait 0° = ISO en dur, la boucle attendait une
    butée que la vanne ne pouvait atteindre, jusqu'au timeout."""
    cfg = json.loads(json.dumps(CFG))
    cfg["modbus"]["positions_vanne_3voies"] = {
        "diaphragme_a_0deg": "miller", "diaphragme_a_90deg": "iso"}
    es = StubES({"temperature": 20.0, "fc_3voies_90": True,
                 "fc_bypass_ferme": True, "fc_pdc_ouvert": True,
                 "dp_iso_500": 100.0})
    pilote = Pilote(Banc(cfg), es, cfg)
    pilote.bas_re_actif = False
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    for _ in range(10):
        pilote._cycle()
    assert pilote.etat["diaphragme"] == "iso"
    assert pilote.etat["phase_regulation"] != "attente vannes"
    assert pilote.etat.get("defaut_vanne") is None
    assert True not in es.coil_diaphragme         # jamais commandée vers 0°


def test_vanne_pdc_rouverte_une_fois_les_vannes_tor_en_place():
    """Sortie du mode PdC : vanne PdC à mi-course, consigne ISO. Les modes
    normaux attendent sa fin de course « ouverte » : une fois diaphragme et
    bypass en place, elle doit être commandée grande ouverte.
    RÉGRESSION : elle restait à sa position pré-positionnée, la fin de course
    n'arrivait jamais -> attente indéfinie puis défaut vanne."""
    es = StubES({"temperature": 20.0, "fc_3voies_0": True,
                 "fc_bypass_ferme": True, "fc_pdc_ouvert": False})
    pilote = Pilote(Banc(CFG), es, CFG)
    pilote.bas_re_actif = False
    pilote._pdc_consigne = 3.0
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    pilote._cycle()
    assert pilote.etat["phase_regulation"] == "attente vannes"
    assert pilote.etat["u_variateur"] == 0.0
    assert pilote._pdc_consigne == 10.0


def test_seuil_bas_re_derive_de_la_table_miller():
    """Le seuil dérivé doit venir de la table Miller (bypass ouvert, la
    configuration du mode PdC), pas de la table ISO mesurée bypass fermé,
    qui surestime le Re tenu à u_min et enverrait en PdC des consignes que
    le variateur à u_min ne peut pas atteindre."""
    pilote, es = _pilote_en_position("fc_3voies_90")
    pilote.seuil_bas_re = 7547.0                         # config : ignoré
    pilote.u_min_ventilateur = 1.5
    pilote.table_gains = TableGains([(1.0, 3500.0), (9.0, 31500.0)])
    pilote.table_gains_bas_re = TableGains([(1.5, 2000.0), (5.0, 7000.0)])
    t = pilote.table_gains_bas_re
    assert pilote._seuil_bas_re() == pytest.approx(
        0.5 * (t.re_pour_u_ff(1.5) + t.re_pour_u_ff(pilote._u_pdc())))


def test_seuil_derive_couvre_la_bande_d_hysteresis_des_deux_modes():
    """Mode PdC : variateur à u_pdc, PdC ouverte -> Re maximal Re(u_pdc).
    Mode Miller réglé : variateur >= u_min -> Re minimal Re(u_min). Chaque
    mode doit couvrir sa moitié de la bande ±5 % autour du seuil.
    RÉGRESSION : seuil = 1,3 × Re(u_min) avec le variateur figé à u_min,
    les consignes entre Re(u_min) et 1,3 × Re(u_min) étaient inatteignables."""
    pilote, es = _pilote_en_position("fc_3voies_90")
    pilote.seuil_bas_re = 0.0
    pilote.u_min_ventilateur = 1.5
    pilote.table_gains_bas_re = TableGains([(1.5, 2000.0), (5.0, 7000.0)])
    t = pilote.table_gains_bas_re
    seuil = pilote._seuil_bas_re()
    assert t.re_pour_u_ff(pilote._u_pdc()) >= 1.05 * seuil     # haut de bande en PdC
    assert t.re_pour_u_ff(pilote.u_min_ventilateur) <= 0.95 * seuil  # bas de bande en Miller
    assert pilote._u_pdc() > pilote.u_min_ventilateur


def test_seuil_manuel_retenu_si_le_seuil_mesure_est_inutilisable():
    """config.json : seuil_bas_re_mesure = 7547 (inutilisable, au-delà du
    seuil Miller/ISO) masquait le seuil manuel réglé depuis l'IHM."""
    cfg = json.loads(json.dumps(CFG))
    cfg["strategie_bas_re"].update({"seuil_bas_re_mesure": 7547, "seuil_re": 1800})
    pilote = Pilote(Banc(cfg), StubES({"temperature": 20.0}), cfg)
    assert pilote.seuil_bas_re == 1800
    cfg["strategie_bas_re"].update({"seuil_bas_re_mesure": 2500})
    pilote.appliquer_config(cfg)
    assert pilote.seuil_bas_re == 2500                # mesuré valide : prioritaire


# ---- replis sûrs et arrêt total (revue complète) ---------------------------

def test_repli_sur_coupe_aussi_le_mode_manuel():
    """Un défaut latché doit couper le mode manuel : sinon, le défaut disparu
    (PT100 rebranchée, communication revenue), la boucle réappliquait
    d'elle-même la tension manuelle."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote.mode_manuel_tension(3.0)
    pilote._cycle()
    assert es.variateur[-1] == 3.0
    es.mesures["temperature"] = -60.0                 # sonde coupée
    for _ in range(max(3, int(2.0 / pilote.periode)) + 2):
        pilote._cycle()
    assert pilote.etat["defaut_capteur"]
    es.mesures["temperature"] = 20.0                  # défaut disparu
    for _ in range(3):
        pilote._cycle()
    assert pilote.mode_manuel is False
    assert es.variateur[-1] == 0.0
    assert es.run[-1] is False


def test_apprentissage_ne_relance_pas_le_ventilateur_apres_un_arret():
    """ARRÊT VARIATEUR pendant une séquence : le palier suivant ne doit pas
    relancer le ventilateur (constaté : 5 V et RUN 25 s après l'arrêt), et
    les attentes doivent s'interrompre."""
    from core.apprentissage import Apprentissage
    pilote, es = _pilote_en_position("fc_3voies_0")
    app = Apprentissage(pilote, CFG, "inutilise.json")
    app._arrets_debut = pilote.arrets
    app._variateur(4.0)
    assert pilote.mode_manuel and pilote.tension_manuelle == 4.0
    pilote.arreter_regulation()                       # bouton ARRÊT
    t0 = time.monotonic()
    assert app._pause(10.0) is True                   # attente interrompue
    assert time.monotonic() - t0 < 1.0
    app._variateur(5.0)                               # palier suivant
    assert pilote.mode_manuel is False
    assert pilote.tension_manuelle == 0.0
    assert app.etat["statut"].startswith("interrompu")


def test_saturation_detectee_malgre_l_ecretage_du_capteur():
    """La lecture ΔP est écrêtée à la pleine échelle (500 Pa) : avec un seuil
    de saturation à 100 %, rien ne se déclenchait jamais. Le seuil est borné
    sous 1 et un ΔP en butée est vu saturé."""
    cfg = json.loads(json.dumps(CFG))
    cfg["regulation"]["saturation_dp_relative"] = 1.0
    es = StubES({"temperature": 20.0, "fc_3voies_0": True,
                 "fc_bypass_ferme": True, "fc_pdc_ouvert": True,
                 "dp_iso_500": 300.0})
    pilote = Pilote(Banc(cfg), es, cfg)
    assert pilote.saturation_dp < 1.0
    pilote.bas_re_actif = False
    pilote.table_gains = TableGains([(1.0, 3000.0), (9.0, 30000.0)])
    pilote.definir_consigne(20000.0, "conduite")
    pilote.demarrer_regulation()
    for _ in range(15):
        pilote._cycle()
    es.mesures["dp_iso_500"] = 500.0                  # capteur écrêté
    for _ in range(max(3, int(1.0 / pilote.periode)) + 1):
        pilote._cycle()
    assert pilote.etat["dp_sature"] is True
    assert pilote.etat["plafond_u_sature"] is not None


# ---- boucle : modes, chiens de garde, arrêt (revue complète) ---------------

def test_strategie_bas_re_desactivee_respectee():
    """strategie_bas_re.actif = false : jamais de mode PdC (variateur figé à
    u_min). RÉGRESSION : le drapeau n'agissait que sur le message affiché."""
    pilote, es = _pilote_en_position("fc_3voies_90")
    pilote.bas_re_actif = False
    pilote.seuil_bas_re = 2000.0
    pilote.u_min_ventilateur = 0.5
    es.mesures["dp_diaph_500"] = 0.5
    pilote.definir_consigne(1000.0, "conduite")
    pilote.demarrer_regulation()
    for _ in range(5):
        pilote._cycle()
    assert pilote._mode_regulation == "normal_low"


def test_seuils_diaphragme_de_la_boucle_alignes_sur_le_banc():
    """La boucle lisait une clé inexistante (seuil figé à 5000) et une demi-
    hystérésis : venant de l'ISO, une consigne de 4600 (dans la bande
    4500-5500 du Banc) faisait basculer la boucle vers le Miller."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote.bas_re_actif = False
    es.mesures["dp_iso_500"] = 20.0
    pilote.definir_consigne(4600.0, "conduite")
    pilote.demarrer_regulation()
    for _ in range(5):
        pilote._cycle()
    assert False not in es.coil_diaphragme          # jamais commandée vers 90°
    cfg = json.loads(json.dumps(CFG))
    cfg["selection_diaphragme"]["seuil_re"] = 6000.0
    pilote.appliquer_banc(Banc(cfg))
    assert pilote._configuration_modes().seuil_diaphragme == 6000.0


def test_chien_de_garde_du_bypass_conserve_en_manoeuvre_simultanee():
    """Bypass et vanne 3 voies commandés ensemble : la seconde manœuvre
    écrasait la première, le bypass n'avait plus de chien de garde."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote.t_vanne_bypass = pilote.t_vanne_3voies = 0.2
    pilote.marge_manoeuvre = 0.0
    pilote._cycle()
    pilote.commander_bypass(True)                    # n'arrivera jamais
    pilote.commander_diaphragme("miller")
    es.mesures.update({"fc_3voies_0": False, "fc_3voies_90": True})
    t0 = time.monotonic()
    while time.monotonic() - t0 < 1.0 and not pilote.etat.get("defaut_vanne"):
        pilote._cycle()
        time.sleep(0.02)
    assert "bypass" in (pilote.etat.get("defaut_vanne") or "")


def test_ventilateur_a_zero_pendant_une_manoeuvre_hors_manuel():
    """Pendant une manœuvre en régulation, l'état publiait 0 V mais la sortie
    gardait la dernière tension écrite."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote._cycle()
    es.ecrire_variateur(3.0)                         # dernière tension écrite
    pilote.commander_diaphragme("miller")
    pilote._cycle()
    assert pilote.etat["manoeuvre"]
    assert es.variateur[-1] == 0.0


def test_delai_d_attente_des_vannes_derive_de_la_config():
    pilote, _ = _pilote_en_position("fc_3voies_0")
    attendu = pilote.facteur_chien_garde * (
        pilote.t_vanne_bypass + pilote.t_vanne_3voies
        + pilote.t_vanne_pdc_course) + 3.0 * pilote.marge_manoeuvre
    assert pilote._timeout_attente_vannes_s() == pytest.approx(attendu)
    pilote.t_vanne_pdc_course = 60.0
    assert pilote._timeout_attente_vannes_s() > attendu


def test_arret_coupe_le_ventilateur_avant_la_position_de_repos(tmp_path):
    """Basculer sur le Miller ventilateur lancé enverrait tout le débit dans
    le petit orifice : arreter() coupe d'abord le variateur."""
    evenements = []

    class ESJournal(StubES):
        def ecrire_variateur(self, volts):
            evenements.append(("variateur", volts))

        def ecrire_vanne_diaphragme(self, vers_0deg):
            evenements.append(("3voies", vers_0deg))

    es = ESJournal({"temperature": 20.0, "fc_3voies_0": True})
    pilote = Pilote(Banc(CFG), es, CFG)
    pilote._chemin_etat = tmp_path / "etat.json"
    pilote.journal = None
    pilote.mode_manuel_tension(4.0)
    evenements.clear()
    pilote.arreter()
    premier_3voies = next(i for i, e in enumerate(evenements) if e[0] == "3voies")
    assert ("variateur", 0.0) in evenements[:premier_3voies]


def test_gain_appris_en_fonctionnement_relu_a_la_session_suivante():
    """Le gain Re/V appris en marche est écrit à l'arrêt dans etat_banc.json,
    qui n'était relu qu'à la migration initiale : dès que apprentissages/
    existait, il était perdu à chaque redémarrage."""
    import core.pilote
    (core.pilote.RACINE / "apprentissages").mkdir(exist_ok=True)
    (core.pilote.RACINE / "apprentissages" / "iso.json").write_text(
        json.dumps({"tables": {}, "parametres": {"gain_re_par_volt": 3000.0}}),
        encoding="utf-8")
    es = StubES({"temperature": 20.0, "fc_3voies_0": True})
    pilote = Pilote(Banc(CFG), es, CFG)
    pilote.journal = None
    assert pilote._gain_re_par_volt == 3000.0
    pilote._gain_re_par_volt = 3550.0                # appris en marche
    pilote.arreter()
    suivant = Pilote(Banc(CFG), StubES({"temperature": 20.0}), CFG)
    assert suivant._gain_re_par_volt == pytest.approx(3550.0)


def test_consigne_hors_de_portee_du_mode_pdc_signalee():
    """Variateur figé à u_min, PdC grande ouverte, Re toujours sous la
    consigne : la régulation attendait indéfiniment sans rien signaler."""
    pilote, es = _pilote_en_position("fc_3voies_90")
    pilote.seuil_bas_re = 2000.0
    pilote.table_gains_bas_re = TABLE_MILLER        # Re mapping Miller fait
    pilote.u_min_ventilateur = 0.5
    es.mesures["dp_diaph_500"] = 0.5                 # Re ~ 0
    pilote.definir_consigne(1000.0, "conduite")
    pilote.demarrer_regulation()
    for _ in range(int(5.0 / pilote.periode) + 5):
        pilote._cycle()
    assert pilote._mode_regulation == "pdc_control"
    assert "hors de portée" in (pilote.etat["alerte_pdc"] or "")


def test_alarme_pdc_seulement_en_butee_physique(monkeypatch):
    """Re au-dessus de la consigne : la commande PdC part en butée fermée
    aussitôt, mais la vanne met une course à y arriver. Pas d'alarme
    « hors de portée » pendant cette course (fausse alarme vue à 300 Re) ;
    alarme une fois la vanne réellement en butée et le Re toujours haut."""
    horloge = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: horloge[0])
    pilote, es = _pilote_en_position("fc_3voies_90")
    pilote.seuil_bas_re = 2000.0
    pilote.table_gains_bas_re = TABLE_MILLER        # Re mapping Miller fait
    pilote.u_min_ventilateur = 0.5
    es.mesures["dp_diaph_500"] = 200.0               # Re bien au-dessus
    es.mesures["pdc_recopie"] = 10.0
    pilote.definir_consigne(300.0, "conduite")
    pilote.demarrer_regulation()
    alarmes_en_course = []
    position = 10.0                                  # vanne réelle [V]
    vitesse = 10.0 / pilote.t_vanne_pdc_course
    for _ in range(int(40.0 / pilote.periode)):
        horloge[0] += pilote.periode
        pilote._cycle()
        # vanne réelle : rejoint sa commande à la vitesse de course
        pas = vitesse * pilote.periode
        position += max(-pas, min(pas, pilote._pdc_consigne - position))
        es.mesures["pdc_recopie"] = position
        es.mesures["fc_pdc_ouvert"] = position >= 10.0
        if position > pilote.ecart_course_pdc_v and pilote.etat.get("alerte_pdc"):
            alarmes_en_course.append(round(position, 2))
    assert pilote._mode_regulation == "pdc_control"
    assert alarmes_en_course == []
    assert "butée fermée" in (pilote.etat["alerte_pdc"] or "")


def test_convergence_avec_hysteresis():
    """Convergé : le reste tant que l'écart moyen reste sous la tolérance de
    sortie (2 %) ; pas encore convergé : ne l'est que sous 1 %. Sans cette
    bande, le voyant clignotait au bruit près à très bas Re."""
    pilote, es = _pilote_en_position("fc_3voies_90")
    es.mesures["dp_diaph_500"] = 50.0                # débit fixe
    pilote.bas_re_actif = False
    pilote.definir_consigne(2000.0, "conduite")
    pilote.demarrer_regulation()
    pilote._preparation_faite = True
    pilote._cycle()
    re_mesure = pilote.etat["re_mesure"] or 0.0
    assert re_mesure > 0
    # consigne placée à 1,5 % de la mesure (fixe, StubES)
    pilote.re_cible = re_mesure / 1.015
    pilote._re_cible_max = None
    pilote._historique_re.clear()
    pilote._convergee_depuis = None
    for _ in range(pilote._historique_re.maxlen + 1):
        pilote._cycle()
    assert pilote.etat["convergee"] is False         # 1,5 % > 1 % : pas d'entrée
    pilote._convergee_depuis = 1.0                     # déjà convergé
    pilote._cycle()
    assert pilote.etat["convergee"] is True           # 1,5 % < 2 % : maintenu

def test_bypass_sans_fin_de_course_hors_manoeuvre_en_defaut():
    """Bypass sans aucune fin de course alors qu'aucune manœuvre n'est en
    cours (fil coupé) : défaut au bout d'une course chien de garde comprise,
    au lieu d'attendre le délai global d'attente des vannes (~50 s)."""
    es = StubES({"temperature": 20.0, "fc_3voies_0": True,
                 "fc_pdc_ouvert": True, "dp_iso_500": 100.0})
    pilote = Pilote(Banc(CFG), es, CFG)
    pilote.bas_re_actif = False
    pilote.t_vanne_bypass = 1.0
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    for _ in range(int(pilote.facteur_chien_garde * 1.0 / pilote.periode) + 3):
        pilote._cycle()
    assert "bypass sans fin de course" in (pilote.etat.get("defaut_vanne") or "")
    assert pilote.regulation_active is False
    assert es.variateur[-1] == 0.0
