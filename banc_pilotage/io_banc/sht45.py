# -*- coding: utf-8 -*-
"""Capteur d'air Sensirion SHT45 (température et humidité relative), lu par
un boîtier SEK-SensorBridge branché en USB sur le PC.

Chaîne : SHT45 --I²C--> SEK-SensorBridge (port ONE ou TWO) --USB/SHDLC--> PC.
Bibliothèques Sensirion : sensirion-shdlc-driver et
sensirion-shdlc-sensorbridge (importées à la connexion seulement : le
logiciel démarre sans elles, capteur signalé absent).

La lecture tourne dans un thread dédié, à la période `sht45.periode_s`
(1 s) : la boucle de régulation (0,2 s) ne fait que reprendre la dernière
mesure, sans jamais attendre la liaison série. Sur erreur (boîtier
débranché, trame corrompue), la liaison est refermée puis retentée toutes
les `reconnexion_s`.

Commandes du SHT4x (fiche technique Sensirion SHT4x) :
  0xFD  mesure T et HR, haute répétabilité (8,3 ms au plus)
  0x89  numéro de série
Réponse : 6 octets = mot T (2) + CRC, mot HR (2) + CRC ;
CRC-8 polynôme 0x31, valeur initiale 0xFF.
  T  [°C]  = -45 + 175 × S_T / 65535
  HR [%HR] =  -6 + 125 × S_HR / 65535, ramenée dans 0-100
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

CMD_MESURE_HAUTE_PRECISION = 0xFD
CMD_NUMERO_SERIE = 0x89
ADRESSE_I2C_DEFAUT = 0x44
# tensions acceptées par le SHT45 (1,08 à 3,6 V) parmi celles que le
# SEK-SensorBridge sait fournir
TENSIONS_ADMISES = (1.2, 1.5, 1.8, 2.1, 2.4, 2.7, 3.0, 3.3, 3.6)
PORTS_BRIDGE = {"ONE": 0, "TWO": 1}
# délai I²C : le boîtier relance la lecture tant que le capteur répond NACK
# (mesure en cours), au plus ce délai
DELAI_I2C_US = 50_000


@dataclass(frozen=True)
class MesureAir:
    temperature_c: float
    humidite_pct: float
    t: float                       # instant de la mesure (time.monotonic)


# --------------------------------------------------------------- décodage

def crc8(donnees) -> int:
    """CRC-8 Sensirion (polynôme 0x31, initial 0xFF, sans inversion)."""
    crc = 0xFF
    for octet in bytes(donnees):
        crc ^= octet
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _mots(rx, n: int) -> list[int]:
    """Mots de 16 bits d'une réponse SHT4x, CRC vérifiés. ValueError sinon."""
    rx = bytes(rx)
    if len(rx) != 3 * n:
        raise ValueError(f"réponse SHT45 de {len(rx)} octets ({3 * n} attendus)")
    mots = []
    for i in range(n):
        mot, crc = rx[3 * i:3 * i + 2], rx[3 * i + 2]
        if crc8(mot) != crc:
            raise ValueError("CRC invalide dans la réponse du SHT45 "
                             "(liaison I²C perturbée)")
        mots.append(mot[0] << 8 | mot[1])
    return mots


def decoder_mesure(rx) -> tuple[float, float]:
    """Réponse de la commande 0xFD -> (T [°C], HR [%HR])."""
    s_t, s_hr = _mots(rx, 2)
    t = -45.0 + 175.0 * s_t / 65535.0
    hr = -6.0 + 125.0 * s_hr / 65535.0
    return t, min(max(hr, 0.0), 100.0)


def decoder_numero_serie(rx) -> int:
    haut, bas = _mots(rx, 2)
    return haut << 16 | bas


def _adresse(valeur) -> int:
    """Adresse I²C : entier (68) ou texte hexadécimal ("0x44")."""
    if isinstance(valeur, str):
        return int(valeur, 0)
    return int(valeur) if valeur is not None else ADRESSE_I2C_DEFAUT


@dataclass
class ConfigSht45:
    port_serie: str = "auto"          # "auto" ou "COM3", "/dev/ttyUSB0"…
    port_bridge: str = "auto"         # "auto", "ONE" ou "TWO"
    tension_v: float = 3.3
    adresse_i2c: int = ADRESSE_I2C_DEFAUT
    debit_bauds: int = 460800
    periode_s: float = 1.0
    reconnexion_s: float = 5.0
    delai_connexion_s: float = 10.0    # « connexion en cours » au démarrage

    @classmethod
    def depuis(cls, cfg: dict | None) -> "ConfigSht45":
        """Section `sht45` de config.json (clés absentes : défauts)."""
        c = cfg or {}
        d = cls()
        tension = float(c.get("tension_v", d.tension_v))
        if tension not in TENSIONS_ADMISES:
            raise ValueError(f"sht45.tension_v = {tension} V : valeurs "
                             f"admises {TENSIONS_ADMISES} (SHT45 : 1,08 à 3,6 V)")
        port_bridge = str(c.get("port_bridge", d.port_bridge)).upper()
        if port_bridge not in ("AUTO", *PORTS_BRIDGE):
            raise ValueError(f"sht45.port_bridge = {port_bridge} : "
                             "\"auto\", \"ONE\" ou \"TWO\"")
        return cls(
            port_serie=str(c.get("port_serie", d.port_serie)),
            port_bridge=port_bridge.lower() if port_bridge == "AUTO" else port_bridge,
            tension_v=tension,
            adresse_i2c=_adresse(c.get("adresse_i2c", d.adresse_i2c)),
            debit_bauds=int(c.get("debit_bauds", d.debit_bauds)),
            periode_s=max(float(c.get("periode_s", d.periode_s)), 0.2),
            reconnexion_s=max(float(c.get("reconnexion_s", d.reconnexion_s)), 0.5),
            delai_connexion_s=max(float(c.get("delai_connexion_s",
                                              d.delai_connexion_s)), 0.0),
        )


# ------------------------------------------------------------- interface

class CapteurAir:
    """Interface commune (matériel, simulé). `derniere_mesure` ne bloque
    jamais : elle rend la dernière mesure valide, ou None."""

    description = "SHT45"
    erreur: str | None = None
    port_serie_actif: str | None = None    # port série en service, ou None
    connexion_en_cours = False             # 1er contact pas encore établi

    def demarrer(self) -> None:
        pass

    def arreter(self) -> None:
        pass

    def derniere_mesure(self) -> MesureAir | None:
        raise NotImplementedError


# ----------------------------------------------------- accès aux bibliothèques

def lister_ports_usb() -> list[str]:
    """Ports série USB, ceux qui ressemblent à un SEK-SensorBridge d'abord."""
    from serial.tools import list_ports
    ports = [p for p in list_ports.comports() if p.vid is not None]

    def rang(p):
        texte = " ".join(str(x) for x in (p.description, p.manufacturer,
                                           p.product) if x).lower()
        return 0 if ("sensirion" in texte or "bridge" in texte) else 1
    return [p.device for p in sorted(ports, key=rang)]


class _BridgeSensirion:
    """SEK-SensorBridge ouvert sur un port série (bibliothèques Sensirion)."""

    def __init__(self, port_serie: str, debit_bauds: int):
        from sensirion_shdlc_driver import ShdlcConnection, ShdlcSerialPort
        from sensirion_shdlc_sensorbridge import (SensorBridgePort,
                                                  SensorBridgeShdlcDevice)
        self._ports = {0: SensorBridgePort.ONE, 1: SensorBridgePort.TWO}
        self._serie = ShdlcSerialPort(port=port_serie, baudrate=debit_bauds,
                                      additional_response_time=0.05)
        try:
            self._device = SensorBridgeShdlcDevice(ShdlcConnection(self._serie),
                                                   slave_address=0)
            self.nom = self._device.get_product_name()
        except Exception:
            self._serie.close()
            raise

    def alimenter(self, port: int, tension_v: float, actif: bool) -> None:
        p = self._ports[port]
        if actif:
            self._device.set_supply_voltage(p, tension_v)
            self._device.switch_supply_on(p)
        else:
            self._device.switch_supply_off(p)

    def echanger_i2c(self, port: int, adresse: int, tx: list[int],
                     n_rx: int) -> bytes:
        return bytes(self._device.transceive_i2c(
            self._ports[port], adresse, tx, n_rx, DELAI_I2C_US))

    def fermer(self) -> None:
        self._serie.close()


# --------------------------------------------------------------- matériel

class Sht45SensorBridge(CapteurAir):
    """SHT45 sur SEK-SensorBridge, lu dans un thread dédié.

    `ouvrir_bridge(port_serie, debit)` et `lister_ports()` sont injectables
    (tests sans matériel) ; par défaut, bibliothèques Sensirion et pyserial."""

    def __init__(self, cfg: dict | None = None, ouvrir_bridge=None,
                 lister_ports=None):
        self.cfg = ConfigSht45.depuis(cfg)
        self._ouvrir = ouvrir_bridge or _BridgeSensirion
        self._lister = lister_ports or lister_ports_usb
        self._verrou = threading.Lock()
        self._derniere: MesureAir | None = None
        self._bridge = None
        self._port = None                  # 0 = ONE, 1 = TWO
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.erreur = "connexion en cours"
        self.description = "SHT45 (non connecté)"
        self.numero_serie: int | None = None
        self.port_serie_actif = None
        self._t_demarrage: float | None = None
        self._deja_mesure = False

    @property
    def connexion_en_cours(self) -> bool:
        """Démarrage normal, distinct d'un capteur perdu : aucune mesure
        encore reçue, et délai de connexion (énumération USB, recherche du
        port) pas écoulé. Après une 1re mesure, une absence est une perte."""
        if self._deja_mesure:
            return False
        return self._t_demarrage is None or \
            time.monotonic() - self._t_demarrage < self.cfg.delai_connexion_s

    # ----- cycle de vie
    def demarrer(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._t_demarrage = time.monotonic()
        self._thread = threading.Thread(target=self._boucle, name="sht45",
                                        daemon=True)
        self._thread.start()

    def arreter(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)
            self._thread = None
        self._fermer()

    def derniere_mesure(self) -> MesureAir | None:
        with self._verrou:
            return self._derniere

    # ----- liaison
    def connecter(self) -> None:
        """Trouve le boîtier puis le port où répond le SHT45. Lève une
        exception au message explicite si rien ne répond."""
        self._fermer()
        c = self.cfg
        if c.port_serie.lower() == "auto":
            candidats = list(self._lister())
            if not candidats:
                raise ConnectionError("aucun port série USB : SEK-SensorBridge "
                                      "débranché ou pilote USB absent")
        else:
            candidats = [c.port_serie]
        echecs = []
        for port_serie in candidats:
            try:
                bridge = self._ouvrir(port_serie, c.debit_bauds)
            except ImportError as exc:
                raise ConnectionError(
                    f"bibliothèques Sensirion absentes ({exc}) : "
                    "pip install -r requirements.txt") from None
            except Exception as exc:  # noqa: BLE001 — port occupé, muet…
                echecs.append(f"{port_serie} : {exc}")
                continue
            port = self._trouver_capteur(bridge)
            if port is not None:
                self._bridge, self._port = bridge, port
                self.port_serie_actif = port_serie
                nom_port = {0: "ONE", 1: "TWO"}[port]
                self.description = (f"SHT45 n° {self.numero_serie} — "
                                    f"{port_serie}, port {nom_port}, "
                                    f"{c.tension_v:g} V")
                log.info("Capteur d'air : %s", self.description)
                return
            echecs.append(f"{port_serie} : SEK-SensorBridge sans SHT45 répondant "
                          f"à l'adresse 0x{c.adresse_i2c:02X}")
            bridge.fermer()
        raise ConnectionError("SHT45 introuvable — " + " ; ".join(echecs))

    def _trouver_capteur(self, bridge) -> int | None:
        c = self.cfg
        ports = ([PORTS_BRIDGE[c.port_bridge]] if c.port_bridge in PORTS_BRIDGE
                 else [0, 1])
        for port in ports:
            try:
                bridge.alimenter(port, c.tension_v, True)
                time.sleep(0.01)             # mise sous tension : 1 ms
                rx = bridge.echanger_i2c(port, c.adresse_i2c,
                                         [CMD_NUMERO_SERIE], 6)
                self.numero_serie = decoder_numero_serie(rx)
                return port
            except Exception:  # noqa: BLE001 — rien sur ce port
                try:
                    bridge.alimenter(port, c.tension_v, False)
                except Exception:  # noqa: BLE001
                    pass
        return None

    def mesurer(self) -> MesureAir:
        """Une mesure haute précision (bloquante, ~10 ms + liaison)."""
        rx = self._bridge.echanger_i2c(self._port, self.cfg.adresse_i2c,
                                       [CMD_MESURE_HAUTE_PRECISION], 6)
        t, hr = decoder_mesure(rx)
        return MesureAir(t, hr, time.monotonic())

    def _fermer(self) -> None:
        self.port_serie_actif = None
        bridge, self._bridge = self._bridge, None
        if bridge is not None:
            try:
                bridge.fermer()
            except Exception:  # noqa: BLE001
                pass

    # ----- thread
    def _boucle(self) -> None:
        while not self._stop.is_set():
            if self._bridge is None:
                try:
                    self.connecter()
                except Exception as exc:  # noqa: BLE001
                    self._signaler(f"{exc}")
                    self._stop.wait(self.cfg.reconnexion_s)
                    continue
            try:
                mesure = self.mesurer()
            except Exception as exc:  # noqa: BLE001
                self._signaler(f"lecture impossible ({exc})")
                self._fermer()
                self._stop.wait(self.cfg.reconnexion_s)
                continue
            with self._verrou:
                self._derniere = mesure
            self.erreur = None
            self._deja_mesure = True
            self._stop.wait(self.cfg.periode_s)

    def _signaler(self, motif: str) -> None:
        if motif != self.erreur:
            log.warning("Capteur d'air SHT45 : %s", motif)
        self.erreur = motif


# --------------------------------------------------------------- simulation

class Sht45Simule(CapteurAir):
    """SHT45 virtuel (mode --simulation, tests). `temperature` : fonction
    rendant la température de l'air simulée [°C] ; humidité constante,
    bruitée. `debrancher()` / `rebrancher()` simulent une panne."""

    description = "SHT45 simulé"
    port_serie_actif = "simulation"

    def __init__(self, temperature=lambda: 20.0, humidite_pct: float = 50.0,
                 bruit_t: float = 0.02, bruit_hr: float = 0.1, graine: int = 45):
        self._temperature = temperature
        # générateur propre : le bruit du simulateur de banc (graine des
        # tests) n'en est pas décalé
        self._aleas = random.Random(graine)
        self.humidite_pct = humidite_pct
        self.bruit_t, self.bruit_hr = bruit_t, bruit_hr
        self._derniere: MesureAir | None = None
        self.erreur = None

    def debrancher(self) -> None:
        self.erreur = "SEK-SensorBridge débranché (simulation)"

    def rebrancher(self) -> None:
        self.erreur = None

    def derniere_mesure(self) -> MesureAir | None:
        if self.erreur is None:
            hr = self.humidite_pct + self._aleas.gauss(0.0, self.bruit_hr)
            self._derniere = MesureAir(
                self._temperature() + self._aleas.gauss(0.0, self.bruit_t),
                min(max(hr, 0.0), 100.0), time.monotonic())
        return self._derniere
