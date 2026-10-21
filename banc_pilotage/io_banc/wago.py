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
import logging
import math
import sys
import threading
import time

try:
    from pymodbus.client import ModbusTcpClient
    from pymodbus.exceptions import ModbusException
    PYMODBUS_DISPONIBLE = True
except ImportError:  # le mode simulation fonctionne sans pymodbus
    PYMODBUS_DISPONIBLE = False

    class ModbusException(Exception):          # noqa: N818 (repli sans pymodbus)
        pass


class EntreesSorties:
    """Interface abstraite commune au matériel réel et au simulateur."""

    #: Dernière image process Modbus telle que lue, AVANT mise à l'échelle :
    #: {"mots": {adresse: brut}, "bits": {adresse: booleen}}. Sert à la page
    #: de mise en service, où l'on veut voir le registre et pas seulement la
    #: grandeur physique : une voie mal adressée donne souvent une valeur
    #: plausible. None si l'implémentation ne la fournit pas.
    image_process: dict | None = None

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

    def ecrire_vanne_diaphragme(self, vers_0deg: bool) -> None:
        """Vanne 3 voies, carte GF3 : True -> ordre F (butée 0°),
        False -> ordre I (position intermédiaire 90°).

        Les deux ordres sont portés par deux relais distincts et ne doivent
        jamais être actifs ensemble : la carte recevrait deux consignes
        contradictoires. La correspondance angle -> diaphragme est dans
        config.json (modbus.positions_vanne_3voies), pas ici."""
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
        # client pymodbus non thread-safe. Réentrant : une écriture tient le
        # verrou du test de son cache jusqu'à sa mise à jour, transaction
        # comprise (_transaction le reprend)
        self._verrou = threading.RLock()
        # dernière valeur écrite par sortie : évite de réémettre une valeur
        # inchangée à chaque cycle, tout en la réaffirmant périodiquement
        # (voir _ecrire_ao_volts et _ecrire_coil)
        self._ao_cache: dict[int, tuple[int, float]] = {}
        self._coil_cache: dict[int, tuple[bool, float]] = {}
        self._t_rafraichissement = cfg_modbus.get(
            "rafraichissement_sorties_s", 2.0)
        # Délai après fermeture socket pour éviter TIME_WAIT (reconnexion rapide)
        self._delai_fermeture_s = cfg_modbus.get("delai_fermeture_s", 0.05)
        # Créer le client Modbus TCP
        # retries : nouvelles tentatives par transaction (pymodbus : 3 par
        # défaut). Une seule : un paquet perdu reste toléré, et un coupleur
        # muet est détecté en 2 × timeout_s au lieu de 4 ×.
        self.client = ModbusTcpClient(
            host=cfg_modbus["ip"], port=cfg_modbus.get("port", 502),
            timeout=cfg_modbus.get("timeout_s", 1.0),
            retries=cfg_modbus.get("retries", 1))

        # Tentatives de connexion avec retry (robustesse à la reconnexion)
        tentatives = 3
        delai_s = 0.5
        for i in range(tentatives):
            if self.client.connect():
                break
            if i < tentatives - 1:
                time.sleep(delai_s)
        else:
            raise ConnectionError(
                f"Connexion impossible au coupleur Wago {cfg_modbus['ip']} "
                f"après {tentatives} tentatives")

    def _transaction(self, requete, **params):
        """Transaction Modbus sous verrou. Les exceptions de pymodbus
        (ConnectionException, ModbusIOException…) ne dérivent pas d'OSError :
        sur une coupure du coupleur, elles passaient au travers des replis
        sûrs (qui attrapent IOError/OSError) et tuaient le thread de la
        boucle, laissant le variateur à sa dernière consigne. Elles sont
        converties en ConnectionError (une OSError)."""
        with self._verrou:
            try:
                return requete(**params)
            except ModbusException as exc:
                raise ConnectionError(f"liaison Modbus perdue : {exc}") from exc

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
            rr = self._transaction(self.client.read_input_registers,
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
            rr = self._transaction(self.client.read_discrete_inputs,
                                   address=debut, count=nombre)
            if rr.isError():
                raise IOError(f"Erreur Modbus lecture DI @{debut}+{nombre}")
            for i in range(nombre):
                bits[debut + i] = bool(rr.bits[i])
        for nom, voie in tor.items():
            # "inverse" : entrée câblée sur un contact NF (MB du variateur,
            # FC3 de la vanne 3 voies). Une rupture de fil produit alors
            # l'état actif, ce qui rend le défaut détectable.
            mesures[nom] = bits[voie["adresse"]] != voie.get("inverse", False)
        self.image_process = {"mots": dict(registres), "bits": dict(bits)}
        return mesures

    # ------------------------------------------------------------- écritures

    def _ecrire_ao_volts(self, voie: dict, volts: float) -> None:
        # valeur non finie (calcul en défaut) : 0 V, jamais la pleine échelle
        # (inf était bornée à 10 V, NaN levait ValueError à la conversion)
        if not math.isfinite(volts):
            logging.error(f"Consigne AO non finie ({volts}) @{voie['adresse']} : 0 V")
            volts = 0.0
        volts = min(max(volts, 0.0), voie.get("pleine_echelle_v", 10.0))
        brut = int(round(volts / voie.get("pleine_echelle_v", 10.0)
                         * self.PLEINE_ECHELLE))
        # écriture sur changement seulement, avec réécriture périodique en
        # garde-fou (coupleur redémarré ou écriture perdue). Un changement
        # de valeur — dont les mises à 0 de sécurité — part immédiatement.
        # test du cache, écriture et mise à jour du cache d'un seul tenant :
        # sinon, ARRÊT (thread IHM) écrivait 0 V, la boucle 3 V juste
        # derrière, puis l'IHM inscrivait 0 dans le cache -> le 0 V suivant
        # était dédupliqué, matériel resté à 3 V jusqu'au rafraîchissement
        adresse = voie["adresse"]
        with self._verrou:
            precedent = self._ao_cache.get(adresse)
            maintenant = time.monotonic()
            if precedent is not None and precedent[0] == brut \
                    and maintenant - precedent[1] < self._t_rafraichissement:
                return
            rr = self._transaction(self.client.write_register,
                                   address=adresse, value=brut)
            if rr.isError():
                # ne PAS mettre en cache : une mise à 0 de sécurité refusée
                # doit être retentée au cycle suivant, pas dédupliquée
                raise IOError(f"Erreur Modbus écriture AO @{adresse}")
            self._ao_cache[adresse] = (brut, maintenant)

    def _ecrire_coil(self, voie: dict, etat: bool) -> bool:
        """Écriture d'une sortie TOR, même politique que les sorties
        analogiques : on n'émet que sur changement, mais on réaffirme
        périodiquement. Sans cette réaffirmation, un coupleur redémarré
        laisserait les relais retombés — donc, sur la vanne 3 voies dont la
        carte est alimentée par ses bornes de commande, un actionneur sans
        tension et sans résistance anti-condensation.

        Retourne True si la sortie a effectivement été écrite."""
        adresse = voie["adresse"]
        with self._verrou:                 # même raison que les AO
            precedent = self._coil_cache.get(adresse)
            maintenant = time.monotonic()
            if precedent is not None and precedent[0] == etat \
                    and maintenant - precedent[1] < self._t_rafraichissement:
                return False
            rr = self._transaction(self.client.write_coil,
                                   address=adresse, value=etat)
            if rr.isError():
                # ne PAS mettre en cache : un retrait de sécurité refusé doit
                # être retenté au cycle suivant, pas dédupliqué
                raise IOError(f"Erreur Modbus écriture coil @{adresse}")
            self._coil_cache[adresse] = (etat, maintenant)
        return True

    def ecrire_variateur(self, volts: float) -> None:
        self._ecrire_ao_volts(self.cfg["sorties"]["variateur"], volts)

    def ecrire_variateur_run(self, marche: bool) -> None:
        if "variateur_run" in self.cfg["sorties"]:
            self._ecrire_coil(self.cfg["sorties"]["variateur_run"], marche)

    def ecrire_vanne_perte_charge(self, volts: float) -> None:
        if "vanne_perte_charge" in self.cfg["sorties"]:
            self._ecrire_ao_volts(
                self.cfg["sorties"]["vanne_perte_charge"], volts)

    def ecrire_vanne_diaphragme(self, vers_0deg: bool) -> None:
        s = self.cfg["sorties"]
        cible = "vanne_diaphragme_0deg" if vers_0deg else "vanne_diaphragme_90deg"
        autre = "vanne_diaphragme_90deg" if vers_0deg else "vanne_diaphragme_0deg"
        # relâcher avant de coller : jamais les deux ordres ensemble
        relache = self._ecrire_coil(s[autre], False)
        # Délai minimal pour que la carte GF3 voie le relâchement de l'ancien
        # ordre avant le nouveau (évite un demi-tour 0°→90°). Seulement si un
        # relâchement a vraiment été émis : la boucle réaffirme l'ordre à
        # chaque cycle, et 50 ms de pause systématique prenaient le quart de
        # la période de 0,2 s.
        if relache and self._coil_cache.get(s[cible]["adresse"], (False,))[0] is not True:
            time.sleep(0.05)  # 50 ms
        self._ecrire_coil(s[cible], True)

    def ecrire_vanne_bypass(self, ouverte: bool) -> None:
        if "vanne_bypass" in self.cfg["sorties"]:
            self._ecrire_coil(self.cfg["sorties"]["vanne_bypass"], ouverte)

    def fermer(self) -> None:
        """Fermeture propre de la connexion Modbus.

        Arrête d'abord le variateur (sécurité), puis ferme la connexion TCP.
        Log les erreurs critiques mais ne lève pas d'exception (best-effort).
        """
        erreurs_critiques = []

        # Tentative 1 : arrêter signal RUN (priorité critique)
        for tentative in range(3):
            try:
                self.ecrire_variateur_run(False)
                break
            except (IOError, OSError, ConnectionError, TimeoutError) as e:
                if tentative == 2:  # Dernier essai
                    msg = f"SHUTDOWN CRITIQUE : Signal RUN non désactivé après 3 tentatives : {e}"
                    erreurs_critiques.append(msg)
                    logging.error(msg)
                time.sleep(0.05)  # 50ms entre tentatives

        # Tentative 2 : forcer tension à 0V (priorité haute)
        for tentative in range(3):
            try:
                self.ecrire_variateur(0.0)
                break
            except (IOError, OSError, ConnectionError, TimeoutError) as e:
                if tentative == 2:
                    msg = f"SHUTDOWN CRITIQUE : Tension variateur non mise à 0V après 3 tentatives : {e}"
                    erreurs_critiques.append(msg)
                    logging.error(msg)
                time.sleep(0.05)

        # Fermeture connexion TCP (best-effort, erreurs attendues si déjà déconnecté)
        try:
            with self._verrou:
                if self.client.connected:
                    self.client.close()
                # Laisser un court délai pour que le socket TCP se ferme
                # proprement côté OS (évite TIME_WAIT bloquant la reconnexion).
                # TCP FIN/ACK prend typiquement 10-50ms selon l'OS.
                time.sleep(self._delai_fermeture_s)
        except (IOError, OSError, ConnectionError) as e:
            # Erreur acceptable (connexion déjà fermée, timeout réseau, etc.)
            logging.debug(f"Fermeture connexion Modbus (erreur ignorée) : {e}")

        # Si échecs critiques, les signaler ET lever exception
        if erreurs_critiques:
            msg_complet = f"FERMETURE INCOMPLETE : {len(erreurs_critiques)} erreurs critiques"
            logging.critical(msg_complet)
            for err in erreurs_critiques:
                print(f"[ERREUR SHUTDOWN] {err}", file=sys.stderr)

            # CRITIQUE : Lever exception pour forcer intervention opérateur
            # Ne PAS ignorer silencieusement un variateur qui reste actif
            raise RuntimeError(
                f"Arrêt sécurisé impossible : {'; '.join(erreurs_critiques)}\n"
                f"INTERVENTION REQUISE : Vérifier état variateur et arrêt manuel si nécessaire"
            )
