#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Contrôle du capteur d'air SHT45 (SEK-SensorBridge en USB), SANS lancer
le logiciel du banc. À exécuter sur le PC du banc :

    python controle_sht45.py            # réglages de config.json (sht45)
    python controle_sht45.py COM3       # port série imposé

Affiche les ports série USB vus, le boîtier et le port (ONE/TWO) où répond
le SHT45, son numéro de série, puis 10 mesures (une par seconde). Copier
toute la sortie en cas de problème.
"""

import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from io_banc.sht45 import Sht45SensorBridge, lister_ports_usb  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    chemin = Path(__file__).resolve().parent / "config.json"
    try:
        cfg = json.loads(chemin.read_text(encoding="utf-8")).get("sht45", {})
    except (OSError, ValueError) as exc:
        print(f"config.json illisible ({exc}) : réglages par défaut")
        cfg = {}
    if len(sys.argv) > 1:
        cfg = dict(cfg, port_serie=sys.argv[1])

    print("== Ports série USB")
    try:
        ports = lister_ports_usb()
    except ImportError:
        print("pyserial absent : pip install -r requirements.txt")
        return 1
    print("   " + (", ".join(ports) if ports else "aucun"))

    capteur = Sht45SensorBridge(cfg)
    print(f"== Connexion (port série {capteur.cfg.port_serie}, port boîtier "
          f"{capteur.cfg.port_bridge}, {capteur.cfg.tension_v:g} V, "
          f"adresse 0x{capteur.cfg.adresse_i2c:02X})")
    try:
        capteur.connecter()
    except Exception as exc:  # noqa: BLE001
        print(f"   ÉCHEC : {exc}")
        return 1
    print(f"   OK : {capteur.description}")

    print("== Mesures")
    erreurs = 0
    try:
        for i in range(10):
            try:
                m = capteur.mesurer()
                print(f"   {i + 1:2d}  T = {m.temperature_c:6.2f} °C   "
                      f"HR = {m.humidite_pct:5.1f} %")
            except Exception as exc:  # noqa: BLE001
                erreurs += 1
                print(f"   {i + 1:2d}  ERREUR : {exc}")
            time.sleep(1.0)
    finally:
        capteur.arreter()
    print("== " + ("terminé sans erreur" if not erreurs
                   else f"{erreurs} lecture(s) en erreur sur 10"))
    return 1 if erreurs else 0


if __name__ == "__main__":
    sys.exit(main())
