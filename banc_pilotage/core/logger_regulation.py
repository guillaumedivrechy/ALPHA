#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Logger dédié au diagnostic de la régulation.

Enregistre dans regulation_debug.log :
- Consignes Re
- Tensions feed-forward (régression vs interpolation)
- Modes de régulation
- Valeurs PI
- Re mesurés
- États vannes

Usage dans pilote.py :
    from core.logger_regulation import LoggerRegulation
    self.logger_reg = LoggerRegulation()
    self.logger_reg.log_cycle(...) # dans la boucle
"""

import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class LoggerRegulation:
    """Logger CSV pour diagnostic approfondi de la régulation."""

    def __init__(self, chemin_log: Path | str = None):
        """Initialise le logger de régulation.

        Args:
            chemin_log: Chemin du fichier CSV. Si None, utilise regulation_debug.log
        """
        if chemin_log is None:
            chemin_racine = Path(__file__).resolve().parent.parent
            self.chemin_log = chemin_racine / "regulation_debug.csv"
        else:
            self.chemin_log = Path(chemin_log)

        self.actif = False  # Activé/désactivé dynamiquement
        self._initialise = False

        # Colonnes du CSV
        self.colonnes = [
            'timestamp',
            'consigne_re',
            'mode_regulation',
            'diaphragme',
            'bypass_ferme',
            'pdc_position_v',
            'u_ff_regression',
            'u_ff_interpolation',
            'u_ff_utilise',
            'methode_ff',
            'u_pi_sortie',
            'u_variateur_final',
            're_mesure',
            'erreur_re',
            'erreur_relative_pct',
            'alarme'
        ]

    def activer(self):
        """Active le logging (créé/remplace le fichier)."""
        try:
            # Créer nouveau fichier avec header
            with open(self.chemin_log, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=self.colonnes)
                writer.writeheader()

            self.actif = True
            self._initialise = True
            log.info(f"Logger régulation activé : {self.chemin_log}")
        except OSError as e:
            log.error(f"Impossible d'activer logger régulation : {e}")
            self.actif = False

    def desactiver(self):
        """Désactive le logging."""
        self.actif = False
        log.info("Logger régulation désactivé")

    def log_cycle(self, donnees: dict[str, Any]):
        """Enregistre un cycle de régulation.

        Args:
            donnees: dict avec les clés correspondant aux colonnes
                (timestamp sera ajouté automatiquement)
        """
        if not self.actif:
            return

        if not self._initialise:
            self.activer()

        try:
            # Ajouter timestamp
            donnees_complete = {
                'timestamp': datetime.now().isoformat(timespec='milliseconds'),
                **donnees
            }

            # Remplir colonnes manquantes avec None
            for col in self.colonnes:
                if col not in donnees_complete:
                    donnees_complete[col] = None

            # Écrire ligne
            with open(self.chemin_log, 'a', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=self.colonnes)
                writer.writerow(donnees_complete)

        except OSError as e:
            log.error(f"Erreur écriture logger régulation : {e}")

    def log_consigne_changement(self, re_ancien: float, re_nouveau: float, mode: str):
        """Enregistre un changement de consigne (ligne spéciale)."""
        if not self.actif:
            return

        self.log_cycle({
            'consigne_re': re_nouveau,
            'mode_regulation': mode,
            'alarme': f"CHANGEMENT CONSIGNE: {re_ancien:.0f} → {re_nouveau:.0f} Re"
        })
