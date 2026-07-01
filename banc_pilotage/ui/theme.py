# -*- coding: utf-8 -*-
"""Tokens de design centralisés (couleurs, espacements, polices, rayon).

Ne jamais coder une couleur en dur dans les widgets — toujours importer
COLORS depuis ce module.

Palette :
  #00B8DE  cyan-bleu   — accent principal, actions, onglet actif
  #FFFFFF  blanc       — surface des panneaux / cartes
  #FF9900  orange      — avertissements
  #14223C  navy        — texte principal, titres
  #EDF3F4  gris-bleu   — fond de page
"""

COLORS = {
    "bg":         "#E8EEF1",
    "surface":    "#FFFFFF",
    "chip":       "#EFF4F6",
    "accent":     "#00B8DE",
    "accentText": "#0883A6",
    "text":       "#14223C",
    "text2":      "#3A4D60",
    "muted":      "#6B7E8E",
    "muted2":     "#7B8FA0",
    "faint":      "#A4B3BF",
    "ok":         "#2BB673",
    "warn":       "#FF9900",
    "error":      "#DC3535",
    "border":     "#DBE5EA",
    "borderSoft": "#E9EEF1",
    "navy":       "#0E1A2C",
}
SPACING = {"xs": 4, "sm": 8, "md": 16, "lg": 24, "xl": 32}
FONT    = {"family": "Inter", "size_base": 13, "size_title": 18}
RADIUS  = 8

BTN_QSS = (
    f"QPushButton{{ background:{COLORS['accent']}; border:none; border-radius:4px;"
    f" color:#FFFFFF; font-weight:700; }}"
    f"QPushButton:hover{{ background:#00A5C9; }}"
    f"QPushButton:pressed{{ background:#FFFFFF; color:{COLORS['accent']}; border:2px solid {COLORS['accent']}; }}"
    f"QPushButton:disabled{{ background:#A8DFF0; color:#FFFFFF; border:none; }}"
)

SEG_OFF_QSS = (
    f"background:#FFFFFF; border:2px solid {COLORS['accent']}; border-radius:7px;"
    f" color:{COLORS['accent']}; font-size:13px; font-weight:500; padding:5px 10px; min-width:0;"
)
SEG_ON_QSS = (
    f"background:{COLORS['accent']}; border:2px solid {COLORS['accent']}; border-radius:7px;"
    f" color:#FFFFFF; font-size:13px; font-weight:700; padding:5px 10px; min-width:0;"
)

APP_QSS = f"""
/* ── base ─────────────────────────────────────────────────────────────── */
QMainWindow, QWidget {{
    background-color: {COLORS['bg']};
    color: {COLORS['text']};
    font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
    font-size: 13px;
}}

/* ── barre d'en-tête ───────────────────────────────────────────────────── */
QWidget#appheader {{
    background-color: {COLORS['text']};
}}
QFrame#appheader-accent {{
    background-color: {COLORS['accent']};
    border: none;
}}
QLabel#appheader-title {{
    background: transparent;
    color: {COLORS['surface']};
    font-size: 16px;
    font-weight: bold;
    letter-spacing: 0.3px;
}}
QLabel#appheader-badge-simu {{
    background-color: {COLORS['warn']};
    color: {COLORS['surface']};
    font-size: 11px;
    font-weight: bold;
    padding: 0 14px;
    border-radius: 14px;
    letter-spacing: 1px;
}}
QLabel#appheader-badge-live {{
    background-color: {COLORS['surface']};
    color: {COLORS['accent']};
    font-size: 11px;
    font-weight: bold;
    padding: 0 14px;
    border-radius: 14px;
    letter-spacing: 1px;
}}

/* ── onglets ───────────────────────────────────────────────────────────── */
QTabWidget::pane {{
    background-color: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-top: none;
}}
QTabBar::tab {{
    background-color: {COLORS['bg']};
    color: {COLORS['muted']};
    padding: 8px 20px;
    border: none;
    border-bottom: 2px solid transparent;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background-color: {COLORS['surface']};
    color: {COLORS['accent']};
    border-bottom: 2px solid {COLORS['accent']};
    font-weight: bold;
}}
QTabBar::tab:hover:!selected {{
    color: {COLORS['text']};
    background-color: #DDEAED;
}}

/* ── groupbox ──────────────────────────────────────────────────────────── */
QGroupBox {{
    background-color: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-top: 3px solid {COLORS['accent']};
    border-radius: {RADIUS}px;
    margin-top: 22px;
    padding: 18px 10px 10px 10px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    top: 4px;
    padding: 0 6px;
    color: {COLORS['accent']};
    font-size: 12px;
    font-weight: bold;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    background: transparent;
}}

/* ── labels ────────────────────────────────────────────────────────────── */
QLabel {{
    background: transparent;
    color: {COLORS['text']};
}}
QLabel#bold {{
    font-weight: bold;
    color: {COLORS['accent']};
    font-size: 14px;
}}
QLabel#muted  {{ color: {COLORS['muted']}; }}
QLabel#alerte {{ color: {COLORS['error']}; font-weight: bold; }}
QLabel#info   {{ color: {COLORS['accent']}; }}
QLabel#ok     {{ color: {COLORS['ok']}; font-weight: bold; }}
QLabel#mono   {{
    font-family: "Consolas", "Courier New", monospace;
    font-size: 11px;
    color: {COLORS['muted']};
}}
QLabel#voyant {{
    font-size: 18px;
    color: {COLORS['border']};
}}

/* ── saisie ────────────────────────────────────────────────────────────── */
QLineEdit {{
    background-color: {COLORS['bg']};
    border: 1px solid {COLORS['border']};
    border-radius: 4px;
    padding: 5px 8px;
    color: {COLORS['text']};
    selection-background-color: {COLORS['accent']};
    selection-color: {COLORS['surface']};
}}
QLineEdit:focus {{
    border-color: {COLORS['accent']};
    background-color: {COLORS['surface']};
}}
QLineEdit:disabled {{
    color: {COLORS['muted']};
    background-color: {COLORS['bg']};
    border-color: {COLORS['border']};
}}

/* ── boutons ───────────────────────────────────────────────────────────── */
QPushButton {{
    background-color: {COLORS['accent']};
    border: none;
    border-radius: 4px;
    padding: 7px 16px;
    color: {COLORS['surface']};
    font-weight: bold;
    min-width: 80px;
}}
QPushButton:hover   {{ background-color: #00A0C4; }}
QPushButton:pressed {{ background-color: #0090B0; }}
QPushButton#primary {{
    background-color: {COLORS['warn']};
    color: {COLORS['surface']};
    font-weight: bold;
    border: none;
}}
QPushButton#primary:hover   {{ background-color: #E68A00; }}
QPushButton#primary:pressed {{ background-color: #CC7A00; }}
QPushButton:disabled {{
    color: {COLORS['surface']};
    background-color: #A8DFF0;
    border: none;
}}

/* ── radio / checkbox ──────────────────────────────────────────────────── */
QCheckBox, QRadioButton {{
    color: {COLORS['text']};
    spacing: 8px;
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {COLORS['border']};
    background-color: {COLORS['surface']};
    border-radius: 3px;
}}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background-color: {COLORS['accent']};
    border-color: {COLORS['accent']};
}}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {COLORS['accent']};
}}

/* ── slider ────────────────────────────────────────────────────────────── */
QSlider:horizontal {{
    min-height: 34px;
}}
QSlider::groove:horizontal {{
    height: 10px;
    background: {COLORS['bg']};
    border: 1px solid {COLORS['border']};
    border-radius: 5px;
    margin: 0 4px;
}}
QSlider::sub-page:horizontal {{
    background: #A8DFF0;
    border-radius: 5px;
}}
QSlider::handle:horizontal {{
    width: 22px; height: 22px;
    margin: -7px -4px;
    background: {COLORS['surface']};
    border-radius: 11px;
    border: 2px solid {COLORS['accent']};
}}
QSlider::handle:horizontal:hover {{
    background: {COLORS['bg']};
    border-color: #00A0C4;
}}
QSlider:disabled::groove:horizontal   {{ background: {COLORS['bg']}; border-color: {COLORS['border']}; }}
QSlider:disabled::sub-page:horizontal {{ background: {COLORS['border']}; }}
QSlider:disabled::handle:horizontal   {{ background: {COLORS['bg']}; border-color: {COLORS['muted']}; }}

/* ── scrollbar ─────────────────────────────────────────────────────────── */
QScrollBar:vertical {{
    background: {COLORS['bg']};
    width: 8px;
    border-radius: 4px;
}}
QScrollBar::handle:vertical {{
    background: {COLORS['border']};
    border-radius: 4px;
    min-height: 20px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

/* ── séparateur ────────────────────────────────────────────────────────── */
QFrame#separator {{
    background-color: {COLORS['border']};
    max-height: 1px;
    border: none;
}}

/* ── messages ──────────────────────────────────────────────────────────── */
QMessageBox {{
    background-color: {COLORS['surface']};
}}
QMessageBox QLabel {{ color: {COLORS['text']}; }}
"""
