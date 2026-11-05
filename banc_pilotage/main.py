#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Logiciel de pilotage du banc aéraulique — diaphragmes ISO 5167 / Miller.

Usage :
    python main.py                # connexion au coupleur Wago (config.json)
    python main.py --simulation   # banc simulé, sans matériel
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtWidgets import QApplication

from core.banc import Banc
from core.pilote import Pilote
from gui.app import Application


def _abandon(message: str) -> None:
    """Démarrage impossible : message clair, sans trace Python. Sans console
    (pythonw, double-clic), le message part dans une boîte de dialogue —
    sinon il disparaissait avec la fenêtre."""
    if sys.stderr is None:
        from PySide6.QtWidgets import QMessageBox
        _app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(None, "Banc aéraulique — démarrage impossible",
                             message)
        sys.exit(1)
    sys.exit(f"\nDémarrage impossible :\n{message}\n")


def _capteur_air(cfg: dict, simulation: bool, es):
    """Capteur d'air SHT45 (SEK-SensorBridge en USB), virtuel en simulation ;
    None si sht45.actif = false. Absent ou débranché, le logiciel démarre :
    le pilote le signale et retombe sur les valeurs saisies."""
    c = cfg.get("sht45", {})
    if not c.get("actif", True):
        return None
    if simulation:
        from io_banc.sht45 import Sht45Simule
        return Sht45Simule(
            temperature=lambda: es.temperature,
            humidite_pct=cfg.get("fluide", {}).get("humidite_relative", 50.0))
    from io_banc.sht45 import Sht45SensorBridge
    return Sht45SensorBridge(c)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulation", action="store_true",
                        help="banc simulé (développement sans matériel)")
    parser.add_argument("--config", default=None,
                        help="chemin du fichier de configuration "
                             "(défaut : config.json à côté de main.py)")
    args = parser.parse_args()

    if args.config:
        chemin_cfg = Path(args.config)
    else:
        chemin_cfg = Path(__file__).resolve().parent / "config.json"
    if not chemin_cfg.exists():
        _abandon(f"Fichier de configuration introuvable : {chemin_cfg}")
    try:
        cfg = json.loads(chemin_cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:     # JSONDecodeError, encodage
        _abandon(f"{chemin_cfg.name} illisible : {exc}\n\n"
                 "Restaurer la dernière version valide du fichier "
                 "(historique git, sauvegarde).")
    try:
        banc = Banc(cfg)
    except KeyError as exc:
        _abandon(f"{chemin_cfg.name} : paramètre obligatoire absent ({exc}).")
    except (ValueError, TypeError) as exc:
        _abandon(f"{chemin_cfg.name} : valeur invalide — {exc}")

    if args.simulation:
        from io_banc.simulateur import SimulateurBanc
        es = SimulateurBanc(banc, cfg.get("simulation"), cfg.get("modbus"), cfg.get("actionneurs"))
        mode = "simulation"
    else:
        from io_banc.wago import WagoModbus
        try:
            es = WagoModbus(cfg["modbus"])
        except (ConnectionError, RuntimeError) as exc:
            _abandon(
                f"{exc}\n\n"
                f"  - Vérifiez l'adresse IP du coupleur 750-362 dans "
                f"config.json (section \"modbus\")\n"
                f"    et que le PC est sur le même sous-réseau "
                f"(test : ping {cfg['modbus']['ip']}).\n"
                f"  - Pour travailler sans le matériel, lancez :  "
                f"python main.py --simulation\n")
        mode = "wago " + cfg["modbus"]["ip"]

    try:
        capteur_air = _capteur_air(cfg, args.simulation, es)
    except ValueError as exc:
        _abandon(f"{chemin_cfg.name}, section \"sht45\" : {exc}")

    try:
        pilote = Pilote(banc, es, cfg, capteur_air)
        pilote.demarrer()
    except KeyError as exc:
        _abandon(f"{chemin_cfg.name} : paramètre obligatoire absent ({exc}).")
    except (ValueError, TypeError, RuntimeError) as exc:
        _abandon(f"Initialisation du pilote impossible : {exc}")

    qt_app = QApplication(sys.argv)
    app = Application(pilote, mode, cfg, chemin_cfg)
    app.show()
    sys.exit(qt_app.exec())


if __name__ == "__main__":
    main()
