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
    """Actionneur 3 positions équipé de la carte GF3 : l'ordre amène la vanne
    directement à la butée demandée, depuis n'importe quelle position — la
    carte gère elle-même le sens grâce à ses deux fins de course
    intermédiaires (FCIO / FCIF). Seules 0° et 90° sont commandées ; 180°
    n'est atteignable qu'à la main, via le débrayage.

    t_90deg_s : temps de course pour 90° (ER20 standard : 11 s).

    Les trois propriétés fc_* reproduisent ce que le coupleur publie APRÈS
    l'inversion déclarée en config pour FC3 : exactement une vraie à l'arrêt
    sur une butée, aucune pendant la course.
    """

    def __init__(self, t_90deg_s: float, angle_initial: float = 0.0):
        self._vitesse = 90.0 / max(t_90deg_s, 0.1)   # °/s
        self._angle = float(angle_initial)
        self._cible = float(angle_initial)
        self._t_prec = time.monotonic()

    def commander(self, vers_0deg: bool) -> None:
        self._cible = 0.0 if vers_0deg else 90.0

    def actualiser(self) -> None:
        t = time.monotonic()
        dt = t - self._t_prec
        self._t_prec = t
        ecart = self._cible - self._angle
        if abs(ecart) < 1e-9:
            return
        pas = min(abs(ecart), self._vitesse * dt)
        self._angle += pas if ecart > 0 else -pas

    @property
    def en_course(self) -> bool:
        return abs(self._cible - self._angle) > 1e-9

    @property
    def fc_0(self) -> bool:
        return self._angle < 1.0

    @property
    def fc_90(self) -> bool:
        return abs(self._angle - 90.0) < 1.0

    @property
    def fc_180(self) -> bool:
        return self._angle > 179.0


class SimulateurBanc(EntreesSorties):
    def __init__(self, banc: Banc, cfg_simu: dict | None = None,
                 cfg_modbus: dict | None = None, cfg_actionneurs: dict | None = None):
        cfg_simu = cfg_simu or {}
        # facultatif : permet de reconstituer l'image process (registres
        # bruts) que le coupleur fournirait, pour que la page de mise en
        # service soit utilisable en simulation. Sans lui, image_process
        # reste None plutôt que d'inventer des adresses.
        self.cfg_modbus = cfg_modbus
        self.banc = banc
        self.gain_qv = cfg_simu.get("gain_qv_m3s_par_volt", 0.0035)
        self.tau_s = cfg_simu.get("constante_temps_s", 2.0)
        self.bruit_rel = cfg_simu.get("bruit_relatif", 0.01)
        # sous cette tension, le ventilateur décroche : débit effondré et
        # erratique (permet de valider l'apprentissage 'freq_min')
        self.u_decrochage = cfg_simu.get("u_decrochage_v", 1.2)
        self.temperature = cfg_simu.get("temperature_c", 20.0)
        self.p_amont_eff = cfg_simu.get("p_amont_eff_pa", 25.0)

        self._u_variateur = 0.0
        self._run = False
        self.defaut_variateur = False        # forçable pour les tests
        self._qv = 0.0

        # vanne 3 voies : 3 positions (0°=ISO, 90°=Miller, 180°=fermeture)
        # t_course_3voies_s = demi-course (0°→90° ou 90°→180°)
        # Position repos sécurité (cohérent avec pilote._position_repos) : Miller (90°)
        # IMPORTANT : Utiliser les temps de course de la config ACTIONNEURS (cohérent avec pilote)
        act_cfg = cfg_actionneurs or {}
        self._v3 = _Vanne3Voies(
            t_90deg_s=act_cfg.get("vanne_3voies_temps_s", 11.0),
            angle_initial=90.0,   # départ sur la butée Miller (position repos)
        )
        # bypass : True = ouvert
        # Position repos sécurité (cohérent avec pilote._position_repos) : ouvert
        self._bypass = _VanneTOR(act_cfg.get("vanne_bypass_temps_s", 11.0),
                                 position_initiale=True)
        # vanne perte de charge : actionneur lent + recopie
        self._pdc_t_course = act_cfg.get("vanne_perte_charge_course_s", 25.0)
        self._pdc_consigne = 10.0  # Démarre grande ouverte (position repos)
        self._pdc_position = 10.0  # Démarre grande ouverte (cohérent avec banc réel)
        self._pdc_t_prec = time.monotonic()
        # fractions de débit atteignant la veine (modèle de la PdC en série et
        # du by-pass). PdC (carte P6, sens usine) : 0 V = fermée (étranglée),
        # 10 V = ouverte. Défauts choisis pour que le régime bas-Re soit pilotable.
        self.bypass_frac  = cfg_simu.get("bypass_frac_veine", 0.35)  # by-pass ouvert, PdC ouverte
        self.pdc_frac_min = cfg_simu.get("pdc_frac_min", 0.08)       # by-pass ouvert, PdC fermée
        self.pdc_serie_min = cfg_simu.get("pdc_serie_min", 0.05)     # by-pass fermé, PdC fermée

    # ----- côté "procédé" : réception des commandes -------------------------

    def ecrire_variateur(self, volts: float) -> None:
        self._u_variateur = min(max(volts, 0.0), 10.0)

    def ecrire_variateur_run(self, marche: bool) -> None:
        self._run = marche

    def ecrire_vanne_diaphragme(self, vers_0deg: bool) -> None:
        self._v3.commander(vers_0deg)   # True = butée 0°, False = butée 90°

    def ecrire_vanne_bypass(self, ouverte: bool) -> None:
        self._bypass.commander(ouverte)

    def ecrire_vanne_perte_charge(self, volts: float) -> None:
        self._pdc_consigne = min(max(volts, 0.0), 10.0)

    # ----- modèles internes ---------------------------------------------------

    def _avancer(self) -> None:
        # actionneur de la vanne perte de charge (course lente) — mis à jour
        # d'abord, sa position sert au calcul du débit qui atteint la veine
        t = time.monotonic()
        dt = t - self._pdc_t_prec
        self._pdc_t_prec = t
        vitesse = 10.0 / self._pdc_t_course
        ecart = self._pdc_consigne - self._pdc_position
        pas = min(abs(ecart), vitesse * dt)
        self._pdc_position += pas if ecart > 0 else -pas

        self._v3.actualiser()
        self._bypass.actualiser()

        # débit refoulé par le ventilateur (sans RUN, le 3G3MV ignore la réf.)
        qv_fan = self.gain_qv * self._u_variateur if self._run else 0.0
        # décrochage sous la tension seuil : le ventilateur ne tient plus son
        # débit, effondré et erratique (détecté par l'apprentissage freq_min)
        if self._run and 0.0 < self._u_variateur < self.u_decrochage:
            qv_fan *= 0.25 * max(0.0, 1.0 + random.gauss(0.0, 0.6))
        # fraction atteignant la veine : la PdC (en série) l'étrangle en se
        # fermant, le by-pass (en parallèle) en dérive une part quand il est
        # ouvert. PdC ouverte (10 V) -> ouv=1 -> pleine passe ; PdC fermée
        # (0 V) -> ouv=0 -> débit mini.
        ouv = min(max(self._pdc_position / 10.0, 0.0), 1.0)
        if self._bypass.position and not self._bypass.en_course:
            frac = self.pdc_frac_min + (self.bypass_frac - self.pdc_frac_min) * ouv
        else:
            frac = self.pdc_serie_min + (1.0 - self.pdc_serie_min) * ouv
        qv_cible = qv_fan * frac

        # 1er ordre au pas de temps REEL entre deux appels : la constante de
        # temps configuree est respectee quelle que soit la periode de la boucle
        a = dt / max(self.tau_s, dt) if dt > 0 else 0.0
        self._qv += a * (qv_cible - self._qv)

    # ----- côté "capteurs" ----------------------------------------------------

    def lire_mesures(self) -> dict:
        self._avancer()
        T = self.temperature
        p1_abs = self.banc.patm_pa + self.p_amont_eff
        rho = self.banc.fluide.rho(p1_abs, T)
        qm = self._qv * rho

        # diaphragme actif selon la position de la vanne 3 voies. Le
        # simulateur applique la correspondance par défaut de config.json
        # (0° = ISO, 90° = Miller) ; une plomberie croisée ne se teste qu'au banc.
        if self._v3.fc_0:
            diaph = "iso"
        elif self._v3.fc_90:
            diaph = "miller"
        else:
            diaph = "iso"      # en transit : flux indéterminé
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
        mesures = {
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
            "fc_pdc_ouvert": self._pdc_position > 9.7,   # 10 V = grande ouverte
            "fc_pdc_ferme": self._pdc_position < 0.3,    # 0 V = fermée
            "defaut_variateur": self.defaut_variateur,
            "defaut_act_bypass": False,
            "defaut_act_pdc": False,
            "defaut_act_3voies": False,
            "frequence_variateur": (self._u_variateur if self._run else 0.0)
                                   + random.gauss(0.0, 0.02),
        }
        self.image_process = self._image_process(mesures)
        return mesures

    # ----- image process reconstituee ---------------------------------------

    PLEINE_ECHELLE = 32767

    def _image_process(self, mesures: dict) -> dict | None:
        """Registres bruts que le coupleur aurait presentes, reconstitues en
        inversant la mise a l'echelle de config.json.

        Le simulateur reproduit deja la physique du banc ; il reproduit ici
        la couche Modbus, pour que la page de mise en service soit exercable
        sans materiel. Sans configuration modbus fournie, on ne devine pas
        d'adresses : l'image reste absente.
        """
        if not self.cfg_modbus:
            return None
        mots, bits = {}, {}
        for nom, voie in self.cfg_modbus.get("entrees", {}).items():
            if nom.startswith("_") or nom not in mesures:
                continue
            valeur = mesures[nom]
            if nom == "temperature":                 # module RTD : 0,1 degC
                brut = int(round(valeur / voie.get("facteur", 0.1)))
            else:
                if "valeur_min" in voie:             # capteur mis a l'echelle
                    v0, v1 = voie["volts_min"], voie["volts_max"]
                    u0, u1 = voie["valeur_min"], voie["valeur_max"]
                    volts = v0 + (valeur - u0) / (u1 - u0) * (v1 - v0)
                else:                                # recopie brute en volts
                    volts = valeur
                pe = voie.get("pleine_echelle_v", 10.0)
                brut = int(round(volts / pe * self.PLEINE_ECHELLE))
            mots[voie["adresse"]] = max(-32768, min(32767, brut))
        for nom, voie in self.cfg_modbus.get("entrees_tor", {}).items():
            if nom.startswith("_") or nom not in mesures:
                continue
            # le bit physique, avant l'inversion des contacts NF
            bits[voie["adresse"]] = bool(mesures[nom]) != voie.get("inverse",
                                                                   False)
        return {"mots": mots, "bits": bits}
