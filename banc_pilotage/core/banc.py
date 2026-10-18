# -*- coding: utf-8 -*-
"""Logique métier du banc :
  - conversion consigne Re (veine ou conduite) -> débit massique visé
  - sélection automatique du diaphragme (Miller si Re_diaphragme < seuil, sinon ISO)
  - calcul du Re calculé à partir des capteurs (ΔP, P_amont, PT100 ;
    voies ΔP Miller et P_amont en maître/esclave 50/500 Pa côté matériel,
    sortie unique 0-10 V = 0-500 Pa)
"""

from __future__ import annotations
import math
from dataclasses import dataclass

from .fluide import ProprietesFluide
from .diaphragmes import (GeometrieDiaphragme,
                          iso_qm_depuis_dP, iso_dP_depuis_qm,
                          miller_qm_depuis_dP, miller_dP_depuis_qm)


@dataclass
class GeometrieVeine:
    """Géométrie de la veine d'essai pour le calcul du Reynolds veine.

    Pour une veine rectangulaire, les dimensions fondamentales sont hauteur_m
    et largeur_m, dont on déduit :
      - section_m2 = hauteur_m × largeur_m
      - périmètre = 2 × (hauteur_m + largeur_m)
      - longueur_caracteristique_m = diamètre hydraulique = 4 × section / périmètre
    """
    hauteur_m: float                     # hauteur de la veine (H) [m]
    largeur_m: float                     # largeur de la veine (W) [m]
    section_m2: float | None = None      # calculé si None
    longueur_caracteristique_m: float | None = None  # Dh, calculé si None

    def __post_init__(self):
        """Calcule section et Dh à partir de H et W si non fournis.

        Si section_m2 et longueur_caracteristique_m sont fournis explicitement
        dans la config, ils sont utilisés tels quels (pour préserver la
        compatibilité binaire exacte avec les tests existants).
        """
        if self.section_m2 is None:
            self.section_m2 = self.hauteur_m * self.largeur_m
        if self.longueur_caracteristique_m is None:
            perimetre = 2 * (self.hauteur_m + self.largeur_m)
            self.longueur_caracteristique_m = 4 * self.section_m2 / perimetre


class Banc:
    def __init__(self, cfg: dict):
        c = cfg["diaphragme_iso"]
        self.geo_iso = GeometrieDiaphragme(
            D20=c["D20_mm"] / 1000.0, d20=c["d20_mm"] / 1000.0,
            alpha_conduite=c["alpha_conduite"],
            alpha_diaphragme=c["alpha_diaphragme"])
        self.formule_C_iso = c.get("formule_C", "RHG")

        c = cfg["diaphragme_miller"]
        self.geo_miller = GeometrieDiaphragme(
            D20=c["D20_mm"] / 1000.0, d20=c["d20_mm"] / 1000.0,
            alpha_conduite=c["alpha_conduite"],
            alpha_diaphragme=c["alpha_diaphragme"])

        v = cfg["veine"]
        self.veine = GeometrieVeine(
            hauteur_m=v["hauteur_m"],
            largeur_m=v["largeur_m"],
            section_m2=v.get("section_m2"),  # optionnel, calculé si absent
            longueur_caracteristique_m=v.get("longueur_caracteristique_m"))

        f = cfg.get("fluide", {})
        self.fluide = ProprietesFluide(
            rho_force=f.get("rho_force"),
            mu_force=f.get("mu_force"),
            gamma=f.get("gamma", 1.40273),
            sutherland_mu_ref=f.get("sutherland_mu_ref", 1.716e-5),
            sutherland_t_ref=f.get("sutherland_t_ref", 273.15),
            sutherland_s=f.get("sutherland_s", 110.4),
            humidite_relative=f.get("humidite_relative", 0.0))

        s = cfg["selection_diaphragme"]
        self.seuil_re = s["seuil_re"]                  # 5000
        self.hysteresis_re = s.get("hysteresis_re", 500)

        self.patm_pa = cfg.get("patm_pa", 101325.0)
        self.D_conduite = self.geo_iso.D20             # conduite commune Ø81,4

    # ---------------------------------------------------------------- consigne

    def re_conduite_vers_qm(self, re_conduite: float, T: float,
                            P1_abs: float) -> float:
        """Re conduite visé -> débit massique [kg/s] : Re = 4·qm/(π·D·μ)."""
        mu = self.fluide.mu(T)
        return re_conduite * math.pi * self.D_conduite * mu / 4.0

    def re_veine_vers_qm(self, re_veine: float, T: float,
                         P1_abs: float) -> float:
        """Re veine visé -> débit massique [kg/s].

        Re_veine = ρ·v_veine·Lc/μ  avec  v_veine = qm/(ρ·A_veine)
        d'où     qm = Re_veine·μ·A_veine/Lc
        """
        mu = self.fluide.mu(T)
        return re_veine * mu * self.veine.section_m2 \
            / self.veine.longueur_caracteristique_m

    def qm_vers_re(self, qm: float, T: float, P1_abs: float) -> dict:
        """Débit massique -> Re conduite et Re veine (conservation du débit)."""
        mu = self.fluide.mu(T)
        rho = self.fluide.rho(P1_abs, T)
        re_cond = 4.0 * qm / (math.pi * self.D_conduite * mu)
        v_veine = qm / (rho * self.veine.section_m2)
        re_veine = rho * v_veine * self.veine.longueur_caracteristique_m / mu
        return {"re_conduite": re_cond, "re_veine": re_veine,
                "v_veine": v_veine, "qv": qm / rho}

    # ------------------------------------------------------ choix diaphragme

    def choisir_diaphragme(self, re_conduite_attendu: float,
                           diaphragme_actuel: str | None = None) -> str:
        """Sélection du diaphragme d'après le Re conduite ATTENDU (consigne),
        avec hystérésis pour éviter les commutations répétées près du seuil.

        Retourne "miller" (Re < seuil, bas Re) ou "iso" (Re > seuil, haut Re).
        """
        if diaphragme_actuel == "miller":
            return "iso" if re_conduite_attendu > self.seuil_re + self.hysteresis_re \
                else "miller"
        if diaphragme_actuel == "iso":
            return "miller" if re_conduite_attendu < self.seuil_re - self.hysteresis_re \
                else "iso"
        return "miller" if re_conduite_attendu < self.seuil_re else "iso"

    # ---------------------------------------------------------------- mesure

    def qm_depuis_mesures(self, diaphragme: str, dP: float, T: float,
                          P1_eff: float) -> dict:
        """Calcul du débit massique à partir des capteurs (sens direct)."""
        P1_abs = self.patm_pa + P1_eff

        # Validation pression absolue (critique pour calculs air_humide et diaphragmes)
        if not (80000 < P1_abs < 120000):
            raise ValueError(
                f"Pression absolue hors plage physique : {P1_abs:.0f} Pa "
                f"(attendu 800-1200 mbar). Vérifier P_atm={self.patm_pa:.0f} Pa "
                f"et P1_eff={P1_eff:.1f} Pa."
            )

        rho = self.fluide.rho(P1_abs, T)
        mu = self.fluide.mu(T)
        nu = self.fluide.nu(P1_abs, T)
        if diaphragme == "iso":
            r = iso_qm_depuis_dP(self.geo_iso, dP, T, P1_abs, rho, nu,
                                 self.fluide.gamma, self.formule_C_iso)
        else:
            r = miller_qm_depuis_dP(self.geo_miller, dP, T, P1_abs, rho, mu,
                                    self.fluide.gamma)
        r.update(self.qm_vers_re(r["qm"], T, P1_abs))
        r["rho"] = rho
        r["mu"] = mu
        r["nu"] = nu
        return r

    def dP_attendu(self, diaphragme: str, qm: float, T: float,
                   P1_eff: float) -> float:
        """ΔP attendu au diaphragme pour un débit visé (sens inverse) —
        sert au pré-positionnement et au choix de gamme capteur."""
        P1_abs = self.patm_pa + P1_eff
        rho = self.fluide.rho(P1_abs, T)
        if diaphragme == "iso":
            nu = self.fluide.nu(P1_abs, T)
            return iso_dP_depuis_qm(self.geo_iso, qm, T, P1_abs, rho, nu,
                                    self.fluide.gamma, self.formule_C_iso)["dP"]
        mu = self.fluide.mu(T)
        return miller_dP_depuis_qm(self.geo_miller, qm, T, P1_abs, rho, mu,
                                   self.fluide.gamma)["dP"]


