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
  - Sorties TOR : coils (FC5) ; l'adressage dépend de la composition réelle
    du nœud — se fier aux adresses de config.json, vérifiées avec
    WAGO I/O CHECK.
  - Module RTD 750-461 (PT100) : valeur signée en 0,1 °C directement.

Vérifiez le mapping réel avec WAGO I/O CHECK ou la doc du coupleur, puis
ajustez config.json — aucun code à modifier.

Le client Modbus est partagé entre plusieurs threads (boucle du pilote,
IHM, apprentissage) : chaque transaction est protégée par un verrou.
"""

from __future__ import annotations
import threading
import time

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
        self._verrou = threading.Lock()   # client pymodbus non thread-safe
        # dernière valeur écrite par sortie analogique : évite de réémettre
        # une valeur inchangée à chaque cycle (voir _ecrire_ao_volts)
        self._ao_cache: dict[int, tuple[int, float]] = {}
        self._t_rafraichissement = cfg_modbus.get(
            "rafraichissement_sorties_s", 2.0)
        self.client = ModbusTcpClient(
            host=cfg_modbus["ip"], port=cfg_modbus.get("port", 502),
            timeout=cfg_modbus.get("timeout_s", 1.0))
        if not self.client.connect():
            raise ConnectionError(
                f"Connexion impossible au coupleur Wago {cfg_modbus['ip']}")

    # ------------------------------------------------------------- lectures

    @staticmethod
    def _signe16(brut: int) -> int:
        """Décodage complément à deux d'un mot 16 bits (valeurs signées)."""
        return brut - 65536 if brut > 32767 else brut

    @staticmethod
    def _blocs(adresses, ecart_max: int = 4) -> list:
        """Regroupe des adresses en blocs (début, nombre) pour lire en une
        transaction par bloc. Les trous ≤ ecart_max sont inclus dans le bloc
        (lire quelques mots inutilisés coûte moins qu'un aller-retour TCP)."""
        blocs: list[list[int]] = []
        for a in sorted(set(adresses)):
            if blocs and a - (blocs[-1][0] + blocs[-1][1] - 1) <= ecart_max:
                blocs[-1][1] = a - blocs[-1][0] + 1
            else:
                blocs.append([a, 1])
        return blocs

    def _volts_vers_unite(self, volts: float, voie: dict) -> float:
        """Conversion volts -> grandeur physique (échelle linéaire capteur)."""
        v0, v1 = voie["volts_min"], voie["volts_max"]
        u0, u1 = voie["valeur_min"], voie["valeur_max"]
        v = min(max(volts, v0), v1)
        return u0 + (v - v0) / (v1 - v0) * (u1 - u0)

    def lire_mesures(self) -> dict:
        # Lectures par blocs d'adresses voisines : 2-3 transactions par cycle
        # au lieu d'une par voie. Moins de latence dans la boucle, et moins
        # d'occasions de timeout (chaque erreur Modbus latche l'arrêt de la
        # régulation côté pilote).
        voies = {nom: v for nom, v in self.cfg["entrees"].items()
                 if not nom.startswith("_")}
        registres = {}
        for debut, nombre in self._blocs(v["adresse"] for v in voies.values()):
            with self._verrou:
                rr = self.client.read_input_registers(
                    address=debut, count=nombre)
            if rr.isError():
                raise IOError(f"Erreur Modbus lecture AI @{debut}+{nombre}")
            for i in range(nombre):
                registres[debut + i] = rr.registers[i]

        mesures = {}
        # échelle capteur si définie (valeur_min/max), valeur brute en volts
        # sinon (recopies) ; PT100 sur module RTD : valeur directe en 0,1 °C
        for nom, voie in voies.items():
            brut = self._signe16(registres[voie["adresse"]])
            if nom == "temperature":
                mesures[nom] = brut * voie.get("facteur", 0.1)
                continue
            volts = brut / self.PLEINE_ECHELLE * voie.get("pleine_echelle_v",
                                                          10.0)
            if "valeur_min" in voie:
                mesures[nom] = self._volts_vers_unite(volts, voie)
            else:
                mesures[nom] = volts

        # fins de course (entrées TOR, FC2), lues en bloc également
        tor = {nom: v for nom, v in self.cfg.get("entrees_tor", {}).items()
               if not nom.startswith("_")}
        bits = {}
        for debut, nombre in self._blocs(v["adresse"] for v in tor.values()):
            with self._verrou:
                rr = self.client.read_discrete_inputs(
                    address=debut, count=nombre)
            if rr.isError():
                raise IOError(f"Erreur Modbus lecture DI @{debut}+{nombre}")
            for i in range(nombre):
                bits[debut + i] = bool(rr.bits[i])
        for nom, voie in tor.items():
            mesures[nom] = bits[voie["adresse"]]
        return mesures

    # ------------------------------------------------------------- écritures

    def _ecrire_ao_volts(self, voie: dict, volts: float) -> None:
        volts = min(max(volts, 0.0), voie.get("pleine_echelle_v", 10.0))
        brut = int(round(volts / voie.get("pleine_echelle_v", 10.0)
                         * self.PLEINE_ECHELLE))
        # écriture sur changement seulement, avec réécriture périodique en
        # garde-fou (coupleur redémarré ou écriture perdue). Un changement
        # de valeur — dont les mises à 0 de sécurité — part immédiatement.
        adresse = voie["adresse"]
        precedent = self._ao_cache.get(adresse)
        maintenant = time.monotonic()
        if precedent is not None and precedent[0] == brut \
                and maintenant - precedent[1] < self._t_rafraichissement:
            return
        with self._verrou:
            self.client.write_register(address=adresse, value=brut)
        self._ao_cache[adresse] = (brut, maintenant)

    def ecrire_variateur(self, volts: float) -> None:
        self._ecrire_ao_volts(self.cfg["sorties"]["variateur"], volts)

    def ecrire_variateur_run(self, marche: bool) -> None:
        if "variateur_run" in self.cfg["sorties"]:
            voie = self.cfg["sorties"]["variateur_run"]
            with self._verrou:
                self.client.write_coil(address=voie["adresse"], value=marche)

    def ecrire_vanne_perte_charge(self, volts: float) -> None:
        if "vanne_perte_charge" in self.cfg["sorties"]:
            self._ecrire_ao_volts(
                self.cfg["sorties"]["vanne_perte_charge"], volts)

    def ecrire_vanne_diaphragme(self, vers_iso: bool) -> None:
        voie = self.cfg["sorties"]["vanne_diaphragme"]
        with self._verrou:
            self.client.write_coil(address=voie["adresse"], value=vers_iso)

    def ecrire_vanne_bypass(self, ouverte: bool) -> None:
        if "vanne_bypass" in self.cfg["sorties"]:
            voie = self.cfg["sorties"]["vanne_bypass"]
            with self._verrou:
                self.client.write_coil(address=voie["adresse"], value=ouverte)

    def fermer(self) -> None:
        try:
            self.ecrire_variateur_run(False)
            self.ecrire_variateur(0.0)
        finally:
            with self._verrou:
                self.client.close()
