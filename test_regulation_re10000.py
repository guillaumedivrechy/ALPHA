#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Test fonctionnel : Régulation Re=10000 en mode ISO."""

import sys
import json
import time
from pathlib import Path
sys.path.insert(0, 'banc_pilotage')

from core.pilote import Pilote
from core.banc import Banc
from io_banc.simulateur import SimulateurBanc

CFG = json.loads((Path("banc_pilotage") / "config.json").read_text(encoding="utf-8"))

def main():
    print("\n" + "="*80)
    print("TEST RÉGULATION Re=10000")
    print("="*80 + "\n")

    banc = Banc(CFG)
    sim = SimulateurBanc(banc, CFG.get("simulation"), CFG.get("modbus"), CFG.get("actionneurs"))
    pilote = Pilote(banc, sim, CFG)

    print("1. Démarrage pilote...")
    pilote.demarrer()
    time.sleep(0.5)

    print("2. Consigne Re=10000...")
    pilote.definir_consigne(10000.0, "conduite")

    print("3. Démarrage régulation...")
    pilote.demarrer_regulation()

    print("4. Attente 30s pour stabilisation...\n")
    time.sleep(30)

    etat = pilote.etat

    print("RÉSULTATS :")
    print(f"  Régulation active    : {etat.get('regulation', False)}")
    print(f"  Re cible             : {etat.get('re_cible', 0):.0f}")
    print(f"  Re mesuré            : {etat.get('re', 0):.0f}")
    print(f"  Tension variateur    : {etat.get('u_variateur', 0):.3f} V")
    print(f"  Variateur RUN        : {etat.get('variateur_run', False)}")
    print(f"  Diaphragme           : {etat.get('diaphragme', '?')}")
    print(f"  Bypass               : {etat.get('bypass', '?')}")
    print(f"  Vanne PDC            : {etat.get('vanne_pdc', '?')}")
    print(f"  Défaut vanne         : {etat.get('defaut_vanne', 'None')}")
    print(f"  Défaut capteur       : {etat.get('defaut_capteur', 'None')}")

    pilote.arreter()

    # Vérifications
    succes = True
    print("\n" + "="*80)
    print("VALIDATION :")

    if etat.get('regulation', False):
        print("[OK] Regulation active")
    else:
        print("[FAIL] Regulation inactive")
        succes = False

    if etat.get('u_variateur', 0) > 0.5:
        print("[OK] Tension variateur > 0.5V")
    else:
        print("[FAIL] Tension variateur trop faible")
        succes = False

    bypass_ok = etat.get('bypass') in ['ferme', 'fermé']
    if etat.get('diaphragme') == 'iso' and bypass_ok:
        print("[OK] Vannes en position correcte (ISO/ferme)")
    else:
        print(f"[FAIL] Vannes mal positionnees (diaph={etat.get('diaphragme')}, bypass={etat.get('bypass')})")
        succes = False

    if not etat.get('defaut_vanne') and not etat.get('defaut_capteur'):
        print("[OK] Aucun defaut detecte")
    else:
        print("[FAIL] Defaut detecte")
        succes = False

    print("="*80)
    if succes:
        print("RESULTAT : [OK] TEST REUSSI")
        return 0
    else:
        print("RESULTAT : [FAIL] TEST ECHOUE")
        return 1

if __name__ == '__main__':
    sys.exit(main())
