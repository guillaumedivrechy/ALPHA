# -*- coding: utf-8 -*-
"""Banc simulé à horloge accélérée, partagé par les tests de scénarios.

Pilote réel + simulateur (mêmes équations que le banc), sans thread ni Qt :
chaque cycle avance l'horloge d'une période. Deux états de caractérisation :
  - « caracterise » : config + tables apprises par la répétition générale
    sur ce simulateur (tests/donnees/banc_caracterise), comme un banc mis en
    service ;
  - « vierge » : config du dépôt seule, aucune table apprise.

À chaque cycle, `invariants()` vérifie les règles d'usage valables en toute
circonstance nominale ; un scénario les relit à la fin (`violations`)."""

from __future__ import annotations

import json
import random
import shutil
import time
from pathlib import Path

from core.banc import Banc
from core.pilote import Pilote
from io_banc.simulateur import SimulateurBanc

RACINE_TESTS = Path(__file__).resolve().parent
CFG_DEPOT = json.loads((RACINE_TESTS.parent / "config.json")
                       .read_text(encoding="utf-8"))
DONNEES = RACINE_TESTS / "donnees" / "banc_caracterise"
FC_3VOIES = ("fc_3voies_0", "fc_3voies_90", "fc_3voies_180")


def _fusion(base: dict, sur: dict) -> None:
    for k, v in sur.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict):
            _fusion(base.setdefault(k, {}), v)
        else:
            base[k] = v


class SimTrace(SimulateurBanc):
    """Simulateur qui garde la dernière tension écrite au variateur."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.u_ecrite = 0.0

    def ecrire_variateur(self, v):
        self.u_ecrite = v
        super().ecrire_variateur(v)


class BancSimule:
    def __init__(self, monkeypatch, racine: Path, caracterise: bool = True,
                 graine: int = 3, cfg_sur: dict | None = None):
        random.seed(graine)
        self.t = [1000.0]
        monkeypatch.setattr(time, "monotonic", lambda: self.t[0])
        import core.pilote
        monkeypatch.setattr(core.pilote, "RACINE", racine)
        cfg = json.loads(json.dumps(CFG_DEPOT))
        if caracterise:
            shutil.copytree(DONNEES / "apprentissages", racine / "apprentissages",
                            dirs_exist_ok=True)
            shutil.copy(DONNEES / "etat_banc.json", racine / "etat_banc.json")
            _fusion(cfg, json.loads((DONNEES / "config_appris.json")
                                    .read_text(encoding="utf-8")))
        if cfg_sur:
            _fusion(cfg, cfg_sur)
        cfg["journal"]["actif"] = False
        self.cfg = cfg
        banc = Banc(cfg)
        self.sim = SimTrace(banc, cfg["simulation"], cfg["modbus"],
                            cfg["actionneurs"])
        self.p = Pilote(banc, self.sim, cfg)
        self.violations: list[str] = []
        self.p._position_repos()
        self.cycles(20.0)

    # ------------------------------------------------------------------ boucle
    def cycles(self, duree_s: float, verifier: bool = True) -> None:
        for _ in range(max(1, int(round(duree_s / self.p.periode)))):
            self.t[0] += self.p.periode
            self.p._cycle()
            if verifier:
                self.invariants()

    def consigne(self, re: float, reference: str) -> None:
        self.p.definir_consigne(re, reference)
        if not self.p.regulation_active:
            self.p.demarrer_regulation()

    def jusqu_a_convergence(self, max_s: float = 150.0) -> float | None:
        """Durée jusqu'à la première convergence (None si jamais)."""
        t0 = self.t[0]
        while self.t[0] - t0 < max_s:
            self.cycles(self.p.periode)
            if self.p.etat.get("convergee"):
                return self.t[0] - t0
        return None

    # -------------------------------------------------------------- contrôles
    def invariants(self) -> None:
        e = self.p.etat
        m = e.get("mesures_brutes") or {}
        t = round(self.t[0] - 1000.0, 1)
        lu = e.get("diaphragme_lu")
        # 1. capteur ΔP : celui de la butée LUE
        if lu in ("iso", "miller") and e.get("diaphragme_mesure") != lu:
            self.violations.append(
                f"t={t} capteur {e.get('diaphragme_mesure')} pour butée {lu}")
        # 2. aucune tension pendant la course d'une vanne TOR (hors manuel)
        bypass_course = not m.get("fc_bypass_ouvert") and not m.get("fc_bypass_ferme")
        if (lu == "en_transit" or bypass_course) and not self.p.mode_manuel \
                and self.sim.u_ecrite > 0.0:
            self.violations.append(
                f"t={t} variateur {self.sim.u_ecrite:.2f} V vanne TOR en course")
        # 3. aucun arrêt ni défaut en marche nominale
        for cle in ("arret_securite", "defaut_vanne", "defaut_capteur",
                    "defaut_variateur", "defaut_pdc", "incoherence_vanne"):
            if e.get(cle):
                self.violations.append(f"t={t} {cle} : {e.get(cle)}")
        # dédoublonnage : une violation durable ne noie pas les autres
        if len(self.violations) > 50:
            self.violations = self.violations[:50]

    def nb_manoeuvres_tor(self, depuis: list) -> int:
        """Nombre de départs de manœuvre TOR (3 voies + bypass) depuis la
        liste d'instantanés `depuis` (un par cycle, cf. suivre())."""
        n, prec = 0, None
        for motif in depuis:
            if motif and motif != prec:
                n += 1
            prec = motif
        return n

    def suivre(self, duree_s: float) -> list:
        """Cycle pendant duree_s et renvoie le motif de manœuvre par cycle."""
        motifs = []
        for _ in range(int(round(duree_s / self.p.periode))):
            self.cycles(self.p.periode)
            motifs.append(self.p.etat.get("manoeuvre"))
        return motifs
