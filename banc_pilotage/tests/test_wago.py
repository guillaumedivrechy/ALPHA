# -*- coding: utf-8 -*-
"""WagoModbus.lire_mesures avec un client factice : régression du crash
sur les clés commentaire de config.json et couverture du mapping."""

import json
import threading
from pathlib import Path

import pytest

from io_banc.wago import WagoModbus

CFG_MODBUS = json.loads(
    (Path(__file__).resolve().parent.parent / "config.json")
    .read_text(encoding="utf-8"))["modbus"]


class _FauxResultat:
    def __init__(self, registers=None, bits=None, erreur=False):
        self.registers = registers or []
        self.bits = bits or []
        self._erreur = erreur

    def isError(self):
        return self._erreur


class _FauxClient:
    """Renvoie 5 V (16384) sur les AI, 20,5 °C sur la PT100, True en TOR.
    Compte les transactions pour vérifier les lectures par blocs."""

    def __init__(self):
        self.transactions = 0
        self.ecritures = 0
        self.erreur_ecriture = False

    def write_register(self, address, value):
        self.ecritures += 1
        return _FauxResultat(erreur=self.erreur_ecriture)

    def read_input_registers(self, address, count=1):
        self.transactions += 1
        t_addr = CFG_MODBUS["entrees"]["temperature"]["adresse"]
        return _FauxResultat(registers=[
            205 if address + i == t_addr else 16384       # 20,5 °C / ~5,0 V
            for i in range(count)])

    def read_discrete_inputs(self, address, count=1):
        self.transactions += 1
        return _FauxResultat(bits=[True] * count)


@pytest.fixture
def wago():
    w = WagoModbus.__new__(WagoModbus)      # sans connexion réseau
    w.cfg = CFG_MODBUS
    w._verrou = threading.RLock()
    w._ao_cache = {}
    w._coil_cache = {}
    w._t_rafraichissement = 2.0
    w.client = _FauxClient()
    return w


def test_lire_mesures_ignore_les_commentaires(wago):
    """Régression : la clé '_commentaire' de entrees_tor levait TypeError
    à chaque cycle -> mode matériel inutilisable."""
    mesures = wago.lire_mesures()
    assert all(not k.startswith("_") for k in mesures)


def test_lire_mesures_complet(wago):
    mesures = wago.lire_mesures()
    # échelles capteur : 5 V sur 0-10 V / 0-500 Pa -> ~250 Pa
    assert mesures["dp_diaph_500"] == pytest.approx(250.0, rel=1e-3)
    assert mesures["dp_iso_500"] == pytest.approx(250.0, rel=1e-3)
    assert mesures["p_amont_500"] == pytest.approx(250.0, rel=1e-3)
    # recopies sans échelle : valeur brute en volts
    assert mesures["pdc_recopie"] == pytest.approx(5.0, rel=1e-3)
    assert mesures["frequence_variateur"] == pytest.approx(5.0, rel=1e-3)
    # PT100 en 0,1 °C
    assert mesures["temperature"] == pytest.approx(20.5)
    # toutes les entrées TOR déclarées sont présentes, et celles câblées sur
    # un contact NF ("inverse") sont complémentées à la lecture
    for nom, voie in CFG_MODBUS["entrees_tor"].items():
        if nom.startswith("_"):
            continue
        assert mesures[nom] is not voie.get("inverse", False)


def test_entrees_tor_inversees(wago):
    """Le drapeau 'inverse' de config.json complémente la lecture : le contact
    NF MB du variateur est lu dans le sens attendu par le code métier, sans
    condition dans core/. FC3 (fc_3voies_90) est un contact NO, pas NF."""
    inversees = {nom for nom, voie in CFG_MODBUS["entrees_tor"].items()
                 if not nom.startswith("_") and voie.get("inverse")}
    assert inversees == {"defaut_variateur"}
    mesures = wago.lire_mesures()               # le faux client renvoie True
    assert mesures["defaut_variateur"] is False  # contact fermé = pas de défaut
    assert mesures["fc_3voies_90"] is True       # FC3 fermé = position stable (0° ou 180°)


def test_regroupement_en_blocs():
    """Adresses voisines regroupées (trous <= 4 inclus), lointaines séparées."""
    assert WagoModbus._blocs([0, 2, 4, 5, 6, 16]) == [[0, 7], [16, 1]]
    assert WagoModbus._blocs([0, 1, 2, 3, 4, 5, 6, 7, 8]) == [[0, 9]]
    assert WagoModbus._blocs([]) == []
    assert WagoModbus._blocs([3, 3, 3]) == [[3, 1]]


def test_lire_mesures_en_peu_de_transactions(wago):
    """Le cycle ne doit pas faire une transaction Modbus par voie : chaque
    aller-retour TCP allonge la boucle et est une occasion de timeout."""
    wago.lire_mesures()
    assert wago.client.transactions <= 4


def test_ecriture_ao_sur_changement_seulement(wago):
    """Une valeur AO inchangée n'est pas réémise à chaque cycle ; le
    rafraîchissement périodique couvre la perte du coupleur. Un changement
    de valeur (dont les mises à 0 de sécurité) part immédiatement."""
    wago.ecrire_variateur(5.0)
    wago.ecrire_variateur(5.0)              # inchangé -> pas de réécriture
    assert wago.client.ecritures == 1
    wago.ecrire_variateur(0.0)              # changement -> écrit tout de suite
    assert wago.client.ecritures == 2
    wago._t_rafraichissement = 0.0          # délai écoulé -> réécrit
    wago.ecrire_variateur(0.0)
    assert wago.client.ecritures == 3


def test_ecriture_ao_refusee_non_cachee(wago):
    """Régression : une écriture AO refusée (réponse d'exception Modbus)
    était mise en cache comme émise — une mise à 0 de sécurité pouvait
    être dédupliquée 2 s au lieu d'être retentée au cycle suivant."""
    wago.client.erreur_ecriture = True
    with pytest.raises(IOError):
        wago.ecrire_variateur(0.0)
    wago.client.erreur_ecriture = False
    wago.ecrire_variateur(0.0)              # retentée -> réellement écrite
    assert wago.client.ecritures == 2


# ── image process brute (page de mise en service) ─────────────────────────

def test_image_process_expose_les_registres_bruts():
    """La page de mise en service doit voir le REGISTRE, pas seulement la
    grandeur physique : une voie mal adressée donne souvent une valeur
    plausible, c'est le brut qui trahit le décalage."""
    es = WagoModbus.__new__(WagoModbus)
    es.cfg = CFG_MODBUS
    es.client = _FauxClient()
    es._verrou = threading.RLock()
    assert es.image_process is None            # rien avant la 1re lecture

    mesures = es.lire_mesures()
    img = es.image_process
    assert set(img) == {"mots", "bits"}

    # chaque voie déclarée en config a son mot, à la bonne adresse
    for nom, voie in CFG_MODBUS["entrees"].items():
        if nom.startswith("_"):
            continue
        assert voie["adresse"] in img["mots"], nom
    t_addr = CFG_MODBUS["entrees"]["temperature"]["adresse"]
    assert img["mots"][t_addr] == 205                  # brut, pas 20,5
    assert mesures["temperature"] == pytest.approx(20.5)

    # les bits sont l'état ÉLECTRIQUE, avant inversion des contacts NF
    for nom, voie in CFG_MODBUS["entrees_tor"].items():
        if nom.startswith("_"):
            continue
        adresse = voie["adresse"]
        assert adresse in img["bits"], nom
        attendu = img["bits"][adresse] != voie.get("inverse", False)
        assert mesures[nom] is attendu, nom


def test_image_process_distingue_un_contact_inverse():
    """Un contact NF doit apparaitre ferme electriquement alors que la
    grandeur logique est fausse : c'est tout l'interet de voir le brut."""
    es = WagoModbus.__new__(WagoModbus)
    es.cfg = CFG_MODBUS
    es.client = _FauxClient()                  # tous les bits a True
    es._verrou = threading.RLock()
    mesures = es.lire_mesures()

    inverses = [n for n, v in CFG_MODBUS["entrees_tor"].items()
                if not n.startswith("_") and v.get("inverse")]
    assert inverses, "config sans contact inverse : test a revoir"
    for nom in inverses:
        adresse = CFG_MODBUS["entrees_tor"][nom]["adresse"]
        assert es.image_process["bits"][adresse] is True   # contact ferme
        assert mesures[nom] is False                       # donc pas actif


def test_reaffirmation_de_l_ordre_3_voies_sans_pause(wago, monkeypatch):
    """La boucle réaffirme l'ordre de la vanne 3 voies à chaque cycle : la
    pause de 50 ms (relâcher puis coller) n'est due qu'à un changement
    d'ordre. Systématique, elle prenait le quart de la période de 0,2 s."""
    import io_banc.wago as module
    ecrites = []

    class Client(_FauxClient):
        def write_coil(self, address, value):
            ecrites.append((address, value))
            return _FauxResultat()

    wago.client = Client()
    pauses = []
    monkeypatch.setattr(module.time, "sleep", lambda s: pauses.append(s))
    wago.ecrire_vanne_diaphragme(True)               # premier ordre : pause
    assert pauses == [0.05]
    for _ in range(5):                               # réaffirmations : aucune
        wago.ecrire_vanne_diaphragme(True)
    assert pauses == [0.05]
    wago.ecrire_vanne_diaphragme(False)              # changement : pause
    assert pauses == [0.05, 0.05]
    s = CFG_MODBUS["sorties"]
    a0 = s["vanne_diaphragme_0deg"]["adresse"]
    a90 = s["vanne_diaphragme_90deg"]["adresse"]
    # relâcher avant de coller, jamais les deux ordres ensemble
    assert ecrites[-2:] == [(a0, False), (a90, True)]


def test_ecritures_concurrentes_cache_fidele_au_materiel(wago):
    """Revue : ARRÊT (thread IHM) écrivait 0 V, la boucle écrivait 3 V
    pendant que l'IHM n'avait pas encore mis 0 en cache ; le cache disait
    0 V alors que le matériel restait à 3 V, et le 0 V suivant était
    dédupliqué. Test, écriture et cache d'un seul tenant : le cache reflète
    toujours le matériel."""
    import time as _t
    adr = CFG_MODBUS["sorties"]["variateur"]["adresse"]
    lent = threading.Event()
    ecrire_reg = wago.client.write_register
    materiel = {}

    def write_register_lent(address, value):
        if value == 0 and not lent.is_set():
            lent.set()
            _t.sleep(0.05)              # la boucle arrive pendant ce temps
        materiel[address] = value
        return ecrire_reg(address=address, value=value)
    wago.client.write_register = write_register_lent
    wago.ecrire_variateur(2.0)
    arret = threading.Thread(target=wago.ecrire_variateur, args=(0.0,))
    arret.start()
    lent.wait(1.0)
    wago.ecrire_variateur(3.0)          # boucle, pendant l'écriture d'arrêt
    arret.join(2.0)
    assert wago._ao_cache[adr][0] == materiel[adr]
    wago.ecrire_variateur(0.0)          # cycle suivant, arrêt latché
    assert materiel[adr] == 0


def test_consigne_non_finie_ecrit_0_v(wago):
    """Revue : une consigne inf (calcul en défaut) était bornée à la pleine
    échelle — ventilateur à 10 V — et NaN levait ValueError."""
    valeurs = []
    wago.client.write_register = lambda address, value: (
        valeurs.append(value) or _FauxResultat())
    wago.ecrire_variateur(float("inf"))
    wago.ecrire_variateur(5.0)
    wago.ecrire_variateur(float("nan"))
    assert valeurs == [0, 16384, 0]
