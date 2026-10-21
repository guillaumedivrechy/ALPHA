# -*- coding: utf-8 -*-
"""Tokens de design centralisés (couleurs, styles de boutons).

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
