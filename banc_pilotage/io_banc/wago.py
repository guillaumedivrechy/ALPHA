# -*- coding: utf-8 -*-
"""Interface Modbus TCP avec le coupleur Wago 750-362.

Le mapping des voies est entièrement défini dans config.json : adresses,
fonctions Modbus et mises à l'échelle s'adaptent à l'ordre réel des bornes
sur le nœud (l'image process Wago dépend de l'ordre physique des modules).

Rappels Wago 750-362 :
  - Entrées analogiques : registres d'entrée (FC4), 1 mot par voie,
    0…32767 (0x7FFF) pour la pleine échelle d'un module 0-10 V (ex. 750-468).
  - Sorties analogiques : registres de sortie (FC6/FC16), même échelle
    (ex. 750-559 0-10 V).
  - Sorties TOR : coils (FC5), adressées à partir de 0x0200 (512) sur les
    coupleurs de la série 750 lorsque des modules de mots précèdent.
  - Module RTD 750-461 (PT100) : valeur signée en 0,1 °C directement.

Vérifiez le mapping réel avec WAGO I/O CHECK ou la doc du coupleur, puis
ajustez config.json — aucun code à modifier.
"""

from __future__ import annotations

try:
    from pymodbus.client import ModbusTcpClient
    PYMODBUS_DISPONIBLE = True
except ImportError:  # le mode simulation fonctionne sans pymodbus
    PYMODBUS_DISPONIBLE = False


class EntreesSorties:
    """Interface abstraite commune au matériel réel et au simulateur."""

    def lire_mesures(self) -> dict:
        """Retourne : dp_diaph_500, dp_diaph_50, p_amont_500, p_amont_50,
        dp_iso_500, temperature  (unités physiques : Pa, °C)."""
        raise NotImplementedError

    def ecrire_variateur(self, volts: float) -> None:
        raise NotImplementedError

    def ecrire_variateur_run(self, marche: bool) -> None:
        """Ordre de marche du variateur (3G3MV : entrée S1, n003 = 1).
        Sans cet ordre, la référence 0-10 V est ignorée par le variateur."""
        raise NotImplementedError

    def ecrire_vanne_diaphragme(self, vers_iso: bool) -> None:
        """Vanne TOR 3 voies : True -> flux vers diaphragme ISO,
        False -> flux vers diaphragme Miller."""
        raise NotImplementedError

    def ecrire_vanne_bypass(self, ouverte: bool) -> None:
        raise NotImplementedError

    def ecrire_vanne_perte_charge(self, volts: float) -> None:
        raise NotImplementedError

    def fermer(self) -> None:
        pass


class WagoModbus(EntreesSorties):
    PLEINE_ECHELLE = 32767  # 0x7FFF pour 10 V sur les modules 0-10 V

    def __init__(self, cfg_modbus: dict):
        if not PYMODBUS_DISPONIBLE:
            raise RuntimeError(
                "pymodbus n'est pas installé : pip install pymodbus")
        self.cfg = cfg_modbus
        self.client = ModbusTcpClient(
            host=cfg_modbus["ip"], port=cfg_modbus.get("port", 502),
            timeout=cfg_modbus.get("timeout_s", 1.0))
        if not self.client.connect():
            raise ConnectionError(
                f"Connexion impossible au coupleur Wago {cfg_modbus['ip']}")

    # ------------------------------------------------------------- lectures

    def _lire_ai_volts(self, voie: dict) -> float:
        """Lit une entrée analogique et la convertit en volts."""
        rr = self.client.read_input_registers(
            address=voie["adresse"], count=1)
        if rr.isError():
            raise IOError(f"Erreur Modbus lecture AI @{voie['adresse']}")
        brut = rr.registers[0]
        if brut > 32767:           # valeurs signées éventuelles
            brut -= 65536
        return brut / self.PLEINE_ECHELLE * voie.get("pleine_echelle_v", 10.0)

    def _volts_vers_unite(self, volts: float, voie: dict) -> float:
        """Conversion volts -> grandeur physique (échelle linéaire capteur)."""
        v0, v1 = voie["volts_min"], voie["volts_max"]
        u0, u1 = voie["valeur_min"], voie["valeur_max"]
        v = min(max(volts, v0), v1)
        return u0 + (v - v0) / (v1 - v0) * (u1 - u0)

    def lire_mesures(self) -> dict:
        voies = self.cfg["entrees"]
        mesures = {}
        for nom in ("dp_diaph_500", "p_amont_500", "dp_iso_500"):
            if nom in voies:
                volts = self._lire_ai_volts(voies[nom])
                mesures[nom] = self._volts_vers_unite(volts, voies[nom])
        # recopie de position 0-10 V de la vanne perte de charge
        if "pdc_recopie" in voies:
            mesures["pdc_recopie"] = self._lire_ai_volts(voies["pdc_recopie"])
        # PT100 sur module RTD : valeur directe en 0,1 °C
        v_t = voies["temperature"]
        rr = self.client.read_input_registers(address=v_t["adresse"], count=1)
        if rr.isError():
            raise IOError("Erreur Modbus lecture PT100")
        brut = rr.registers[0]
        if brut > 32767:
            brut -= 65536
        mesures["temperature"] = brut * v_t.get("facteur", 0.1)

        # fins de course (entrées TOR, FC2)
        tor = self.cfg.get("entrees_tor", {})
        for nom, voie in tor.items():
            rr = self.client.read_discrete_inputs(
                address=voie["adresse"], count=1)
            if rr.isError():
                raise IOError(f"Erreur Modbus lecture DI @{voie['adresse']}")
            mesures[nom] = bool(rr.bits[0])
        return mesures

    # ------------------------------------------------------------- écritures

    def _ecrire_ao_volts(self, voie: dict, volts: float) -> None:
        volts = min(max(volts, 0.0), voie.get("pleine_echelle_v", 10.0))
        brut = int(round(volts / voie.get("pleine_echelle_v", 10.0)
                         * self.PLEINE_ECHELLE))
        self.client.write_register(address=voie["adresse"], value=brut)

    def ecrire_variateur(self, volts: float) -> None:
        self._ecrire_ao_volts(self.cfg["sorties"]["variateur"], volts)

    def ecrire_variateur_run(self, marche: bool) -> None:
        if "variateur_run" in self.cfg["sorties"]:
            voie = self.cfg["sorties"]["variateur_run"]
            self.client.write_coil(address=voie["adresse"], value=marche)

    def ecrire_vanne_perte_charge(self, volts: float) -> None:
        if "vanne_perte_charge" in self.cfg["sorties"]:
            self._ecrire_ao_volts(
                self.cfg["sorties"]["vanne_perte_charge"], volts)

    def ecrire_vanne_diaphragme(self, vers_iso: bool) -> None:
        voie = self.cfg["sorties"]["vanne_diaphragme"]
        self.client.write_coil(address=voie["adresse"], value=vers_iso)

    def ecrire_vanne_bypass(self, ouverte: bool) -> None:
        if "vanne_bypass" in self.cfg["sorties"]:
            voie = self.cfg["sorties"]["vanne_bypass"]
            self.client.write_coil(address=voie["adresse"], value=ouverte)

    def fermer(self) -> None:
        try:
            self.ecrire_variateur_run(False)
            self.ecrire_variateur(0.0)
        finally:
            self.client.close()
