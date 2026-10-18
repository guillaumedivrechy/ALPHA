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
        sys.exit(f"Fichier de configuration introuvable : {chemin_cfg}")
    cfg = json.loads(chemin_cfg.read_text(encoding="utf-8"))
    banc = Banc(cfg)

    if args.simulation:
        from io_banc.simulateur import SimulateurBanc
        es = SimulateurBanc(banc, cfg.get("simulation"), cfg.get("modbus"), cfg.get("actionneurs"))
        mode = "simulation"
    else:
        from io_banc.wago import WagoModbus
        try:
            es = WagoModbus(cfg["modbus"])
        except (ConnectionError, RuntimeError) as exc:
            sys.exit(
                f"\n{exc}\n\n"
                f"  - Vérifiez l'adresse IP du coupleur 750-362 dans "
                f"config.json (section \"modbus\")\n"
                f"    et que le PC est sur le même sous-réseau "
                f"(test : ping {cfg['modbus']['ip']}).\n"
                f"  - Pour travailler sans le matériel, lancez :  "
                f"python main.py --simulation\n")
        mode = "wago " + cfg["modbus"]["ip"]

    pilote = Pilote(banc, es, cfg)
    pilote.demarrer()

    qt_app = QApplication(sys.argv)
    app = Application(pilote, mode, cfg, chemin_cfg)
    app.show()
    sys.exit(qt_app.exec())


if __name__ == "__main__":
    main()
