"""
Module de lecture et décodage de l'état physique des vannes.

Ce module fournit des fonctions pour décoder les signaux de fins de course (FC)
des trois vannes (diaphragme, bypass, PdC) et déterminer leur position physique actuelle.
"""

from enum import Enum
from dataclasses import dataclass
import logging

log = logging.getLogger(__name__)


class PositionDiaphragme(Enum):
    """Position du diaphragme 3 voies."""
    ISO = "iso"
    MILLER = "miller"
    EN_TRANSIT = "en_transit"
    INCONNU = "inconnu"


class PositionBypass(Enum):
    """Position de la vanne bypass."""
    OUVERT = "ouvert"
    FERME = "ferme"
    EN_TRANSIT = "en_transit"
    INCONNU = "inconnu"


class PositionPDC(Enum):
    """Position de la vanne perte de charge."""
    OUVERT = "ouvert"
    FERME = "ferme"
    EN_TRANSIT = "en_transit"
    INCONNU = "inconnu"


@dataclass(frozen=True)
class EtatVannes:
    """
    État physique des trois vannes, lu depuis les fins de course.

    Attributes:
        diaphragme: Position du diaphragme 3 voies
        bypass: Position de la vanne bypass
        pdc: Position de la vanne perte de charge
        coherent: True si tous les états sont valides (aucun INCONNU)
    """
    diaphragme: PositionDiaphragme
    bypass: PositionBypass
    pdc: PositionPDC
    coherent: bool


def lire_etat_vannes(mesures: dict) -> EtatVannes:
    """
    Décode les signaux de fins de course pour déterminer la position physique des vannes.

    Applique une logique "un-parmi-trois" pour chaque vanne :
    - FC_A activé, FC_B désactivé → Position A confirmée
    - FC_A désactivé, FC_B activé → Position B confirmée
    - Les deux désactivés → Position en transit (vanne en mouvement)
    - Les deux activés → Position incohérente (défaut capteur ou vanne bloquée)

    Args:
        mesures: Dictionnaire contenant les signaux FC booléens :
            - fc_3voies_0, fc_3voies_90 (diaphragme)
            - fc_bypass_o, fc_bypass_f (bypass)
            - fc_pdc_o, fc_pdc_f (perte de charge)

    Returns:
        EtatVannes contenant les positions des trois vannes et le flag de cohérence.

    Examples:
        >>> mesures = {
        ...     "fc_3voies_0": True, "fc_3voies_90": False,
        ...     "fc_bypass_o": False, "fc_bypass_f": True,
        ...     "fc_pdc_o": True, "fc_pdc_f": False
        ... }
        >>> etat = lire_etat_vannes(mesures)
        >>> etat.diaphragme
        <PositionDiaphragme.ISO: 'iso'>
        >>> etat.bypass
        <PositionBypass.FERME: 'ferme'>
        >>> etat.coherent
        True
    """
    # Lecture des FC
    fc_3v_0 = mesures.get("fc_3voies_0", False)
    fc_3v_90 = mesures.get("fc_3voies_90", False)
    fc_bypass_o = mesures.get("fc_bypass_ouvert", False)
    fc_bypass_f = mesures.get("fc_bypass_ferme", False)
    fc_pdc_o = mesures.get("fc_pdc_ouvert", False)
    fc_pdc_f = mesures.get("fc_pdc_ferme", False)

    # DEBUG
    log.debug(f"FC lus: 3v_0={fc_3v_0}, 3v_90={fc_3v_90}, bypass_o={fc_bypass_o}, bypass_f={fc_bypass_f}, pdc_o={fc_pdc_o}, pdc_f={fc_pdc_f}")

    # Décodage diaphragme
    if fc_3v_0 and not fc_3v_90:
        diaphragme = PositionDiaphragme.ISO
    elif not fc_3v_0 and fc_3v_90:
        diaphragme = PositionDiaphragme.MILLER
    elif not fc_3v_0 and not fc_3v_90:
        diaphragme = PositionDiaphragme.EN_TRANSIT
    else:  # Les deux activés
        log.warning("État incohérent diaphragme : fc_3voies_0=%s, fc_3voies_90=%s", fc_3v_0, fc_3v_90)
        diaphragme = PositionDiaphragme.INCONNU

    # Décodage bypass
    if fc_bypass_o and not fc_bypass_f:
        bypass = PositionBypass.OUVERT
    elif not fc_bypass_o and fc_bypass_f:
        bypass = PositionBypass.FERME
    elif not fc_bypass_o and not fc_bypass_f:
        bypass = PositionBypass.EN_TRANSIT
    else:  # Les deux activés
        log.warning("État incohérent bypass : fc_bypass_o=%s, fc_bypass_f=%s", fc_bypass_o, fc_bypass_f)
        bypass = PositionBypass.INCONNU

    # Décodage PdC
    if fc_pdc_o and not fc_pdc_f:
        pdc = PositionPDC.OUVERT
    elif not fc_pdc_o and fc_pdc_f:
        pdc = PositionPDC.FERME
    elif not fc_pdc_o and not fc_pdc_f:
        pdc = PositionPDC.EN_TRANSIT
    else:  # Les deux activés
        log.warning("État incohérent PdC : fc_pdc_o=%s, fc_pdc_f=%s", fc_pdc_o, fc_pdc_f)
        pdc = PositionPDC.INCONNU

    # Cohérence globale
    coherent = (
        diaphragme != PositionDiaphragme.INCONNU and
        bypass != PositionBypass.INCONNU and
        pdc != PositionPDC.INCONNU
    )

    return EtatVannes(
        diaphragme=diaphragme,
        bypass=bypass,
        pdc=pdc,
        coherent=coherent
    )
