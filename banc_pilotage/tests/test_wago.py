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
    def __init__(self, registers=None, bits=None):
        self.registers = registers or []
        self.bits = bits or []

    def isError(self):
        return False


class _FauxClient:
    """Renvoie 5 V (16384) sur les AI, 20,5 °C sur la PT100, True en TOR."""

    def read_input_registers(self, address, count=1):
        if address == CFG_MODBUS["entrees"]["temperature"]["adresse"]:
            return _FauxResultat(registers=[205])          # 20,5 °C
        return _FauxResultat(registers=[16384])            # ~5,0 V

    def read_discrete_inputs(self, address, count=1):
        return _FauxResultat(bits=[True])


@pytest.fixture
def wago():
    w = WagoModbus.__new__(WagoModbus)      # sans connexion réseau
    w.cfg = CFG_MODBUS
    w._verrou = threading.Lock()
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
    # toutes les entrées TOR déclarées sont présentes
    for nom in CFG_MODBUS["entrees_tor"]:
        if not nom.startswith("_"):
            assert mesures[nom] is True
