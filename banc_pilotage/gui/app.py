# -*- coding: utf-8 -*-
"""Interface graphique PySide6 — console layout fidèle au design HTML.

header 56px (navy, logo IMT, nav)
body : left rail 262px | center stack | right rail 344px
footer 30px (blanc, IBM Plex Mono)
"""

from __future__ import annotations
import json
import logging
import math
from collections import deque
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QTabWidget, QStackedWidget, QScrollArea,
    QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLabel, QLineEdit, QPushButton,
    QCheckBox, QSlider, QMessageBox, QFileDialog,
    QFrame, QProgressBar, QProgressDialog,
    QSpinBox, QDoubleSpinBox,
)
from PySide6.QtGui import QPixmap
from PySide6.QtCore import Qt, QTimer
import pyqtgraph as pg

from core.banc import Banc
from core.pilote import Pilote
from core.sequence import charger_sequence_csv
from ui.theme import COLORS, BTN_QSS, SEG_OFF_QSS, SEG_ON_QSS

PERIODE_IHM_MS = 250
HISTORIQUE_PTS = 600

_LOGO_PATH = Path(__file__).parent.parent / "ui" / "NEW_LOGO_IMT_NordRF_KitCom_Blanc-01.png"

log = logging.getLogger(__name__)

# ── validation ────────────────────────────────────────────────────────────────

def _positif(v, _):
    return None if v > 0 else "doit être strictement positif"

def _positif_ou_nul(v, _):
    return None if v >= 0 else "doit être positif ou nul"

def _alpha(v, _):
    return None if 0 <= v < 1e-3 else "coefficient de dilatation invraisemblable"

def _humidite_relative(v, _):
    return None if 0 <= v <= 100 else "doit être entre 0 et 100 %"

def _d_inferieur_D(prefixe):
    def f(v, valeurs):
        D = valeurs.get((prefixe, "D20_mm"))
        if D is not None and v >= D:
            return f"l'orifice doit être plus petit que la conduite ({D} mm)"
        return _positif(v, valeurs)
    return f

def _beta_iso(v, valeurs):
    err = _d_inferieur_D("diaphragme_iso")(v, valeurs)
    if err:
        return err
    D = valeurs.get(("diaphragme_iso", "D20_mm"))
    if D:
        beta = v / D
        if not 0.10 <= beta <= 0.75:
            return f"β = {beta:.3f} hors plage ISO 5167 (0,10 à 0,75)"
    return None

PARAMETRES = [
    ("Diaphragme ISO 5167", [
        (("diaphragme_iso", "D20_mm"),         "Diamètre conduite D20",  "mm",   _positif),
        (("diaphragme_iso", "d20_mm"),          "Diamètre orifice d20",   "mm",   _beta_iso),
        (("diaphragme_iso", "alpha_conduite"),  "Dilatation conduite α",  "1/°C", _alpha),
        (("diaphragme_iso", "alpha_diaphragme"),"Dilatation diaphragme α","1/°C", _alpha),
    ]),
    ("Diaphragme Miller", [
        (("diaphragme_miller", "D20_mm"),          "Diamètre conduite D20",  "mm",   _positif),
        (("diaphragme_miller", "d20_mm"),           "Diamètre orifice d20",   "mm",
         _d_inferieur_D("diaphragme_miller")),
        (("diaphragme_miller", "alpha_conduite"),  "Dilatation conduite α",  "1/°C", _alpha),
        (("diaphragme_miller", "alpha_diaphragme"),"Dilatation diaphragme α","1/°C", _alpha),
    ]),
    ("Veine d'essai", [
        (("veine", "section_m2"),                "Section de passage",            "m²", _positif),
        (("veine", "longueur_caracteristique_m"),"Longueur caractéristique (Re)", "m",  _positif),
    ]),
    ("Conditions et sélection", [
        (("patm_pa",),                              "Pression atmosphérique",   "Pa", _positif),
        (("selection_diaphragme", "seuil_re"),      "Seuil Re Miller / ISO",    "—",  _positif),
        (("selection_diaphragme", "hysteresis_re"), "Hystérésis de commutation","—",  _positif_ou_nul),
    ]),
]

def _cfg_lire(cfg, chemin):
    """Lit une valeur dans un dict imbriqué via un chemin de clés.

    Args:
        cfg: Dict de configuration
        chemin: Liste de clés pour naviguer dans le dict (ex: ["regulation", "kp"])

    Returns:
        La valeur trouvée, ou None si le chemin n'existe pas

    Raises:
        KeyError: Si une clé du chemin n'existe pas (avec message explicite)
    """
    v = cfg
    try:
        for k in chemin:
            v = v[k]
        return v
    except KeyError as e:
        # Construire chemin lisible pour diagnostic (ex: "regulation.kp")
        chemin_str = ".".join(str(k) for k in chemin)
        raise KeyError(
            f"Clé manquante dans config.json : {chemin_str} "
            f"(échec à '{e.args[0]}' dans {chemin})"
        ) from e

def _cfg_ecrire(cfg, chemin, valeur):
    """Écrit une valeur dans un dict imbriqué via un chemin de clés.

    Args:
        cfg: Dict de configuration
        chemin: Liste de clés (ex: ["regulation", "kp"])
        valeur: Valeur à écrire

    Raises:
        KeyError: Si une clé du chemin (sauf la dernière) n'existe pas
    """
    v = cfg
    try:
        for k in chemin[:-1]:
            v = v[k]
        v[chemin[-1]] = valeur
    except KeyError as e:
        chemin_str = ".".join(str(k) for k in chemin)
        raise KeyError(
            f"Clé parente manquante dans config.json : {chemin_str} "
            f"(échec à '{e.args[0]}' dans {chemin[:-1]})"
        ) from e

def _hsep():
    sep = QFrame()
    sep.setFrameShape(QFrame.Shape.HLine)
    sep.setObjectName("separator")
    return sep

# ── QSS console ───────────────────────────────────────────────────────────────

_QSS = f"""
/* rails */
QWidget#left-rail  {{ background:#FFFFFF; border-right:1px solid #DBE5EA; }}
QWidget#left-rail QWidget {{ background:transparent; }}
QWidget#right-rail {{ background:#FFFFFF; border-left:1px solid #DBE5EA;  }}

/* header */
QWidget#appheader {{ background:#0E1A2C; }}
QFrame#header-sep {{ background:rgba(255,255,255,.12); border:none; max-width:1px; }}
QLabel#appheader-title {{
    color:#FFFFFF; font-size:15px; font-weight:600;
    letter-spacing:.2px; background:transparent;
}}
QLabel#badge-simu {{
    background:{COLORS['warn']}; color:#FFFFFF;
    font-size:11px; font-weight:700; padding:0 12px;
    border-radius:12px; letter-spacing:.8px;
}}
QLabel#badge-live {{
    background:rgba(255,255,255,.08); color:{COLORS['accent']};
    font-size:11px; font-weight:700; padding:0 12px;
    border-radius:12px; letter-spacing:.8px;
}}
QPushButton#nav-btn {{
    background:transparent; border:none;
    border-bottom:2px solid transparent; border-radius:0;
    color:rgba(255,255,255,.45); font-size:13px; font-weight:400;
    padding:0 14px; min-width:0;
}}
QPushButton#nav-btn:hover {{ color:rgba(255,255,255,.85); }}
QPushButton#nav-btn-active {{
    background:{COLORS['accent']}; border:none; border-radius:6px;
    color:{COLORS['text']}; font-size:13px; font-weight:700;
    padding:0 14px; min-width:0;
}}

/* section labels */
QLabel#section-hdr {{
    color:#14223C; font-size:12px; font-weight:700; letter-spacing:1.4px;
    background:transparent;
}}
/* channel */
QLabel#ch-name {{
    color:#3A4D60; font-size:11.5px; font-weight:500; background:transparent;
}}
QLabel#ch-val {{
    font-family:"Inter",monospace; font-size:13.5px; font-weight:600;
    color:#14223C; background:transparent;
}}
QProgressBar {{
    border:none; border-radius:3px;
    background:rgba(20,34,60,.09); max-height:6px; min-height:6px;
}}
QProgressBar::chunk {{ border-radius:3px; background:{COLORS['accent']}; }}
QLabel#act-dot {{ font-size:10px; background:transparent; }}
QLabel#act-val {{
    font-family:"IBM Plex Mono",monospace; font-size:12px; font-weight:600;
    color:#14223C; background:transparent;
}}
QLabel#act-sub {{
    font-size:10px; color:#6B7E8E;
    font-family:"IBM Plex Mono",monospace; background:transparent;
}}

/* Re display */
QWidget#re-card {{
    background:#EFF4F6; border:1px solid #DBE5EA; border-radius:12px;
}}
QLabel#re-big {{
    font-family:"Inter"; font-size:42px; font-weight:600;
    color:#FF9900; background:transparent;
}}
QLabel#re-unit {{
    font-family:"IBM Plex Mono",monospace; font-size:14px; font-weight:500;
    color:#6B7E8E; background:transparent;
}}
QLabel#re-hdr {{
    font-size:10px; font-weight:700; letter-spacing:.12em;
    color:#6B7E8E; background:transparent;
}}
QLabel#re-state {{ font-size:10px; font-weight:700; background:transparent; }}

/* pilotage labels */
QLabel#ctrl-lbl  {{ font-size:12.5px; color:#3A4D60; background:transparent; }}
QLabel#ctrl-unit {{ font-family:"IBM Plex Mono",monospace; font-size:11px; color:#6B7E8E; background:transparent; }}
QLabel#ctrl-sub  {{ font-size:10px; font-family:"IBM Plex Mono",monospace; color:#6B7E8E; background:transparent; margin-top:3px; }}
QLabel#dirty-dot {{ color:#FF9900; font-size:8px; background:transparent; }}

/* number input */
QLineEdit#num-input {{
    font-family:"Inter"; font-size:13px; font-weight:600;
    color:#0892B4; background:#FFFFFF;
    border:1px solid #DBE5EA; border-radius:6px; padding:3px 7px;
}}
QLineEdit#num-input:focus {{ border-color:{COLORS['accent']}; }}

/* segmented */
QWidget#seg-box {{ background:#FFFFFF; border:1px solid {COLORS['accent']}; border-radius:9px; }}

/* action log */
QLabel#log-t   {{ font-family:"IBM Plex Mono",monospace; font-size:10.5px; color:#6B7E8E; background:transparent; }}
QLabel#log-dot {{ font-size:10px; background:transparent; }}
QLabel#log-msg {{ font-size:11.5px; color:#14223C; background:transparent; line-height:1.35; }}

/* transport bar */
QWidget#transport {{ background:#FFFFFF; border-top:1px solid #DBE5EA; }}
QPushButton#apply-btn {{
    background:{COLORS['accent']}; color:#FFFFFF; font-size:13px; font-weight:700;
    letter-spacing:.03em; border:2px solid transparent; border-radius:10px;
    padding:11px 0; min-width:0;
}}
QPushButton#apply-btn:hover   {{ background:#00A5C9; }}
QPushButton#apply-btn:pressed {{
    background:#FFFFFF; color:{COLORS['accent']};
    border:2px solid {COLORS['accent']};
}}
QPushButton#apply-btn:disabled {{ background:#A8DFF0; color:#FFFFFF; border:none; }}

QPushButton#estop {{
    background:#DC3535; color:#FFFFFF; font-size:12.5px; font-weight:700;
    letter-spacing:.03em; border:2px solid transparent; border-radius:10px;
    padding:11px 0; min-width:0;
}}
QPushButton#estop:hover   {{ background:#C83030; }}
QPushButton#estop:pressed {{
    background:#FFFFFF; color:#DC3535;
    border:2px solid #DC3535;
}}

/* footer */
QWidget#footer {{ background:#FFFFFF; border-top:1px solid #DBE5EA; }}
QLabel#foot-item {{
    font-family:"IBM Plex Mono",monospace; font-size:11px;
    color:#7B8FA0; background:transparent;
}}
QLabel#foot-state {{
    font-family:"IBM Plex Mono",monospace; font-size:11px;
    font-weight:700; color:{COLORS['accentText']}; background:transparent;
}}
QLabel#foot-cnx {{ font-size:10px; background:transparent; }}

/* center views */
QWidget#cv-panel {{
    background:#FFFFFF; border:1px solid #DBE5EA; border-radius:12px;
}}

/* groupbox dans les sous-vues */
QGroupBox {{
    background:#FFFFFF; border:1px solid #DBE5EA;
    border-top:3px solid {COLORS['accent']}; border-radius:8px;
    margin-top:22px; padding:18px 10px 10px 10px;
}}
QGroupBox::title {{
    subcontrol-origin:margin; subcontrol-position:top left;
    left:10px; top:4px; padding:0 6px;
    color:{COLORS['accent']}; font-size:12px; font-weight:700;
    text-transform:uppercase; letter-spacing:.8px; background:transparent;
}}
QLabel {{ background:transparent; color:#14223C; }}
QLabel#bold {{ font-weight:700; color:{COLORS['accent']}; font-size:14px; }}
QLabel#muted {{ color:#6B7E8E; }}
QLabel#alerte {{ color:#DC3535; font-weight:700; }}
QLabel#info {{ color:{COLORS['accent']}; }}
QLabel#ok {{ color:{COLORS['ok']}; font-weight:700; }}
QLabel#mono {{ font-family:"IBM Plex Mono",monospace; font-size:11px; color:#6B7E8E; }}
QLabel#voyant {{ font-size:18px; color:#DBE5EA; }}
QLineEdit {{
    background:#E8EEF1; border:1px solid #DBE5EA; border-radius:4px;
    padding:5px 8px; color:#14223C;
    selection-background-color:{COLORS['accent']}; selection-color:#FFFFFF;
}}
QLineEdit:focus {{ border-color:{COLORS['accent']}; background:#FFFFFF; }}
QLineEdit:disabled {{ color:#6B7E8E; background:#E8EEF1; border-color:#DBE5EA; }}
QPushButton {{
    background:{COLORS['accent']}; border:2px solid transparent;
    border-radius:4px; padding:5px 14px;
    color:#FFFFFF; font-weight:700; min-width:80px;
}}
QPushButton:hover   {{ background:#00A5C9; }}
QPushButton:pressed {{
    background:#FFFFFF; color:{COLORS['accent']};
    border:2px solid {COLORS['accent']};
}}
QPushButton:disabled {{ background:#A8DFF0; color:#FFFFFF; border:2px solid transparent; }}
QCheckBox,QRadioButton {{ color:#14223C; spacing:8px; }}
QCheckBox::indicator,QRadioButton::indicator {{
    width:16px; height:16px; border:1px solid #DBE5EA;
    background:#FFFFFF; border-radius:3px;
}}
QRadioButton::indicator {{ border-radius:8px; }}
QCheckBox::indicator:checked,QRadioButton::indicator:checked {{
    background:{COLORS['accent']}; border-color:{COLORS['accent']};
}}
QSlider:horizontal {{ min-height:34px; }}
QSlider::groove:horizontal {{
    height:10px; background:#E8EEF1; border:1px solid #DBE5EA;
    border-radius:5px; margin:0 4px;
}}
QSlider::sub-page:horizontal {{ background:#A8DFF0; border-radius:5px; }}
QSlider::handle:horizontal {{
    width:22px; height:22px; margin:-7px -4px;
    background:#FFFFFF; border-radius:11px; border:2px solid {COLORS['accent']};
}}
QSlider:disabled::groove:horizontal {{ background:#E8EEF1; border-color:#DBE5EA; }}
QSlider:disabled::sub-page:horizontal {{ background:#DBE5EA; }}
QSlider:disabled::handle:horizontal {{ background:#E8EEF1; border-color:#6B7E8E; }}
QScrollBar:vertical {{ background:#E8EEF1; width:8px; border-radius:4px; }}
QScrollBar::handle:vertical {{ background:#DBE5EA; border-radius:4px; min-height:20px; }}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {{ height:0; }}
QFrame#separator {{ background:#DBE5EA; max-height:1px; border:none; }}
QTabWidget::pane {{ background:#FFFFFF; border:1px solid #DBE5EA; border-top:none; }}
QTabBar::tab {{
    background:#E8EEF1; color:#6B7E8E; padding:8px 20px;
    border:none; border-bottom:2px solid transparent; margin-right:2px;
}}
QTabBar::tab:selected {{
    background:#FFFFFF; color:{COLORS['accent']};
    border-bottom:2px solid {COLORS['accent']}; font-weight:700;
}}
QTabBar::tab:hover:!selected {{ color:#14223C; background:#DDE9EC; }}
QMessageBox {{ background:#FFFFFF; }}
QMessageBox QLabel {{ color:#14223C; }}
QComboBox {{
    background:#E8EEF1; border:1px solid #DBE5EA; border-radius:4px;
    padding:5px 8px; color:#14223C;
}}
QComboBox:focus {{ border-color:{COLORS['accent']}; }}
QComboBox::drop-down {{ border:none; }}
QComboBox QAbstractItemView {{
    background:#FFFFFF; border:1px solid #DBE5EA;
    selection-background-color:#E3F4FB; color:#14223C; outline:0;
}}
"""


_NAV_LABELS = ["Graphique", "Voies", "Séquence", "Apprentissage",
               "Configuration", "Maintenance"]

# (clé etat/brutes, nom affiché, max barre, format, couleur barre)
# Les clés "dp_iso" et "dp_miller" sont résolues dans _tick_left via mesures_brutes
# Section CALCULS - Temps réel (couleur violette)
_CALCULS = [
    ("qv",          "Qv [m³/s]",     0.05,    "{:.6f}", "#9B59B6"),
    ("rho",         "ρ [kg/m³]",     2.0,     "{:.3f}", "#9B59B6"),
    ("mu",          "μ [Pa·s]",      0.00003, "{:.5e}", "#9B59B6"),
    ("nu",          "ν [m²/s]",      0.00002, "{:.5e}", "#9B59B6"),
]

# Section PRESSIONS - Temps réel (couleur orange)
_PRESSIONS = [
    ("p_amont",     "P amont [Pa]",   500,   "{:.2f}", "#FF9900"),
    ("dp_iso",      "ΔP ISO [Pa]",    500,   "{:.2f}", "#FF9900"),
    ("dp_miller",   "ΔP Miller [Pa]", 500,   "{:.2f}", "#FF9900"),
]

# Compatibilité : fusion des deux sections pour l'onglet Voies
_CHANNELS = _CALCULS + _PRESSIONS
# méta pour l'onglet Voies (unité, plage affichée)
_CH_UNIT  = ["m³/s",  "kg/m³", "Pa·s", "m²/s", "Pa",  "Pa",  "Pa"]
_CH_RANGE = ["0 – 0,05", "0 – 2", "0 – 3×10⁻⁵", "0 – 2×10⁻⁵", "0 – 500", "0 – 500", "0 – 500"]

# (clé interne, nom, couleur dot/barre, clé valeur, max bar)
# Couleur verte pour toutes les jauges des actionneurs
_ACTUATORS = [
    ("variateur", "Variateur soufflerie", "#27AE60", "u_variateur",  10.0),
    ("vanne3",    "Vanne Diaphragme",     "#27AE60", "diaphragme",   None),
    ("bypass",    "Vanne By-Pass",        "#27AE60", "bypass_state", None),
    ("pdc",       "Vanne Perte de Charge","#27AE60", "pdc_position", 10.0),
]


class Application(QMainWindow):

    ENTREES_ANALOGIQUES = [
        ("pdc_recopie",        "Recopie vanne perte de charge",        "V",   "{:.2f}"),
        ("dp_iso_500",         "ΔP diaphragme ISO (0-10 V / 0-500 Pa)", "Pa",  "{:.2f}"),
        ("dp_diaph_500",       "ΔP diaphragme Miller (0-10 V / 0-500 Pa)", "Pa", "{:.2f}"),
        ("p_amont_500",        "P amont (0-10 V / 0-500 Pa)",          "Pa",  "{:.2f}"),
        ("frequence_variateur","Moniteur fréquence variateur (AM)",    "V",   "{:.2f}"),
        ("temperature",        "Température PT100",                     "°C",  "{:.2f}"),
    ]
    FINS_DE_COURSE = [
        ("fc_3voies_0",       "Vanne 3 voies — butée 0°"),
        ("fc_3voies_90",      "Vanne 3 voies — butée 90°"),
        ("fc_3voies_180",     "Vanne 3 voies — butée 180° (hors usage)"),
        ("fc_bypass_ouvert",  "Bypass — ouvert"),
        ("fc_bypass_ferme",   "Bypass — fermé"),
        ("fc_pdc_ouvert",     "Perte de charge — ouverte"),
        ("fc_pdc_ferme",      "Perte de charge — fermée"),
        ("defaut_variateur",  "Défaut variateur 3G3MV (contact MB)"),
        ("defaut_act_bypass", "Défaut actionneur — bypass"),
        ("defaut_act_pdc",    "Défaut actionneur — perte de charge"),
        ("defaut_act_3voies", "Défaut actionneur — 3 voies"),
    ]
    # alertes journalisées sur front montant (clé d'état -> libellé)
    ALERTES = [
        ("erreur_com",        "⚠ Communication"),
        ("defaut_capteur",    "⚠ Défaut capteur"),
        ("defaut_vanne",      "⚠ Défaut vanne"),
        ("defaut_variateur",  "⚠ Défaut variateur"),
        ("defaut_pdc",        "⚠ Défaut PdC"),
        ("incoherence_vanne", "⚠ Vanne 3 voies incohérente"),
        ("dp_sature",         "⚠ ΔP saturé — Re au-delà de la plage "
                              "mesurable du capteur"),
        ("bas_re_motif",      "ℹ Stratégie bas-Re indisponible"),
        ("consigne_limitee",  "⚠ Consigne limitée au maximum mesurable"),
        ("alerte_table_ff",   "⚠ Table de gains à refaire"),
    ]

    def __init__(self, pilote: Pilote, mode: str, cfg: dict, chemin_cfg: Path):
        super().__init__()
        self.pilote     = pilote
        self.cfg        = cfg
        self.chemin_cfg = Path(chemin_cfg)
        self._hist_re    = deque(maxlen=HISTORIQUE_PTS)
        self._hist_cible = deque(maxlen=HISTORIQUE_PTS)
        self._action_log: deque[dict] = deque(maxlen=20)
        self._t0_acq: datetime | None = None
        self._mode = "asservi"   # "manuel" | "asservi" | "sequence"
        self._ref_re = "veine"      # référence de la consigne Re : conduite | veine (défaut: veine)
        self._echelle_re = 250.0    # échelle slider Re (sera recalculée selon référence)
        self._recording = False
        self._alertes_actives: set[str] = set()
        self._sections_en_defaut: set[str] = set()
        self._dirty: dict[str, bool] = {
            "re": False, "vman": False, "pdc": False,
        }

        # Limites physiques de la vanne PDC pour affichage
        act = cfg.get("actionneurs", {})
        self._pdc_umin = act.get("pdc_recopie_min_v", 0.0)
        self._pdc_umax = act.get("pdc_recopie_max_v", 10.0)

        # Validation des limites PDC
        if self._pdc_umax <= self._pdc_umin:
            log.warning(
                f"Configuration PDC invalide: Umax ({self._pdc_umax}V) <= Umin ({self._pdc_umin}V). "
                "L'affichage du pourcentage sera désactivé (toujours 0%)."
            )

        pg.setConfigOption("background", "#FFFFFF")
        pg.setConfigOption("foreground", COLORS["text"])
        pg.setConfigOptions(antialias=True)

        self.setWindowTitle(
            f"Pilotage banc ALPHA — Diaphragmes ISO / Miller  [{mode.upper()}]"
        )
        self.resize(1280, 760)
        self.setStyleSheet(_QSS)

        from core.apprentissage import Apprentissage
        self.apprentissage = Apprentissage(pilote, cfg, chemin_cfg)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header(mode))

        body = QWidget()
        bhl = QHBoxLayout(body)
        bhl.setContentsMargins(0, 0, 0, 0)
        bhl.setSpacing(0)
        bhl.addWidget(self._build_left_rail())

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_scope())      # 0
        self._stack.addWidget(self._build_voies())      # 1
        self._stack.addWidget(self._build_sequence())   # 2
        self._stack.addWidget(self._build_apprentissage())  # 3
        self._stack.addWidget(self._build_configuration())  # 4
        self._stack.addWidget(self._build_mise_en_service())  # 5
        bhl.addWidget(self._stack, 1)

        bhl.addWidget(self._build_right_rail())
        root.addWidget(body, 1)
        root.addWidget(self._build_footer(mode))

        self._nav(0)
        self._update_mode_seg()
        self._update_controls_enabled()

        # Initialiser l'échelle du slider Re selon la référence par défaut
        self._set_ref_re(self._ref_re)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(PERIODE_IHM_MS)

        # Initialisation des vannes au démarrage (position de repos)
        self._initialiser_vannes_demarrage()

    def _pdc_v_vers_pct(self, volts: float | None) -> int:
        """Convertit tension PDC [Umin, Umax] en pourcentage de FERMETURE [0%, 100%].

        Convention vanne perte de charge:
        - 0% = vanne ouverte (Umax = débit max)
        - 100% = vanne fermée (Umin = débit min)
        """
        if volts is None:
            return 0
        if self._pdc_umax <= self._pdc_umin:
            log.warning(
                f"Conversion PDC impossible: Umax ({self._pdc_umax}V) <= Umin ({self._pdc_umin}V). "
                "Retour 0% par défaut."
            )
            return 0
        # Inverser: 100% = fermée (Umin), 0% = ouverte (Umax)
        pct = (self._pdc_umax - volts) / (self._pdc_umax - self._pdc_umin) * 100
        return int(max(0, min(100, pct)))

    def closeEvent(self, event):
        """Fermeture par la croix, Alt-F4 ou fin de session.

        arreter() ne fait qu'arreter le thread et fermer la liaison : les
        sorties du coupleur gardent leur derniere valeur. Sans l'arret de
        regulation prealable, fermer la fenetre ventilateur en marche le
        laissait tourner, hors de toute supervision et sans plus aucun
        moyen de l'arreter depuis le logiciel.

        On ne remet pas les vannes en position de repos ici : c'est le role
        délibéré du bouton Quitter. La croix garantit le minimum, l'arret
        de la machine tournante."""
        self._timer.stop()
        try:
            self.pilote.arreter_regulation()   # 0 V + RUN retire, mode manuel coupe
        except Exception as e:                 # noqa: BLE001
            log.warning(f"Erreur arrêt régulation (closeEvent) : {e}")  # la fermeture ne doit jamais echouer
        self.pilote.arreter()
        event.accept()

    def _initialiser_vannes_demarrage(self) -> None:
        """Initialise les vannes en position de repos au démarrage :
        - Vanne 3 voies sur Miller (90°)
        - Bypass ouvert
        - Perte de charge ouverte (100%)

        Affiche un popup modal pendant le positionnement des vannes.
        Utilise QTimer.singleShot pour ne pas bloquer l'affichage initial.
        """
        # Retarder l'initialisation de 100ms pour laisser l'interface se dessiner
        QTimer.singleShot(100, self._lancer_initialisation_vannes)

    def _lancer_initialisation_vannes(self) -> None:
        """Lance effectivement l'initialisation des vannes (appelé par timer)."""
        # Créer le dialog d'attente
        self._dialog_init = QProgressDialog(
            "Initialisation des vannes en cours...\n\n"
            "• Vanne 3 voies → Miller (90°)\n"
            "• Bypass → Ouvert\n"
            "• Perte de charge → Ouverte",
            None,  # Pas de bouton Cancel
            0, 0,  # Range 0-0 = mode indéterminé (busy indicator)
            self
        )
        self._dialog_init.setWindowTitle("Initialisation du banc")
        self._dialog_init.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._dialog_init.setMinimumDuration(0)  # Afficher immédiatement
        self._dialog_init.setCancelButton(None)  # Pas de bouton annuler
        self._dialog_init.setAutoClose(False)
        self._dialog_init.setAutoReset(False)
        # Arrêter le timer si l'utilisateur ferme manuellement le dialog
        self._dialog_init.rejected.connect(self._annuler_init_vannes)
        self._dialog_init.show()

        # Vérifier que le pilote est bien démarré
        if not self.pilote._thread or not self.pilote._thread.is_alive():
            self._dialog_init.close()
            self._log("⚠ Pilote non démarré : initialisation annulée", COLORS["error"])
            return

        # Lancer les commandes de vannes
        try:
            self.pilote.commander_diaphragme("miller")
            self.pilote.commander_bypass(True)
            self.pilote.commander_vanne_perte_charge_pct(100.0)  # 100% ouverture (0% fermeture)
        except Exception as e:  # noqa: BLE001
            log.warning(f"Erreur initialisation vannes : {e}")
            self._dialog_init.close()
            return

        # Timer pour vérifier l'avancement et fermer le dialog
        self._temps_ecoule_init = 0.0
        # Timeout basé sur les temps de course réels + marge de sécurité
        act = self.cfg.get("actionneurs", {})
        t_max_vanne = max(
            act.get("vanne_3voies_temps_s", 11.0),
            act.get("vanne_bypass_temps_s", 11.0),
            act.get("vanne_perte_charge_course_s", 25.0)
        )
        self._timeout_max_init_s = t_max_vanne * 1.5  # Facteur 1.5 pour marge

        # Créer le timer de vérification
        self._timer_verif_init = QTimer(self)
        self._timer_verif_init.timeout.connect(self._verifier_vannes_init)
        self._timer_verif_init.start(200)  # Vérifier toutes les 200ms

    def _verifier_vannes_init(self) -> None:
        """Vérifie si les vannes sont en place et ferme le dialog."""
        self._temps_ecoule_init += 0.2  # Timer tick tous les 200ms

        # Lire l'état des vannes
        etat = self.pilote.etat

        # Vérifier si toutes les vannes sont en position
        miller_ok = etat.get("fc_3voies_90", False)
        bypass_ok = etat.get("fc_bypass_ouvert", False)
        # PDC doit être ouverte (fermeture <= 10%, soit >= 90% de la plage vers Umax)
        pdc_90pct_v = self._pdc_umin + 0.9 * (self._pdc_umax - self._pdc_umin)
        pdc_ok = etat.get("pdc_position", 0.0) >= pdc_90pct_v

        # Mettre à jour le message
        status = []
        status.append("✓ Vanne 3 voies → Miller" if miller_ok else "⏳ Vanne 3 voies...")
        status.append("✓ Bypass → Ouvert" if bypass_ok else "⏳ Bypass...")
        status.append("✓ Perte de charge → Ouverte" if pdc_ok else "⏳ Perte de charge...")

        self._dialog_init.setLabelText(
            "Initialisation des vannes en cours...\n\n" + "\n".join(status)
        )

        # Fermer si toutes les vannes sont en place ou timeout
        if (miller_ok and bypass_ok and pdc_ok) or self._temps_ecoule_init >= self._timeout_max_init_s:
            self._timer_verif_init.stop()
            self._dialog_init.close()
            if self._temps_ecoule_init >= self._timeout_max_init_s:
                self._log("⚠ Initialisation vannes : timeout atteint", COLORS["error"])
            else:
                self._log("✓ Vannes initialisées (position de repos)", COLORS["ok"])

    def _annuler_init_vannes(self) -> None:
        """Arrête le timer d'initialisation si le dialog est fermé manuellement."""
        if hasattr(self, '_timer_verif_init') and self._timer_verif_init.isActive():
            self._timer_verif_init.stop()
            self._log("Initialisation vannes annulée", COLORS["warning"])

    # ══════════════════════════════════════════════════════════════ HEADER

    def _build_header(self, mode: str) -> QWidget:
        bar = QWidget()
        bar.setObjectName("appheader")
        bar.setFixedHeight(56)
        hl = QHBoxLayout(bar)
        hl.setContentsMargins(18, 0, 18, 0)
        hl.setSpacing(0)

        # Logo IMT
        if _LOGO_PATH.exists():
            lbl_logo = QLabel()
            # Charger l'image haute résolution et redimensionner avec qualité
            px = QPixmap(str(_LOGO_PATH))
            # Redimensionner à une hauteur légèrement inférieure pour laisser du padding
            px = px.scaledToHeight(48, Qt.TransformationMode.SmoothTransformation)
            lbl_logo.setPixmap(px)
            lbl_logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl_logo.setStyleSheet("background:transparent; padding: 4px 0px;")
            hl.addWidget(lbl_logo)
        else:
            lbl_logo = QLabel("IMT Nord Europe")
            lbl_logo.setStyleSheet("color:#FFFFFF; font-weight:700; background:transparent;")
            hl.addWidget(lbl_logo)

        # Titre
        hl.addSpacing(18)
        lbl_titre = QLabel("ALPHA V1.0")
        lbl_titre.setStyleSheet(
            "color:#FFFFFF; font-size:15px; font-weight:700;"
            " letter-spacing:.1em; background:transparent;")
        hl.addWidget(lbl_titre)

        # Séparateur
        sep = QFrame()
        sep.setObjectName("header-sep")
        sep.setFixedSize(1, 28)
        sep.setStyleSheet("background:rgba(255,255,255,.12);")
        hl.addSpacing(18)
        hl.addWidget(sep)
        hl.addSpacing(18)

        # Nav
        self._nav_btns: list[QPushButton] = []
        for i, label in enumerate(_NAV_LABELS):
            btn = QPushButton(label)
            btn.setObjectName("nav-btn")
            btn.setFixedHeight(56)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, idx=i: self._nav(idx))
            self._nav_btns.append(btn)
            hl.addWidget(btn)

        hl.addStretch()

        # Connexion
        self._lbl_cnx_dot = QLabel("●")
        self._lbl_cnx_dot.setObjectName("foot-cnx")
        self._lbl_cnx_dot.setStyleSheet(
            f"color:{COLORS['ok']}; font-size:10px; background:transparent;")
        hl.addWidget(self._lbl_cnx_dot)
        hl.addSpacing(6)
        lbl_cnx = QLabel("Wago 750-362")
        lbl_cnx.setStyleSheet(
            "color:#9DB2C4; font-size:11px; "
            "font-family:'IBM Plex Mono',monospace; background:transparent;")
        hl.addWidget(lbl_cnx)
        hl.addSpacing(14)

        # Badge mode
        is_simu = mode.lower() == "simulation"
        badge = QLabel(mode.upper())
        badge.setObjectName("badge-simu" if is_simu else "badge-live")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        badge.setFixedHeight(28)
        hl.addWidget(badge)

        return bar

    PAGE_MAINTENANCE = 5

    def _nav(self, idx: int) -> None:
        precedent = self._stack.currentIndex()
        self._stack.setCurrentIndex(idx)

        if idx == self.PAGE_MAINTENANCE and precedent != self.PAGE_MAINTENANCE:
            self._mes_reg_charger()

        # Quitter la page de maintenance rend la main a la boucle. Ses
        # commandes de vannes suspendent le maintien des ordres TOR ; sans
        # ce retablissement il restait suspendu indefiniment, et sur carte
        # GF3 un ordre non reaffirme laisse l'actionneur hors tension, donc
        # sans resistance anti-condensation. On ne touche a rien si une
        # sequence d'apprentissage tourne : c'est elle qui a la main.
        if (precedent == self.PAGE_MAINTENANCE
                and idx != self.PAGE_MAINTENANCE
                and not self.apprentissage.etat["en_cours"]):
            self.pilote.reprendre_maintien_tor()
        for i, btn in enumerate(self._nav_btns):
            name = "nav-btn-active" if i == idx else "nav-btn"
            if btn.objectName() != name:
                btn.setObjectName(name)
                btn.setStyle(btn.style())

    # ══════════════════════════════════════════════════════════ LEFT RAIL

    def _build_left_rail(self) -> QWidget:
        rail = QWidget()
        rail.setObjectName("left-rail")
        rail.setFixedWidth(262)
        vl = QVBoxLayout(rail)
        vl.setContentsMargins(16, 14, 16, 14)
        vl.setSpacing(0)

        # ── CALCULS — TEMPS RÉEL ───────────────────────────────
        hdr1 = QWidget()
        h1 = QHBoxLayout(hdr1)
        h1.setContentsMargins(0, 0, 0, 8)
        lbl_c = QLabel("🎓 CALCULS — TEMPS RÉEL")
        lbl_c.setObjectName("section-hdr")
        h1.addWidget(lbl_c)
        h1.addStretch()
        lbl_c_count = QLabel(str(len(_CALCULS)))
        lbl_c_count.setStyleSheet(
            "font-size:10px; font-family:'IBM Plex Mono',monospace;"
            f" color: #14223C; background:transparent;")
        h1.addWidget(lbl_c_count)
        vl.addWidget(hdr1)

        self._ch_rows: list[tuple] = []

        # Afficher les calculs (Qv, ρ, μ, ν)
        for key, name, max_val, fmt, color in _CALCULS:
            w = QWidget()
            wvl = QVBoxLayout(w)
            wvl.setContentsMargins(0, 11, 0, 11)
            wvl.setSpacing(7)
            w.setStyleSheet("border-bottom:1px solid #E9EEF1;")

            # name | value
            top = QWidget()
            top.setStyleSheet("border:none;")
            thl = QHBoxLayout(top)
            thl.setContentsMargins(0, 0, 0, 0)
            thl.setSpacing(8)
            ln = QLabel(name)
            ln.setObjectName("ch-name")
            thl.addWidget(ln, 1)
            lv = QLabel("—")
            lv.setObjectName("ch-val")
            lv.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            thl.addWidget(lv)
            wvl.addWidget(top)

            # progress bar
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setValue(0)
            bar.setTextVisible(False)
            bar.setStyleSheet(
                f"QProgressBar {{ background:rgba(20,34,60,.09); border:none;"
                f" border-radius:3px; max-height:6px; min-height:6px; }}"
                f"QProgressBar::chunk {{ background:{color}; border-radius:3px; }}")
            wvl.addWidget(bar)
            vl.addWidget(w)
            self._ch_rows.append((key, lv, bar, fmt, max_val))

        # ── PRESSIONS — TEMPS RÉEL ─────────────────────────────
        hdr_p = QWidget()
        h_p = QHBoxLayout(hdr_p)
        h_p.setContentsMargins(0, 32, 0, 8)  # Marge haut augmentée pour espacement
        lbl_p = QLabel("💨 PRESSIONS — TEMPS RÉEL")
        lbl_p.setObjectName("section-hdr")
        h_p.addWidget(lbl_p)
        h_p.addStretch()
        lbl_p_count = QLabel(str(len(_PRESSIONS)))
        lbl_p_count.setStyleSheet(
            "font-size:10px; font-family:'IBM Plex Mono',monospace;"
            f" color: #14223C; background:transparent;")
        h_p.addWidget(lbl_p_count)
        vl.addWidget(hdr_p)

        # Afficher les pressions (P amont, ΔP ISO, ΔP Miller)
        for key, name, max_val, fmt, color in _PRESSIONS:
            w = QWidget()
            wvl = QVBoxLayout(w)
            wvl.setContentsMargins(0, 11, 0, 11)
            wvl.setSpacing(7)
            w.setStyleSheet("border-bottom:1px solid #E9EEF1;")

            # name | value
            top = QWidget()
            top.setStyleSheet("border:none;")
            thl = QHBoxLayout(top)
            thl.setContentsMargins(0, 0, 0, 0)
            thl.setSpacing(8)
            ln = QLabel(name)
            ln.setObjectName("ch-name")
            thl.addWidget(ln, 1)
            lv = QLabel("—")
            lv.setObjectName("ch-val")
            lv.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            thl.addWidget(lv)
            wvl.addWidget(top)

            # progress bar
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setValue(0)
            bar.setTextVisible(False)
            bar.setStyleSheet(
                f"QProgressBar {{ background:rgba(20,34,60,.09); border:none;"
                f" border-radius:3px; max-height:6px; min-height:6px; }}"
                f"QProgressBar::chunk {{ background:{color}; border-radius:3px; }}")
            wvl.addWidget(bar)
            vl.addWidget(w)
            self._ch_rows.append((key, lv, bar, fmt, max_val))

        # ── ACTIONNEURS ────────────────────────────────────────
        hdr2 = QWidget()
        h2 = QHBoxLayout(hdr2)
        h2.setContentsMargins(0, 32, 0, 8)  # Marge haut augmentée pour espacement
        la = QLabel("⚡ ACTIONNEURS — ÉTAT")
        la.setObjectName("section-hdr")
        h2.addWidget(la)
        h2.addStretch()
        la4 = QLabel("4")
        la4.setStyleSheet(
            "font-size:10px; font-family:'IBM Plex Mono',monospace;"
            f" color:#6B7E8E; background:transparent;")
        h2.addWidget(la4)
        vl.addWidget(hdr2)

        self._act_dots:  dict[str, QLabel] = {}
        self._act_vals:  dict[str, QLabel] = {}
        self._act_subs:  dict[str, QLabel] = {}
        self._act_bars:  dict[str, QProgressBar] = {}

        for act_key, act_name, dot_color, _, max_v in _ACTUATORS:
            w = QWidget()
            wvl = QVBoxLayout(w)
            wvl.setContentsMargins(0, 11, 0, 11)
            wvl.setSpacing(6)
            w.setStyleSheet("border-bottom:1px solid #E9EEF1;")

            # dot + name | value
            top = QWidget()
            top.setStyleSheet("border:none;")
            thl = QHBoxLayout(top)
            thl.setContentsMargins(0, 0, 0, 0)
            thl.setSpacing(8)
            dot = QLabel("●")
            dot.setObjectName("act-dot")
            dot.setFixedWidth(10)
            dot.setStyleSheet(f"color:#DBE5EA; font-size:10px; background:transparent;")
            self._act_dots[act_key] = dot
            thl.addWidget(dot)
            ln = QLabel(act_name)
            ln.setObjectName("ch-name")
            thl.addWidget(ln, 1)
            lv = QLabel("—")
            lv.setObjectName("act-val")
            lv.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._act_vals[act_key] = lv
            thl.addWidget(lv)
            wvl.addWidget(top)

            # bar + sub-label
            bot = QWidget()
            bot.setStyleSheet("border:none;")
            bhl = QHBoxLayout(bot)
            bhl.setContentsMargins(18, 0, 0, 0)
            bhl.setSpacing(8)
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setValue(0)
            bar.setTextVisible(False)
            bar.setStyleSheet(
                f"QProgressBar {{ background:rgba(20,34,60,.09); border:none;"
                f" border-radius:3px; max-height:5px; min-height:5px; }}"
                f"QProgressBar::chunk {{ background:{dot_color}; border-radius:3px; }}")
            self._act_bars[act_key] = bar
            bhl.addWidget(bar, 1)
            sub = QLabel("—")
            sub.setObjectName("act-sub")
            sub.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            sub.setFixedWidth(74)
            self._act_subs[act_key] = sub
            bhl.addWidget(sub)
            wvl.addWidget(bot)
            vl.addWidget(w)

        vl.addStretch()
        return rail

    # ══════════════════════════════════════════════════════════ RIGHT RAIL

    def _seg(self, labels: list[str]) -> tuple[QWidget, list[QPushButton]]:
        """Segmented pill control."""
        box = QWidget()
        box.setObjectName("seg-box")
        hl = QHBoxLayout(box)
        hl.setContentsMargins(4, 4, 4, 4)
        hl.setSpacing(4)
        btns = []
        for lbl in labels:
            btn = QPushButton(lbl)
            btn.setStyleSheet(SEG_OFF_QSS)
            hl.addWidget(btn)
            btns.append(btn)
        return box, btns

    def _seg_set(self, btns: list[QPushButton], idx: int) -> None:
        for i, btn in enumerate(btns):
            btn.setStyleSheet(SEG_ON_QSS if i == idx else SEG_OFF_QSS)

    def _build_right_rail(self) -> QWidget:
        rail = QWidget()
        rail.setObjectName("right-rail")
        rail.setFixedWidth(344)

        outer = QVBoxLayout(rail)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── contenu scrollable ─────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        scroll.setStyleSheet("QScrollArea { background:#FFFFFF; border:none; }")
        scroll.verticalScrollBar().setStyleSheet(
            "QScrollBar:vertical { background:#E8EEF1; width:8px; border-radius:4px; margin:0; }"
            "QScrollBar::handle:vertical { background:#BCC8D0; border-radius:4px; min-height:24px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }"
        )

        content = QWidget()
        content.setStyleSheet("background:#FFFFFF;")
        content.setMinimumHeight(1000)
        vl = QVBoxLayout(content)
        vl.setContentsMargins(16, 16, 16, 8)
        vl.setSpacing(14)

        # header PILOTAGE + mode badge
        ph = QWidget()
        phl = QHBoxLayout(ph)
        phl.setContentsMargins(0, 0, 0, 0)
        lbl_p = QLabel("PILOTAGE")
        lbl_p.setObjectName("section-hdr")
        phl.addWidget(lbl_p)
        phl.addStretch()
        self._lbl_mode_badge = QLabel(f"mode {self._mode}")
        self._lbl_mode_badge.setStyleSheet(
            "font-size:10px; font-family:'IBM Plex Mono',monospace;"
            f" color:{COLORS['accentText']}; background:transparent;")
        phl.addWidget(self._lbl_mode_badge)
        vl.addWidget(ph)

        # Re display card
        re_card = QWidget()
        re_card.setObjectName("re-card")
        rcl = QVBoxLayout(re_card)
        rcl.setContentsMargins(16, 16, 16, 16)
        rcl.setSpacing(9)
        # header row
        rch = QWidget()
        rch.setStyleSheet("background:transparent;")
        rchl = QHBoxLayout(rch)
        rchl.setContentsMargins(0, 0, 0, 0)
        lbl_re_hdr = QLabel("REYNOLDS Re — MESURÉ")
        lbl_re_hdr.setObjectName("re-hdr")
        rchl.addWidget(lbl_re_hdr)
        rchl.addStretch()
        self._lbl_re_state = QLabel("ARRÊT")
        self._lbl_re_state.setObjectName("re-state")
        self._lbl_re_state.setStyleSheet(
            "font-size:10px; font-weight:700; padding:3px 9px;"
            " border-radius:5px; background:#DBE5EA; color:#6B7E8E;")
        rchl.addWidget(self._lbl_re_state)
        rcl.addWidget(rch)
        # value row
        rcv = QWidget()
        rcv.setStyleSheet("background:transparent;")
        rcvl = QHBoxLayout(rcv)
        rcvl.setContentsMargins(0, 0, 0, 0)
        rcvl.setSpacing(9)
        self._lbl_re_big = QLabel("—")
        self._lbl_re_big.setObjectName("re-big")
        rcvl.addWidget(self._lbl_re_big)
        rcvl.addStretch()
        re_u = QLabel("Re")
        re_u.setObjectName("re-unit")
        re_u.setAlignment(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft)
        rcvl.addWidget(re_u)
        rcl.addWidget(rcv)

        # dispersion et durée du palier : l'opérateur part mesurer sur le banc,
        # il lui faut de quoi chiffrer l'incertitude du point, pas seulement
        # un voyant binaire
        self._lbl_re_sub = QLabel("")
        self._lbl_re_sub.setStyleSheet(
            "font-family:'IBM Plex Mono',monospace; font-size:10.5px;"
            f" color:{COLORS['muted2']}; background:transparent;")
        rcl.addWidget(self._lbl_re_sub)
        vl.addWidget(re_card)

        # Mode segmenté (Manuel | Asservi | Séquence)
        mode_box, self._mode_btns = self._seg(["Manuel", "Asservi", "Séquence"])
        self._mode_btns[0].clicked.connect(lambda: self._set_mode("manuel"))
        self._mode_btns[1].clicked.connect(lambda: self._set_mode("asservi"))
        self._mode_btns[2].clicked.connect(lambda: self._set_mode("sequence"))
        vl.addWidget(mode_box)

        # Consigne Reynolds
        vl.addWidget(self._ctrl_slider_block(
            label="Consigne Reynolds",
            dirty_key="re",
            unit_lbl="Re",
            slider_min=0, slider_max=100,  # *250 → 0-25 000
            default_slider=60,             # → 15 000 par défaut
            inp_width=78,
            on_slider=self._on_re_slider,
            on_input=self._on_re_input,
            attr_slider="_slider_re",
            attr_inp="inp_re",
            sub_lbl=None,
        ))

        # Référence du Reynolds : conduite Ø81,4 ou veine d'essai
        ref_outer = QWidget()
        ref_outer.setStyleSheet("background:transparent;")
        rvl = QVBoxLayout(ref_outer)
        rvl.setContentsMargins(0, 0, 0, 0)
        rvl.setSpacing(7)
        lref = QLabel("Référence Re")
        lref.setObjectName("ctrl-lbl")
        rvl.addWidget(lref)
        ref_seg, self._ref_btns = self._seg(["Conduite", "Veine"])
        self._ref_btns[0].clicked.connect(lambda: self._set_ref_re("conduite"))
        self._ref_btns[1].clicked.connect(lambda: self._set_ref_re("veine"))
        self._seg_set(self._ref_btns, 1)  # Défaut: Veine (index 1)
        rvl.addWidget(ref_seg)
        vl.addWidget(ref_outer)

        # Tension variateur (manuel)
        vl.addWidget(self._ctrl_slider_block(
            label="Tension variateur (manuel)",
            dirty_key="vman",
            unit_lbl="V",
            slider_min=0, slider_max=100,   # /10 → 0-10 V
            default_slider=0,
            inp_width=62,
            on_slider=self._on_vman_slider,
            on_input=self._on_vman_input,
            attr_slider="_slider_u",
            attr_inp="_inp_vman",
            sub_lbl=None,
        ))

        # CONSIGNES VANNES
        lbl_ca = QLabel("CONSIGNES VANNES")
        lbl_ca.setObjectName("section-hdr")
        vl.addWidget(lbl_ca)

        # Vanne PdC
        pdc_block = self._ctrl_slider_block(
            label="Vanne Perte de charge",
            dirty_key="pdc",
            unit_lbl="% ferm.",  # % fermeture (0=ouverte, 100=fermée)
            slider_min=0, slider_max=100,
            default_slider=0,  # Défaut: ouverte (0% fermeture)
            inp_width=56,
            on_slider=self._on_pdc_slider,
            on_input=self._on_pdc_input,
            attr_slider="_slider_pdc",
            attr_inp="_inp_pdc",
            sub_lbl="_lbl_pdc_v",
        )
        vl.addWidget(pdc_block)

        # Vanne Diaphragme (Fermée | ISO | Miller)
        diaph_outer = QWidget()
        diaph_outer.setStyleSheet("background:transparent;")
        dvl = QVBoxLayout(diaph_outer)
        dvl.setContentsMargins(0, 0, 0, 0)
        dvl.setSpacing(7)
        dh = QWidget()
        dh.setStyleSheet("background:transparent;")
        dhl = QHBoxLayout(dh)
        dhl.setContentsMargins(0, 0, 0, 0)
        dhl.setSpacing(6)
        ld = QLabel("Vanne Diaphragme")
        ld.setObjectName("ctrl-lbl")
        dhl.addWidget(ld)
        self._dot_diaph = QLabel("●")
        self._dot_diaph.setObjectName("dirty-dot")
        self._dot_diaph.setVisible(False)
        dhl.addWidget(self._dot_diaph)
        dhl.addStretch()
        dvl.addWidget(dh)
        diaph_seg, self._diaph_btns = self._seg(["ISO", "Miller"])
        self._diaph_btns[0].clicked.connect(
            lambda: self._cmd_diaph("iso"))
        self._diaph_btns[1].clicked.connect(
            lambda: self._cmd_diaph("miller"))
        dvl.addWidget(diaph_seg)
        vl.addWidget(diaph_outer)

        # Vanne By-pass (Ouvert | Fermée)
        bp_outer = QWidget()
        bp_outer.setStyleSheet("background:transparent;")
        bvl = QVBoxLayout(bp_outer)
        bvl.setContentsMargins(0, 0, 0, 0)
        bvl.setSpacing(7)
        bh = QWidget()
        bh.setStyleSheet("background:transparent;")
        bhl2 = QHBoxLayout(bh)
        bhl2.setContentsMargins(0, 0, 0, 0)
        bhl2.setSpacing(6)
        lb = QLabel("Vanne By-pass")
        lb.setObjectName("ctrl-lbl")
        bhl2.addWidget(lb)
        self._dot_bypass = QLabel("●")
        self._dot_bypass.setObjectName("dirty-dot")
        self._dot_bypass.setVisible(False)
        bhl2.addWidget(self._dot_bypass)
        bhl2.addStretch()
        bvl.addWidget(bh)
        bp_seg, self._bypass_btns = self._seg(["Ouvert", "Fermée"])
        self._bypass_btns[0].clicked.connect(lambda: self._cmd_bypass(True))
        self._bypass_btns[1].clicked.connect(lambda: self._cmd_bypass(False))
        bvl.addWidget(bp_seg)
        vl.addWidget(bp_outer)

        # Journal d'action
        vl.addWidget(self._build_action_log())

        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        # ── transport bar (sticky bottom) ──────────────────────
        transport = QWidget()
        transport.setObjectName("transport")
        tvl = QVBoxLayout(transport)
        tvl.setContentsMargins(16, 13, 16, 16)
        tvl.setSpacing(10)

        btn_row = QWidget()
        btn_row.setStyleSheet("background:transparent;")
        brhl = QHBoxLayout(btn_row)
        brhl.setContentsMargins(0, 0, 0, 0)
        brhl.setSpacing(10)
        self._btn_apply = QPushButton("✓  APPLIQUER")
        self._btn_apply.setObjectName("apply-btn")
        self._btn_apply.setStyleSheet(
            f"QPushButton{{background:{COLORS['accent']};color:#FFFFFF;font-size:13px;font-weight:700;"
            f"letter-spacing:.03em;border:2px solid transparent;border-radius:10px;padding:11px 0;min-width:0;}}"
            f"QPushButton:hover{{background:#00A5C9;}}"
            f"QPushButton:pressed{{background:#FFFFFF;color:{COLORS['accent']};border:2px solid {COLORS['accent']};}}"
            f"QPushButton:disabled{{background:#A8DFF0;color:#FFFFFF;border:none;}}"
        )
        self._btn_apply.clicked.connect(self._appliquer)
        brhl.addWidget(self._btn_apply, 1)
        tvl.addWidget(btn_row)

        btn_estop = QPushButton("✕  ARRÊT VARIATEUR")
        btn_estop.setObjectName("estop")
        btn_estop.clicked.connect(self._arreter)
        tvl.addWidget(btn_estop)

        btn_quit = QPushButton("⏻  QUITTER")
        btn_quit.setStyleSheet(
            f"QPushButton{{background:{COLORS['text']};color:#FFFFFF;font-size:12.5px;font-weight:700;"
            f"letter-spacing:.03em;border:2px solid transparent;border-radius:10px;padding:11px 0;min-width:0;}}"
            f"QPushButton:hover{{background:#1E3050;}}"
            f"QPushButton:pressed{{background:#FFFFFF;color:{COLORS['text']};border:2px solid {COLORS['text']};}}"
        )
        btn_quit.clicked.connect(self._quitter)
        tvl.addWidget(btn_quit)
        outer.addWidget(transport)

        return rail

    def _ctrl_slider_block(
        self, label, dirty_key, unit_lbl,
        slider_min, slider_max, default_slider, inp_width,
        on_slider, on_input,
        attr_slider, attr_inp, sub_lbl,
    ) -> QWidget:
        """Bloc label+dirty / input+unit / slider [/ sous-label]."""
        block = QWidget()
        block.setStyleSheet("background:transparent;")
        bvl = QVBoxLayout(block)
        bvl.setContentsMargins(0, 0, 0, 0)
        bvl.setSpacing(0)

        # Row 1: label + dirty | input + unit
        row1 = QWidget()
        row1.setStyleSheet("background:transparent;")
        rhl = QHBoxLayout(row1)
        rhl.setContentsMargins(0, 0, 0, 7)
        rhl.setSpacing(6)
        lbl = QLabel(label)
        lbl.setObjectName("ctrl-lbl")
        rhl.addWidget(lbl)
        dot = QLabel("●")
        dot.setObjectName("dirty-dot")
        dot.setVisible(False)
        setattr(self, f"_dot_{dirty_key}", dot)
        rhl.addWidget(dot)
        rhl.addStretch()
        _default_val = (default_slider * 250 if dirty_key == "re"
                        else (default_slider / 10.0 if dirty_key == "vman"
                              else default_slider))
        inp = QLineEdit(str(int(_default_val) if dirty_key == "re"
                            else f"{_default_val:.1f}" if dirty_key == "vman"
                            else str(int(_default_val))))
        inp.setObjectName("num-input")
        inp.setFixedWidth(inp_width)
        inp.setAlignment(Qt.AlignmentFlag.AlignRight)
        inp.textEdited.connect(on_input)
        setattr(self, attr_inp, inp)
        rhl.addWidget(inp)
        u = QLabel(unit_lbl)
        u.setObjectName("ctrl-unit")
        rhl.addWidget(u)
        bvl.addWidget(row1)

        # Row 2: slider
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(slider_min, slider_max)
        slider.setValue(default_slider)
        slider.valueChanged.connect(on_slider)
        setattr(self, attr_slider, slider)
        bvl.addWidget(slider)

        # Row 3: optional sub-label
        if sub_lbl:
            sl = QLabel("≙ 10.0 V (0–10 V)")
            sl.setObjectName("ctrl-sub")
            setattr(self, sub_lbl, sl)
            bvl.addWidget(sl)

        return block

    def _build_action_log(self) -> QWidget:
        outer = QWidget()
        outer.setStyleSheet("background:transparent;")
        vl = QVBoxLayout(outer)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        hdr = QWidget()
        hdr.setStyleSheet("background:transparent;")
        hhl = QHBoxLayout(hdr)
        hhl.setContentsMargins(0, 0, 0, 6)
        la = QLabel("JOURNAL D'ACTION")
        la.setObjectName("section-hdr")
        hhl.addWidget(la)
        hhl.addStretch()
        ls = QLabel("actionneurs")
        ls.setStyleSheet(
            "font-size:9.5px; font-family:'IBM Plex Mono',monospace;"
            " color:#A4B3BF; background:transparent;")
        hhl.addWidget(ls)
        vl.addWidget(hdr)

        self._log_rows: list[tuple] = []
        for _ in range(5):
            row = QWidget()
            row.setStyleSheet(
                "background:transparent; border-bottom:1px solid #E9EEF1;")
            rhl = QHBoxLayout(row)
            rhl.setContentsMargins(0, 6, 0, 6)
            rhl.setSpacing(9)
            t_lbl = QLabel("—")
            t_lbl.setObjectName("log-t")
            t_lbl.setFixedWidth(52)
            rhl.addWidget(t_lbl)
            dot_lbl = QLabel("●")
            dot_lbl.setObjectName("log-dot")
            dot_lbl.setStyleSheet("color:#DBE5EA; font-size:9px; background:transparent;")
            dot_lbl.setFixedWidth(10)
            rhl.addWidget(dot_lbl)
            msg_lbl = QLabel("")
            msg_lbl.setObjectName("log-msg")
            msg_lbl.setWordWrap(True)
            rhl.addWidget(msg_lbl, 1)
            vl.addWidget(row)
            self._log_rows.append((t_lbl, dot_lbl, msg_lbl))

        return outer

    # ══════════════════════════════════════════════════════════════ FOOTER


    # ═══════════════════════════════════════════════════ MAINTENANCE

    def _mes_carte(self, titre: str, sous_titre: str = "") -> tuple:
        """Carte de la page de maintenance. Retourne (widget, layout)."""
        carte = QWidget()
        carte.setObjectName("cv-panel")
        vl = QVBoxLayout(carte)
        vl.setContentsMargins(16, 13, 16, 14)
        vl.setSpacing(8)
        t = QLabel(titre)
        t.setStyleSheet(
            "font-size:11px; font-weight:700; letter-spacing:.09em;"
            f" color:{COLORS['text']}; background:transparent;")
        vl.addWidget(t)
        if sous_titre:
            st = QLabel(sous_titre)
            st.setWordWrap(True)
            st.setStyleSheet(
                f"font-size:11px; color:{COLORS['muted2']};"
                " background:transparent;")
            vl.addWidget(st)
        return carte, vl

    def _mes_bouton(self, libelle: str, slot) -> QPushButton:
        b = QPushButton(libelle)
        b.setMinimumHeight(34)
        b.clicked.connect(slot)
        self._mes_actionneurs.append(b)
        return b

    def _build_mise_en_service(self) -> QWidget:
        """Diagnostic organe par organe, pour le cablage et apres un defaut.

        Ne caracterise rien : les chronos, zeros et gains restent du ressort
        de l'onglet Apprentissage. Ici on isole -- un organe, un bouton, une
        reponse immediate. Tout ce qui se lit sans rien bouger est affiche en
        permanence ; les commandes passent par les methodes publiques du
        pilote (jamais d'ecriture directe es.*) et sont verrouillees tant que
        la regulation tourne.
        """
        page = QWidget()
        exte = QVBoxLayout(page)
        exte.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        exte.addWidget(scroll)
        hote = QWidget()
        scroll.setWidget(hote)
        vl = QVBoxLayout(hote)
        vl.setContentsMargins(16, 16, 16, 24)
        vl.setSpacing(12)

        self._mes_actionneurs: list = []
        self._mes_run_arme = False
        self._mes_lbl: dict[str, QLabel] = {}

        def val(cle: str, largeur: int = 0) -> QLabel:
            l = QLabel("—")
            l.setObjectName("mono")
            if largeur:
                l.setMinimumWidth(largeur)
            self._mes_lbl[cle] = l
            return l

        # bandeau de verrouillage
        self._mes_lbl_verrou = QLabel("")
        self._mes_lbl_verrou.setWordWrap(True)
        self._mes_lbl_verrou.setStyleSheet(
            f"background:#FFF3E0; color:{COLORS['warn']};"
            " font-size:11.5px; font-weight:600; padding:9px 13px;"
            " border-radius:6px;")
        self._mes_lbl_verrou.setVisible(False)
        vl.addWidget(self._mes_lbl_verrou)

        # ── 1. image process ──────────────────────────────────────────
        c, cl = self._mes_carte(
            "ADRESSAGE MODBUS",
            "Registres bruts tels que le coupleur les presente, en regard du "
            "nom attendu par config.json. Une voie mal adressee donne souvent "
            "une valeur plausible : c'est le brut qui trahit le decalage. "
            "Lecture seule, rien n'est commande.")
        self._mes_tbl_mots = QLabel("—")
        self._mes_tbl_mots.setObjectName("mono")
        self._mes_tbl_mots.setTextFormat(Qt.TextFormat.RichText)
        cl.addWidget(self._mes_tbl_mots)
        cl.addWidget(_hsep())
        self._mes_tbl_bits = QLabel("—")
        self._mes_tbl_bits.setObjectName("mono")
        self._mes_tbl_bits.setTextFormat(Qt.TextFormat.RichText)
        cl.addWidget(self._mes_tbl_bits)
        vl.addWidget(c)

        # ── 2. PT100 ──────────────────────────────────────────────────
        c, cl = self._mes_carte(
            "PT100 — temperature",
            "rho et mu en dependent, donc le Reynolds : 1 K d'erreur vaut "
            "0,42 % sur le Re. Le coupleur Modbus n'a pas acces aux octets "
            "de statut du 750-450 ; la vraisemblance de la valeur est la "
            "seule detection possible.")
        h = QHBoxLayout(); h.setSpacing(18)
        h.addWidget(val("pt100_valeur", 110))
        h.addWidget(val("pt100_verdict"))
        h.addStretch()
        cl.addLayout(h)
        # Paramètres calculés (ρ, μ)
        h2 = QHBoxLayout(); h2.setSpacing(18)
        lbl_rho = QLabel("ρ (air) :")
        lbl_rho.setStyleSheet("font-size:12px; color:#3A4D60;")
        h2.addWidget(lbl_rho)
        h2.addWidget(val("pt100_rho", 110))
        lbl_mu = QLabel("μ (air) :")
        lbl_mu.setStyleSheet("font-size:12px; color:#3A4D60;")
        h2.addWidget(lbl_mu)
        h2.addWidget(val("pt100_mu", 110))
        h2.addStretch()
        cl.addLayout(h2)
        vl.addWidget(c)

        # ── 3. FCO432 ─────────────────────────────────────────────────
        c, cl = self._mes_carte(
            "FCO432 — les 3 voies de pression",
            "Souffler doucement sur un piquage doit faire reagir une voie et "
            "une seule : c'est ainsi qu'on identifie laquelle est laquelle. "
            "Le zero se releve soufflerie a l'arret.")
        for cle, nom in (("dp_iso_500", "dP diaphragme ISO"),
                         ("dp_diaph_500", "dP diaphragme Miller"),
                         ("p_amont_500", "P amont")):
            h = QHBoxLayout(); h.setSpacing(12)
            n = QLabel(nom)
            n.setMinimumWidth(190)
            n.setStyleSheet("background:transparent;")
            h.addWidget(n)
            h.addWidget(val("fco_" + cle, 150))
            h.addWidget(val("fcoz_" + cle))
            h.addStretch()
            cl.addLayout(h)
        cl.addWidget(self._mes_bouton(
            "Relever le zero des capteurs (soufflerie arretee)",
            lambda: self._lancer_test("zero_capteurs")))
        vl.addWidget(c)

        # ── 4. vanne 3 voies ──────────────────────────────────────────
        c, cl = self._mes_carte(
            "VANNE 3 VOIES — carte GF3",
            "Exactement une des trois butees doit etre vraie. Toute autre "
            "combinaison signale une rupture de fil ou un 24 V commun absent. "
            "Premiere mise sous tension : commander F d'abord — la vanne "
            "sortant d'usine a 0°, elle ne doit PAS bouger, ce qui valide le "
            "relais sans aucun mouvement.")
        h = QHBoxLayout(); h.setSpacing(10)
        h.addWidget(self._mes_bouton(
            "Ordre F → 0° (ISO)", lambda: self._mes_diaphragme("iso")))
        h.addWidget(self._mes_bouton(
            "Ordre I → 90° (Miller)", lambda: self._mes_diaphragme("miller")))
        h.addStretch()
        cl.addLayout(h)
        cl.addWidget(val("v3_etat"))
        vl.addWidget(c)

        # ── 5. bypass ─────────────────────────────────────────────────
        c, cl = self._mes_carte(
            "VANNE BYPASS",
            "Les deux fins de course doivent commuter, et aucune ne doit "
            "rester active pendant la course.")
        h = QHBoxLayout(); h.setSpacing(10)
        h.addWidget(self._mes_bouton("Ouvrir",
                                     lambda: self._mes_bypass(True)))
        h.addWidget(self._mes_bouton("Fermer",
                                     lambda: self._mes_bypass(False)))
        h.addStretch()
        cl.addLayout(h)
        cl.addWidget(val("bypass_etat"))
        vl.addWidget(c)

        # ── 6. vanne perte de charge ──────────────────────────────────
        c, cl = self._mes_carte(
            "VANNE PERTE DE CHARGE — carte POSI P6",
            "Sens usine : 0 V = fermee, 10 V = grande ouverte. La recopie "
            "doit suivre la consigne ; un ecart durable revele une carte en "
            "sens inverse ou une recopie non cablee.")
        h = QHBoxLayout(); h.setSpacing(10)
        h.addWidget(self._mes_bouton("0 V — fermer",
                                     lambda: self._mes_pdc(0.0)))
        h.addWidget(self._mes_bouton("5 V — mi-course",
                                     lambda: self._mes_pdc(5.0)))
        h.addWidget(self._mes_bouton("10 V — ouvrir",
                                     lambda: self._mes_pdc(10.0)))
        h.addStretch()
        cl.addLayout(h)
        cl.addWidget(val("pdc_etat"))
        vl.addWidget(c)

        # ── 7. variateur ──────────────────────────────────────────────
        c, cl = self._mes_carte(
            "VARIATEUR 3G3MV + moteur SD42",
            "Le retour « consigne atteinte » vient de la sortie moniteur AM "
            "(image 0-10 V de la frequence reelle), la DI « frequence "
            "atteinte » n'etant pas cablee. Verifier n004 = 2 et "
            "n011 = 50,0 Hz avant tout essai.")
        h = QHBoxLayout(); h.setSpacing(10)
        h.addWidget(self._mes_bouton("RUN", self._mes_run))
        b_stop = QPushButton("ARRET")
        b_stop.setMinimumHeight(34)
        b_stop.setStyleSheet(
            f"background:{COLORS['error']}; color:#FFFFFF; font-weight:700;")
        b_stop.clicked.connect(self._mes_arret)
        h.addWidget(b_stop)                    # jamais desactive
        h.addStretch()
        cl.addLayout(h)

        hs = QHBoxLayout(); hs.setSpacing(12)
        self._mes_slider = QSlider(Qt.Orientation.Horizontal)
        self._mes_slider.setRange(0, 1000)
        self._mes_slider.setEnabled(False)
        self._mes_slider.valueChanged.connect(self._mes_tension)
        self._mes_actionneurs.append(self._mes_slider)
        hs.addWidget(self._mes_slider, 1)
        hs.addWidget(val("var_consigne", 165))
        cl.addLayout(hs)
        cl.addWidget(val("var_mesure"))
        cl.addWidget(val("var_defaut"))
        vl.addWidget(c)

        # ── 8. paramètres de régulation ───────────────────────────────
        c, cl = self._mes_carte(
            "PARAMÈTRES DE RÉGULATION",
            "Ajustement en temps réel des gains PI, rampe, tau_consigne et "
            "stratégie bas-Re. Les modifications sont appliquées immédiatement "
            "et sauvegardées dans config.json.")

        # Conteneur pour les spinbox
        self._mes_reg_widgets = {}

        def spin_double(key: str, mini: float, maxi: float, decimales: int = 2, suffix: str = "") -> QDoubleSpinBox:
            s = QDoubleSpinBox()
            s.setRange(mini, maxi)
            s.setDecimals(decimales)
            s.setSingleStep(10 ** (-decimales))
            if suffix:
                s.setSuffix(suffix)
            s.setMinimumWidth(100)
            s.setStyleSheet("background:#2A2A2A; color:#E0E0E0; border:1px solid #404040; padding:4px;")
            self._mes_reg_widgets[key] = s
            self._mes_actionneurs.append(s)
            return s

        def spin_int(key: str, mini: int, maxi: int, suffix: str = "") -> QSpinBox:
            s = QSpinBox()
            s.setRange(mini, maxi)
            if suffix:
                s.setSuffix(suffix)
            s.setMinimumWidth(100)
            s.setStyleSheet("background:#2A2A2A; color:#E0E0E0; border:1px solid #404040; padding:4px;")
            self._mes_reg_widgets[key] = s
            self._mes_actionneurs.append(s)
            return s

        def label_info(texte: str) -> QLabel:
            """Label d'information grisé pour les explications."""
            l = QLabel(texte)
            l.setStyleSheet(f"font-size:10px; color:{COLORS['muted2']}; background:transparent; font-style:italic;")
            return l

        # Gains PI
        h = QHBoxLayout(); h.setSpacing(18)
        h.addWidget(QLabel("Kp (gain proportionnel)"))
        h.addWidget(spin_double("kp", 0.0, 5.0, 2))
        h.addWidget(label_info("par défaut : 0.50 | correction en volts autour du feed-forward ; ↑ = plus vif, risque d'oscillation"))
        h.addStretch()
        cl.addLayout(h)

        h = QHBoxLayout(); h.setSpacing(18)
        h.addWidget(QLabel("Ki (gain intégral)"))
        h.addWidget(spin_double("ki", 0.0, 2.0, 3))
        h.addWidget(label_info("par défaut : 0.62 /s | annule l'écart table/banc ; ↑ = plus rapide, risque de dépassement"))
        h.addStretch()
        cl.addLayout(h)

        # Rampe et tau_consigne
        h = QHBoxLayout(); h.setSpacing(18)
        h.addWidget(QLabel("Rampe variateur"))
        h.addWidget(spin_double("rampe_v_par_s", 0.0, 10.0, 2, " V/s"))
        h.addWidget(label_info("par défaut : 2.0 V/s | ↑ = montée plus rapide, ↓ = plus douce (évite à-coups)"))
        h.addStretch()
        cl.addLayout(h)

        h = QHBoxLayout(); h.setSpacing(18)
        h.addWidget(QLabel("Tau consigne"))
        h.addWidget(spin_double("tau_consigne_s", 0.0, 2.0, 2, " s"))
        h.addWidget(label_info("= constante de temps du procédé (essai indiciel) | modèle de référence suivi par la correction"))
        h.addStretch()
        cl.addLayout(h)

        h = QHBoxLayout(); h.setSpacing(18)
        h.addWidget(QLabel("Tolérance convergence"))
        h.addWidget(spin_double("tolerance_convergence_relative", 0.001, 0.1, 3))
        h.addWidget(label_info("par défaut : 0.01 (1%) | erreur relative Re tolérée pour déclarer la convergence ; ↓ = plus strict"))
        h.addStretch()
        cl.addLayout(h)

        h = QHBoxLayout(); h.setSpacing(18)
        h.addWidget(QLabel("Fenêtre convergence"))
        h.addWidget(spin_double("fenetre_convergence_s", 0.5, 10.0, 1, " s"))
        h.addWidget(label_info("par défaut : 2.0 s | durée pendant laquelle Re doit rester dans la tolérance ; ↑ = plus stable"))
        h.addStretch()
        cl.addLayout(h)

        cl.addWidget(_hsep())

        # Stratégie bas-Re
        h = QHBoxLayout(); h.setSpacing(18)
        h.addWidget(QLabel("Seuil bas-Re"))
        h.addWidget(spin_int("seuil_re", 0, 10000, " Re"))
        h.addWidget(label_info("par défaut : 5000 Re | En dessous : bypass ouvert + Miller. 0 = auto depuis table"))
        h.addStretch()
        cl.addLayout(h)

        h = QHBoxLayout(); h.setSpacing(18)
        h.addWidget(QLabel("Facteur gains bas-Re"))
        h.addWidget(spin_double("gain_scale", 0.05, 1.0, 2))
        h.addWidget(label_info("par défaut : 0.20 | ↓ = régulation plus douce en mode PDC (vanne lente)"))
        h.addStretch()
        cl.addLayout(h)

        h = QHBoxLayout(); h.setSpacing(18)
        h.addWidget(QLabel("Marge seuil"))
        h.addWidget(spin_double("marge_seuil", 1.0, 2.0, 2))
        h.addWidget(label_info("par défaut : 1.30 | Marge (×) appliquée à u_min pour dériver le seuil auto"))
        h.addStretch()
        cl.addLayout(h)

        # Boutons
        h = QHBoxLayout(); h.setSpacing(10)
        h.addWidget(self._mes_bouton(
            "Recharger depuis config.json",
            self._mes_reg_charger))
        h.addWidget(self._mes_bouton(
            "Appliquer et sauvegarder",
            self._mes_reg_appliquer))
        h.addStretch()
        cl.addLayout(h)

        self._mes_lbl_reg_status = QLabel("")
        self._mes_lbl_reg_status.setWordWrap(True)
        self._mes_lbl_reg_status.setStyleSheet(
            f"font-size:11px; color:{COLORS['muted2']}; background:transparent;")
        cl.addWidget(self._mes_lbl_reg_status)

        vl.addWidget(c)

        vl.addStretch(1)
        return page

    # ----- actions de la page (toutes via les methodes publiques du pilote)

    def _mes_prepare(self) -> None:
        """Relache le maintien des ordres TOR : sans cela la boucle
        ramenerait aussitot la vanne en place. Meme mecanique que pour les
        sequences d'apprentissage. Repris en quittant la page."""
        self.pilote.suspendre_maintien_tor()

    def _mes_diaphragme(self, position: str) -> None:
        self._mes_prepare()
        self.pilote.commander_diaphragme(position)
        self._log(f"Mise en service : vanne 3 voies → {position}",
                  COLORS["accentText"])

    def _mes_bypass(self, ouverte: bool) -> None:
        self._mes_prepare()
        self.pilote.commander_bypass(ouverte)
        self._log("Mise en service : bypass → "
                  + ("ouvert" if ouverte else "ferme"), COLORS["accentText"])

    def _mes_pdc(self, volts: float) -> None:
        self.pilote.commander_vanne_perte_charge(volts)
        self._log(f"Mise en service : vanne PdC → {volts:.0f} V",
                  COLORS["accentText"])

    def _mes_run(self) -> None:
        if QMessageBox.question(
                self, "Demarrer le ventilateur",
                "La soufflerie va tourner.\n\nVerifier que n004 = 2 et "
                "n011 = 50,0 Hz sont regles sur le variateur, et que la "
                "veine est degagee.\n\nDemarrer ?") \
                != QMessageBox.StandardButton.Yes:
            return
        self._mes_slider.setValue(0)
        self.pilote.mode_manuel_tension(0.0)
        self._mes_run_arme = True
        self._log("Mise en service : RUN variateur (0 V)", COLORS["warn"])

    def _mes_tension(self, valeur: int) -> None:
        if not self._mes_run_arme:
            return                     # deplacement programme, pas operateur
        self.pilote.mode_manuel_tension(valeur / 100.0)

    def _mes_arret(self) -> None:
        """Arret total, y compris si la page n'a rien demarre : c'est le
        bouton de repli de cette page."""
        self._mes_run_arme = False
        self._mes_slider.setValue(0)
        self.pilote.arreter_regulation()
        self.pilote.reprendre_maintien_tor()
        self._log("Maintenance : ARRET", COLORS["error"])

    def _mes_reg_charger(self) -> None:
        """Recharge les paramètres de régulation depuis config.json."""
        try:
            cfg_path = Path(__file__).resolve().parent.parent / "config.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            reg = cfg.get("regulation", {})
            sr = cfg.get("strategie_bas_re", {})

            # Charger les valeurs dans les widgets
            self._mes_reg_widgets["kp"].setValue(reg.get("kp", 0.5))
            self._mes_reg_widgets["ki"].setValue(reg.get("ki", 0.15))
            self._mes_reg_widgets["rampe_v_par_s"].setValue(reg.get("rampe_v_par_s", 2.0))
            self._mes_reg_widgets["tau_consigne_s"].setValue(reg.get("tau_consigne_s", 0.0))
            self._mes_reg_widgets["tolerance_convergence_relative"].setValue(reg.get("tolerance_convergence_relative", 0.01))
            self._mes_reg_widgets["fenetre_convergence_s"].setValue(reg.get("fenetre_convergence_s", 2.0))
            # Note : gain_scheduling n'est plus dans l'IHM (widget retiré)
            # self._mes_reg_gain_scheduling.setChecked(reg.get("gain_scheduling", False))

            self._mes_reg_widgets["seuil_re"].setValue(sr.get("seuil_re", 5000))
            self._mes_reg_widgets["split_range_point"].setValue(sr.get("split_range_point", 5.0))
            self._mes_reg_widgets["gain_scale"].setValue(sr.get("gain_scale", 0.2))
            self._mes_reg_widgets["marge_seuil"].setValue(sr.get("marge_seuil", 1.3))

            self._mes_lbl_reg_status.setText(f"✓ Paramètres rechargés depuis config.json")
            self._mes_lbl_reg_status.setStyleSheet(f"font-size:11px; color:{COLORS['success']}; background:transparent;")
            self._log("Maintenance : paramètres de régulation rechargés", COLORS["accentText"])
        except Exception as e:
            self._mes_lbl_reg_status.setText(f"✗ Erreur : {str(e)}")
            self._mes_lbl_reg_status.setStyleSheet(f"font-size:11px; color:{COLORS['error']}; background:transparent;")
            self._log(f"Erreur rechargement paramètres : {e}", COLORS["error"])

    def _mes_reg_appliquer(self) -> None:
        """Applique les paramètres de régulation et les sauvegarde dans config.json."""
        try:
            # Lire config actuelle
            cfg_path = Path(__file__).resolve().parent.parent / "config.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

            # Mettre à jour les valeurs
            if "regulation" not in cfg:
                cfg["regulation"] = {}
            if "strategie_bas_re" not in cfg:
                cfg["strategie_bas_re"] = {}

            cfg["regulation"]["kp"] = self._mes_reg_widgets["kp"].value()
            cfg["regulation"]["ki"] = self._mes_reg_widgets["ki"].value()
            cfg["regulation"]["rampe_v_par_s"] = self._mes_reg_widgets["rampe_v_par_s"].value()
            cfg["regulation"]["tau_consigne_s"] = self._mes_reg_widgets["tau_consigne_s"].value()
            cfg["regulation"]["tolerance_convergence_relative"] = self._mes_reg_widgets["tolerance_convergence_relative"].value()
            cfg["regulation"]["fenetre_convergence_s"] = self._mes_reg_widgets["fenetre_convergence_s"].value()
            # Note : gain_scheduling n'est plus dans l'IHM (widget retiré)
            # cfg["regulation"]["gain_scheduling"] = self._mes_reg_gain_scheduling.isChecked()

            cfg["strategie_bas_re"]["seuil_re"] = self._mes_reg_widgets["seuil_re"].value()
            cfg["strategie_bas_re"]["split_range_point"] = self._mes_reg_widgets["split_range_point"].value()
            cfg["strategie_bas_re"]["gain_scale"] = self._mes_reg_widgets["gain_scale"].value()
            cfg["strategie_bas_re"]["marge_seuil"] = self._mes_reg_widgets["marge_seuil"].value()

            # Sauvegarder config.json
            cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")

            # Appliquer immédiatement au pilote (gains, rampe, tau, bas-Re)
            self.pilote.appliquer_config(cfg)

            self._mes_lbl_reg_status.setText(
                f"✓ Paramètres appliqués et sauvegardés | "
                f"Kp={cfg['regulation']['kp']:.2f}, Ki={cfg['regulation']['ki']:.3f}, "
                f"Rampe={cfg['regulation']['rampe_v_par_s']:.1f} V/s")
            self._mes_lbl_reg_status.setStyleSheet(f"font-size:11px; color:{COLORS['success']}; background:transparent;")
            self._log(
                f"Maintenance : paramètres appliqués - "
                f"Kp={cfg['regulation']['kp']:.2f}, Ki={cfg['regulation']['ki']:.3f}",
                COLORS["success"])
        except Exception as e:
            self._mes_lbl_reg_status.setText(f"✗ Erreur : {str(e)}")
            self._mes_lbl_reg_status.setStyleSheet(f"font-size:11px; color:{COLORS['error']}; background:transparent;")
            self._log(f"Erreur application paramètres : {e}", COLORS["error"])

    # ----- rafraichissement -----------------------------------------------

    def _tick_mise_en_service(self, e: dict) -> None:
        if self._stack.currentIndex() != self.PAGE_MAINTENANCE:
            return
        brutes = e.get("mesures_brutes") or {}
        img = e.get("image_process")
        mb = self.cfg.get("modbus", {})

        # verrou : pas d'actionnement pendant une regulation ou une sequence
        occupe = bool(e.get("regulation")) or self.apprentissage.etat["en_cours"]
        for w in self._mes_actionneurs:
            w.setEnabled(not occupe and (w is not self._mes_slider
                                         or self._mes_run_arme))
        self._mes_lbl_verrou.setVisible(occupe)
        if occupe:
            self._mes_lbl_verrou.setText(
                "Commandes verrouillees : une regulation ou une sequence "
                "d'apprentissage est en cours. Utiliser ARRET pour reprendre "
                "la main.")

        def mets(cle, txt, couleur=None):
            l = self._mes_lbl.get(cle)
            if l is None:
                return
            l.setText(txt)
            l.setStyleSheet(
                f"color:{couleur or COLORS['text']}; background:transparent;")

        # -- image process --
        if not img:
            self._mes_tbl_mots.setText(
                "<i>image process indisponible "
                "(simulateur sans configuration Modbus)</i>")
            self._mes_tbl_bits.setText("")
        else:
            noms = {v["adresse"]: n for n, v in mb.get("entrees", {}).items()
                    if not n.startswith("_")}
            entete = (f"{'mot':>3}  {'nom attendu':<22}"
                      + f"{'brut':>7}    {'valeur'}")
            lignes = [f"<b>{entete}</b>"]
            for a in sorted(img["mots"]):
                nom = noms.get(a, "?")
                v = brutes.get(nom)
                txt = f"{v:,.2f}".replace(",", " ") if isinstance(v, float) \
                    else "—"
                ligne = (f"{a:>3}  {nom:<22}"
                         + f"{img['mots'][a]:>7}    {txt}")
                lignes.append(ligne)
            html = "<pre>" + "\n".join(lignes) + "</pre>"
            self._mes_tbl_mots.setText(html)

            nomst = {v["adresse"]: (n, v.get("inverse", False))
                     for n, v in mb.get("entrees_tor", {}).items()
                     if not n.startswith("_")}
            entete = (f"{'bit':>3}  {'nom attendu':<22}"
                      + f"{'brut':>5}  {'interprete'}")
            lignes = [f"<b>{entete}</b>"]
            for a in sorted(img["bits"]):
                nom, inv = nomst.get(a, ("?", False))
                etat = "●" if brutes.get(nom) else "○"
                suffixe = " <i>(contact NF)</i>" if inv else ""
                ligne = (f"{a:>3}  {nom:<22}"
                         + f"{1 if img['bits'][a] else 0:>5}  {etat}{suffixe}")
                lignes.append(ligne)
            html = "<pre>" + "\n".join(lignes) + "</pre>"
            self._mes_tbl_bits.setText(html)

        # -- PT100 --
        T = brutes.get("temperature")
        r = self.cfg.get("regulation", {})
        tmin = r.get("temperature_min_c", 2.0)
        tmax = r.get("temperature_max_c", 45.0)
        if T is None:
            mets("pt100_valeur", "—")
            mets("pt100_verdict", "voie absente", COLORS["error"])
            mets("pt100_rho", "—")
            mets("pt100_mu", "—")
        else:
            mets("pt100_valeur", f"{T:.2f} °C".replace(".", ","))
            if tmin <= T <= tmax:
                mets("pt100_verdict",
                     f"plausible (bornes {tmin:.0f} à {tmax:.0f} °C)",
                     COLORS["ok"])
            else:
                mets("pt100_verdict",
                     f"HORS BORNES {tmin:.0f}–{tmax:.0f} °C — sonde coupee, "
                     "module sature ou voie mal adressee", COLORS["error"])

            # Calcul ρ (densité) et μ (viscosité)
            P_abs = e.get("p_amont") or self.pilote.banc.patm_pa
            from core.fluide import rho_air, mu_air
            rho = rho_air(P_abs, T)
            mu = mu_air(T)
            mets("pt100_rho", f"{rho:.4f} kg/m³".replace(".", ","))
            mets("pt100_mu", f"{mu:.2e} Pa·s".replace(".", ","))

        # -- FCO432 --
        zeros = self.cfg.get("zero_capteurs", {})
        for cle in ("dp_iso_500", "dp_diaph_500", "p_amont_500"):
            v = brutes.get(cle)
            mets("fco_" + cle,
                 f"{v:.2f} Pa".replace(".", ",") if v is not None else "—")
            z = zeros.get(cle)
            mets("fcoz_" + cle,
                 f"zero appris {z:+.2f} Pa".replace(".", ",")
                 if isinstance(z, (int, float)) else "zero non releve",
                 COLORS["muted2"])

        # -- vanne 3 voies : decodage un-parmi-trois --
        actifs = [nom for nom in ("fc_3voies_0", "fc_3voies_90",
                                  "fc_3voies_180") if brutes.get(nom)]
        if len(actifs) == 1:
            angle = actifs[0].rsplit("_", 1)[1]
            diaph = e.get("diaphragme") or "?"
            mets("v3_etat", f"butee {angle}° — un parmi trois ✓"
                 + (f"  ({diaph})" if angle in ("0", "90") else ""),
                 COLORS["ok"])
        elif e.get("manoeuvre"):
            mets("v3_etat", f"en course — {e['manoeuvre']}", COLORS["warn"])
        else:
            mets("v3_etat",
                 f"combinaison invalide : {actifs or 'aucune butee'} — "
                 "rupture de fil ou 24 V commun absent", COLORS["error"])

        # -- bypass --
        fc1_bypass = brutes.get("fc_bypass_ouvert")
        fc2_bypass = brutes.get("fc_bypass_ferme")
        bits_bypass = []
        bits_bypass.append(f"FC1 (DI1) : {'fermé' if fc1_bypass else 'ouvert'}")
        bits_bypass.append(f"FC2 (DI2) : {'fermé' if fc2_bypass else 'ouvert'}")
        # Détection d'anomalie : les deux fins de course actives simultanément
        if fc1_bypass and fc2_bypass:
            bits_bypass.append("ANOMALIE : les deux FC actives")
            mets("bypass_etat", "   ·   ".join(bits_bypass), COLORS["error"])
        else:
            mets("bypass_etat", "   ·   ".join(bits_bypass), COLORS["text"])

        # -- vanne PdC --
        cons = e.get("pdc_consigne")
        rec = brutes.get("pdc_recopie")
        bits = []
        if cons is not None:
            bits.append(f"consigne {cons:.2f} V".replace(".", ","))
        if rec is not None:
            bits.append(f"recopie {rec:.2f} V".replace(".", ","))
        # Affichage systématique des fins de course avec leurs identifiants
        fc1_ouvert = brutes.get("fc_pdc_ouvert")
        fc2_ferme = brutes.get("fc_pdc_ferme")
        bits.append(f"FC1 (DI3) : {'fermé' if fc1_ouvert else 'ouvert'}")
        bits.append(f"FC2 (DI4) : {'fermé' if fc2_ferme else 'ouvert'}")
        # le diagnostic de divergence est deja fait par la boucle
        # (modele de course + hysteresis) : ne pas re-inventer un seuil ici,
        # la vanne met 25 s a parcourir sa course et tout ecart instantane
        # serait un faux positif
        if e.get("defaut_pdc"):
            bits.append("recopie incoherente avec la consigne")
        mets("pdc_etat", "   ·   ".join(bits) or "—",
             COLORS["error"] if e.get("defaut_pdc") else COLORS["text"])

        # -- variateur --
        fmax = self.cfg.get("variateur", {}).get("frequence_max_hz", 50.0)
        tol = self.cfg.get("variateur", {}).get("tolerance_consigne_v", 0.15)
        u = e.get("u_variateur") or 0.0
        am = brutes.get("frequence_variateur")
        mets("var_consigne",
             f"{u:.2f} V  =  {fmax * u / 10.0:.1f} Hz".replace(".", ","))
        if am is None:
            mets("var_mesure", "moniteur AM absent — voie non cablee ?",
                 COLORS["error"])
        else:
            hz = fmax * am / 10.0
            atteinte = abs(am - u) <= tol
            mets("var_mesure",
                 f"moniteur AM {am:.2f} V  =  {hz:.1f} Hz".replace(".", ",")
                 + ("   ✓ consigne atteinte" if atteinte
                    else "   … en rampe"),
                 COLORS["ok"] if atteinte else COLORS["accentText"])
        d = e.get("defaut_variateur")
        mets("var_defaut", f"DEFAUT : {d}" if d
             else "contact MB : pas de defaut signale",
             COLORS["error"] if d else COLORS["ok"])

    def _build_footer(self, mode: str) -> QWidget:
        foot = QWidget()
        foot.setObjectName("footer")
        foot.setFixedHeight(30)
        hl = QHBoxLayout(foot)
        hl.setContentsMargins(16, 0, 16, 0)
        hl.setSpacing(20)

        self._foot_cnx = QLabel("●")
        self._foot_cnx.setStyleSheet(
            f"color:{COLORS['ok']}; font-size:10px; background:transparent;")
        hl.addWidget(self._foot_cnx)

        is_simu = mode.lower() == "simulation"
        lbl_wago = QLabel(
            "SIMULATION" if is_simu else "Wago 750-362 · 192.168.1.10")
        lbl_wago.setObjectName("foot-item")
        hl.addWidget(lbl_wago)

        self._foot_freq = QLabel("Fréq : —")
        self._foot_freq.setObjectName("foot-item")
        hl.addWidget(self._foot_freq)

        self._foot_samples = QLabel("Échantillons : 0")
        self._foot_samples.setObjectName("foot-item")
        hl.addWidget(self._foot_samples)

        hl.addStretch()

        self._foot_journal = QLabel()
        self._foot_journal.setObjectName("foot-item")
        self._foot_journal.setVisible(False)
        hl.addWidget(self._foot_journal)

        self._foot_rec = QLabel()
        self._foot_rec.setObjectName("foot-item")
        self._foot_rec.setVisible(False)
        hl.addWidget(self._foot_rec)

        self.lbl_etat = QLabel("ARRÊT")
        self.lbl_etat.setObjectName("foot-state")
        hl.addWidget(self.lbl_etat)

        self._foot_elapsed = QLabel("· 0 s")
        self._foot_elapsed.setObjectName("foot-item")
        hl.addWidget(self._foot_elapsed)

        return foot

    # ═══════════════════════════════════════════════════════ VUES CENTRALES

    def _build_scope(self) -> QWidget:
        page = QWidget()
        vl = QVBoxLayout(page)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        panel = QWidget()
        panel.setObjectName("cv-panel")
        pvl = QVBoxLayout(panel)
        pvl.setContentsMargins(0, 0, 0, 0)
        pvl.setSpacing(0)

        # Toolbar
        tb = QWidget()
        tb.setStyleSheet("background:transparent; border-bottom:1px solid #DBE5EA;")
        tbhl = QHBoxLayout(tb)
        tbhl.setContentsMargins(18, 14, 18, 12)
        tbhl.setSpacing(16)
        # légende
        self._scope_legend_re = QLabel()
        self._scope_legend_re.setStyleSheet(
            "font-size:11.5px; color:#14223C; background:transparent;")
        tbhl.addWidget(self._scope_legend_re)
        self._scope_legend_sp = QLabel()
        self._scope_legend_sp.setStyleSheet(
            "font-size:11.5px; color:#14223C; background:transparent;")
        tbhl.addWidget(self._scope_legend_sp)
        tbhl.addStretch()
        self._lbl_scope_state = QLabel("ARRÊT")
        self._lbl_scope_state.setStyleSheet(
            f"font-size:10.5px; font-family:'IBM Plex Mono',monospace;"
            f" color:#06384A; background:{COLORS['accent']};"
            f" padding:5px 10px; border-radius:6px; font-weight:600;")
        tbhl.addWidget(self._lbl_scope_state)
        pvl.addWidget(tb)

        # Plot
        self._plot_re = pg.PlotWidget()
        self._plot_re.setMenuEnabled(False)
        self._plot_re.showGrid(x=True, y=True, alpha=0.12)
        self._plot_re.setLabel("left",   "Reynolds")
        self._plot_re.setLabel("bottom", "Temps (s)")
        self._plot_re.setBackground("#FFFFFF")
        self._plot_re.enableAutoRange(axis="x", enable=False)
        left_axis = self._plot_re.getAxis("left")
        left_axis.setPen(pg.mkPen(color=(50, 50, 50), width=1))
        left_axis.setTextPen(pg.mkPen(color=(20, 34, 60)))
        left_axis.show()
        bottom_axis = self._plot_re.getAxis("bottom")
        bottom_axis.setPen(pg.mkPen(color=(50, 50, 50), width=1))
        bottom_axis.setTextPen(pg.mkPen(color=(20, 34, 60)))
        bottom_axis.show()
        self._curve_cible = self._plot_re.plot(
            pen=pg.mkPen("#FF9900", width=2,
                         style=Qt.PenStyle.DashLine), name="Consigne")
        self._curve_re = self._plot_re.plot(
            pen=pg.mkPen(COLORS["accent"], width=2.5), name="Re calculé")
        pvl.addWidget(self._plot_re, 1)

        vl.addWidget(panel, 1)
        return page

    def _build_voies(self) -> QWidget:
        page = QWidget()
        vl = QVBoxLayout(page)
        vl.setContentsMargins(16, 16, 16, 16)
        vl.setSpacing(0)

        panel = QWidget()
        panel.setObjectName("cv-panel")
        pvl = QVBoxLayout(panel)
        pvl.setContentsMargins(0, 0, 0, 0)
        pvl.setSpacing(0)

        _col_w = [4, 2, 2, 2, 3, 2]

        # En-tête
        hdr = QWidget()
        hdr.setStyleSheet("background:transparent; border-bottom:1px solid #DBE5EA;")
        hg = QGridLayout(hdr)
        hg.setContentsMargins(18, 14, 18, 14)
        hg.setSpacing(10)
        for i, txt in enumerate(["VOIE", "UNITÉ", "GAIN", "OFFSET", "PLAGE", "ÉTAT"]):
            l = QLabel(txt)
            l.setStyleSheet(
                "font-size:10px; font-weight:700; letter-spacing:.07em;"
                " color:#6B7E8E; background:transparent;")
            hg.addWidget(l, 0, i)
        for c, w in enumerate(_col_w):
            hg.setColumnStretch(c, w)
        pvl.addWidget(hdr)

        self.lbl_entrees: dict[str, QLabel] = {}
        self.lbl_fc: dict[str, QLabel] = {}   # compatibilité _tick_entrees

        for i, (key, name, max_val, fmt, color) in enumerate(_CHANNELS):
            unit   = _CH_UNIT[i]
            plage  = _CH_RANGE[i]
            row = QWidget()
            row.setStyleSheet("background:transparent; border-bottom:1px solid #E9EEF1;")
            rg = QGridLayout(row)
            rg.setContentsMargins(18, 13, 18, 13)
            rg.setSpacing(10)

            # colonne VOIE : carré coloré + nom
            nw = QWidget(); nw.setStyleSheet("background:transparent;")
            nhl = QHBoxLayout(nw); nhl.setContentsMargins(0,0,0,0); nhl.setSpacing(9)
            sq = QLabel("■")
            sq.setStyleSheet(f"color:{color}; font-size:10px; background:transparent;")
            nhl.addWidget(sq)
            ln = QLabel(name)
            ln.setStyleSheet("font-size:13px; font-weight:500; color:#14223C; background:transparent;")
            nhl.addWidget(ln)
            rg.addWidget(nw, 0, 0)

            rg.addWidget(self._mono(unit),  0, 1)
            rg.addWidget(self._mono("1,000"), 0, 2)
            rg.addWidget(self._mono("0,00"), 0, 3)
            rg.addWidget(self._mono(plage),  0, 4)

            ok = QLabel("● Actif")
            ok.setStyleSheet("font-size:11px; font-weight:600; color:#1F8F5B; background:transparent;")
            rg.addWidget(ok, 0, 5)

            for c, w in enumerate(_col_w):
                rg.setColumnStretch(c, w)
            pvl.addWidget(row)
            self.lbl_entrees[key] = ok   # réf pour _tick_entrees (état actif/inactif)

        pvl.addStretch()
        vl.addWidget(panel, 1)
        return page

    def _mono(self, txt: str) -> QLabel:
        l = QLabel(txt)
        l.setStyleSheet(
            "font-family:'IBM Plex Mono',monospace; font-size:12px;"
            " color:#7B8FA0; background:transparent;")
        return l

    def _build_sequence(self) -> QWidget:
        """Suivi d'un profil CSV (temps [s] ; Re cible) par échelons."""
        page = QWidget()
        vl = QVBoxLayout(page)
        vl.setContentsMargins(16, 16, 16, 16)
        panel = QWidget()
        panel.setObjectName("cv-panel")
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(18, 18, 18, 18)
        pl.setSpacing(12)

        self._seq_points: list | None = None

        # barre de commandes — style scopé par objectName : un
        # setStyleSheet("background:transparent;") sans sélecteur se
        # propagerait aux boutons enfants et les rendrait invisibles
        bar = QWidget()
        bar.setObjectName("seq-bar")
        bar.setStyleSheet("QWidget#seq-bar { background:transparent; }")
        bhl = QHBoxLayout(bar)
        bhl.setContentsMargins(0, 0, 0, 0)
        bhl.setSpacing(10)
        # NB : les propriétés géométriques QSS (min-width, padding) priment
        # sur setMinimumSize/setFixedSize -> dimensions fixées dans le QSS
        _btn_seq_qss = BTN_QSS + " QPushButton { padding:8px 18px; }"
        btn_charger = QPushButton("📂  Charger CSV…")
        btn_charger.setStyleSheet(_btn_seq_qss)
        btn_charger.clicked.connect(self._charger_sequence)
        bhl.addWidget(btn_charger)
        self._btn_seq_start = QPushButton("▶  Démarrer")
        self._btn_seq_start.setStyleSheet(_btn_seq_qss)
        self._btn_seq_start.setEnabled(False)
        self._btn_seq_start.clicked.connect(self._demarrer_sequence)
        bhl.addWidget(self._btn_seq_start)
        btn_stop = QPushButton("■  Arrêter")
        btn_stop.setStyleSheet(_btn_seq_qss)
        btn_stop.clicked.connect(self._arreter_sequence)
        bhl.addWidget(btn_stop)
        bh = QPushButton("?")
        bh.setStyleSheet(
            f"QPushButton {{ background:{COLORS['warn']}; border:none;"
            f" border-radius:4px; color:#FFFFFF; font-weight:700;"
            f" min-width:44px; max-width:44px;"
            f" min-height:34px; max-height:34px; padding:0; }}"
            f"QPushButton:hover {{ background:#E68A00; }}")
        bh.clicked.connect(lambda: QMessageBox.information(
            self, "Mode séquence",
            "Fichier CSV à 2 colonnes : temps [s] ; Reynolds cible.\n"
            "Séparateur « ; », « , » ou tabulation ; virgule décimale "
            "acceptée ; ligne d'en-tête optionnelle ; lignes vides ou "
            "commençant par # ignorées.\n\n"
            "La consigne évolue par échelons aux échéances du profil "
            "(référence Conduite/Veine du panneau Pilotage). Le temps de "
            "la dernière ligne marque la fin de la séquence : sa consigne "
            "est ensuite maintenue — mettre Re = 0 en dernière ligne pour "
            "arrêter la soufflerie en fin d'essai."))
        bhl.addWidget(bh)
        bhl.addStretch()
        self._lbl_seq_fichier = QLabel("aucun fichier chargé")
        self._lbl_seq_fichier.setObjectName("muted")
        bhl.addWidget(self._lbl_seq_fichier)
        pl.addWidget(bar)

        # aperçu du profil + progression
        self._plot_seq = pg.PlotWidget()
        self._plot_seq.setMenuEnabled(False)
        self._plot_seq.showGrid(x=True, y=True, alpha=0.15)
        self._plot_seq.setLabel("bottom", "Temps (s)")
        self._plot_seq.setLabel("left", "Reynolds cible")
        self._curve_seq = self._plot_seq.plot(
            pen=pg.mkPen(COLORS["accent"], width=2),
            symbol="o", symbolSize=6, symbolBrush=COLORS["accent"],
            symbolPen=pg.mkPen("#E8EEF1", width=1))
        self._line_seq_t = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen(COLORS["warn"], width=2, style=Qt.PenStyle.DashLine))
        self._line_seq_t.setVisible(False)
        self._plot_seq.addItem(self._line_seq_t)
        pl.addWidget(self._plot_seq, 1)

        self._lbl_seq_statut = QLabel("—")
        self._lbl_seq_statut.setObjectName("mono")
        pl.addWidget(self._lbl_seq_statut)

        vl.addWidget(panel, 1)
        return page

    # ── mode séquence : commandes ───────────────────────────────────────

    def _charger_sequence(self) -> None:
        chemin, _ = QFileDialog.getOpenFileName(
            self, "Charger une séquence", "",
            "Fichiers CSV (*.csv *.txt);;Tous les fichiers (*)")
        if not chemin:
            return
        try:
            points = charger_sequence_csv(chemin)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Séquence",
                                 f"Fichier invalide :\n{exc}")
            return
        self._seq_points = points
        # tracé en échelons : chaque consigne tient jusqu'à l'échéance suivante
        xs, ys = [], []
        for i, (t, re) in enumerate(points):
            if i:
                xs.append(t)
                ys.append(points[i - 1][1])
            xs.append(t)
            ys.append(re)
        self._curve_seq.setData(xs, ys)
        self._line_seq_t.setVisible(False)
        self._lbl_seq_fichier.setText(
            f"{Path(chemin).name} — {len(points)} étapes, "
            f"{points[-1][0]:.0f} s")
        self._lbl_seq_statut.setText("profil chargé — prêt à démarrer")
        self._btn_seq_start.setEnabled(True)
        self._log(f"Séquence chargée ({len(points)} étapes)", COLORS["accent"])

    def _demarrer_sequence(self) -> None:
        if not self._seq_points:
            return
        if self.apprentissage.etat["en_cours"]:
            QMessageBox.warning(self, "Séquence",
                                "Apprentissage en cours — attendre la fin.")
            return
        err = self.pilote.demarrer_sequence(self._seq_points, self._ref_re)
        if err:
            QMessageBox.warning(self, "Séquence", err)
            return
        self._set_mode("sequence")
        self._log(f"Séquence démarrée ({len(self._seq_points)} étapes, "
                  f"{self._seq_points[-1][0]:.0f} s)", COLORS["accent"])

    def _arreter_sequence(self) -> None:
        self.pilote.arreter_regulation()
        self._line_seq_t.setVisible(False)
        self._lbl_seq_statut.setText("séquence arrêtée")
        self._log("Séquence arrêtée", COLORS["warn"])

    def _build_apprentissage(self) -> QWidget:
        page = QWidget()
        vl = QVBoxLayout(page)
        vl.setContentsMargins(16, 16, 16, 16)
        vl.setSpacing(12)

        _help_qss = (
            f"QPushButton {{ background:{COLORS['warn']}; border:none;"
            f" border-radius:4px; color:#FFFFFF; font-weight:700;"
            f" min-width:0; padding:0; }}"
            f"QPushButton:hover {{ background:#E68A00; }}"
        )

        lbl = QLabel(
            "Caractérisation automatique du banc — régulation arrêtée. "
            "Chaque séquence remet le banc dans un état sûr en fin de test.")
        lbl.setWordWrap(True)
        lbl.setObjectName("muted")
        vl.addWidget(lbl)

        sub = QTabWidget()
        sub.setDocumentMode(True)
        vl.addWidget(sub, 1)

        _HELP = {
            "verif_cablage": ("Contrôle câblage E/S",
                "Actionne chaque sortie et vérifie le retour attendu : "
                "fins de course bypass et vanne 3 voies, recopie PdC, "
                "réponse du variateur. Identifie ensuite quelle position de "
                "la vanne 3 voies alimente quel diaphragme (le capteur ΔP "
                "qui répond fait foi) ; un mapping ISO/Miller inversé est "
                "corrigé par « Appliquer »."),
            "zero_capteurs": ("Zéro capteurs ΔP",
                "Ventilateur arrêté, moyenne ~10 s des deux capteurs ΔP : "
                "les offsets de zéro sont ensuite soustraits des mesures "
                "par la boucle. Décisif en bas-Re, où le ΔP utile ne vaut "
                "que quelques pascals."),
            "calibration_pdc": ("Calibration recopie PdC",
                "Envoie 0 V puis 10 V à la vanne PdC et mesure la tension brute "
                "à chaque butée (5 s de stabilisation). Prérequis pour les tests "
                "de temps de course."),
            "vanne_3voies": ("Vanne 3 voies",
                "Chronomètre les deux courses utiles de la carte GF3 : "
                "T1 de 0° (ISO) vers 90° (Miller) par l'ordre I, T2 de 90° "
                "vers 0° par l'ordre F. La position 180° n'est pas commandée "
                "— le constructeur ne la définit pas pour un boisseau en L. "
                "Sauvegarde le temps max + 10 % comme chien de garde."),
            "vanne_bypass": ("Vanne bypass",
                "Démarre en fermeture complète (FC1). "
                "T1 : FC1→ouverture (FC2), T2 : FC2→FC1. "
                "Sauvegarde le temps max + 10 %."),
            "vanne_pdc": ("Vanne perte de charge",
                "Démarre en fermeture complète (fc_pdc_ferme). "
                "T1 : fermeture → ouverture (fc_pdc_ouvert), T2 : ouverture → fermeture. "
                "Sauvegarde le temps max + 5 %."),
        }

        # ── Vannes ────────────────────────────────────────────
        tab_v = QWidget()
        gv = QGridLayout(tab_v)
        gv.setContentsMargins(16, 24, 16, 16)
        gv.setSpacing(12)
        gv.setColumnStretch(0, 3)
        gv.setColumnMinimumWidth(1, 52)
        gv.setColumnStretch(2, 2)

        self._btns_vanne: dict[str, QPushButton] = {}
        self._lbls_vanne: dict[str, tuple] = {}

        entries = [
            # ORDRE CHRONOLOGIQUE : séquence à suivre pour la mise en service
            ("▶  1. Zéro capteurs ΔP", "zero_capteurs",
             lambda r: "  ".join(f"{v.replace('_500', '')} = {o:.2f} Pa"
                                 for v, o in r.items())),
            None,
            ("▶  2. Calibration recopie vanne perte de charge", "calibration_pdc",
             lambda r: (f"0% = {r['v_min_recopie']} V  "
                        f"100% = {r['v_max_recopie']} V")),
            None,
            ("▶  3. Vanne 3 voies", "vanne_3voies",
             lambda r: (f"T1={r['T1_0_vers_90_s']} s  "
                        f"T2={r['T2_90_vers_0_s']} s")),
            ("▶  4. Vanne bypass", "vanne_bypass",
             lambda r: f"T1={r['ouverture_s']} s  T2={r['fermeture_s']} s"),
            ("▶  5. Vanne perte de charge", "vanne_pdc",
             lambda r: f"T1={r['ouverture_s']} s  T2={r['fermeture_s']} s"),
            None,
            ("▶  6. Contrôle câblage E/S (vérification finale)", "verif_cablage",
             lambda r: r["resume"]),
        ]
        gr = 0
        for entry in entries:
            if entry is None:
                gv.addWidget(_hsep(), gr, 0, 1, 3)
                gr += 1
                continue
            label, test, fmt = entry
            btn = QPushButton(label)
            btn.clicked.connect(lambda c=False, t=test: self._lancer_test(t))
            btn.setMinimumHeight(40)
            gv.addWidget(btn, gr, 0)
            t, b = _HELP[test]
            bh = QPushButton("?")
            bh.setFixedSize(44, 40)
            bh.setStyleSheet(_help_qss)
            bh.clicked.connect(
                lambda c=False, tt=t, bb=b:
                QMessageBox.information(self, tt, bb))
            gv.addWidget(bh, gr, 1)
            rl = QLabel("—")
            rl.setObjectName("mono")
            gv.addWidget(rl, gr, 2)
            self._btns_vanne[test] = btn
            self._lbls_vanne[test] = (rl, fmt)
            gv.setRowMinimumHeight(gr, 44)
            gr += 1
        gv.setRowStretch(gr, 1)
        sub.addTab(tab_v, "  1-6. Vannes  ")

        # ── Re mapping Miller (plage basse) ──────────────────
        # ORDRE CHRONOLOGIQUE : faire re_mapping_miller AVANT re_mapping_iso et indiciel
        tab_miller = QWidget()
        vm = QVBoxLayout(tab_miller)
        vm.setContentsMargins(16, 16, 16, 16)
        vm.setSpacing(16)
        col_m = QWidget()
        cml = QVBoxLayout(col_m)
        cml.setContentsMargins(0, 0, 0, 0)
        cml.setSpacing(8)
        lbl_m = QLabel(
            "<b>7. Re mapping Miller (plage basse)</b><br>"
            "Détecte le décrochage du ventilateur et détermine u_min, u_seuil et PDC min. "
            "Couvre la plage basse 250-5000 Re (Miller, bypass ouvert).<br>"
            "<i>Paliers 12s, mesure automatique u_seuil et PDC min.</i>")
        lbl_m.setWordWrap(True)
        lbl_m.setObjectName("muted")
        cml.addWidget(lbl_m)
        cml.addSpacing(8)
        self._btn_re_mapping_miller = QPushButton(
            "▶  7. Lancer Re mapping Miller")
        self._btn_re_mapping_miller.setMinimumHeight(40)
        self._btn_re_mapping_miller.clicked.connect(
            lambda: self._lancer_test("re_mapping_miller"))
        cml.addWidget(self._btn_re_mapping_miller)
        self._lbl_statut_re_mapping_miller = QLabel("—")
        self._lbl_statut_re_mapping_miller.setObjectName("mono")
        cml.addWidget(self._lbl_statut_re_mapping_miller)
        cml.addStretch()
        vm.addWidget(col_m, 0)
        self._plot_re_mapping_miller = pg.PlotWidget()
        self._plot_re_mapping_miller.setMenuEnabled(False)
        self._plot_re_mapping_miller.showGrid(x=True, y=True, alpha=0.15)
        self._plot_re_mapping_miller.setLabel("bottom", "Tension [V]")
        self._plot_re_mapping_miller.setLabel("left", "Reynolds")
        self._curve_miller_stable = self._plot_re_mapping_miller.plot(
            pen=pg.mkPen(COLORS["accent"], width=2), symbol="o",
            symbolSize=6, symbolBrush=COLORS["accent"],
            symbolPen=pg.mkPen("#E8EEF1", width=1))
        self._curve_miller_stall = self._plot_re_mapping_miller.plot(
            pen=None, symbol="x", symbolSize=10,
            symbolBrush=COLORS["error"],
            symbolPen=pg.mkPen(COLORS["error"], width=2))
        self._line_miller_min = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen(COLORS["warn"], width=1,
                         style=Qt.PenStyle.DashLine),
            label="U min", labelOpts={"color": COLORS["warn"]})
        self._line_miller_min.setVisible(False)
        self._plot_re_mapping_miller.addItem(self._line_miller_min)
        vm.addWidget(self._plot_re_mapping_miller, 1)
        sub.addTab(tab_miller, "  7. Re Miller  ")

        # ── Re mapping ISO (plage haute) ──────────────────
        tab_iso = QWidget()
        viso = QVBoxLayout(tab_iso)
        viso.setContentsMargins(16, 16, 16, 16)
        viso.setSpacing(16)
        col_iso = QWidget()
        cisol = QVBoxLayout(col_iso)
        cisol.setContentsMargins(0, 0, 0, 0)
        cisol.setSpacing(8)
        lbl_iso = QLabel(
            "<b>8. Re mapping ISO (plage haute)</b><br>"
            "Caractérisation plage haute 5000-15000 Re (ISO, bypass fermé). "
            "Nécessite que re_mapping_miller ait été lancé au préalable.<br>"
            "<i>Paliers 12s, montée depuis u_seuil jusqu'à 10V.</i>")
        lbl_iso.setWordWrap(True)
        lbl_iso.setObjectName("muted")
        cisol.addWidget(lbl_iso)
        cisol.addSpacing(8)
        self._btn_re_mapping_iso = QPushButton(
            "▶  8. Lancer Re mapping ISO")
        self._btn_re_mapping_iso.setMinimumHeight(40)
        self._btn_re_mapping_iso.clicked.connect(
            lambda: self._lancer_test("re_mapping_iso"))
        cisol.addWidget(self._btn_re_mapping_iso)
        self._lbl_statut_re_mapping_iso = QLabel("—")
        self._lbl_statut_re_mapping_iso.setObjectName("mono")
        cisol.addWidget(self._lbl_statut_re_mapping_iso)
        cisol.addStretch()
        viso.addWidget(col_iso, 0)
        self._plot_re_mapping_iso = pg.PlotWidget()
        self._plot_re_mapping_iso.setMenuEnabled(False)
        self._plot_re_mapping_iso.showGrid(x=True, y=True, alpha=0.15)
        self._plot_re_mapping_iso.setLabel("bottom", "Tension [V]")
        self._plot_re_mapping_iso.setLabel("left", "Reynolds")
        self._curve_iso_stable = self._plot_re_mapping_iso.plot(
            pen=pg.mkPen(COLORS["accent"], width=2), symbol="o",
            symbolSize=6, symbolBrush=COLORS["accent"],
            symbolPen=pg.mkPen("#E8EEF1", width=1))
        viso.addWidget(self._plot_re_mapping_iso, 1)
        sub.addTab(tab_iso, "  8. Re ISO  ")

        # ── Indiciel ──────────────────────────────────────────
        tab_i = QWidget()
        gi = QGridLayout(tab_i)
        gi.setContentsMargins(16, 24, 16, 16)
        gi.setSpacing(12)
        gi.setColumnStretch(0, 3)
        gi.setColumnMinimumWidth(1, 52)
        gi.setColumnStretch(2, 2)

        lbl_i = QLabel(
            "<b>9. Essai indiciel (réglage PI)</b><br>"
            "Stabilise à U1, applique l'échelon U1→U2, dépouille automatiquement "
            "le temps mort, τ, gain K et propose kp/ki (IMC). "
            "Prérequis : tests 7-8 (Re mapping Miller + ISO).<br>"
            "<i>Configuration : diaphragme Miller (90°), bypass fermé, vanne PDC ouverte.</i>")
        lbl_i.setWordWrap(True)
        lbl_i.setObjectName("muted")
        gi.addWidget(lbl_i, 0, 0, 1, 3)

        hv = QHBoxLayout()
        hv.addWidget(QLabel("U1 [V] :"))
        self.inp_u1 = QLineEdit("2.0")
        self.inp_u1.setFixedWidth(60)
        hv.addWidget(self.inp_u1)
        hv.addSpacing(16)
        hv.addWidget(QLabel("U2 [V] :"))
        self.inp_u2 = QLineEdit("4.0")
        self.inp_u2.setFixedWidth(60)
        hv.addWidget(self.inp_u2)
        hv.addStretch()
        gi.addLayout(hv, 1, 0, 1, 3)

        self._btn_indiciel = QPushButton("▶  9. Lancer l'essai indiciel")
        self._btn_indiciel.setMinimumHeight(40)
        self._btn_indiciel.clicked.connect(self._lancer_indiciel)
        gi.addWidget(self._btn_indiciel, 2, 0)

        bhi = QPushButton("?")
        bhi.setFixedSize(44, 40)
        bhi.setStyleSheet(_help_qss)
        bhi.clicked.connect(lambda: QMessageBox.information(
            self, "Essai indiciel",
            "Stabilise à U1, applique l'échelon vers U2, dépouille "
            "automatiquement : t_mort, τ, gain K, propose kp/ki IMC."))
        gi.addWidget(bhi, 2, 1)
        self._lbl_statut_indiciel = QLabel("—")
        self._lbl_statut_indiciel.setObjectName("mono")
        gi.addWidget(self._lbl_statut_indiciel, 2, 2)
        gi.setRowStretch(3, 1)
        sub.addTab(tab_i, "  9. Indiciel  ")

        # boutons bas
        hb = QHBoxLayout()
        hb.addStretch()
        btn_ann = QPushButton("Annuler")
        btn_ann.setFixedSize(200, 40)
        btn_ann.setStyleSheet(BTN_QSS)
        btn_ann.clicked.connect(self.apprentissage.annuler)
        hb.addWidget(btn_ann)
        hb.addSpacing(12)
        self.btn_appliquer_app = QPushButton("Appliquer")
        self.btn_appliquer_app.setFixedSize(200, 40)
        self.btn_appliquer_app.setStyleSheet(BTN_QSS)
        self.btn_appliquer_app.setEnabled(False)
        self.btn_appliquer_app.clicked.connect(self._appliquer_apprentissage)
        hb.addWidget(self.btn_appliquer_app)
        hb.addStretch()
        vl.addLayout(hb)

        self.lbl_app_statut = QLabel("")
        self.lbl_app_statut.setObjectName("info")
        vl.addWidget(self.lbl_app_statut)
        self.lbl_app_resultats = QLabel("")
        self.lbl_app_resultats.setObjectName("mono")
        vl.addWidget(self.lbl_app_resultats)
        return page

    def _build_configuration(self) -> QWidget:
        """Vue configuration : groupes lus depuis config.json, éditables après déverrouillage."""
        page = QWidget()
        vl = QVBoxLayout(page)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        # ── barre de contrôle ───────────────────────────────
        ctrl = QWidget()
        ctrl.setStyleSheet("background:#FFFFFF; border-bottom:1px solid #DBE5EA;")
        cl = QHBoxLayout(ctrl)
        cl.setContentsMargins(18, 10, 18, 10)
        cl.setSpacing(12)
        self.chk_unlock = QCheckBox("Déverrouiller les paramètres")
        self.chk_unlock.toggled.connect(self._basculer_verrouillage)
        cl.addWidget(self.chk_unlock)
        lf = QLabel(str(self.chemin_cfg.name))
        lf.setObjectName("muted")
        cl.addWidget(lf)
        cl.addStretch()
        self.btn_enregistrer = QPushButton("💾  Enregistrer")
        self.btn_enregistrer.setStyleSheet(BTN_QSS)
        self.btn_enregistrer.setEnabled(False)
        self.btn_enregistrer.clicked.connect(self._enregistrer_configuration)
        cl.addWidget(self.btn_enregistrer)
        btn_ann2 = QPushButton("↺  Annuler")
        btn_ann2.setStyleSheet(BTN_QSS)
        btn_ann2.clicked.connect(self._recharger_champs)
        cl.addWidget(btn_ann2)
        self.lbl_cfg_statut = QLabel("")
        self.lbl_cfg_statut.setStyleSheet(f"color:{COLORS['ok']};")
        cl.addWidget(self.lbl_cfg_statut)
        vl.addWidget(ctrl)

        # ── contenu scrollable ─────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        corps = QWidget()
        corps.setStyleSheet(f"background:{COLORS['bg']};")
        gl = QGridLayout(corps)
        gl.setContentsMargins(16, 16, 16, 16)
        gl.setSpacing(12)
        gl.setColumnStretch(0, 1)
        gl.setColumnStretch(1, 1)

        self.champs: dict[tuple, tuple] = {}

        # valeurs d'usine non calibrées (config.json : "A AJUSTER") — tant que
        # le champ garde cette valeur, on suppose qu'il n'a pas été mesuré
        VALEURS_A_AJUSTER = {
            ("veine", "section_m2"): 0.04,
            ("veine", "longueur_caracteristique_m"): 0.2,
        }

        def _grp(titre: str, rows: list[tuple], col: int, row: int) -> None:
            """Groupe en carte : (chemin_tuple, libelle, unite, valeur_fmt, validation|None)."""
            card = QWidget()
            card.setStyleSheet(
                "QWidget { background:#FFFFFF; border:1px solid #DBE5EA;"
                " border-radius:10px; }")
            cvl = QVBoxLayout(card)
            cvl.setContentsMargins(0, 0, 0, 0)
            cvl.setSpacing(0)
            # titre
            th = QWidget()
            th.setStyleSheet(
                "background:transparent; border-bottom:1px solid #DBE5EA;")
            thl = QHBoxLayout(th)
            thl.setContentsMargins(16, 12, 16, 12)
            lt = QLabel(titre.upper())
            lt.setStyleSheet(
                f"font-size:10px; font-weight:700; letter-spacing:.12em;"
                f" color:{COLORS['accent']}; background:transparent; border:none;")
            thl.addWidget(lt)
            cvl.addWidget(th)
            # lignes
            for chemin, libelle, unite, val_fmt, validation in rows:
                try:
                    raw = _cfg_lire(self.cfg, chemin)
                except (KeyError, TypeError):
                    raw = "—"
                rw = QWidget()
                rw.setStyleSheet(
                    "background:transparent; border-bottom:1px solid #E9EEF1;")
                rwl = QHBoxLayout(rw)
                rwl.setContentsMargins(16, 11, 16, 11)
                rwl.setSpacing(8)
                ll = QLabel(libelle)
                ll.setStyleSheet("font-size:12.5px; color:#3A4D60; background:transparent; border:none;")
                rwl.addWidget(ll)
                defaut = VALEURS_A_AJUSTER.get(chemin)
                if defaut is not None and isinstance(raw, float) and abs(raw - defaut) < 1e-9:
                    lw = QLabel("à ajuster")
                    lw.setToolTip(
                        "Valeur d'usine, non mesurée sur le banc réel — "
                        "à renseigner avant un essai en référence Re veine.")
                    lw.setStyleSheet(
                        f"font-size:10px; font-weight:700; color:{COLORS['warn']};"
                        " background:transparent; border:none;")
                    rwl.addWidget(lw)
                rwl.addStretch()
                if validation is not None:
                    # champ éditable
                    inp = QLineEdit(val_fmt.format(raw) if isinstance(raw, float) else str(raw))
                    inp.setObjectName("num-input")
                    inp.setAlignment(Qt.AlignmentFlag.AlignRight)
                    inp.setFixedWidth(100)
                    inp.setEnabled(False)
                    rwl.addWidget(inp)
                    self.champs[chemin] = (inp, validation)
                else:
                    # valeur en lecture seule (texte)
                    lv = QLabel(str(raw))
                    lv.setStyleSheet(
                        "font-family:'IBM Plex Mono',monospace; font-size:12px;"
                        " font-weight:600; color:#14223C; background:transparent; border:none;")
                    rwl.addWidget(lv)
                lbl_u = QLabel(unite)
                lbl_u.setStyleSheet(
                    "font-size:11px; color:#6B7E8E; background:transparent; border:none;")
                lbl_u.setFixedWidth(30 if len(unite) <= 3 else 46)
                lbl_u.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                rwl.addWidget(lbl_u)
                cvl.addWidget(rw)
            gl.addWidget(card, row, col)

        cfg = self.cfg
        act = cfg.get("actionneurs", {})
        reg = cfg.get("regulation", {})
        mdb = cfg.get("modbus", {})

        # ═══════════════════════════════════════════════════════════════
        # SECTION A : CALCUL REYNOLDS (paramètres physiques des formules)
        # ═══════════════════════════════════════════════════════════════

        # Titre section A
        titre_a = QLabel("CALCUL REYNOLDS — Paramètres physiques")
        titre_a.setStyleSheet(
            f"font-size:13px; font-weight:700; color:{COLORS['accent']};"
            " background:transparent; padding:12px 0 8px 0;")
        gl.addWidget(titre_a, 0, 0, 1, 2)  # span 2 colonnes

        # colonne gauche - section A
        _grp("Fluide", [
            (("patm_pa",),                      "Pression atmosphérique", "Pa",   "{:.2f}", _positif),
            (("fluide","gamma"),                "γ (rapport Cp/Cv)",      "—",    "{:.5f}", _positif),
            (("fluide","humidite_relative"),    "Humidité relative",      "%",    "{:.1f}", _humidite_relative),
        ], col=0, row=1)

        _grp("Viscosité dynamique (Sutherland)", [
            (("fluide","sutherland_mu_ref"),  "μ référence",      "Pa·s", "{:.2e}", _positif),
            (("fluide","sutherland_t_ref"),   "T référence",      "K",    "{:.2f}", _positif),
            (("fluide","sutherland_s"),       "Constante S",      "K",    "{:.1f}", _positif),
        ], col=0, row=2)

        _grp("Géométrie veine d'essai (rectangulaire)", [
            (("veine","hauteur_m"),             "Hauteur H",        "m",    "{:.3f}", _positif),
            (("veine","largeur_m"),             "Largeur W",        "m",    "{:.3f}", _positif),
            (("veine","section_m2"),            "Section S = H×W",  "m²",   "{:.5f}", None),
            (("veine","longueur_caracteristique_m"),"Dh = 4S/P",    "m",    "{:.6f}", None),
        ], col=0, row=3)

        # colonne droite - section A
        _grp("Diaphragme ISO 5167", [
            (("diaphragme_iso","D20_mm"),          "D conduite",       "mm",   "{:.2f}", _positif),
            (("diaphragme_iso","d20_mm"),           "d orifice",        "mm",   "{:.3f}", _beta_iso),
            (("diaphragme_iso","alpha_conduite"),   "α conduite",       "1/°C", "{:.2e}", _alpha),
            (("diaphragme_iso","alpha_diaphragme"), "α diaphragme",     "1/°C", "{:.2e}", _alpha),
            (("diaphragme_iso","formule_C"),        "Formule C",        "—",    "{}",     None),
        ], col=1, row=1)

        _grp("Diaphragme Miller", [
            (("diaphragme_miller","D20_mm"),          "D conduite",   "mm",   "{:.2f}", _positif),
            (("diaphragme_miller","d20_mm"),           "d orifice",    "mm",   "{:.3f}",
             _d_inferieur_D("diaphragme_miller")),
            (("diaphragme_miller","alpha_conduite"),   "α conduite",   "1/°C", "{:.2e}", _alpha),
            (("diaphragme_miller","alpha_diaphragme"), "α diaphragme", "1/°C", "{:.2e}", _alpha),
        ], col=1, row=2)

        # Encart caractéristiques de l'air (temps réel) - section A
        air_card = QWidget()
        air_card.setStyleSheet(
            "QWidget { background:#FFFFFF; border:1px solid #DBE5EA; border-radius:10px; }")
        acl = QVBoxLayout(air_card)
        acl.setContentsMargins(0, 0, 0, 0)
        acl.setSpacing(0)
        # titre
        ath = QWidget()
        ath.setStyleSheet("background:transparent; border-bottom:1px solid #DBE5EA;")
        athl = QHBoxLayout(ath)
        athl.setContentsMargins(16, 12, 16, 12)
        alt = QLabel("CARACTÉRISTIQUES DE L'AIR (TEMPS RÉEL)")
        alt.setStyleSheet(
            f"font-size:10px; font-weight:700; letter-spacing:.12em;"
            f" color:{COLORS['accent']}; background:transparent; border:none;")
        athl.addWidget(alt)
        acl.addWidget(ath)

        # Température (temps réel, capteur)
        arow_t = QWidget()
        arow_t.setStyleSheet("background:transparent; border-bottom:1px solid #E9EEF1;")
        artl = QHBoxLayout(arow_t)
        artl.setContentsMargins(16, 11, 16, 11)
        artl.setSpacing(8)
        albl_t = QLabel("Température")
        albl_t.setStyleSheet("font-size:12.5px; color:#3A4D60; background:transparent; border:none;")
        artl.addWidget(albl_t)
        artl.addStretch()
        self._lbl_air_temp = QLabel("—")
        self._lbl_air_temp.setStyleSheet(
            "font-family:'IBM Plex Mono',monospace; font-size:12px; font-weight:600;"
            " color:#14223C; background:transparent; border:none;")
        artl.addWidget(self._lbl_air_temp)
        albl_tu = QLabel("°C")
        albl_tu.setStyleSheet("font-size:11px; color:#6B7E8E; background:transparent; border:none;")
        albl_tu.setFixedWidth(46)
        albl_tu.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        artl.addWidget(albl_tu)
        acl.addWidget(arow_t)

        # Masse volumique (ρ)
        arow_rho = QWidget()
        arow_rho.setStyleSheet("background:transparent; border-bottom:1px solid #E9EEF1;")
        arhol = QHBoxLayout(arow_rho)
        arhol.setContentsMargins(16, 11, 16, 11)
        arhol.setSpacing(8)
        albl_rho = QLabel("Masse volumique ρ")
        albl_rho.setStyleSheet("font-size:12.5px; color:#3A4D60; background:transparent; border:none;")
        arhol.addWidget(albl_rho)
        arhol.addStretch()
        self._lbl_air_rho = QLabel("—")
        self._lbl_air_rho.setStyleSheet(
            "font-family:'IBM Plex Mono',monospace; font-size:12px; font-weight:600;"
            f" color:{COLORS['accent']}; background:transparent; border:none;")
        arhol.addWidget(self._lbl_air_rho)
        albl_rhou = QLabel("kg/m³")
        albl_rhou.setStyleSheet("font-size:11px; color:#6B7E8E; background:transparent; border:none;")
        albl_rhou.setFixedWidth(46)
        albl_rhou.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        arhol.addWidget(albl_rhou)
        acl.addWidget(arow_rho)

        # Viscosité dynamique (μ)
        arow_mu = QWidget()
        arow_mu.setStyleSheet("background:transparent; border-bottom:1px solid #E9EEF1;")
        armul = QHBoxLayout(arow_mu)
        armul.setContentsMargins(16, 11, 16, 11)
        armul.setSpacing(8)
        albl_mu = QLabel("Viscosité dynamique μ")
        albl_mu.setStyleSheet("font-size:12.5px; color:#3A4D60; background:transparent; border:none;")
        armul.addWidget(albl_mu)
        armul.addStretch()
        self._lbl_air_mu = QLabel("—")
        self._lbl_air_mu.setStyleSheet(
            "font-family:'IBM Plex Mono',monospace; font-size:12px; font-weight:600;"
            f" color:{COLORS['accent']}; background:transparent; border:none;")
        armul.addWidget(self._lbl_air_mu)
        albl_muu = QLabel("Pa·s")
        albl_muu.setStyleSheet("font-size:11px; color:#6B7E8E; background:transparent; border:none;")
        albl_muu.setFixedWidth(46)
        albl_muu.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        armul.addWidget(albl_muu)
        acl.addWidget(arow_mu)

        # Viscosité cinématique (ν)
        arow_nu = QWidget()
        arow_nu.setStyleSheet("background:transparent;")
        arnul = QHBoxLayout(arow_nu)
        arnul.setContentsMargins(16, 11, 16, 11)
        arnul.setSpacing(8)
        albl_nu = QLabel("Viscosité cinématique ν")
        albl_nu.setStyleSheet("font-size:12.5px; color:#3A4D60; background:transparent; border:none;")
        arnul.addWidget(albl_nu)
        arnul.addStretch()
        self._lbl_air_nu = QLabel("—")
        self._lbl_air_nu.setStyleSheet(
            "font-family:'IBM Plex Mono',monospace; font-size:12px; font-weight:600;"
            f" color:{COLORS['accent']}; background:transparent; border:none;")
        arnul.addWidget(self._lbl_air_nu)
        albl_nuu = QLabel("m²/s")
        albl_nuu.setStyleSheet("font-size:11px; color:#6B7E8E; background:transparent; border:none;")
        albl_nuu.setFixedWidth(46)
        albl_nuu.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        arnul.addWidget(albl_nuu)
        acl.addWidget(arow_nu)

        # Section repliable "Détails" pour Pvs, Pv, x, v, h
        # Bouton pour afficher/masquer les détails
        self._air_details_visible = False
        arow_toggle = QWidget()
        arow_toggle.setStyleSheet("background:transparent; border-top:1px solid #E9EEF1;")
        atogl = QHBoxLayout(arow_toggle)
        atogl.setContentsMargins(16, 8, 16, 8)
        self._btn_air_details = QPushButton("▸ Afficher les détails")
        self._btn_air_details.setStyleSheet(
            "QPushButton { font-size:11px; color:#6B7E8E; background:transparent; "
            "border:none; text-align:left; padding:0; }"
            "QPushButton:hover { color:#3A4D60; }")
        self._btn_air_details.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_air_details.clicked.connect(self._basculer_air_details)
        atogl.addWidget(self._btn_air_details)
        atogl.addStretch()
        acl.addWidget(arow_toggle)

        # Container des détails (initialement masqué)
        self._air_details_container = QWidget()
        self._air_details_container.setStyleSheet("background:transparent;")
        self._air_details_container.setVisible(False)
        adetails = QVBoxLayout(self._air_details_container)
        adetails.setContentsMargins(0, 0, 0, 0)
        adetails.setSpacing(0)

        # Pvs - Pression de vapeur saturante
        def _make_detail_row(label, unit):
            row = QWidget()
            row.setStyleSheet("background:transparent; border-bottom:1px solid #E9EEF1;")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(16, 8, 16, 8)
            rl.setSpacing(8)
            lbl = QLabel(label)
            lbl.setStyleSheet("font-size:11px; color:#6B7E8E; background:transparent; border:none;")
            rl.addWidget(lbl)
            rl.addStretch()
            val_lbl = QLabel("—")
            val_lbl.setStyleSheet(
                "font-family:'IBM Plex Mono',monospace; font-size:11px;"
                " color:#6B7E8E; background:transparent; border:none;")
            rl.addWidget(val_lbl)
            u_lbl = QLabel(unit)
            u_lbl.setStyleSheet("font-size:10px; color:#9EAAB5; background:transparent; border:none;")
            u_lbl.setFixedWidth(46)
            u_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            rl.addWidget(u_lbl)
            return row, val_lbl

        pvs_row, self._lbl_air_Pvs = _make_detail_row("Pvs (vapeur saturante)", "Pa")
        adetails.addWidget(pvs_row)

        pv_row, self._lbl_air_Pv = _make_detail_row("Pv (partielle vapeur)", "Pa")
        adetails.addWidget(pv_row)

        x_row, self._lbl_air_x = _make_detail_row("x (humidité spécifique)", "kg/kg")
        adetails.addWidget(x_row)

        v_row, self._lbl_air_v = _make_detail_row("v (volume spécifique)", "m³/kg")
        adetails.addWidget(v_row)

        h_row, self._lbl_air_h = _make_detail_row("h (enthalpie)", "kJ/kg")
        adetails.addWidget(h_row)

        acl.addWidget(self._air_details_container)

        gl.addWidget(air_card, 4, 0)

        # ═══════════════════════════════════════════════════════════════
        # SECTION B : PILOTAGE & RÉGULATION (paramètres techniques)
        # ═══════════════════════════════════════════════════════════════

        # Titre section B
        titre_b = QLabel("PILOTAGE & RÉGULATION — Paramètres techniques")
        titre_b.setStyleSheet(
            f"font-size:13px; font-weight:700; color:{COLORS['accent']};"
            " background:transparent; padding:24px 0 8px 0;")
        gl.addWidget(titre_b, 5, 0, 1, 2)  # span 2 colonnes

        # colonne gauche - section B
        _grp("Régulation PI", [
            (("regulation","periode_s"),      "Période",          "s",    "{:.1f}", _positif),
            (("regulation","kp"),             "Kp",               "—",    "{:.2f}", _positif_ou_nul),
            (("regulation","ki"),             "Ki",               "—",    "{:.2f}", _positif_ou_nul),
            (("regulation","rampe_v_par_s"),  "Rampe variateur",  "V/s",  "{:.1f}", _positif),
            (("regulation","tolerance_convergence_relative"), "Tolérance conv.", "%",
             lambda v, _: f"{v*100:.0f}", None),
        ], col=0, row=6)

        _grp("Surveillance et replis", [
            (("regulation","fenetre_convergence_s"),   "Fenêtre convergence", "s",
             "{:.1f}", _positif),
            (("regulation","temperature_min_c"),       "PT100 mini",       "°C",
             "{:.1f}", None),
            (("regulation","temperature_max_c"),       "PT100 maxi",       "°C",
             "{:.1f}", None),
            (("regulation","saturation_dp_relative"),  "Seuil ΔP saturé",  "×",
             "{:.2f}", _positif),
            (("regulation","recul_saturation_v_par_s"),"Recul si saturé", "V/s",
             "{:.2f}", _positif),
        ], col=0, row=7)

        # colonne droite - section B
        _grp("Sélection diaphragme", [
            (("selection_diaphragme","seuil_re"),     "Seuil Re",    "—", "{:.0f}", _positif),
            (("selection_diaphragme","hysteresis_re"),"Hystérésis",  "±", "{:.0f}", _positif_ou_nul),
        ], col=1, row=6)

        _grp("Actionneurs — temps de course", [
            (("actionneurs","vanne_3voies_temps_s"),      "Vanne 3 voies",    "s",  "{:.1f}", _positif),
            (("actionneurs","vanne_bypass_temps_s"),       "Vanne by-pass",    "s",  "{:.1f}", _positif),
            (("actionneurs","vanne_perte_charge_course_s"),"Perte de charge",  "s",  "{:.1f}", _positif),
            (("actionneurs","facteur_chien_garde"),        "Chien de garde",   "×",  "{:.1f}", _positif),
        ], col=1, row=7)

        _grp("Communication Modbus", [
            (("modbus","ip"),         "Adresse IP",  "",    "{}",     None),
            (("modbus","port"),       "Port",        "",    "{}",     None),
            (("modbus","timeout_s"),  "Timeout",     "s",   "{:.1f}", None),
            (("variateur","frequence_max_hz"), "Variateur n011", "Hz",
             "{:.1f}", _positif),
        ], col=1, row=8)

        scroll.setWidget(corps)
        vl.addWidget(scroll, 1)
        return page

    # ══════════════════════════════════════════════════════════════ COMMANDES

    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        if mode == "manuel":
            # Capturer les valeurs courantes avant d'arrêter la régulation
            e = self.pilote.etat
            u_courant = e.get("u_variateur", 0.0)
            pdc_v_courant = e.get("pdc_consigne", 0.0)
            if e.get("regulation"):
                self.pilote.arreter_regulation()
            self.pilote.mode_manuel_tension(0.0)
            # Pré-remplir les contrôles avec les valeurs qui étaient actives
            u_aff = round(max(0.0, min(u_courant, 10.0)), 1)
            self._slider_u.setValue(round(u_aff * 10))
            self._inp_vman.setText(f"{u_aff:.1f}")
            pdc_pct = round(max(0.0, min(pdc_v_courant * 10.0, 100.0)))
            self._slider_pdc.setValue(pdc_pct)
            self._inp_pdc.setText(str(pdc_pct))
            self._lbl_pdc_v.setText(f"≙ {pdc_v_courant:.1f} V — 0 V = fermée")
        elif mode == "asservi":
            self.pilote.mode_asservi()
        elif mode == "sequence":
            self._nav(2)                     # afficher l'onglet Séquence
        self._update_mode_seg()
        self._update_controls_enabled()
        self._log(f"Mode → {mode}", COLORS["accent"])

    def _update_controls_enabled(self) -> None:
        """Active ou inhibe les contrôles manuels selon le mode courant :
        en asservi et en séquence, les actionneurs sont pilotés par la
        régulation — commandes manuelles inhibées.

        En mode manuel, la consigne Reynolds est désactivée (seuls les
        actionneurs sont pilotables manuellement)."""
        en_manuel = (self._mode == "manuel")
        en_asservi = (self._mode == "asservi")

        # Contrôles actionneurs : actifs uniquement en mode manuel
        self._slider_u.setEnabled(en_manuel)
        self._inp_vman.setEnabled(en_manuel)
        self._slider_pdc.setEnabled(en_manuel)
        self._inp_pdc.setEnabled(en_manuel)
        for btn in self._diaph_btns:
            btn.setEnabled(en_manuel)
        for btn in self._bypass_btns:
            btn.setEnabled(en_manuel)

        # Consigne Reynolds : active uniquement en mode asservi
        self._slider_re.setEnabled(en_asservi)
        self.inp_re.setEnabled(en_asservi)
        for btn in self._ref_btns:
            btn.setEnabled(en_asservi)

    def _update_mode_seg(self) -> None:
        idx = {"manuel": 0, "asservi": 1, "sequence": 2}.get(self._mode, 1)
        self._seg_set(self._mode_btns, idx)
        self._lbl_mode_badge.setText(f"mode {self._mode}")

    def _appliquer(self) -> None:
        if self._mode == "asservi":
            try:
                re_val = float(
                    getattr(self, "inp_re").text().replace(",", "."))
                if re_val > 0:
                    self.pilote.definir_consigne(re_val, self._ref_re)
                    self.pilote.demarrer_regulation()
                    self._log(f"Consigne Re → {re_val:.0f} ({self._ref_re})",
                              COLORS["accent"])
                    self._dirty["re"] = False
            except ValueError:
                pass
        elif self._mode == "manuel":
            try:
                v = float(self._inp_vman.text().replace(",", "."))
                v = min(max(v, 0.0), 10.0)
                self.pilote.commander_variateur_manuel(v)
                self._log(f"Variateur → {v:.1f} V", COLORS["accent"])
                self._dirty["vman"] = False
            except ValueError:
                pass

        # PdC toujours
        try:
            pct_ferm = float(self._inp_pdc.text().replace(",", "."))
            pct_ferm = min(max(pct_ferm, 0.0), 100.0)
            # Convertir % fermeture → % ouverture pour le pilote
            pct_ouv = 100.0 - pct_ferm
            self.pilote.commander_vanne_perte_charge_pct(pct_ouv)
            self._log(f"PdC → {pct_ferm:.0f} % fermeture", "#FF9900")
            self._dirty["pdc"] = False
        except ValueError:
            pass

        self._update_dirty()

    def _arreter(self) -> None:
        # coupe régulation ET mode manuel (le pilote remet aussi RUN à 0)
        self.pilote.arreter_regulation()
        self._log("ARRÊT VARIATEUR", COLORS["error"])

    def _quitter(self) -> None:
        self.pilote.arreter_regulation()
        self.pilote.commander_vanne_perte_charge(10.0)   # PdC grande ouverte (10 V)
        self.pilote.commander_bypass(False)              # bypass fermé
        self.pilote.commander_diaphragme("iso")          # diaphragme ISO
        self.close()

    def _cmd_diaph(self, pos: str) -> None:
        self.pilote.commander_diaphragme(pos)
        self._log(f"Vanne Diaphragme → {pos.upper()}", "#FF9900")

    def _cmd_bypass(self, ouvert: bool) -> None:
        self.pilote.commander_bypass(ouvert)
        self._log(f"Bypass → {'Ouvert' if ouvert else 'Fermé'}", COLORS["ok"])

    def _calculer_k(self) -> float:
        """Calcule le facteur de conversion Re_conduite / Re_veine à partir
        des dimensions actuelles de la veine (veine interchangeable)."""
        # Débit massique de référence pour le calcul de k
        qm_ref = 0.028402  # kg/s
        T = 20.0
        P1_abs = self.pilote.banc.patm_pa
        res = self.pilote.banc.qm_vers_re(qm_ref, T, P1_abs)
        return res["re_conduite"] / res["re_veine"] if res["re_veine"] > 0 else 1.0

    def _re_max_pour_reference(self, ref: str) -> int:
        """Calcule le Re maximum pour la référence donnée (conduite ou veine).

        - Mode CONDUITE : max fixe à 25000 Re
        - Mode VEINE : calculé dynamiquement depuis re_max_iso et dimensions veine
        """
        if ref == "conduite":
            return 25000  # Maximum fixe en mode conduite
        else:  # veine
            re_max_iso = self.pilote.re_max_iso or 24500
            k = self._calculer_k()
            return int(re_max_iso / k)

    def _set_ref_re(self, ref: str) -> None:
        """Change la référence Re (conduite/veine) et adapte les limites du slider."""
        self._ref_re = ref
        self._seg_set(self._ref_btns, 0 if ref == "conduite" else 1)

        # Calculer le Re max et l'échelle pour cette référence
        re_max = self._re_max_pour_reference(ref)
        slider_max = 100
        self._echelle_re = re_max / slider_max

        # Adapter la consigne actuelle si elle dépasse le nouveau maximum
        try:
            consigne_actuelle = int(self.inp_re.text().replace(",", ".").replace(" ", ""))
            if consigne_actuelle > re_max:
                self.inp_re.setText(str(re_max))
                self._slider_re.blockSignals(True)
                self._slider_re.setValue(slider_max)
                self._slider_re.blockSignals(False)
        except (ValueError, AttributeError):
            pass

        self._mark_dirty("re")

    # ── slider/input sync ──────────────────────────────────────────────────

    def _on_re_slider(self, v: int) -> None:
        """Slider Re déplacé : mettre à jour l'input avec l'échelle dynamique."""
        re_val = int(v * self._echelle_re)
        self.inp_re.setText(str(re_val))
        self._mark_dirty("re")

    def _on_re_input(self, text: str) -> None:
        """Input Re modifié : synchroniser le slider avec l'échelle dynamique."""
        try:
            re_val = int(text.replace(",", ".").replace(" ", ""))
            # Synchroniser le slider avec la valeur saisie
            slider_val = int(re_val / self._echelle_re)
            self._slider_re.blockSignals(True)
            self._slider_re.setValue(slider_val)
            self._slider_re.blockSignals(False)
        except ValueError:
            pass
        self._mark_dirty("re")

    def _on_vman_slider(self, v: int) -> None:
        self._inp_vman.setText(f"{v / 10:.1f}")
        self._mark_dirty("vman")

    def _on_vman_input(self, text: str) -> None:
        try:
            v_val = float(text.replace(",", "."))
            # Synchroniser le slider avec la valeur saisie (V * 10)
            slider_val = int(v_val * 10)
            self._slider_u.blockSignals(True)
            self._slider_u.setValue(slider_val)
            self._slider_u.blockSignals(False)
        except ValueError:
            pass
        self._mark_dirty("vman")

    def _on_pdc_slider(self, v: int) -> None:
        """Slider PDC: v = % fermeture (0=ouverte, 100=fermée)."""
        self._inp_pdc.setText(str(v))
        # Convertir % fermeture en volts: 0% → Umax, 100% → Umin
        volts = self._pdc_umax - (v / 100.0) * (self._pdc_umax - self._pdc_umin)
        self._lbl_pdc_v.setText(f"≙ {volts:.2f} V — {self._pdc_umin:.1f}V = fermée")
        self._mark_dirty("pdc")

    def _on_pdc_input(self, text: str) -> None:
        """Input PDC: saisie directe % fermeture."""
        try:
            pct_ferm = float(text.replace(",", "."))
            pct_ferm = max(0, min(100, pct_ferm))  # Limiter à [0, 100]
            # Convertir % fermeture en volts
            volts = self._pdc_umax - (pct_ferm / 100.0) * (self._pdc_umax - self._pdc_umin)
            self._lbl_pdc_v.setText(f"≙ {volts:.2f} V — {self._pdc_umin:.1f}V = fermée")
            # Synchroniser le curseur avec la valeur saisie
            self._slider_pdc.blockSignals(True)
            self._slider_pdc.setValue(int(pct_ferm))
            self._slider_pdc.blockSignals(False)
        except ValueError:
            pass
        self._mark_dirty("pdc")

    def _mark_dirty(self, key: str) -> None:
        self._dirty[key] = True
        self._update_dirty()

    def _update_dirty(self) -> None:
        for key in ("re", "vman", "pdc"):
            dot = getattr(self, f"_dot_{key}", None)
            if dot:
                dot.setVisible(self._dirty.get(key, False))
        count = sum(1 for v in self._dirty.values() if v)
        label = f"✓  Appliquer ({count})" if count else "✓  Appliquer"
        self._btn_apply.setText(label)

    # ── action log ────────────────────────────────────────────────────────

    def _log(self, msg: str, color: str = "#6B7E8E") -> None:
        self._action_log.appendleft({
            "t":   datetime.now().strftime("%H:%M:%S"),
            "dot": color,
            "msg": msg,
        })

    # ══════════════════════════════════════════════════════════ APPRENTISSAGE

    def _lancer_test(self, test: str, **kw) -> None:
        err = self.apprentissage.demarrer(test, **kw)
        if err:
            QMessageBox.warning(self, "Apprentissage", err)

    def _lancer_indiciel(self) -> None:
        try:
            u1 = float(self.inp_u1.text().replace(",", "."))
            u2 = float(self.inp_u2.text().replace(",", "."))
            if not (0 <= u1 < u2 <= 10):
                raise ValueError
        except ValueError:
            QMessageBox.critical(self, "Essai indiciel",
                                 "0 ≤ U1 < U2 ≤ 10 V requis.")
            return
        self._lancer_test("indiciel", u1=u1, u2=u2)

    def _appliquer_apprentissage(self) -> None:
        rep = QMessageBox.question(
            self, "Confirmer",
            "Appliquer les résultats mesurés à la configuration ?")
        if rep != QMessageBox.StandardButton.Yes:
            return
        resume = self.apprentissage.appliquer()
        self.lbl_app_statut.setStyleSheet(f"color:{COLORS['ok']};")
        self.lbl_app_statut.setText(resume)

    # ══════════════════════════════════════════════════════════ CONFIGURATION

    def _basculer_air_details(self) -> None:
        """Affiche/masque les détails des propriétés de l'air humide."""
        self._air_details_visible = not self._air_details_visible
        self._air_details_container.setVisible(self._air_details_visible)
        if self._air_details_visible:
            self._btn_air_details.setText("▾ Masquer les détails")
        else:
            self._btn_air_details.setText("▸ Afficher les détails")

    def _basculer_verrouillage(self) -> None:
        unlock = self.chk_unlock.isChecked()
        if unlock and self.pilote.etat.get("regulation"):
            QMessageBox.warning(self, "Régulation active",
                                "Arrêtez la régulation avant de modifier.")
            self.chk_unlock.setChecked(False)
            return
        for entree, _ in self.champs.values():
            entree.setEnabled(unlock)
        self.btn_enregistrer.setEnabled(unlock)
        self.lbl_cfg_statut.setText("")

    def _recharger_champs(self) -> None:
        for chemin, (entree, _) in self.champs.items():
            try:
                entree.setText(str(_cfg_lire(self.cfg, chemin)))
            except (KeyError, TypeError):
                pass
        self.lbl_cfg_statut.setStyleSheet(f"color:{COLORS['muted']};")
        self.lbl_cfg_statut.setText("modifications annulées")

    def _enregistrer_configuration(self) -> None:
        valeurs, erreurs = {}, []
        for chemin, (entree, validation) in self.champs.items():
            brut = entree.text().strip().replace(",", ".")
            try:
                valeurs[chemin] = float(brut)
            except ValueError:
                erreurs.append(f"« {brut} » invalide")
        if not erreurs:
            for chemin, (_, validation) in self.champs.items():
                if chemin in valeurs:
                    err = validation(valeurs[chemin], valeurs)
                    if err:
                        erreurs.append(f"{chemin[-1]} : {err}")
        if erreurs:
            QMessageBox.critical(self, "Saisie invalide", "\n".join(erreurs))
            return

        # Recalculer S et Dh à partir de H et W si modifiés
        h_key = ("veine", "hauteur_m")
        w_key = ("veine", "largeur_m")
        if h_key in valeurs and w_key in valeurs:
            H = valeurs[h_key]
            W = valeurs[w_key]
            S = H * W
            P = 2 * (H + W)
            Dh = 4 * S / P
            valeurs[("veine", "section_m2")] = S
            valeurs[("veine", "longueur_caracteristique_m")] = Dh

        # Le deverrouillage verifie l'arret de la regulation, mais rien
        # n'empechait de demarrer ENTRE le deverrouillage et la sauvegarde.
        # appliquer_config relit des parametres utilises par la boucle temps
        # reel (gains, periode, fenetre de moyennage) : les changer sous ses
        # pieds n'a rien d'anodin.
        if self.pilote.etat.get("regulation"):
            QMessageBox.warning(
                self, "Régulation active",
                "La régulation a démarré depuis le déverrouillage.\n\n"
                "Arrêtez-la avant d'enregistrer : ces paramètres sont lus "
                "par la boucle temps réel.")
            return
        rep = QMessageBox.question(
            self, "Confirmer",
            "Enregistrer ces paramètres dans config.json ?")
        if rep != QMessageBox.StandardButton.Yes:
            return
        for chemin, v in valeurs.items():
            _cfg_ecrire(self.cfg, chemin, v)
        try:
            self.chemin_cfg.write_text(
                json.dumps(self.cfg, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "Écriture impossible", str(exc))
            return
        self.pilote.appliquer_banc(Banc(self.cfg))
        # le Banc ne porte que les geometries et les seuils de selection ;
        # gains, tolerances, chiens de garde et bornes capteurs sont mis en
        # cache par le pilote et doivent etre relus, sinon la saisie
        # n'agirait qu'au redemarrage suivant
        self.pilote.appliquer_config(self.cfg)
        self.chk_unlock.setChecked(False)
        self.lbl_cfg_statut.setStyleSheet(f"color:{COLORS['ok']};")
        self.lbl_cfg_statut.setText("✓ configuration enregistrée et appliquée")

    # ══════════════════════════════════════════════════════════ RAFRAÎCHISSEMENT

    def _section(self, nom: str, fonction, *args) -> None:
        """Rafraîchit une section d'affichage en l'isolant des autres.

        Sans cela, une seule exception dans un formateur arrête le QTimer et
        fige l'IHM ENTIÈRE — mesures, états de vannes et alertes de sécurité
        comprises. C'est arrivé : une clé renommée côté cœur levait un
        KeyError à chaque tick. Une section fautive doit dégrader son propre
        panneau, pas emporter la supervision.

        Le motif n'est journalisé qu'une fois par section : à 5 Hz, une
        panne permanente noierait le journal d'action en quelques secondes.
        """
        try:
            fonction(*args)
        except Exception as exc:                 # noqa: BLE001
            cle = f"{nom}:{type(exc).__name__}:{exc}"
            if cle not in self._sections_en_defaut:
                self._sections_en_defaut.add(cle)
                self._log(f"Affichage « {nom} » indisponible : "
                          f"{type(exc).__name__} {exc}", COLORS["error"])

    def _tick(self) -> None:
        e = self.pilote.etat
        self._section("mesures", self._tick_left, e)
        self._section("supervision", self._tick_right, e)
        self._section("pied de page", self._tick_footer, e)
        self._section("voies", self._tick_entrees, e)
        self._section("apprentissage", self._tick_apprentissage)
        self._section("séquence", self._tick_sequence, e)
        self._section("configuration", self._tick_config, e)
        self._section("maintenance", self._tick_mise_en_service, e)
        self._section("courbe", self._historiser, e)

    def _historiser(self, e: dict) -> None:
        """Alimente la courbe. Isolé comme les autres sections : pyqtgraph
        lève sur une valeur non finie ou non numérique, et cette exception
        remontait jusqu'au QTimer."""
        re = e.get("re_mesure")
        if not isinstance(re, (int, float)) or not math.isfinite(re):
            return
        cible = e.get("re_cible", 0.0)
        if not isinstance(cible, (int, float)) or not math.isfinite(cible):
            cible = 0.0
        if self._t0_acq is None:
            self._t0_acq = datetime.now()
        self._hist_re.append(re)
        self._hist_cible.append(cible)
        self._tracer()

    def _tick_left(self, e: dict) -> None:
        brutes = e.get("mesures_brutes") or {}
        # Résolution des canaux depuis mesures_brutes ou etat
        vals = {
            "qv":          e.get("qv"),
            "rho":         e.get("rho"),
            "mu":          e.get("mu"),
            "nu":          e.get("nu"),
            "p_amont":     brutes.get("p_amont_500",    e.get("p_amont")),
            "dp_iso":      brutes.get("dp_iso_500"),
            "dp_miller":   brutes.get("dp_diaph_500"),  # addr 0 = Miller
            "temperature": e.get("temperature") or brutes.get("temperature"),
            "u_variateur": brutes.get("frequence_variateur", e.get("u_variateur")),
        }
        for key, lv, bar, fmt, max_val in self._ch_rows:
            val = vals.get(key)
            # Afficher "—" seulement si vraiment None (pas si 0)
            # Pour rho, mu, nu: 0 est une valeur invalide, mais ils ne devraient jamais être 0
            if val is None:
                lv.setText("—")
                bar.setValue(0)
            else:
                # Formatage avec remplacement e → E pour notation scientifique
                txt = fmt.format(val).replace(".", ",").replace("e", "E")
                lv.setText(txt)
                # Pour la barre: si val très petit (mu, nu), normaliser quand même
                bar.setValue(int(min(max(val / max_val, 0.0), 1.0) * 1000))

        # ── Actionneurs ─────────────────────────────────────────────────────
        u = e.get("u_variateur")
        is_running = (u or 0) > 0.1
        self._act_vals["variateur"].setText(
            f"{u:.2f} V".replace(".", ",") if u is not None else "—")
        self._act_dots["variateur"].setStyleSheet(
            f"color:{'#00B8DE' if is_running else '#DBE5EA'};"
            " font-size:10px; background:transparent;")
        self._act_bars["variateur"].setValue(
            int(min((u or 0) / 10.0, 1.0) * 1000))
        self._act_subs["variateur"].setText("Arrêté" if not is_running else
                                            f"{u:.2f} V".replace(".", ","))

        d = e.get("diaphragme", "")
        d_txt = {"iso": "ISO", "miller": "Miller"}.get(d, "—")
        self._act_vals["vanne3"].setText(d_txt)
        self._act_dots["vanne3"].setStyleSheet(
            f"color:{'#FF9900' if d in ('iso','miller') else '#DBE5EA'};"
            " font-size:10px; background:transparent;")
        self._act_bars["vanne3"].setValue(
            1000 if d in ("iso", "miller") else 0)
        self._act_subs["vanne3"].setText(f"Position {d_txt}" if d else "—")

        manoeuvre_bp = "bypass" in (e.get("manoeuvre") or "")
        if manoeuvre_bp:
            bp_val, bp_sub, bp_col, bp_pct = "Manœuvre…", "Manœuvre…", COLORS["warn"], 500
        elif brutes.get("fc_bypass_ouvert"):
            bp_val, bp_sub, bp_col, bp_pct = "Ouverte", "Ouverte", "#2BB673", 1000
        elif brutes.get("fc_bypass_ferme"):
            bp_val, bp_sub, bp_col, bp_pct = "Fermée", "Fermée", "#DBE5EA", 0
        else:
            bp_val, bp_sub, bp_col, bp_pct = "—", "—", "#DBE5EA", 0
        self._act_vals["bypass"].setText(bp_val)
        self._act_dots["bypass"].setStyleSheet(
            f"color:{bp_col}; font-size:10px; background:transparent;")
        self._act_bars["bypass"].setValue(bp_pct)
        self._act_subs["bypass"].setText(bp_sub)

        pdc = e.get("pdc_position")
        pdc_pct = self._pdc_v_vers_pct(pdc)
        self._act_vals["pdc"].setText(
            f"{pdc_pct} %" if pdc is not None else "—")
        self._act_dots["pdc"].setStyleSheet(
            f"color:{'#FF9900' if pdc_pct > 10 else '#DBE5EA'};"
            " font-size:10px; background:transparent;")
        self._act_bars["pdc"].setValue(pdc_pct * 10)
        self._act_subs["pdc"].setText(
            f"{pdc:.2f} V".replace(".", ",") if pdc is not None else "—")

    def _tick_right(self, e: dict) -> None:
        re = e.get("re_mesure")
        self._lbl_re_big.setText(f"{re:.0f}" if re is not None else "—")

        # « ± 28  (0,5 %)  ·  60 s » : dispersion du Re sur la fenêtre de
        # convergence et durée du palier courant
        moyen = e.get("re_moyen")
        sigma = e.get("re_ecart_type")
        sigma_rel = e.get("re_ecart_type_relatif")
        depuis = e.get("convergee_depuis_s")
        morceaux = []
        if moyen is not None:
            # le grand chiffre est la mesure instantanée ; la dispersion ne
            # veut rien dire sans la valeur à laquelle elle se rapporte
            morceaux.append(f"moy. {moyen:.0f}")
        if sigma is not None:
            txt_s = f"± {sigma:.0f}"
            if sigma_rel is not None:
                txt_s += f" ({sigma_rel * 100:.1f} %)".replace(".", ",")
            morceaux.append(txt_s)
        if depuis is not None:
            morceaux.append(f"{depuis:.0f} s" if depuis < 120
                            else f"{depuis / 60:.0f} min")
        self._lbl_re_sub.setText("  ·  ".join(morceaux))

        # State badge
        if e.get("manoeuvre"):
            bg, fg, txt = "#FFF3E0", "#FF9900", "MANŒUVRE"
        elif e.get("manuel"):
            bg, fg, txt = "#FFF3E0", "#FF9900", "MANUEL"
        elif e.get("regulation"):
            seq = e.get("sequence")
            if seq and not seq.get("terminee"):
                bg, fg, txt = "#E3F4FB", COLORS["accentText"], \
                    f"SÉQUENCE {seq['etape']}/{seq['n']}"
            elif e.get("convergee"):
                bg, fg, txt = "#E6F9EF", "#1F8F5B", "RÉGULÉ ✓"
            else:
                bg, fg, txt = "#E3F4FB", COLORS["accentText"], "RÉGULATION…"
        else:
            bg, fg, txt = "#DBE5EA", "#6B7E8E", "ARRÊT"
        self._lbl_re_state.setStyleSheet(
            f"font-size:10px; font-weight:700; padding:3px 9px;"
            f" border-radius:5px; background:{bg}; color:{fg};")
        self._lbl_re_state.setText(txt)
        self._lbl_scope_state.setText(txt)
        self._lbl_scope_state.setStyleSheet(
            f"font-size:10.5px; font-family:'IBM Plex Mono',monospace;"
            f" font-weight:600; padding:5px 10px; border-radius:6px;"
            f" background:{bg}; color:{fg};")

        # Légende scope
        re_val = f"{re:.0f}" if re is not None else "—"
        sp_val = f"{e.get('re_cible', 0.0):.0f}"
        self._scope_legend_re.setText(
            f"<span style='color:#00B8DE'>▬</span>"
            f" Re calculé  <b style='color:#00B8DE'>{re_val}</b>")
        self._scope_legend_sp.setText(
            f"<span style='color:#FF9900'>╌</span>"
            f" Re consigne  <b style='color:#FF9900'>{sp_val}</b>")

        # Bas-Re
        if e.get("mode_bas_re"):
            # shown in right rail via log; nothing extra needed
            pass

        # Diaphragme segmenté
        d = e.get("diaphragme", "")
        dmap = {"iso": 0, "miller": 1}
        self._seg_set(self._diaph_btns, dmap.get(d, -1))

        # Bypass segmenté
        brutes = e.get("mesures_brutes") or {}
        if brutes.get("fc_bypass_ouvert"):
            self._seg_set(self._bypass_btns, 0)
        elif brutes.get("fc_bypass_ferme"):
            self._seg_set(self._bypass_btns, 1)

        # Journal
        entries = list(self._action_log)
        for i, (t_lbl, dot_lbl, msg_lbl) in enumerate(self._log_rows):
            if i < len(entries):
                ent = entries[i]
                t_lbl.setText(ent["t"])
                dot_lbl.setStyleSheet(
                    f"color:{ent['dot']}; font-size:9px; background:transparent;")
                msg_lbl.setText(ent["msg"])
            else:
                t_lbl.setText("")
                dot_lbl.setStyleSheet("color:#DBE5EA; font-size:9px; background:transparent;")
                msg_lbl.setText("")

        # Alertes : journalisées sur FRONT MONTANT.
        # L'ancienne déduplication comparait les 6 premiers caractères sur les
        # 3 dernières lignes : « ⚠ Défa » étant commun aux défauts vanne,
        # variateur et PdC, un défaut pouvait en masquer un autre — et un
        # défaut persistant se re-journalisait sans fin dès qu'il sortait de
        # la fenêtre de 3 lignes.
        actives = {cle for cle, _ in self.ALERTES if e.get(cle)}
        for cle, msg in self.ALERTES:
            if cle in actives and cle not in self._alertes_actives:
                self._log(f"{msg} : {e[cle]}" if isinstance(e[cle], str) else msg,
                          COLORS["error"])
        for cle, msg in self.ALERTES:
            if cle in self._alertes_actives and cle not in actives:
                self._log(f"{msg} — rétabli", COLORS["ok"])
        self._alertes_actives = actives

    def _tick_footer(self, e: dict) -> None:
        ok = not bool(e.get("erreur_com"))
        self._foot_cnx.setStyleSheet(
            f"color:{COLORS['ok'] if ok else COLORS['error']};"
            " font-size:10px; background:transparent;")

        if e.get("manoeuvre"):
            txt = f"MANŒUVRE — {e['manoeuvre']}"
            col = COLORS["warn"]
        elif e.get("manuel"):
            txt, col = "MANUEL", COLORS["warn"]
        elif e.get("regulation"):
            txt = "RÉGULATION ✓" if e.get("convergee") else "RÉGULATION…"
            col = COLORS["ok"] if e.get("convergee") else COLORS["accentText"]
        else:
            txt, col = "ARRÊT", COLORS["muted2"]
        self.lbl_etat.setText(txt)
        self.lbl_etat.setStyleSheet(
            f"font-family:'IBM Plex Mono',monospace; font-size:11px;"
            f" font-weight:700; color:{col}; background:transparent;")

        # durée réelle du cycle : un dépassement durable de la période
        # signale une communication Modbus lente (timeouts imminents)
        duree = e.get("duree_cycle_s")
        cycle_txt = ""
        if duree is not None:
            cycle_txt = f" · cycle {duree * 1000:.0f} ms"
            if duree > self.pilote.periode:
                cycle_txt += " ⚠"
        self._foot_samples.setText(
            f"Échantillons : {e.get('cycles', 0)}{cycle_txt}")

        # reverrouillage vivant : si la regulation demarre alors que les
        # champs sont deverrouilles, on retire la main avant la sauvegarde
        if e.get("regulation") and self.chk_unlock.isChecked():
            self.chk_unlock.setChecked(False)
            self.lbl_cfg_statut.setStyleSheet(f"color:{COLORS['warn']};")
            self.lbl_cfg_statut.setText(
                "paramètres reverrouillés : régulation démarrée")

        journal = e.get("journal")
        erreur_j = e.get("journal_erreur")
        if erreur_j:
            self._foot_journal.setVisible(True)
            self._foot_journal.setText(f"⚠ {erreur_j}")
            self._foot_journal.setStyleSheet(
                f"color:{COLORS['error']}; font-family:'IBM Plex Mono',"
                "monospace; font-size:11px; background:transparent;")
        elif journal:
            from pathlib import Path as _P
            self._foot_journal.setVisible(True)
            self._foot_journal.setText(
                f"⏺ {_P(journal).name} · {e.get('journal_lignes', 0)} l.")
            self._foot_journal.setStyleSheet(
                f"color:{COLORS['muted2']}; font-family:'IBM Plex Mono',"
                "monospace; font-size:11px; background:transparent;")
        else:
            self._foot_journal.setVisible(False)

        if self._recording:
            self._foot_rec.setVisible(True)
            self._foot_rec.setText("● REC")
            self._foot_rec.setStyleSheet(
                f"color:{COLORS['error']}; font-family:'IBM Plex Mono',"
                "monospace; font-size:11px; background:transparent;")
        else:
            self._foot_rec.setVisible(False)

    def _tick_entrees(self, e: dict) -> None:
        # lbl_entrees ne contient plus que les 6 labels "Actif" du tableau Voies
        # → rien à mettre à jour dynamiquement (tous actifs en permanence)
        pass

    def _tick_sequence(self, e: dict) -> None:
        seq = e.get("sequence")
        en_cours = bool(seq) and not seq.get("terminee")
        self._btn_seq_start.setEnabled(
            bool(self._seq_points) and not en_cours)
        if not seq:
            self._line_seq_t.setVisible(False)
            return
        if seq.get("terminee"):
            self._lbl_seq_statut.setText(
                f"séquence terminée — consigne finale "
                f"Re = {seq['re_cible']:.0f} maintenue")
            self._line_seq_t.setValue(seq["t_fin"])
        else:
            self._lbl_seq_statut.setText(
                f"étape {seq['etape']}/{seq['n']} — "
                f"t = {seq['t']:.0f}/{seq['t_fin']:.0f} s — "
                f"Re cible {seq['re_cible']:.0f}")
            self._line_seq_t.setValue(seq["t"])
        self._line_seq_t.setVisible(True)

    def _tick_apprentissage(self) -> None:
        a = self.apprentissage
        if a.etat["en_cours"]:
            self.lbl_app_statut.setStyleSheet(
                f"color:{COLORS['accent']};")
            self.lbl_app_statut.setText(
                f"[{a.etat['test']}]  {a.etat['statut']}")
        elif a.etat["statut"] and not \
                self.lbl_app_statut.text().startswith("Appliqué"):
            self.lbl_app_statut.setStyleSheet(f"color:{COLORS['ok']};")
            self.lbl_app_statut.setText(a.etat["statut"])

        lignes = [
            f"{nom:14s} : {', '.join(f'{k}={v}' for k, v in res.items())}"
            for nom, res in a.resultats.items()
        ]
        self.lbl_app_resultats.setText("\n".join(lignes))
        self.btn_appliquer_app.setEnabled(
            bool(a.resultats) and not a.etat["en_cours"])

        pdc_ok = (self.pilote.pdc_recopie_max
                  - self.pilote.pdc_recopie_min) >= 1.0
        self._btns_vanne["vanne_pdc"].setEnabled(pdc_ok)
        self._btns_vanne["vanne_pdc"].setToolTip(
            "" if pdc_ok else "Lancer d'abord la calibration recopie PdC")

        test_actif = a.etat.get("test") if a.etat["en_cours"] else None

        def _maj(btn, lbl, key, fmt_r):
            if key == test_actif:
                btn.setStyleSheet(
                    f"background:{COLORS['warn']}; color:#FFFFFF;")
                lbl.setStyleSheet(
                    f"color:{COLORS['warn']}; font-style:italic;")
                lbl.setText(a.etat["statut"])
            elif key in a.resultats:
                btn.setStyleSheet("")
                lbl.setStyleSheet(
                    f"color:{COLORS['ok']}; font-weight:700;")
                try:
                    lbl.setText(fmt_r(a.resultats[key]))
                except (KeyError, TypeError, ValueError) as exc:
                    # un formateur désynchronisé des clés produites ne doit pas
                    # faire tomber le QTimer : l'IHM entière cesserait de se
                    # rafraîchir, y compris les alertes de sécurité
                    lbl.setText(f"résultat illisible ({exc})")
            else:
                btn.setStyleSheet("")
                lbl.setStyleSheet(f"color:{COLORS['muted']};")
                lbl.setText("—")

        for key in self._btns_vanne:
            btn = self._btns_vanne[key]
            lbl, fmt = self._lbls_vanne[key]
            _maj(btn, lbl, key, fmt)

        r = a.resultats
        _maj(self._btn_indiciel, self._lbl_statut_indiciel, "indiciel",
             lambda r: (f"K={r['gain_re_par_v']} Re/V  "
                        f"τ={r['tau_s']} s  "
                        f"kp={r['kp_propose']}  ki={r['ki_propose']}"))
        # Re mapping Miller (plage basse)
        _maj(self._btn_re_mapping_miller, self._lbl_statut_re_mapping_miller, "re_mapping_miller",
             lambda r: (f"U min = {r['u_min_exploitable_v']} V  "
                        f"u_seuil = {r.get('u_seuil_bas_re_v', '?')} V"))
        paliers_miller = (a.etat.get("paliers_re_mapping")
                          or (r["re_mapping_miller"]["paliers_V_Re"]
                              if "re_mapping_miller" in r else None))
        if paliers_miller:
            if "re_mapping_miller" in r:
                u_min = r["re_mapping_miller"]["u_min_exploitable_v"]
                ok  = [(u, re) for u, re in paliers_miller if u >= u_min]
                nok = [(u, re) for u, re in paliers_miller if u < u_min]
                self._curve_miller_stable.setData(
                    [p[0] for p in ok], [p[1] for p in ok])
                self._curve_miller_stall.setData(
                    [p[0] for p in nok], [p[1] for p in nok])
                self._line_miller_min.setValue(u_min)
                self._line_miller_min.setVisible(True)
            else:
                self._curve_miller_stable.setData(
                    [p[0] for p in paliers_miller], [p[1] for p in paliers_miller])
                self._curve_miller_stall.setData([], [])
                self._line_miller_min.setVisible(False)

        # Re mapping ISO (plage haute)
        _maj(self._btn_re_mapping_iso, self._lbl_statut_re_mapping_iso, "re_mapping_iso",
             lambda r: (f"{len(r.get('paliers_plage_haute', []))} paliers  "
                        f"u_seuil = {r.get('u_seuil_utilise_v', '?')} V"))
        paliers_iso = (a.etat.get("paliers_plage_haute")
                       or (r["re_mapping_iso"]["paliers_plage_haute"]
                           if "re_mapping_iso" in r else None))
        if paliers_iso:
            self._curve_iso_stable.setData(
                [p[0] for p in paliers_iso], [p[1] for p in paliers_iso])

    def _tick_config(self, e: dict) -> None:
        """Mise à jour temps réel de l'encart Caractéristiques de l'air (onglet Configuration)."""
        # Uniquement si l'onglet Configuration est visible (page 4)
        if self._stack.currentIndex() != 4:
            return

        # Température du capteur
        T = e.get("temperature")
        P_abs = self.pilote.banc.patm_pa + (e.get("p_amont") or 0.0)

        if T is not None:
            # Affichage température
            self._lbl_air_temp.setText(f"{T:.1f}".replace(".", ","))

            # Récupérer l'humidité relative depuis la config
            HR_pct = self.cfg.get("fluide", {}).get("humidite_relative", 0.0)

            # Calcul propriétés de l'air
            if HR_pct > 0:
                # Utiliser les formules air humide
                from core.air_humide import calculer_proprietes_air_humide
                try:
                    props = calculer_proprietes_air_humide(P_abs, T, HR_pct / 100.0)
                    rho = props["rho"]
                    mu = props["mu"]
                    nu = props["nu"]
                    # Remplir les détails
                    self._lbl_air_Pvs.setText(f"{props['Pvs']:.2f}".replace(".", ","))
                    self._lbl_air_Pv.setText(f"{props['Pv']:.2f}".replace(".", ","))
                    self._lbl_air_x.setText(f"{props['x']:.6f}".replace(".", ","))
                    self._lbl_air_v.setText(f"{props['v']:.4f}".replace(".", ","))
                    self._lbl_air_h.setText(f"{props['h']:.2f}".replace(".", ","))
                except ValueError:
                    # Si erreur (HR invalide), retomber sur air sec
                    from core.fluide import rho_air, mu_air
                    rho = rho_air(P_abs, T)
                    mu = mu_air(T)
                    nu = mu / rho
                    # Détails non applicables pour air sec
                    self._lbl_air_Pvs.setText("—")
                    self._lbl_air_Pv.setText("—")
                    self._lbl_air_x.setText("—")
                    self._lbl_air_v.setText("—")
                    self._lbl_air_h.setText("—")
            else:
                # Air sec (HR = 0)
                from core.fluide import rho_air, mu_air
                rho = rho_air(P_abs, T)
                mu = mu_air(T)
                nu = mu / rho
                # Détails non applicables pour air sec
                self._lbl_air_Pvs.setText("—")
                self._lbl_air_Pv.setText("—")
                self._lbl_air_x.setText("—")
                self._lbl_air_v.setText("—")
                self._lbl_air_h.setText("—")

            # Affichage propriétés calculées
            self._lbl_air_rho.setText(f"{rho:.4f}".replace(".", ","))

            # Format μ et ν : x.xxxxE-05 (5 décimales + exposant)
            mu_str = f"{mu:.5e}".replace(".", ",").replace("e", "E")
            nu_str = f"{nu:.5e}".replace(".", ",").replace("e", "E")
            self._lbl_air_mu.setText(mu_str)
            self._lbl_air_nu.setText(nu_str)
        else:
            # Si pas de température, tout en "—"
            self._lbl_air_temp.setText("—")
            self._lbl_air_rho.setText("—")
            self._lbl_air_mu.setText("—")
            self._lbl_air_nu.setText("—")

    def _tracer(self) -> None:
        donnees = list(self._hist_re)
        cibles  = list(self._hist_cible)
        if len(donnees) < 2:
            return
        dt     = PERIODE_IHM_MS / 1000.0
        t_now  = (datetime.now() - self._t0_acq).total_seconds() if self._t0_acq else 0.0
        n      = len(donnees)
        t0     = t_now - (n - 1) * dt
        xs     = [t0 + i * dt for i in range(n)]
        self._curve_re.setData(xs, donnees)
        self._curve_cible.setData(xs, cibles)

        window  = 20.0
        x_right = max(window, t_now)
        x_left  = x_right - window
        self._plot_re.setXRange(x_left, x_right, padding=0)

        maxi = max(max(donnees), max(cibles), 1.0) * 1.05
        self._plot_re.setYRange(0, maxi, padding=0)
