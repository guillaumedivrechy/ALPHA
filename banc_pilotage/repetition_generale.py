# -*- coding: utf-8 -*-
"""Répétition générale de la mise en service, en simulation.

Déroule la procédure complète du jour J dans l'ordre, avec « Appliquer »
après chaque étape, puis une consigne de contrôle :

  zero_capteurs -> verif_cablage -> calibration_pdc -> vanne_3voies ->
  vanne_bypass -> vanne_pdc -> freq_min -> courbe_gain -> indiciel ->
  consigne Re 6000 (convergence attendue avec les réglages appris)

(le zéro des capteurs se mesure ventilateur arrêté, sans dépendre du
câblage : le passer en premier corrige les offsets pour l'identification
ISO/Miller du contrôle de câblage)

Les fichiers écrits (config appliquée, état) vont dans un dossier
temporaire : la configuration réelle n'est pas modifiée.
Durée : ~10 minutes (courses de vannes et paliers en temps réel).

    python repetition_generale.py
"""

from __future__ import annotations
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# console Windows cp1252 : ne pas planter sur les caractères des statuts (≈…)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.apprentissage import Apprentissage
from core.banc import Banc
from core.pilote import Pilote
from io_banc.simulateur import SimulateurBanc


def _exiger(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    dossier = Path(tempfile.mkdtemp(prefix="banc_repetition_"))
    cfg = json.loads((Path(__file__).resolve().parent / "config.json")
                     .read_text(encoding="utf-8"))
    cfg["fichier_etat"] = str(dossier / "etat_banc.json")

    banc = Banc(cfg)
    es = SimulateurBanc(banc, cfg.get("simulation"))
    pilote = Pilote(banc, es, cfg)
    pilote.demarrer()
    app = Apprentissage(pilote, cfg, dossier / "config.json")
    print(f"Répétition générale (simulation) — fichiers dans {dossier}")

    def etape(test: str, verifier=None, timeout: float = 300.0,
              **params) -> None:
        t0 = time.monotonic()
        err = app.demarrer(test, **params)
        if err:
            raise RuntimeError(f"{test} refusé : {err}")
        while app.etat["en_cours"]:
            if time.monotonic() - t0 > timeout:
                app.annuler()
                raise TimeoutError(f"{test} : > {timeout:.0f} s "
                                   f"({app.etat['statut']})")
            time.sleep(0.5)
        statut = app.etat["statut"]
        if not statut.startswith("terminé"):
            raise RuntimeError(f"{test} : {statut}")
        if verifier is not None:
            verifier(app.resultats[test])
        print(f"  OK  {test:16s} {time.monotonic() - t0:5.0f} s  {statut}")
        print(f"      -> {app.appliquer()}")

    etape("zero_capteurs",
          lambda r: _exiger(all(abs(o) < 2.0 for o in r.values()),
                            f"zéros non nuls : {r}"))
    # le verdict agrégé (resume) couvre mapping + défauts : source unique
    etape("verif_cablage",
          lambda r: _exiger(r["resume"] == "câblage conforme", r["resume"]))
    etape("calibration_pdc",
          lambda r: _exiger(r["plage_v"] > 8.0, f"plage recopie : {r}"))
    etape("vanne_3voies",
          lambda r: _exiger(0.5 < r["max_s"] < 30.0, f"chronos 3 voies : {r}"))
    etape("vanne_bypass",
          lambda r: _exiger(0.5 < r["max_s"] < 40.0, f"chronos bypass : {r}"))
    etape("vanne_pdc",
          lambda r: _exiger(5.0 < r["max_s"] < 60.0, f"chronos PdC : {r}"))
    etape("freq_min",
          lambda r: _exiger(0.5 <= r["u_min_exploitable_v"] <= 2.5,
                            f"u_min hors plage : {r}"))
    def _courbe_gain(r: dict) -> None:
        _exiger(len(r["points_V_Re"]) >= 4, f"table incomplète : {r}")
        # le simulateur est linéaire : une forte non-linéarité signale des
        # points corrompus (ex. saturation du capteur ΔP non exclue)
        _exiger(r["non_linearite_pct"] < 15.0, f"non-linéarité anormale : {r}")

    etape("courbe_gain", _courbe_gain, timeout=900.0)
    etape("indiciel",
          lambda r: _exiger(0.3 < r["tau_s"] < 10.0,
                            f"dépouillement indiciel : {r}"))

    # consigne de contrôle : la boucle converge avec les réglages appris
    pilote.definir_consigne(6000.0, "conduite")
    pilote.demarrer_regulation()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 45.0 and not pilote.etat.get("convergee"):
        time.sleep(0.5)
    convergee = bool(pilote.etat.get("convergee"))
    re_final = pilote.etat.get("re_mesure", 0.0)
    t_conv = time.monotonic() - t0
    pilote.arreter_regulation()
    pilote.arreter()
    print(f"  {'OK' if convergee else 'ÉCHEC'}  consigne 6000     "
          f"{t_conv:5.0f} s  Re = {re_final:.0f}")
    _exiger(convergee, "la consigne de contrôle n'a pas convergé en 45 s")
    print("RÉPÉTITION GÉNÉRALE : tout est passé.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
