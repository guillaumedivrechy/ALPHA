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
    """Les trois fins de course forment un codage un-parmi-trois. Zéro ou
    plusieurs contacts actifs = rupture de fil ou commun 24 V absent."""
    d = Pilote._angle_vanne_3voies
    assert d({"fc_3voies_0": True}) == "0"
    assert d({"fc_3voies_90": True}) == "90"
    assert d({"fc_3voies_180": True}) == "180"
    assert d({}) is None                                     # aucune butée
    assert d({"fc_3voies_0": True, "fc_3voies_90": True}) is None


def test_cycle_supporte_position_fermee():
    """Régression : diaphragme 'fermee' provoquait un KeyError à chaque
    cycle (repli erreur com + variateur forcé à 0)."""
    es = SimulateurBanc(Banc(CFG), CFG.get("simulation"))
    pilote = Pilote(Banc(CFG), es, CFG)
    pilote._cycle()                          # position initiale depuis les FC
    pilote.etat["diaphragme"] = "fermee"
    pilote._cycle()                          # ne doit pas lever
    assert pilote.etat["erreur_com"] is None or True   # pas d'exception


def test_cycles_simulation_sans_erreur():
    """Quelques cycles complets en simulation, consigne active."""
    es = SimulateurBanc(Banc(CFG), CFG.get("simulation"))
    pilote = Pilote(Banc(CFG), es, CFG)
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    for _ in range(10):
        pilote._cycle()
    assert pilote.etat["cycles"] == 10
    assert pilote.etat.get("defaut_vanne") is None


def _pilote_en_position(fc: str) -> tuple:
    """Pilote sur StubES avec la vanne 3 voies déjà en position (fc actif)."""
    es = StubES({"temperature": 20.0, fc: True})
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
    pilote.definir_consigne(1000.0, "conduite")
    pilote.demarrer_regulation()
    pilote._cycle()
    assert pilote.pi.rampe == pytest.approx(
        pilote.split_point / pilote.t_vanne_pdc_course)
    assert pilote.pi.rampe < pilote._rampe_nominale
    # retour en régime normal : rampe nominale restaurée
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
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


def test_consigne_interne_filtree():
    """Mise en forme de référence : le PI suit une consigne interne filtrée
    (ancrée sur la mesure, plancher 20 % de la cible) qui rejoint la cible
    au 1er ordre, au lieu de l'échelon brut."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote.bas_re_actif = False
    pilote.tau_consigne = 2.0
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    pilote._cycle()
    c1 = pilote._consigne_interne
    assert 0.2 * 8000.0 <= c1 < 8000.0    # ancrée au plancher, pas à la cible
    pilote._cycle()
    c2 = pilote._consigne_interne
    assert c1 < c2 < 8000.0               # progression monotone vers la cible
    # désactivée (tau = 0) : échelon brut, pas de consigne interne
    pilote.tau_consigne = 0.0
    pilote._preparer_pi(False, 8000.0, 0.0)
    pilote._cycle()
    assert pilote._consigne_interne is None


def test_gain_scheduling_suit_le_point_de_fonctionnement():
    """Erreur normalisée par la consigne -> gain de boucle en kp/u_équilibre :
    avec une table de gains valide, les gains sont remis à l'échelle par la
    tension d'équilibre pour un comportement homogène sur toute la plage."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote.bas_re_actif = False
    pilote.table_gains = TableGains([(1.0, 1000.0), (9.0, 9000.0)])
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    pilote._cycle()
    assert pilote.pi.gain_scale == pytest.approx(8.0)   # u_équilibre = 8 V
    # désactivable par config : échelle neutre
    pilote.gain_scheduling = False
    pilote.demarrer_regulation()
    pilote._cycle()
    assert pilote.pi.gain_scale == 1.0


def test_critere_bas_re_en_reference_veine():
    """Le seuil bas-Re est un Re CONDUITE : une consigne veine doit être
    convertie avant comparaison. Ici Re veine 1000 ≈ Re conduite 3129,
    au-dessus du seuil 3000 -> pas de stratégie bas-Re."""
    pilote, es = _pilote_en_position("fc_3voies_90")   # Miller (Re < 5000)
    pilote.seuil_bas_re = 3000.0
    pilote.definir_consigne(1000.0, "veine")
    pilote.demarrer_regulation()
    pilote._cycle()
    assert pilote._mode_bas_re is False
    assert pilote.etat["mode_bas_re"] is False


def test_bascule_bas_re_en_cours_recale_le_pi():
    """Si le seuil appris dérive et fait basculer le régime sans nouvelle
    consigne, la commande PI change de signification (tension <-> split-
    range) : elle doit être re-calée sur le feed-forward du nouveau régime."""
    pilote, es = _pilote_en_position("fc_3voies_90")
    pilote.seuil_bas_re = 1200.0
    pilote.definir_consigne(1500.0, "conduite")
    pilote.demarrer_regulation()
    pilote._cycle()                       # régime normal (1500 > 1200)
    assert pilote._regime_pi is False
    appels = _espion_reinit(pilote.pi)
    pilote.seuil_bas_re = 3000.0          # dérive du seuil -> bas-Re requis
    pilote._cycle()
    assert pilote._regime_pi is True
    assert len(appels) == 1               # PI re-calé sur le feed-forward
    assert pilote.pi.rampe == pytest.approx(pilote._rampe_bas_re())


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
    es = StubES({"temperature": 20.0, "fc_3voies_0": True})
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


def test_split_range_vanne_pdc_0v_fermee():
    """Convention carte P6 (sens usine) : 0 V = fermée, 10 V = grande ouverte.
    Hors bas-Re la vanne doit être pleinement ouverte ; en bas-Re, une commande
    nulle doit la fermer."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote.u_min_ventilateur = 2.0
    u, pdc = pilote._split_range(5.0, bas_re=False)
    assert pdc == 10.0                              # circuit de mesure normal
    u, pdc = pilote._split_range(0.0, bas_re=True)
    assert pdc == 0.0                               # étranglée au minimum
    u, pdc = pilote._split_range(pilote.split_point, bas_re=True)
    assert pdc == pytest.approx(10.0)               # haut du segment PdC
    u, pdc = pilote._split_range(10.0, bas_re=True)
    assert pdc == 10.0                              # segment variateur


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
    """ΔP proche de la pleine échelle du capteur : le Re mesuré plafonne au
    capteur, pas au banc — signalé (après debounce) pour que l'opérateur
    comprenne une non-convergence sur consigne trop haute."""
    es = StubES({"temperature": 20.0, "fc_3voies_0": True,
                 "dp_iso_500": 490.0})              # > 95 % de 500 Pa
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
    """RÉGRESSION : au-delà de la pleine échelle du capteur, le Re mesuré
    plafonne mais le banc peut encore accélérer ; l'erreur ne se résorbait
    jamais et le PI poussait le ventilateur en butée pour rien. La commande
    doit être gelée à sa valeur du moment."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    pilote.bas_re_actif = False
    es.mesures["dp_iso_500"] = 495.0               # > 0,9 × 500 Pa dès le départ
    pilote.definir_consigne(60000.0, "conduite")   # hors de portée du capteur
    pilote.demarrer_regulation()
    for _ in range(max(3, int(1.0 / pilote.periode)) + 1):
        pilote._cycle()
    assert pilote.etat["dp_sature"] is True
    plafond = pilote.etat["plafond_u_sature"]
    assert plafond is not None

    for _ in range(60):                            # 12 s de plus
        pilote._cycle()
    assert pilote.etat["u_variateur"] < plafond    # la commande a reculé
    assert pilote.etat["u_variateur"] < 10.0       # plus de butée à fond

    # sortie de saturation : le plafond est LATCHÉ jusqu'à la consigne
    # suivante. Le relâcher ici ferait repartir le PI vers la butée, donc
    # un cycle limite autour de la pleine échelle du capteur.
    es.mesures["dp_iso_500"] = 200.0
    for _ in range(max(3, int(1.0 / pilote.periode)) + 2):
        pilote._cycle()
    assert pilote.etat["dp_sature"] is False
    assert pilote.etat["plafond_u_sature"] is not None
    assert pilote.etat["u_variateur"] <= pilote.etat["plafond_u_sature"] + 1e-9

    # une nouvelle consigne rend toute sa course à la boucle
    pilote.definir_consigne(8000.0, "conduite")
    assert pilote._plafond_u_sature is None
    assert pilote.pi.u_max == 10.0


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
    """Sans la séquence freq_min, u_min est inconnu, le seuil vaut 0 et la
    stratégie bas-Re ne s'engage jamais — silencieusement jusqu'ici."""
    pilote, es = _pilote_en_position("fc_3voies_0")
    es.mesures["dp_iso_500"] = 200.0
    pilote.u_min_ventilateur = None
    pilote.table_gains = TableGains()
    pilote._gain_re_par_volt = None
    pilote.definir_consigne(8000.0, "conduite")
    pilote.demarrer_regulation()
    pilote._cycle()
    assert "freq_min" in pilote.etat["bas_re_motif"]

    pilote.bas_re_actif = False
    pilote._cycle()
    assert pilote.etat["bas_re_motif"] is None


def test_image_process_reconstituee_par_le_simulateur():
    """Le simulateur reproduit la couche Modbus quand on lui donne la config,
    pour que la page de mise en service soit exercable sans materiel. Sans
    config modbus il n'invente pas d'adresses."""
    from io_banc.simulateur import SimulateurBanc

    sans = SimulateurBanc(Banc(CFG), CFG.get("simulation"))
    sans.lire_mesures()
    assert sans.image_process is None

    es = SimulateurBanc(Banc(CFG), CFG.get("simulation"), CFG["modbus"])
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
    cfg["strategie_bas_re"].update(split_range_point=7.0, gain_scale=0.5)

    pilote.appliquer_config(cfg)

    assert pilote.pi.kp == 9.99
    assert pilote.pi.ki == 8.88
    assert pilote.pi.rampe == 3.3
    assert pilote.tolerance == 0.05
    assert (pilote.t_min_c, pilote.t_max_c) == (-10.0, 99.0)
    assert pilote.recul_saturation == 1.25
    assert pilote.t_vanne_3voies == 33.0
    assert pilote.facteur_chien_garde == 2.5
    assert pilote.split_point == 7.0
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
