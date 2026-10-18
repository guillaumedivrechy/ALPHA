#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Gestionnaire des fichiers d'apprentissage séparés.

Architecture : fichiers JSON séparés par type d'apprentissage au lieu
d'un fichier monolithique unique, pour éviter les pertes de données et
améliorer la traçabilité.

Structure :
    apprentissages/
        miller.json      - Table Miller, table PDC, paramètres bas Re
        iso.json         - Table ISO, gain Re/V, paramètres haut Re
        vannes.json      - Temps de course, positions FC (futur)
        essai_indiciel.json - Tau, modèle dynamique (futur)

Chaque fichier contient :
    - metadata : date, version, température, pression, commentaire
    - tables : données d'apprentissage (table_gains, etc.)
    - parametres : u_min, seuils, etc.
"""

from __future__ import annotations
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from .fichiers import ecrire_json_atomique

log = logging.getLogger(__name__)


class GestionnaireApprentissages:
    """Gère le chargement et la sauvegarde des apprentissages dans des fichiers séparés."""

    def __init__(self, chemin_racine: Path, chemin_etat: Path | None = None):
        """Initialise le gestionnaire.

        Args:
            chemin_racine: Répertoire racine (banc_pilotage/)
            chemin_etat: état de session du pilote (défaut : etat_banc.json
                sous la racine)
        """
        self.chemin_racine = Path(chemin_racine)
        self.chemin_apprentissages = self.chemin_racine / "apprentissages"
        self.chemin_ancien_etat = Path(chemin_etat) if chemin_etat \
            else self.chemin_racine / "etat_banc.json"

        # Créer le dossier apprentissages si nécessaire
        self.chemin_apprentissages.mkdir(exist_ok=True)

        # Chemins des fichiers spécialisés
        self.fichiers = {
            'miller': self.chemin_apprentissages / "miller.json",
            'iso': self.chemin_apprentissages / "iso.json",
            'vannes': self.chemin_apprentissages / "vannes.json",
            'essai_indiciel': self.chemin_apprentissages / "essai_indiciel.json",
        }

    def charger_tout(self) -> dict[str, Any]:
        """Charge tous les apprentissages et fusionne dans un dict unifié.

        Retourne dict avec les clés attendues par le code existant :
            - table_gains (ISO)
            - table_gains_bas_re (Miller)
            - table_gains_pdc (PDC)
            - gain_re_par_volt
            - u_min_ventilateur_v
            - etc.

        Gère automatiquement la migration depuis l'ancien format si nécessaire.
        """
        # Vérifier si migration nécessaire
        if self.chemin_ancien_etat.exists() and not any(f.exists() for f in self.fichiers.values()):
            log.info("Migration détectée : conversion ancien format → nouveaux fichiers")
            self._migrer_ancien_format()

        # Charger chaque fichier
        donnees = {}

        miller_data = self._charger_fichier('miller')
        if miller_data:
            donnees.update(miller_data.get('tables', {}))
            donnees.update(miller_data.get('parametres', {}))

        iso_data = self._charger_fichier('iso')
        if iso_data:
            donnees.update(iso_data.get('tables', {}))
            donnees.update(iso_data.get('parametres', {}))

        # Vannes et essai indiciel (pour le futur)
        vannes_data = self._charger_fichier('vannes')
        if vannes_data:
            donnees.update(vannes_data.get('parametres', {}))

        indiciel_data = self._charger_fichier('essai_indiciel')
        if indiciel_data:
            donnees.update(indiciel_data.get('parametres', {}))

        # Gain Re/V appris EN FONCTIONNEMENT : le pilote l'écrit à l'arrêt
        # dans l'état de session, plus récent que celui d'iso.json. Il
        # n'était relu qu'à la migration initiale, donc perdu ensuite.
        etat = self._lire_etat_session()
        if etat.get("gain_re_par_volt"):
            donnees["gain_re_par_volt"] = etat["gain_re_par_volt"]

        return donnees

    def _lire_etat_session(self) -> dict:
        if not self.chemin_ancien_etat.exists():
            return {}
        try:
            return json.loads(self.chemin_ancien_etat.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.error(f"Erreur lecture {self.chemin_ancien_etat}: {e}")
            return {}

    def sauvegarder_miller(self, tables: dict, parametres: dict,
                          temperature_c: float = None, pression_pa: float = None,
                          commentaire: str = "") -> None:
        """Sauvegarde l'apprentissage Miller dans miller.json.

        Args:
            tables: dict avec 'table_gains_bas_re', 'table_gains_pdc', régressions
            parametres: dict avec u_min_ventilateur_v, u_seuil_bas_re_v, etc.
            temperature_c: Température lors de l'apprentissage
            pression_pa: Pression atmosphérique lors de l'apprentissage
            commentaire: Commentaire optionnel
        """
        data = {
            'metadata': self._creer_metadata(temperature_c, pression_pa, commentaire),
            'tables': tables,
            'parametres': parametres
        }

        self._sauvegarder_fichier('miller', data)
        log.info(f"Apprentissage Miller sauvegardé : {self.fichiers['miller']}")

    def sauvegarder_iso(self, tables: dict, parametres: dict,
                       temperature_c: float = None, pression_pa: float = None,
                       commentaire: str = "") -> None:
        """Sauvegarde l'apprentissage ISO dans iso.json.

        Args:
            tables: dict avec 'table_gains', régression
            parametres: dict avec gain_re_par_volt, etc.
            temperature_c: Température lors de l'apprentissage
            pression_pa: Pression atmosphérique lors de l'apprentissage
            commentaire: Commentaire optionnel
        """
        data = {
            'metadata': self._creer_metadata(temperature_c, pression_pa, commentaire),
            'tables': tables,
            'parametres': parametres
        }

        self._sauvegarder_fichier('iso', data)
        log.info(f"Apprentissage ISO sauvegardé : {self.fichiers['iso']}")

    def obtenir_metadata(self, type_apprentissage: str) -> dict | None:
        """Retourne les métadonnées d'un apprentissage (date, conditions, etc.).

        Args:
            type_apprentissage: 'miller', 'iso', 'vannes', 'essai_indiciel'

        Returns:
            dict avec date, version, temperature, pression, commentaire ou None
        """
        data = self._charger_fichier(type_apprentissage)
        return data.get('metadata') if data else None

    def _charger_fichier(self, nom: str) -> dict | None:
        """Charge un fichier d'apprentissage.

        Args:
            nom: 'miller', 'iso', 'vannes', 'essai_indiciel'

        Returns:
            dict avec metadata, tables, parametres ou None si fichier absent
        """
        chemin = self.fichiers.get(nom)
        if not chemin or not chemin.exists():
            return None

        try:
            with open(chemin, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            log.error(f"Erreur lecture {chemin}: {e}")
            return None

    def _sauvegarder_fichier(self, nom: str, data: dict) -> None:
        """Sauvegarde un fichier d'apprentissage.

        Args:
            nom: 'miller', 'iso', 'vannes', 'essai_indiciel'
            data: dict à sauvegarder
        """
        chemin = self.fichiers[nom]

        try:
            ecrire_json_atomique(chemin, data)
        except OSError as e:
            log.error(f"Erreur écriture {chemin}: {e}")
            raise

    def _creer_metadata(self, temperature_c: float = None,
                       pression_pa: float = None,
                       commentaire: str = "") -> dict:
        """Crée un dict de métadonnées pour un apprentissage.

        Args:
            temperature_c: Température ambiante
            pression_pa: Pression atmosphérique
            commentaire: Commentaire utilisateur

        Returns:
            dict avec date, version, conditions
        """
        metadata = {
            'date': datetime.now().isoformat(),
            'version_logiciel': '2.1.0',  # TODO: extraire depuis __version__
        }

        if temperature_c is not None:
            metadata['temperature_c'] = round(temperature_c, 1)

        if pression_pa is not None:
            metadata['pression_atm_pa'] = round(pression_pa, 0)

        if commentaire:
            metadata['commentaire'] = commentaire

        return metadata

    def _migrer_ancien_format(self) -> None:
        """Migre l'ancien etat_banc.json vers les nouveaux fichiers séparés.

        Lit etat_banc.json et répartit les données dans :
            - miller.json : table_gains_bas_re, table_gains_pdc, u_min, u_seuil
            - iso.json : table_gains, gain_re_par_volt
        """
        try:
            with open(self.chemin_ancien_etat, 'r', encoding='utf-8') as f:
                ancien = json.load(f)
        except (OSError, ValueError) as e:
            log.warning(f"Impossible de lire ancien format {self.chemin_ancien_etat}: {e}")
            return

        log.info("=" * 70)
        log.info("MIGRATION ANCIEN FORMAT → FICHIERS SÉPARÉS")
        log.info("=" * 70)

        # Migrer données Miller
        tables_miller = {}
        params_miller = {}

        if 'table_gains_bas_re' in ancien:
            tables_miller['table_gains_bas_re'] = ancien['table_gains_bas_re']
            log.info(f"  Migration table_gains_bas_re : {len(ancien['table_gains_bas_re'])} points")

        if 'table_gains_pdc' in ancien:
            tables_miller['table_gains_pdc'] = ancien['table_gains_pdc']
            log.info(f"  Migration table_gains_pdc : {len(ancien['table_gains_pdc'])} points")

        if 'u_min_ventilateur_v' in ancien:
            params_miller['u_min_ventilateur_v'] = ancien['u_min_ventilateur_v']
            log.info(f"  Migration u_min_ventilateur_v : {ancien['u_min_ventilateur_v']}V")

        if 'u_seuil_bas_re_v' in ancien:
            params_miller['u_seuil_bas_re_v'] = ancien['u_seuil_bas_re_v']

        if tables_miller or params_miller:
            self.sauvegarder_miller(tables_miller, params_miller,
                                   commentaire="Migration automatique depuis ancien format")
            log.info("  → miller.json créé")

        # Migrer données ISO
        tables_iso = {}
        params_iso = {}

        if 'table_gains' in ancien:
            tables_iso['table_gains'] = ancien['table_gains']
            log.info(f"  Migration table_gains : {len(ancien['table_gains'])} points")

        if 'gain_re_par_volt' in ancien:
            params_iso['gain_re_par_volt'] = ancien['gain_re_par_volt']
            log.info(f"  Migration gain_re_par_volt : {ancien['gain_re_par_volt']:.2f} Re/V")

        if tables_iso or params_iso:
            self.sauvegarder_iso(tables_iso, params_iso,
                                commentaire="Migration automatique depuis ancien format")
            log.info("  → iso.json créé")

        log.info("")
        log.info("Migration terminée avec succès")
        log.info("L'ancien fichier etat_banc.json est conservé pour compatibilité")
        log.info("=" * 70)
