"""
Module de gestion des modes de régulation.

Ce module fournit des fonctions pour déterminer :
- Le mode cible (où on veut aller) depuis la consigne Reynolds
- Le mode effectif (où on est) depuis l'état physique des vannes
"""

from enum import Enum
from dataclasses import dataclass
from typing import Optional
import logging

from .etat_vannes import EtatVannes, PositionDiaphragme

log = logging.getLogger(__name__)


class ModeRegulation(Enum):
    """Mode de régulation du banc."""
    NORMAL_HIGH = "normal_high"  # ISO, bypass fermé, variateur > u_min
    NORMAL_LOW = "normal_low"    # Miller, bypass ouvert, variateur > u_min
    PDC_CONTROL = "pdc_control"  # Miller, bypass ouvert, vanne PdC active, variateur à u_min


@dataclass(frozen=True)
class ConfigurationModes:
    """
    Configuration des seuils de basculement entre modes.

    Attributes:
        seuil_diaphragme: Seuil ISO/Miller en Re conduite (ex: 5000)
        largeur_hysteresis_diaphragme: Largeur bande hystérésis diaphragme (ex: 500)
        seuil_bas_re: Seuil NORMAL_LOW/PDC_CONTROL en Re conduite (ex: 2000)
        largeur_hysteresis_bas_re: Largeur bande hystérésis PDC (ex: 100)
        u_min: Tension minimale variateur en V (ex: 1.2)
    """
    seuil_diaphragme: float
    largeur_hysteresis_diaphragme: float
    seuil_bas_re: float
    largeur_hysteresis_bas_re: float
    u_min: float


def determiner_mode_cible(
    re_cible_conduite: float,
    config: ConfigurationModes,
    mode_precedent: Optional[ModeRegulation] = None
) -> ModeRegulation:
    """
    Détermine le mode cible (où on veut aller) depuis la consigne Reynolds.

    Applique une hystérésis pour éviter les oscillations :
    - Hystérésis diaphragme : seuil ± largeur/2
    - Hystérésis PDC : seuil_bas_re × (0.95 à 1.05)

    Args:
        re_cible_conduite: Reynolds cible en conduite
        config: Configuration des seuils
        mode_precedent: Mode cible au cycle précédent (pour hystérésis)

    Returns:
        ModeRegulation cible (NORMAL_HIGH, NORMAL_LOW, ou PDC_CONTROL)

    Examples:
        >>> config = ConfigurationModes(
        ...     seuil_diaphragme=5000, largeur_hysteresis_diaphragme=500,
        ...     seuil_bas_re=2000, largeur_hysteresis_bas_re=100, u_min=1.2
        ... )
        >>> determiner_mode_cible(10000, config)
        <ModeRegulation.NORMAL_HIGH: 'normal_high'>
        >>> determiner_mode_cible(3000, config)
        <ModeRegulation.NORMAL_LOW: 'normal_low'>
        >>> determiner_mode_cible(1500, config)
        <ModeRegulation.PDC_CONTROL: 'pdc_control'>
    """
    # Seuils avec hystérésis pour diaphragme
    seuil_haut = config.seuil_diaphragme + config.largeur_hysteresis_diaphragme / 2
    seuil_bas = config.seuil_diaphragme - config.largeur_hysteresis_diaphragme / 2

    # Étape 1 : Choix diaphragme (ISO vs Miller)
    if re_cible_conduite > seuil_haut:
        diaphragme_cible = "iso"
    elif re_cible_conduite < seuil_bas:
        diaphragme_cible = "miller"
    else:
        # Zone d'hystérésis : conserver choix précédent
        if mode_precedent is None:
            # Par défaut, choisir selon position dans la bande
            diaphragme_cible = "iso" if re_cible_conduite >= config.seuil_diaphragme else "miller"
        else:
            # Conserver diaphragme du mode précédent
            if mode_precedent == ModeRegulation.NORMAL_HIGH:
                diaphragme_cible = "iso"
            else:
                diaphragme_cible = "miller"

    # Étape 2 : Si diaphragme ISO → NORMAL_HIGH
    if diaphragme_cible == "iso":
        return ModeRegulation.NORMAL_HIGH

    # Étape 3 : Si diaphragme Miller → NORMAL_LOW ou PDC_CONTROL
    # Hystérésis ±5% sur seuil_bas_re
    seuil_pdc_bas = config.seuil_bas_re * 0.95
    seuil_pdc_haut = config.seuil_bas_re * 1.05

    if re_cible_conduite < seuil_pdc_bas:
        return ModeRegulation.PDC_CONTROL
    elif re_cible_conduite > seuil_pdc_haut:
        return ModeRegulation.NORMAL_LOW
    else:
        # Zone d'hystérésis PDC : conserver mode précédent
        if mode_precedent is None or mode_precedent == ModeRegulation.NORMAL_HIGH:
            # Par défaut ou transition depuis ISO
            return ModeRegulation.NORMAL_LOW if re_cible_conduite >= config.seuil_bas_re else ModeRegulation.PDC_CONTROL
        else:
            # Conserver mode précédent (NORMAL_LOW ou PDC_CONTROL)
            return mode_precedent


def determiner_mode_effectif(
    etat_vannes: EtatVannes,
    re_mesure_conduite: float,
    config: ConfigurationModes,
    mode_precedent: Optional[ModeRegulation] = None
) -> ModeRegulation:
    """
    Détermine le mode effectif (où on est) depuis l'état physique des vannes.

    Le mode effectif suit le diaphragme PHYSIQUE (lu sur FC), pas la consigne.
    Cela évite le problème CRITIQUE-1 (mode déterminé depuis consigne au lieu du diaphragme réel).

    Args:
        etat_vannes: État physique des vannes (depuis lire_etat_vannes)
        re_mesure_conduite: Reynolds mesuré en conduite
        config: Configuration des seuils
        mode_precedent: Mode effectif au cycle précédent (pour hystérésis)

    Returns:
        ModeRegulation effectif (NORMAL_HIGH, NORMAL_LOW, ou PDC_CONTROL)

    Examples:
        >>> from etat_vannes import EtatVannes, PositionDiaphragme, PositionBypass, PositionPDC
        >>> etat = EtatVannes(
        ...     diaphragme=PositionDiaphragme.ISO,
        ...     bypass=PositionBypass.FERME,
        ...     pdc=PositionPDC.OUVERT,
        ...     coherent=True
        ... )
        >>> config = ConfigurationModes(5000, 500, 2000, 100, 1.2)
        >>> determiner_mode_effectif(etat, 8000, config)
        <ModeRegulation.NORMAL_HIGH: 'normal_high'>
    """
    # Cas 1 : Diaphragme physique = ISO → toujours NORMAL_HIGH
    if etat_vannes.diaphragme == PositionDiaphragme.ISO:
        return ModeRegulation.NORMAL_HIGH

    # Cas 2 : Diaphragme physique = MILLER → NORMAL_LOW ou PDC_CONTROL
    if etat_vannes.diaphragme == PositionDiaphragme.MILLER:
        # Hystérésis ±5% sur seuil_bas_re
        seuil_pdc_bas = config.seuil_bas_re * 0.95
        seuil_pdc_haut = config.seuil_bas_re * 1.05

        if re_mesure_conduite < seuil_pdc_bas:
            return ModeRegulation.PDC_CONTROL
        elif re_mesure_conduite > seuil_pdc_haut:
            return ModeRegulation.NORMAL_LOW
        else:
            # Zone d'hystérésis : conserver mode précédent
            if mode_precedent is None or mode_precedent == ModeRegulation.NORMAL_HIGH:
                # Par défaut ou transition depuis ISO
                return ModeRegulation.NORMAL_LOW if re_mesure_conduite >= config.seuil_bas_re else ModeRegulation.PDC_CONTROL
            else:
                # Conserver mode précédent
                return mode_precedent

    # Cas 3 : Diaphragme EN_TRANSIT → conserver mode précédent
    if etat_vannes.diaphragme == PositionDiaphragme.EN_TRANSIT:
        if mode_precedent is not None:
            log.debug("Diaphragme en transit, conservation mode précédent : %s", mode_precedent.name)
            return mode_precedent
        else:
            # Par défaut si pas de précédent
            log.warning("Diaphragme en transit sans mode précédent, retour NORMAL_HIGH par défaut")
            return ModeRegulation.NORMAL_HIGH

    # Cas 4 : Diaphragme INCONNU → mode sécuritaire NORMAL_HIGH + alarme
    log.error("État diaphragme INCONNU, retour mode sécuritaire NORMAL_HIGH")
    return ModeRegulation.NORMAL_HIGH
