# -*- coding: utf-8 -*-
"""Simulateur du banc — développement et tests sans matériel.

Modèle physique simplifié :
  - ventilateur : débit volumique proportionnel à la tension, 1er ordre
  - ΔP au diaphragme actif : modèle inverse (mêmes équations ISO/Miller)
  - vanne 3 voies : course de 11 s, fins de course 0° / 90° / 180°
    (90° activée à mi-course), ΔP erratique pendant la manœuvre
  - vanne bypass : course de 11 s, fins de course ouvert / fermé
  - vanne perte de charge : actionneur 25 s pleine course, recopie 0-10 V
"""

from __future__ import annotations
import random
import time

from core.banc import Banc
from io_banc.wago import EntreesSorties


class _VanneTOR:
    """Vanne TOR à course lente avec fins de course."""

    def __init__(self, t_course: float, position_initiale: bool = False):
        self.t_course = t_course
        self.cible = position_initiale
        self.position = position_initiale      # position de DÉPART de la course
        self._t_fin = 0.0

    def commander(self, cible: bool) -> None:
        if cible != self.cible:
            self.cible = cible
            self._t_fin = time.monotonic() + self.t_course

    @property
    def en_course(self) -> bool:
        return time.monotonic() < self._t_fin

    @property
    def fraction_course(self) -> float:
        """0 = départ, 1 = arrivée."""
        if not self.en_course:
            return 1.0
        return 1.0 - (self._t_fin - time.monotonic()) / self.t_course

    def actualiser(self) -> None:
        if not self.en_course:
            self.position = self.cible


class _Vanne3Voies:
    """Actionneur 3 positions (0°=ISO, 90°=Miller, 180°=fermeture) avec arrêt
    intermédiaire automatique à 90° dans les deux sens de commande.
    Passer de 0°→180° ou 180°→0° nécessite 2 impulsions relais successives.
    t_demi_course_s : temps de 0°→90° (ou 90°→180°).
    """

    def __init__(self, t_demi_course_s: float, angle_initial: float = 0.0):
        self._vitesse = 90.0 / max(t_demi_course_s, 0.1)   # °/s
        self._angle = float(angle_initial)
        self._direction = 0.0   # -1 = vers 0° (ISO), +1 = vers 180° (fermeture)
        self._t_prec = time.monotonic()

    def commander(self, vers_iso: bool) -> None:
        self._direction = -1.0 if vers_iso else +1.0

    def actualiser(self) -> None:
        t = time.monotonic()
        dt = t - self._t_prec
        self._t_prec = t
        if self._direction == 0.0:
            return
        old = self._angle
        self._angle += self._direction * self._vitesse * dt
        # Butées extrêmes
        if self._angle <= 0.0:
            self._angle = 0.0
            self._direction = 0.0
            return
        if self._angle >= 180.0:
            self._angle = 180.0
            self._direction = 0.0
            return
        # Arrêt intermédiaire automatique à 90° (dans les deux sens)
        if self._direction < 0 and old > 90.0 and self._angle <= 90.0:
            self._angle = 90.0
            self._direction = 0.0
        elif self._direction > 0 and old < 90.0 and self._angle >= 90.0:
            self._angle = 90.0
            self._direction = 0.0

    @property
    def en_course(self) -> bool:
        return self._direction != 0.0

    @property
    def fc_0(self) -> bool:
        return self._angle < 1.0 and not self.en_course

    @property
    def fc_90(self) -> bool:
        return abs(self._angle - 90.0) < 1.0 and not self.en_course

    @property
    def fc_180(self) -> bool:
        return self._angle > 179.0 and not self.en_course


class SimulateurBanc(EntreesSorties):
    def __init__(self, banc: Banc, cfg_simu: dict | None = None):
        cfg_simu = cfg_simu or {}
        self.banc = banc
        self.gain_qv = cfg_simu.get("gain_qv_m3s_par_volt", 0.0035)
        self.tau_s = cfg_simu.get("constante_temps_s", 2.0)
        self.periode_s = cfg_simu.get("periode_s", 0.2)
        self.bruit_rel = cfg_simu.get("bruit_relatif", 0.01)
        self.temperature = cfg_simu.get("temperature_c", 20.0)
        self.p_amont_eff = cfg_simu.get("p_amont_eff_pa", 25.0)

        self._u_variateur = 0.0
        self._run = False
        self.defaut_variateur = False        # forçable pour les tests
        self._qv = 0.0

        # vanne 3 voies : 3 positions (0°=ISO, 90°=Miller, 180°=fermeture)
        # t_course_3voies_s = demi-course (0°→90° ou 90°→180°)
        self._v3 = _Vanne3Voies(
            t_demi_course_s=cfg_simu.get("t_course_3voies_s", 5.5),
            angle_initial=0.0,   # départ ISO
        )
        # bypass : True = ouvert
        self._bypass = _VanneTOR(cfg_simu.get("t_course_bypass_s", 11.0),
                                 position_initiale=False)
        # vanne perte de charge : actionneur lent + recopie
        self._pdc_t_course = cfg_simu.get("t_course_pdc_s", 25.0)
        self._pdc_consigne = 0.0
        self._pdc_position = 0.0
        self._pdc_t_prec = time.monotonic()

    # ----- côté "procédé" : réception des commandes -------------------------

    def ecrire_variateur(self, volts: float) -> None:
        self._u_variateur = min(max(volts, 0.0), 10.0)

    def ecrire_variateur_run(self, marche: bool) -> None:
        self._run = marche

    def ecrire_vanne_diaphragme(self, vers_iso: bool) -> None:
        self._v3.commander(vers_iso)   # True=vers 0°(ISO), False=vers 180°(fermeture)

    def ecrire_vanne_bypass(self, ouverte: bool) -> None:
        self._bypass.commander(ouverte)

    def ecrire_vanne_perte_charge(self, volts: float) -> None:
        self._pdc_consigne = min(max(volts, 0.0), 10.0)

    # ----- modèles internes ---------------------------------------------------

    def _avancer(self) -> None:
        # sans ordre RUN, le 3G3MV ignore la référence 0-10 V
        qv_cible = self.gain_qv * self._u_variateur if self._run else 0.0
        # bypass ouvert : une partie du débit recircule -> moins dans la boucle
        if self._bypass.position and not self._bypass.en_course:
            qv_cible *= 0.35
        a = self.periode_s / max(self.tau_s, self.periode_s)
        self._qv += a * (qv_cible - self._qv)

        # actionneur de la vanne perte de charge (0,4 V/s)
        t = time.monotonic()
        dt = t - self._pdc_t_prec
        self._pdc_t_prec = t
        vitesse = 10.0 / self._pdc_t_course
        ecart = self._pdc_consigne - self._pdc_position
        pas = min(abs(ecart), vitesse * dt)
        self._pdc_position += pas if ecart > 0 else -pas

        self._v3.actualiser()
        self._bypass.actualiser()

    # ----- côté "capteurs" ----------------------------------------------------

    def lire_mesures(self) -> dict:
        self._avancer()
        T = self.temperature
        p1_abs = self.banc.patm_pa + self.p_amont_eff
        rho = self.banc.fluide.rho(p1_abs, T)
        qm = self._qv * rho

        # diaphragme actif selon la position de la vanne 3 voies
        if self._v3.fc_0:
            diaph = "iso"
        elif self._v3.fc_90:
            diaph = "miller"
        else:
            diaph = "iso"      # en transit ou à 180° : flux indéterminé
        dp_actif = self.banc.dP_attendu(diaph, qm, T, self.p_amont_eff)
        if self._v3.en_course or self._v3.fc_180:
            dp_actif *= max(0.0, 1.0 + random.gauss(0.0, 0.5))  # flux indéterminé
        dp_actif *= 1.0 + random.gauss(0.0, self.bruit_rel)
        dp_actif = max(dp_actif, 0.0)

        # Deux capteurs physiques indépendants, un par diaphragme.
        # Quand un diaphragme n'est pas traversé, son capteur lit ~0.
        dp_miller = min(dp_actif if diaph == "miller" else 0.0, 500.0)
        dp_iso    = min(dp_actif if diaph == "iso"    else 0.0, 500.0)

        bp_en_course = self._bypass.en_course
        return {
            "dp_diaph_500": dp_miller,   # capteur Miller (adresse Wago 0)
            "p_amont_500": self.p_amont_eff * (1 + random.gauss(0, 0.005)),
            "dp_iso_500": dp_iso,        # capteur ISO (adresse Wago 4)
            "temperature": T + random.gauss(0.0, 0.05),
            "pdc_recopie": self._pdc_position + random.gauss(0.0, 0.02),
            "fc_3voies_0": self._v3.fc_0,
            "fc_3voies_90": self._v3.fc_90,
            "fc_3voies_180": self._v3.fc_180,
            "fc_bypass_ouvert": (not bp_en_course) and self._bypass.position,
            "fc_bypass_ferme": (not bp_en_course) and not self._bypass.position,
            "fc_pdc_ouvert": self._pdc_position < 0.3,   # 0 V = ouvert
            "fc_pdc_ferme": self._pdc_position > 9.7,    # 10 V = fermé
            "defaut_variateur": self.defaut_variateur,
            "frequence_atteinte": self._run and abs(
                self._qv - self.gain_qv * self._u_variateur)
                < 0.03 * max(self.gain_qv * self._u_variateur, 1e-9),
            "frequence_variateur": (self._u_variateur if self._run else 0.0)
                                   + random.gauss(0.0, 0.02),
        }
