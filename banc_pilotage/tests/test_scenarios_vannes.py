# -*- coding: utf-8 -*-
"""Cas d'usage de la gestion des vannes et du variateur, sur le banc simulé
à horloge accélérée. Chaque test vérifie l'ORDRE réel des écritures vers les
actionneurs et des fins de course, pas seulement l'état final.

Règles communes : lecture des fins de course, commande des seules vannes
hors position, toutes simultanément, aucune consigne (variateur ou PdC)
avant que toutes soient en butée, consigne initiale issue de la table du
mode (ISO ou Miller), puis régulation continue de l'organe du mode."""

import json
import random
import time
from pathlib import Path

import pytest

from core.banc import Banc
from core.pilote import Pilote, TableGains
from io_banc.simulateur import SimulateurBanc

CFG = json.loads((Path(__file__).resolve().parent.parent / "config.json")
                 .read_text(encoding="utf-8"))
FC = ("fc_3voies_0", "fc_3voies_90", "fc_bypass_ouvert", "fc_bypass_ferme",
      "fc_pdc_ouvert")


class SimTrace(SimulateurBanc):
    """Simulateur qui journalise chaque CHANGEMENT de valeur écrite."""

    def __init__(self, *a, horloge, **k):
        super().__init__(*a, **k)
        self.horloge = horloge
        self.ev: list[tuple[float, str, object]] = []
        self._dern: dict = {}

    def _note(self, quoi, v):
        if self._dern.get(quoi) != v:
            self._dern[quoi] = v
            self.ev.append((self.horloge[0], quoi, v))

    def ecrire_variateur(self, v):
        self._note("variateur", round(v, 3))
        super().ecrire_variateur(v)

    def ecrire_vanne_diaphragme(self, vers_0deg):
        self._note("3voies", "0" if vers_0deg else "90")
        super().ecrire_vanne_diaphragme(vers_0deg)

    def ecrire_vanne_bypass(self, ouverte):
        self._note("bypass", ouverte)
        super().ecrire_vanne_bypass(ouverte)

    def ecrire_vanne_perte_charge(self, v):
        self._note("pdc", round(v, 3))
        super().ecrire_vanne_perte_charge(v)


class BancSimule:
    """Pilote + simulateur à horloge accélérée, tables apprises connues."""

    def __init__(self, monkeypatch):
        random.seed(3)
        self.t = [1000.0]
        monkeypatch.setattr(time, "monotonic", lambda: self.t[0])
        cfg = json.loads(json.dumps(CFG))
        cfg["journal"]["actif"] = False
        banc = Banc(cfg)
        self.sim = SimTrace(banc, cfg["simulation"], cfg["modbus"],
                            cfg["actionneurs"], horloge=self.t)
        p = Pilote(banc, self.sim, cfg)
        p.u_min_ventilateur = 1.5
        p.table_gains_bas_re = TableGains([(1.5, 2020.0), (3.0, 4050.0), (5.0, 6750.0)])
        p.table_gains = TableGains([(2.0, 7000.0), (4.0, 15000.0), (6.0, 21000.0)])
        p.table_gains_pdc = TableGains([(20.0, 600.0), (60.0, 1500.0), (100.0, 2300.0)])
        self.p = p
        self.fc: list[tuple[float, frozenset]] = []
        p._position_repos()                   # Miller, bypass et PdC ouverts
        self.cycles(20.0)

    def cycles(self, duree_s: float) -> None:
        for _ in range(int(duree_s / self.p.periode)):
            self.t[0] += self.p.periode
            self.p._cycle()
            m = self.p.etat.get("mesures_brutes", {})
            actives = frozenset(k for k in FC if m.get(k))
            if not self.fc or self.fc[-1][1] != actives:
                self.fc.append((self.t[0], actives))

    def consigne(self, re: float) -> float:
        self.sim.ev.clear()
        self.fc.clear()
        self.p.definir_consigne(re, "conduite")
        if not self.p.regulation_active:
            self.p.demarrer_regulation()
        return self.t[0]

    def ecritures(self, quoi: str) -> list[tuple[float, object]]:
        return [(t, v) for t, q, v in self.sim.ev if q == quoi]

    def t_fc(self, *attendues: str) -> float:
        """Premier instant où toutes ces fins de course sont actives."""
        return next(t for t, a in self.fc if set(attendues) <= a)


@pytest.fixture
def banc(monkeypatch):
    return BancSimule(monkeypatch)


def _premiere_consigne_utile(b, t0):
    """Instant de la première tension variateur > 0 après t0."""
    return next(t for t, v in b.ecritures("variateur") if t > t0 and v > 0)


def test_scenario1_miller_variateur_depuis_le_repos(banc):
    """Re = 1000, Miller, régulation par le variateur. Vannes déjà en place
    (repos) : aucun ordre de vanne, PdC jamais fermée, variateur réglé."""
    b = banc
    b.p.seuil_bas_re = 500.0                  # Re_freq_min < 1000 (banc réel)
    t0 = b.consigne(1000.0)
    b.cycles(20.0)
    assert b.ecritures("3voies") == [] and b.ecritures("bypass") == []
    assert all(v >= 10.0 - 1e-9 for _, v in b.ecritures("pdc"))
    assert b.p._mode_regulation == "normal_low"
    assert b.p._table_ff("normal_low") is b.p.table_gains_bas_re      # table Miller
    tensions = {v for t, v in b.ecritures("variateur") if t > t0}
    assert len(tensions) > 3                  # régulation continue du variateur


def test_scenario2_iso_vannes_simultanees_puis_table_iso(banc):
    """Re = 10000 depuis le repos (Miller, bypass ouvert) : vanne 3 voies et
    bypass commandés au MÊME cycle ; aucune tension avant toutes les butées ;
    feed-forward tiré de la table ISO."""
    b = banc
    t0 = b.consigne(10000.0)
    b.cycles(30.0)
    (t_3v, a), = b.ecritures("3voies")
    (t_bp, f), = b.ecritures("bypass")
    assert (a, f) == ("0", False) and t_3v == t_bp          # simultanés
    t_pret = b.t_fc("fc_3voies_0", "fc_bypass_ferme", "fc_pdc_ouvert")
    assert _premiere_consigne_utile(b, t0) > t_pret
    assert b.p._mode_regulation == "normal_high"
    assert b.p._diag["u_ff_cible"] == pytest.approx(
        b.p.table_gains.u_pour_re_ff(b.p._re_cible_conduite), abs=1e-3)
    assert b.p.etat["convergee"]


def test_scenario3_iso_vers_pdc(banc):
    """ISO établi -> Re = 300 : vannes TOR simultanées, PdC GARDÉE à 100 %
    pendant les manœuvres, puis variateur ET PdC (table Miller / PdC)
    envoyés ensemble, et seule la PdC est ensuite régulée."""
    b = banc
    b.consigne(10000.0)
    b.cycles(40.0)
    t0 = b.consigne(300.0)
    b.cycles(30.0)
    (t_3v, a), = b.ecritures("3voies")
    (t_bp, o), = b.ecritures("bypass")
    assert (a, o) == ("90", True) and t_3v == t_bp
    t_pret = b.t_fc("fc_3voies_90", "fc_bypass_ouvert", "fc_pdc_ouvert")
    avant = [(t, v) for t, v in b.ecritures("pdc") if t <= t_pret]
    assert all(v >= 10.0 - 1e-9 for _, v in avant)          # PdC à 100 %
    assert all(v == 0.0 for t, v in b.ecritures("variateur") if t0 < t <= t_pret)
    t_var = _premiere_consigne_utile(b, t0)
    t_pdc = next(t for t, v in b.ecritures("pdc") if t > t_pret and v < 10.0)
    assert t_var == t_pdc                                    # envoyées ensemble
    ff = b.p._pdc_ff_v(300.0)
    v_pdc_1 = next(v for t, v in b.ecritures("pdc") if t == t_pdc)
    assert v_pdc_1 == pytest.approx(ff, abs=0.2)             # table PdC
    assert b.p._mode_regulation == "pdc_control"
    assert b.p.etat["u_variateur"] == pytest.approx(b.p._u_pdc())
    assert len({v for t, v in b.ecritures("pdc") if t > t_pdc}) > 3


def test_scenario3bis_pdc_deja_regulee_non_rouverte(banc):
    """Miller + PdC déjà régulée, nouvelle consigne sous Re_freq_min : la
    PdC n'est PAS repassée à 100 %, elle rejoint directement la table."""
    b = banc
    b.consigne(800.0)
    b.cycles(40.0)
    assert b.p._pdc_regulee
    t0 = b.consigne(1400.0)
    b.cycles(5.0)
    ecr = [(t, v) for t, v in b.ecritures("pdc") if t > t0]
    assert ecr and all(v < 10.0 for _, v in ecr)
    assert ecr[0][1] == pytest.approx(b.p._pdc_ff_v(1400.0), abs=0.2)
    assert b.ecritures("3voies") == [] and b.ecritures("bypass") == []


def test_bascule_pdc_vers_variateur_sans_coupure(banc):
    """Sortie du mode PdC (seule la PdC doit se rouvrir) : la tension est
    tenue, jamais coupée, et aucune nouvelle consigne avant la butée PdC."""
    b = banc
    b.consigne(800.0)
    b.cycles(40.0)
    u_tenue = b.p.etat["u_variateur"]
    t0 = b.consigne(3000.0)
    b.cycles(30.0)
    t_pret = b.t_fc("fc_3voies_90", "fc_bypass_ouvert", "fc_pdc_ouvert")
    assert all(v > 0 for t, v in b.ecritures("variateur") if t > t0)
    assert all(v == pytest.approx(u_tenue)
               for t, v in b.ecritures("variateur") if t0 < t < t_pret)
    assert b.p._mode_regulation == "normal_low"


def test_nouvelle_consigne_pendant_le_deplacement_inverse_les_vannes(banc):
    """Consigne ISO puis, en pleine course, consigne Miller : les deux vannes
    repartent aussitôt, ensemble, vers les positions Miller (avant : la
    course inutile allait à son terme)."""
    b = banc
    b.consigne(10000.0)
    b.cycles(3.0)
    t_nouv = b.t[0]
    b.p.definir_consigne(1000.0, "conduite")
    b.cycles(1.0)
    inv_3v = [t for t, a in b.ecritures("3voies") if a == "90" and t > t_nouv]
    inv_bp = [t for t, o in b.ecritures("bypass") if o is True and t > t_nouv]
    assert inv_3v and inv_bp and inv_3v[0] == inv_bp[0]
    assert inv_3v[0] - t_nouv <= 2 * b.p.periode
    b.cycles(30.0)
    assert b.p.etat.get("defaut_vanne") is None
    assert b.p._mode_regulation == "pdc_control"


def test_aucune_consigne_tant_que_les_vannes_bougent(banc):
    """Pendant toute manœuvre : variateur à 0 V, PdC non commandée sous 100 %."""
    b = banc
    t0 = b.consigne(10000.0)
    b.cycles(30.0)
    t_pret = b.t_fc("fc_3voies_0", "fc_bypass_ferme", "fc_pdc_ouvert")
    assert all(v == 0.0 for t, v in b.ecritures("variateur") if t0 < t <= t_pret)
    assert all(v >= 10.0 - 1e-9 for t, v in b.ecritures("pdc") if t <= t_pret)


def test_consigne_veine_hors_capteur_bornee_et_convergee(banc):
    """15 000 Re veine ≈ 28 000 Re conduite : au-delà du capteur ΔP ISO.
    La régulation vise le maximum mesurable EN RE VEINE et s'y stabilise,
    déclarée convergée (Re moyen jugé sur la consigne limitée)."""
    b = banc
    # plafond un peu sous le seuil de saturation (0,95 × 0,95 × 500 Pa) :
    # avec marge_consigne_dp_relative = 1,0, il tombe SUR le seuil et le
    # recul sur saturation retient la mesure ~1 % en dessous
    b.p.marge_consigne_dp = 0.95
    b.p.definir_consigne(15000.0, "veine")
    b.p.demarrer_regulation()
    b.cycles(120.0)
    e = b.p.etat
    assert 13000 < e["re_vise"] < 15000                 # plafond en Re veine
    assert e["re_moyen"] == pytest.approx(e["re_vise"], rel=0.01)
    assert e["convergee"]


def test_config_plafond_consigne_sous_le_seuil_de_saturation():
    """marge_consigne_dp_relative = 1 placerait la consigne limitée SUR le
    seuil de saturation : le bruit le franchit sans cesse, le recul sur
    saturation s'enclenche et la régulation ne converge jamais."""
    assert CFG["regulation"]["marge_consigne_dp_relative"] < 1.0
