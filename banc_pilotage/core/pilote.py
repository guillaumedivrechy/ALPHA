# -*- coding: utf-8 -*-
"""Pilote du banc : la boucle de régulation qui relie tout.

Séquence quand l'opérateur valide une consigne Re :
  1. estimation du débit massique visé (Re cible -> qm)
  2. choix du diaphragme (Miller / ISO) et commande de la vanne 3 voies
  3. calcul du ΔP attendu -> choix de la gamme capteur, pré-positionnement
     du variateur (feed-forward)
  4. boucle PI : lecture capteurs -> qm réel (ISO 5167 ou Miller) ->
     Re mesuré -> correction tension variateur, jusqu'à convergence
  5. surveillance continue (la T° dérive -> rho, mu recalculés à chaque cycle)
"""

from __future__ import annotations
import json
import threading
import time
from pathlib import Path
from bisect import bisect_left


class TableGains:
    """Relation tension variateur -> Re conduite, par points mesurés et
    interpolation linéaire. Sert au pré-positionnement (feed-forward) :
    pour un Re visé, on retrouve la tension par interpolation inverse.

    Points = liste de (volts, re) triés par tension croissante. Hors plage,
    on extrapole sur le segment extrême (pente locale), borné à 0-10 V.
    """

    def __init__(self, points: list | None = None):
        self.points = sorted(points or [], key=lambda p: p[0])

    def est_valide(self) -> bool:
        # au moins deux points distincts en tension et Re croissant
        return (len(self.points) >= 2
                and all(self.points[i][0] < self.points[i + 1][0]
                        and self.points[i][1] < self.points[i + 1][1]
                        for i in range(len(self.points) - 1)))

    def re_pour_tension(self, volts: float) -> float:
        pts = self.points
        if volts <= pts[0][0]:
            (x0, y0), (x1, y1) = pts[0], pts[1]
        elif volts >= pts[-1][0]:
            (x0, y0), (x1, y1) = pts[-2], pts[-1]
        else:
            i = bisect_left([p[0] for p in pts], volts)
            (x0, y0), (x1, y1) = pts[i - 1], pts[i]
        return y0 + (y1 - y0) * (volts - x0) / (x1 - x0)

    def tension_pour_re(self, re: float) -> float:
        """Interpolation inverse : tension à appliquer pour viser ce Re."""
        pts = self.points
        res = [p[1] for p in pts]
        if re <= res[0]:
            (x0, y0), (x1, y1) = pts[0], pts[1]
        elif re >= res[-1]:
            (x0, y0), (x1, y1) = pts[-2], pts[-1]
        else:
            i = bisect_left(res, re)
            (x0, y0), (x1, y1) = pts[i - 1], pts[i]
        v = x0 + (x1 - x0) * (re - y0) / (y1 - y0)
        return min(max(v, 0.0), 10.0)

    def to_list(self) -> list:
        return [[round(v, 3), round(re, 1)] for v, re in self.points]

from core.banc import Banc
from core.regulation import RegulateurPI


class Pilote:
    def __init__(self, banc: Banc, es, cfg: dict):
        self.banc = banc
        self.es = es                       # EntreesSorties (Wago ou simulateur)
        r = cfg["regulation"]
        self.periode = r["periode_s"]
        self.tolerance = r.get("tolerance_convergence_relative", 0.01)
        self.pi = RegulateurPI(
            kp=r["kp"], ki=r["ki"], periode_s=self.periode,
            rampe_v_par_s=r.get("rampe_v_par_s", 0.5),
            bande_morte_relative=r.get("bande_morte_relative", 0.0))
        # temps de course des actionneurs (servent de CHIEN DE GARDE :
        # défaut vanne si la fin de course n'est pas atteinte à temps)
        act = cfg.get("actionneurs", {})
        self.t_vanne_3voies = act.get("vanne_3voies_temps_s", 11.0)
        self.t_vanne_bypass = act.get("vanne_bypass_temps_s", 11.0)
        self.t_vanne_pdc_course = act.get("vanne_perte_charge_course_s", 25.0)
        self.marge_manoeuvre = act.get("marge_apres_manoeuvre_s", 2.0)
        self.facteur_chien_garde = act.get("facteur_chien_garde", 1.5)

        # correspondance diaphragme -> fin de course de la vanne 3 voies
        # (voie 1 = ISO, voie 2 = Miller ; angles à confirmer sur site)
        pos = cfg.get("modbus", {}).get("positions_vanne_3voies", {})
        self._fc_par_diaphragme = {
            "iso": pos.get("fc_iso", "fc_3voies_0"),
            "miller": pos.get("fc_miller", "fc_3voies_90"),
        }

        # manœuvre en cours : None ou dict {motif, fc_attendue, t_debut, t_max}
        self._manoeuvre: dict | None = None
        self._t_fin_gel = 0.0              # petite marge après fin de course

        # vanne perte de charge : recopie 0-10 V (pas de fin de course).
        # L'estimation interne (consigne suivie à 10 V / temps de course)
        # sert de diagnostic : divergence avec la recopie = défaut actionneur.
        self._pdc_consigne = 0.0
        self._pdc_position_estimee = 0.0
        self._pdc_cycles_divergents = 0
        self._pdc_recopie_vue = False      # estimée pas encore calée sur la recopie
        self._t_dernier_cycle = None
        # butées calibrées de la recopie (0 % et 100 % réels en volts)
        self.pdc_recopie_min = act.get("pdc_recopie_min_v", 0.0)
        self.pdc_recopie_max = act.get("pdc_recopie_max_v", 10.0)

        # consigne
        self.re_cible = 0.0
        self.reference = "conduite"        # "conduite" ou "veine"
        self.regulation_active = False
        self.mode_manuel = False
        self.tension_manuelle = 0.0

        # séquence : profil (t_s, re_cible) suivi par la boucle, ou None
        self._sequence: dict | None = None

        # stratégie bas-Re : sous ce Re, on ouvre le bypass et on règle en
        # split-range (variateur + vanne perte de charge). Le seuil dérive de
        # la tension minimale exploitable du ventilateur, mesurée par
        # l'apprentissage (sinon valeur de config, sinon désactivé).
        srcfg = cfg.get("strategie_bas_re", {})
        self.bas_re_actif = srcfg.get("actif", True)
        self.seuil_bas_re = srcfg.get("seuil_re", 0)        # 0 = auto/désactivé
        self.u_min_ventilateur = srcfg.get("u_min_ventilateur_v", 0.0)
        self._mode_bas_re = False          # état courant de la stratégie
        sel = cfg.get("selection_diaphragme", {})
        self._hysteresis_bypass = sel.get("hysteresis_re", 500)

        # état courant (lu par l'IHM)
        self.etat = {"diaphragme": "miller", "u_variateur": 0.0,
                     "convergee": False, "erreur_com": None}
        # gain statique estimé du banc [Re conduite / volt], appris en
        # fonctionnement -> pré-positionnement (feed-forward) des consignes.
        # Persisté sur disque pour que la première consigne d'une session
        # bénéficie du gain appris lors des sessions précédentes.
        self._chemin_etat = Path(cfg.get("fichier_etat", "etat_banc.json"))
        if not self._chemin_etat.is_absolute():
            # relatif au dossier du programme, pas au répertoire de lancement
            self._chemin_etat = (Path(__file__).resolve().parent.parent
                                 / self._chemin_etat)
        self._gain_re_par_volt: float | None = None
        self.table_gains = TableGains()
        try:
            donnees = json.loads(self._chemin_etat.read_text(encoding="utf-8"))
            g = donnees.get("gain_re_par_volt")
            if g and 100 < g < 1e6:          # garde-fou de vraisemblance
                self._gain_re_par_volt = g
            pts = donnees.get("table_gains")
            if pts:
                t = TableGains(pts)
                if t.est_valide():
                    self.table_gains = t
            umin = donnees.get("u_min_ventilateur_v")
            if umin and 0 < umin < 10:
                self.u_min_ventilateur = umin
        except (OSError, ValueError):
            pass
        self._verrou = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -------------------------------------------------------------- commandes

    def definir_consigne(self, re_cible: float, reference: str) -> None:
        with self._verrou:
            self.re_cible = re_cible
            self.reference = reference

    def demarrer_regulation(self) -> None:
        with self._verrou:
            self.mode_manuel = False
            self.regulation_active = True
            self._sequence = None          # une consigne fixe annule la séquence
            self._preparation_faite = False
        self.etat["sequence"] = None

    def demarrer_sequence(self, points: list, reference: str) -> str | None:
        """Suivi d'un profil (t_s, Re cible) : la consigne évolue par
        échelons aux échéances du profil ; le temps de la dernière ligne
        marque la fin, sa consigne est ensuite maintenue.
        Retourne un message d'erreur ou None."""
        if not points:
            return "Séquence vide."
        with self._verrou:
            self.mode_manuel = False
            self.tension_manuelle = 0.0
            self.reference = reference
            self.re_cible = float(points[0][1])
            self.regulation_active = True
            self._sequence = {
                "points": [(float(t), float(re)) for t, re in points],
                "t0": time.monotonic(),
                "index": 0,
            }
        self._preparation_faite = False
        return None

    def arreter_regulation(self) -> None:
        """Arrêt du variateur : coupe la régulation, le mode manuel ET la
        séquence (sinon la boucle réappliquerait une consigne au cycle
        suivant — inacceptable pour un bouton d'arrêt)."""
        with self._verrou:
            self.regulation_active = False
            self.mode_manuel = False
            self.tension_manuelle = 0.0
            self._sequence = None
        self.etat["sequence"] = None
        self.es.ecrire_variateur(0.0)
        self.es.ecrire_variateur_run(False)
        self._run_actif = False
        self.pi.reinitialiser(0.0)

    def mode_manuel_tension(self, volts: float) -> None:
        with self._verrou:
            self.mode_manuel = True
            self.regulation_active = False
            self.tension_manuelle = volts
            self._sequence = None          # le mode manuel annule la séquence
        self.etat["sequence"] = None

    def mode_asservi(self) -> None:
        """Quitte le mode manuel sans démarrer la régulation."""
        with self._verrou:
            self.mode_manuel = False

    def commander_variateur_manuel(self, volts: float) -> None:
        """Mode manuel : fixe la tension variateur (RUN géré automatiquement)."""
        self.mode_manuel_tension(volts)

    def appliquer_banc(self, banc: Banc) -> None:
        """Remplace le moteur de calcul après modification de la
        configuration (géométries…). À appeler régulation arrêtée ;
        le gain appris est conservé (la dynamique du banc ne change pas)."""
        with self._verrou:
            self.banc = banc

    def commander_vanne_perte_charge(self, volts: float) -> None:
        """Consigne 0-10 V de la vanne proportionnelle. La position réelle
        est lue sur la recopie 0-10 V (etat['pdc_position']) ; l'actionneur
        met ~25 s pleine course."""
        self._pdc_consigne = min(max(volts, 0.0), 10.0)
        self.es.ecrire_vanne_perte_charge(self._pdc_consigne)

    def commander_vanne_perte_charge_pct(self, pct: float) -> None:
        """Consigne en % : 0 % = 0 V = ouverte, 100 % = 10 V = fermée."""
        self.commander_vanne_perte_charge(min(max(pct, 0.0), 100.0) / 10.0)

    def commander_bypass(self, ouverte: bool) -> None:
        """Vanne TOR bypass : régulation gelée jusqu'à la fin de course
        (fc_bypass_ouvert / fc_bypass_ferme), chien de garde sur le temps
        de course."""
        self.es.ecrire_vanne_bypass(ouverte)
        self._demarrer_manoeuvre(
            motif="manœuvre bypass " + ("(ouverture)" if ouverte
                                        else "(fermeture)"),
            fc_attendue="fc_bypass_ouvert" if ouverte else "fc_bypass_ferme",
            t_course=self.t_vanne_bypass)

    def commander_diaphragme(self, position: str) -> None:
        """Vanne 3 voies vers "iso" (0°), "miller" (90°) ou "fermee" (180°).

        L'actionneur s'arrête automatiquement à 90° dans les deux sens :
        un trajet 0° ↔ 180° demande une seconde impulsion relais, déclenchée
        par la boucle à la détection de l'arrêt intermédiaire (fc_relance).
        Régulation gelée jusqu'à la fin de course cible (chien de garde)."""
        mesures = self.etat.get("mesures_brutes") or {}
        fc_iso = self._fc_par_diaphragme["iso"]
        fc_miller = self._fc_par_diaphragme["miller"]
        fc_cible = {"iso": fc_iso, "miller": fc_miller,
                    "fermee": "fc_3voies_180"}[position]
        if mesures.get(fc_cible):              # déjà en position
            self.etat["diaphragme"] = position
            return
        if position == "miller":
            # depuis 180° on remonte (impulsion vers ISO), sinon on descend
            vers_iso = bool(mesures.get("fc_3voies_180"))
            relance = None
        else:
            vers_iso = (position == "iso")
            # extrême → extrême : arrêt auto à 90°, puis 2e impulsion
            relance = None if mesures.get(fc_miller) else fc_miller
        self.es.ecrire_vanne_diaphragme(vers_iso)
        self.etat["diaphragme"] = position
        self._demarrer_manoeuvre(
            motif=f"vanne 3 voies → {position}",
            fc_attendue=fc_cible, t_course=self.t_vanne_3voies,
            fc_relance=relance, vers_iso=vers_iso)

    def _demarrer_manoeuvre(self, motif: str, fc_attendue: str,
                            t_course: float, fc_relance: str | None = None,
                            vers_iso: bool | None = None) -> None:
        self._manoeuvre = {
            "motif": motif,
            "fc_attendue": fc_attendue,
            "fc_relance": fc_relance,       # arrêt intermédiaire à relancer
            "vers_iso": vers_iso,
            "t_relance": None,
            "t_debut": time.monotonic(),
            "t_max": time.monotonic() + t_course * self.facteur_chien_garde,
        }

    # ----------------------------------------------------------------- boucle

    def demarrer(self) -> None:
        self._thread = threading.Thread(target=self._boucle, daemon=True)
        self._thread.start()

    def arreter(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        self.es.fermer()
        # persistance du gain appris pour les sessions suivantes
        if self._gain_re_par_volt or self.table_gains.est_valide():
            try:
                donnees = {}
                if self._gain_re_par_volt:
                    donnees["gain_re_par_volt"] = self._gain_re_par_volt
                if self.table_gains.est_valide():
                    donnees["table_gains"] = self.table_gains.to_list()
                self._chemin_etat.write_text(
                    json.dumps(donnees), encoding="utf-8")
            except OSError:
                pass

    def _boucle(self) -> None:
        self._preparation_faite = False
        while not self._stop.is_set():
            debut = time.monotonic()
            try:
                self._cycle()
                self.etat["erreur_com"] = None
            except Exception as exc:           # erreur Modbus : repli sûr
                self.etat["erreur_com"] = str(exc)
                # état sûr documenté : variateur 0 V ET ordre RUN retiré.
                # On latche l'arrêt (regulation_active=False), comme les
                # défauts variateur/vanne, pour ne pas relancer la soufflerie
                # tout seul dès que la communication revient.
                with self._verrou:
                    self.regulation_active = False
                try:
                    self.es.ecrire_variateur(0.0)
                except Exception:
                    pass
                try:
                    self.es.ecrire_variateur_run(False)
                    self._run_actif = False
                except Exception:
                    pass
            reste = self.periode - (time.monotonic() - debut)
            if reste > 0:
                self._stop.wait(reste)

    def _cycle(self) -> None:
        mesures = self.es.lire_mesures()
        T = mesures["temperature"]
        # mesures brutes publiées telles quelles pour la supervision
        # (onglet lecture seule de l'IHM)
        self.etat["mesures_brutes"] = dict(mesures)
        self.etat["cycles"] = self.etat.get("cycles", 0) + 1

        # au premier cycle : position réelle de la vanne 3 voies lue sur les
        # fins de course (ne pas supposer la position au démarrage)
        if not getattr(self, "_position_initiale_lue", False):
            for diaph_nom, fc in self._fc_par_diaphragme.items():
                if mesures.get(fc):
                    self.etat["diaphragme"] = diaph_nom
            if mesures.get("fc_3voies_180"):
                self.etat["diaphragme"] = "fermee"
            self._position_initiale_lue = True

        p_amont = mesures.get("p_amont_500", 0.0)
        # Chaque diaphragme a son propre capteur physique câblé sur ses prises
        # de pression : on lit le bon capteur selon la position de la vanne 3 voies.
        diaph_courant = self.etat["diaphragme"]
        dp = (mesures.get("dp_iso_500",    0.0) if diaph_courant == "iso"
              else mesures.get("dp_diaph_500", 0.0))

        # ---- déroulement de la séquence (profil temps -> Re cible) ----------
        # Avancé même pendant un gel de manœuvre : le profil est temporel.
        with self._verrou:
            seq = self._sequence
        if seq is not None:
            pts = seq["points"]
            t_ecoule = time.monotonic() - seq["t0"]
            i = 0
            for j, (t_pt, _) in enumerate(pts):
                if t_ecoule >= t_pt:
                    i = j
            if i != seq["index"]:
                seq["index"] = i
                with self._verrou:
                    self.re_cible = pts[i][1]
                # rechoix diaphragme + feed-forward sur le nouvel échelon
                self._preparation_faite = False
            terminee = t_ecoule >= pts[-1][0]
            self.etat["sequence"] = {
                "t": min(t_ecoule, pts[-1][0]), "t_fin": pts[-1][0],
                "etape": i + 1, "n": len(pts),
                "re_cible": pts[i][1], "terminee": terminee,
            }
            if terminee:
                # fin de profil : la consigne finale reste appliquée,
                # la régulation continue jusqu'à l'arrêt par l'opérateur
                with self._verrou:
                    self._sequence = None

        with self._verrou:
            re_cible = self.re_cible
            reference = self.reference
            regul = self.regulation_active
            manuel = self.mode_manuel
            u_manu = self.tension_manuelle

        diaph = self.etat["diaphragme"]

        # ---- préparation à l'activation de la consigne --------------------
        if regul and not self._preparation_faite and re_cible > 0:
            if reference == "veine":
                qm_vise = self.banc.re_veine_vers_qm(re_cible, T, p_amont)
            else:
                qm_vise = self.banc.re_conduite_vers_qm(re_cible, T, p_amont)
            re_cond_attendu = self.banc.qm_vers_re(
                qm_vise, T, self.banc.patm_pa + p_amont)["re_conduite"]

            nouveau_diaph = self.banc.choisir_diaphragme(re_cond_attendu, diaph)
            if nouveau_diaph != diaph:
                # commande + gel jusqu'à la fin de course (chien de garde),
                # double impulsion gérée si la vanne part de 180°
                self.commander_diaphragme(nouveau_diaph)
                diaph = nouveau_diaph

            self.etat["dp_attendu"] = self.banc.dP_attendu(
                diaph, qm_vise, T, p_amont)
            # pré-positionnement : table de gains (tension->Re mesurée) si
            # disponible, sinon gain unique appris, sinon tension courante
            if self.table_gains.est_valide():
                u_ff = self.table_gains.tension_pour_re(re_cond_attendu)
            elif self._gain_re_par_volt:
                u_ff = min(max(re_cond_attendu / self._gain_re_par_volt,
                               0.0), 10.0)
            else:
                u_ff = self.etat.get("u_variateur", 0.0)
            self.pi.reinitialiser(u_ff)
            self._preparation_faite = True

        # ---- vanne perte de charge : recopie réelle + diagnostic ------------
        maintenant = time.monotonic()
        if self._t_dernier_cycle is not None:
            dt = maintenant - self._t_dernier_cycle
            vitesse = 10.0 / self.t_vanne_pdc_course      # V/s de course
            ecart_pdc = self._pdc_consigne - self._pdc_position_estimee
            pas = min(abs(ecart_pdc), vitesse * dt)
            self._pdc_position_estimee += pas if ecart_pdc > 0 else -pas
        self._t_dernier_cycle = maintenant

        pdc_recopie = mesures.get("pdc_recopie")
        if pdc_recopie is not None:
            self.etat["pdc_position"] = pdc_recopie
            if not self._pdc_recopie_vue:
                # première recopie : caler l'estimée sur la position réelle
                # (sinon fausse divergence tant que la vanne rejoint la consigne)
                self._pdc_position_estimee = pdc_recopie
                self._pdc_recopie_vue = True
                self._pdc_cycles_divergents = 0
                self.etat["defaut_pdc"] = False
            # diagnostic : recopie incohérente avec le modèle d'actionneur
            elif abs(pdc_recopie - self._pdc_position_estimee) > 1.0:
                self._pdc_cycles_divergents += 1
            else:
                self._pdc_cycles_divergents = 0
                # recalage du modèle sur la réalité
                self._pdc_position_estimee = pdc_recopie
            self.etat["defaut_pdc"] = self._pdc_cycles_divergents \
                > max(3, int(5.0 / self.periode))
        else:
            self.etat["pdc_position"] = self._pdc_position_estimee
            self.etat["defaut_pdc"] = False

        # ---- gel pendant une manœuvre de vanne TOR -------------------------
        # Fin de gel : fin de course atteinte (+ marge). Chien de garde :
        # défaut vanne si la fin de course n'arrive pas à temps.
        if self._manoeuvre is not None:
            m = self._manoeuvre
            fc_atteinte = bool(mesures.get(m["fc_attendue"], False))
            if fc_atteinte:
                if self._t_fin_gel == 0.0:
                    self._t_fin_gel = maintenant + self.marge_manoeuvre
                if maintenant >= self._t_fin_gel:
                    self._manoeuvre = None
                    self._t_fin_gel = 0.0
            elif m.get("fc_relance") and mesures.get(m["fc_relance"]):
                # arrêt intermédiaire à 90° : 2e impulsion relais après une
                # courte pause, chien de garde réarmé pour la 2e demi-course
                if m["t_relance"] is None:
                    m["t_relance"] = maintenant + 0.5
                elif maintenant >= m["t_relance"]:
                    self.es.ecrire_vanne_diaphragme(m["vers_iso"])
                    m["fc_relance"] = None
                    m["t_max"] = maintenant \
                        + self.t_vanne_3voies * self.facteur_chien_garde
            elif maintenant > m["t_max"]:
                # DÉFAUT VANNE : fin de course jamais atteinte -> repli sûr
                # (variateur 0 V ET ordre RUN retiré, comme sur défaut variateur)
                self.es.ecrire_variateur(0.0)
                self.es.ecrire_variateur_run(False)
                self._run_actif = False
                with self._verrou:
                    self.regulation_active = False
                self.etat.update({
                    "defaut_vanne": f"{m['motif']} : fin de course "
                                    f"{m['fc_attendue']} non atteinte "
                                    f"(vanne bloquée ou câblage)",
                    "manoeuvre": None, "regulation": False,
                    "u_variateur": 0.0, "convergee": False,
                })
                self._manoeuvre = None
                self._t_fin_gel = 0.0
                return
            if self._manoeuvre is not None:
                self.etat.update({
                    "temperature": T, "p_amont": p_amont, "dp": dp,
                    "manoeuvre": m["motif"],
                    "manoeuvre_restant_s": max(m["t_max"] - maintenant, 0.0),
                    "fc_attendue": m["fc_attendue"],
                    "convergee": False, "regulation": regul, "manuel": manuel,
                })
                return
        self.etat["manoeuvre"] = None

        # ---- cohérence position vanne 3 voies / diaphragme attendu ---------
        # Hors manœuvre, la fin de course du diaphragme actif doit être
        # présente. Sinon : mapping erroné, vanne déplacée à la main, ou
        # défaut de fin de course.
        fc_attendue = self._fc_par_diaphragme.get(diaph)   # None si "fermee"
        if fc_attendue and fc_attendue in mesures and not mesures[fc_attendue]:
            self._cycles_incoherence = getattr(self, "_cycles_incoherence", 0) + 1
        else:
            self._cycles_incoherence = 0
        self.etat["incoherence_vanne"] = self._cycles_incoherence \
            > max(3, int(3.0 / self.periode))

        # incohérence persistante en régulation : on réguerait sur un capteur
        # ΔP qui ne correspond plus à la position réelle de la vanne -> repli
        # sûr + latch (l'opérateur vérifie la vanne puis relance).
        if self.etat["incoherence_vanne"] and regul:
            self.es.ecrire_variateur(0.0)
            self.es.ecrire_variateur_run(False)
            self._run_actif = False
            with self._verrou:
                self.regulation_active = False
            self.etat.update({
                "defaut_vanne": "position vanne 3 voies incohérente avec le "
                                "diaphragme actif (fin de course absente) — "
                                "vérifier la vanne puis relancer",
                "regulation": False, "u_variateur": 0.0, "convergee": False,
            })
            return

        # ---- mesure du débit et des Reynolds -------------------------------
        # le diaphragme a pu changer pendant la préparation (vanne déjà en
        # position, sans manœuvre) : relire le ΔP sur le capteur correspondant
        # au diaphragme finalement retenu, pas celui du début de cycle.
        dp = (mesures.get("dp_iso_500", 0.0) if diaph == "iso"
              else mesures.get("dp_diaph_500", 0.0))
        resultat = self.banc.qm_depuis_mesures(diaph, dp, T, p_amont)
        re_mesure = resultat["re_veine"] if reference == "veine" \
            else resultat["re_conduite"]

        # apprentissage du gain statique du banc (filtré), pour le
        # pré-positionnement des prochaines consignes. Suspendu tant que le
        # variateur n'a pas atteint sa fréquence de consigne (sortie
        # multifonction 'fréquence atteinte' du 3G3MV, si câblée).
        # Suspendu aussi en stratégie bas-Re / bypass ouvert : le Re y dépend
        # de la vanne PdC et du bypass, pas seulement de la tension -> le
        # rapport Re/volt n'est plus le gain du banc.
        u_courant = self.etat.get("u_variateur", 0.0)
        freq_ok = mesures.get("frequence_atteinte", True)
        if (freq_ok and u_courant > 0.3 and resultat["re_conduite"] > 200
                and not self._mode_bas_re
                and not mesures.get("fc_bypass_ouvert")):
            g = resultat["re_conduite"] / u_courant
            if self._gain_re_par_volt is None:
                self._gain_re_par_volt = g
            else:
                self._gain_re_par_volt += 0.05 * (g - self._gain_re_par_volt)

        # ---- relecture autoritaire de la consigne avant d'agir -------------
        # Un arrêt (arreter_regulation) survenu APRÈS l'instantané de début de
        # cycle doit primer : sinon RUN et tension seraient réémis ici alors
        # que l'opérateur vient de couper. On relit l'état juste avant les
        # écritures pour réduire la fenêtre de course à ce strict minimum.
        with self._verrou:
            regul = self.regulation_active
            manuel = self.mode_manuel
            re_cible = self.re_cible
            u_manu = self.tension_manuelle

        # ---- ordre RUN du variateur (3G3MV : S1, n003=1) --------------------
        # RUN dès qu'une tension > 0 doit être appliquée ; arrêt sinon.
        run_voulu = (manuel and u_manu > 0.05) or (regul and re_cible > 0)
        if run_voulu != getattr(self, "_run_actif", None):
            self.es.ecrire_variateur_run(run_voulu)
            self._run_actif = run_voulu

        # défaut variateur remonté par le relais MA/MB (si câblé)
        if mesures.get("defaut_variateur"):
            self.es.ecrire_variateur(0.0)
            self.es.ecrire_variateur_run(False)
            self._run_actif = False
            with self._verrou:
                self.regulation_active = False
            self.etat.update({
                "defaut_variateur": "défaut signalé par le 3G3MV "
                                    "(relais MA/MB) — acquitter sur le "
                                    "variateur puis relancer",
                "regulation": False, "u_variateur": 0.0, "convergee": False,
            })
            return
        self.etat["defaut_variateur"] = None

        # ---- décision stratégie bas-Re (mode automatique) ------------------
        # Seuil effectif : valeur de config si fixée, sinon dérivé de la
        # tension min exploitable et du gain (Re atteignable à u_min + marge).
        seuil = self.seuil_bas_re
        if not seuil and self.u_min_ventilateur and self.table_gains.est_valide():
            seuil = self.table_gains.re_pour_tension(
                self.u_min_ventilateur) * 1.3
        elif not seuil and self.u_min_ventilateur and self._gain_re_par_volt:
            seuil = self.u_min_ventilateur * self._gain_re_par_volt * 1.3

        # ---- action -------------------------------------------------------
        if manuel:
            # mode manuel : tension imposée, vanne PdC et bypass pilotés
            # explicitement par l'opérateur (commandes dédiées)
            u = u_manu
            self.es.ecrire_variateur(u)
            self._mode_bas_re = False
        elif regul and re_cible > 0:
            # hystérésis bypass : on entre en bas-Re si re_cible < seuil,
            # on en sort seulement si re_cible > seuil + hysteresis_bypass.
            if self._mode_bas_re:
                besoin_bas_re = (self.bas_re_actif and seuil
                                 and re_cible < seuil + self._hysteresis_bypass)
            else:
                besoin_bas_re = (self.bas_re_actif and seuil
                                 and re_cible < seuil)
            # gestion du bypass selon le besoin (manœuvre = gel géré ailleurs)
            bypass_ouvert = bool(mesures.get("fc_bypass_ouvert"))
            if besoin_bas_re and not bypass_ouvert \
                    and self._manoeuvre is None:
                self.commander_bypass(True)
            elif not besoin_bas_re and bypass_ouvert \
                    and self._manoeuvre is None:
                self.commander_bypass(False)
            self._mode_bas_re = besoin_bas_re

            # sortie unique du PI, répartie en split-range
            cmd = self.pi.calculer(re_cible, re_mesure)   # 0..10 (V équiv.)
            u, pdc_v = self._split_range(cmd, besoin_bas_re)
            self.es.ecrire_variateur(u)
            self.commander_vanne_perte_charge(pdc_v)
        else:
            u = 0.0
            self.es.ecrire_variateur(u)
            self._mode_bas_re = False

        convergee = regul and re_cible > 0 and \
            abs(re_mesure - re_cible) / re_cible < self.tolerance

        self.etat.update({
            "temperature": T, "p_amont": p_amont, "dp": dp,
            "qm": resultat["qm"], "qv": resultat.get("qv", 0.0),
            "re_conduite": resultat["re_conduite"],
            "re_veine": resultat["re_veine"],
            "rho": resultat.get("rho", 0.0),
            "C": resultat.get("C"), "u_variateur": u,
            "re_cible": re_cible, "reference": reference,
            "re_mesure": re_mesure, "convergee": convergee,
            "regulation": regul, "manuel": manuel,
            "pdc_consigne": self._pdc_consigne,
            "mode_bas_re": self._mode_bas_re,
            "seuil_bas_re_effectif": round(seuil) if seuil else None,
        })

    def _split_range(self, cmd: float, bas_re: bool) -> tuple[float, float]:
        """Répartit la commande PI unique (0..10) sur variateur + vanne PdC.

        Hors bas-Re : variateur seul sur toute la plage, vanne PdC ouverte
        (résistance minimale).

        En bas-Re : le variateur est maintenu à sa tension minimale
        exploitable (zone stable). Le bypass est ouvert en parallèle sur le
        ventilateur : une partie du refoulement court-circuite la boucle
        principale. La PdC, en série sur le refoulement vers la chambre,
        dose la fraction de débit qui atteint la veine :
          cmd faible  -> PdC fermée  (résistance forte, débit dévié par bypass, Re bas)
          cmd élevée  -> PdC ouverte (résistance faible, débit forcé vers la veine, Re haut)
        Au-delà de la pleine ouverture de la PdC, le variateur reprend la
        main depuis u_min pour continuer à monter (transition sans à-coup).
        """
        cmd = min(max(cmd, 0.0), 10.0)
        if not bas_re:
            return cmd, 0.0                # variateur seul, vanne grande ouverte (0 V)

        u_min = self.u_min_ventilateur or 1.0
        # Rappel convention : 0 V = ouvert, 10 V = fermé.
        # première moitié : variateur à u_min, PdC passe de fermée (10V) à ouverte (0V)
        # seconde moitié  : PdC grande ouverte (0V), variateur monte depuis u_min
        if cmd <= 5.0:
            u = u_min
            pdc_v = 10.0 * (1.0 - cmd / 5.0)       # cmd=0 → 10V (fermé), cmd=5 → 0V (ouvert)
        else:
            pdc_v = 0.0                              # grande ouverte (0 V)
            u = u_min + (cmd - 5.0) / 5.0 * (10.0 - u_min)
        return u, pdc_v
