"""
Module de calcul des commandes vannes.

Ce module fournit des fonctions pour calculer les commandes TOR
à envoyer aux trois vannes pour passer de l'état actuel vers le mode cible,
en respectant la logique de transition sécurisée.
"""

from dataclasses import dataclass
from typing import Optional
import logging

from .etat_vannes import EtatVannes, PositionDiaphragme, PositionBypass
from .gestion_modes import ModeRegulation

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandesVannes:
    """
    Commandes à envoyer aux trois vannes.

    Attributes:
        diaphragme: "iso", "miller", ou None (pas de changement)
        bypass: True (ouvrir), False (fermer), ou None (pas de changement)
        pdc: Tension 0-10V, ou None (géré par régulation)
    """
    diaphragme: Optional[str]
    bypass: Optional[bool]
    pdc: Optional[float]


def calculer_commandes_vannes(
    etat_actuel: EtatVannes,
    mode_cible: ModeRegulation,
    manoeuvre_en_cours: bool
) -> CommandesVannes:
    """
    Calcule les commandes à envoyer aux vannes pour atteindre le mode cible.

    Logique de transition sécurisée :
    - HAUT → BAS (ISO → Miller) : Commande simultanée diaphragme + bypass ouvert
    - BAS → HAUT (Miller → ISO) : Séquentiel (fermer bypass d'abord, puis diaphragme)

    Args:
        etat_actuel: État physique actuel des vannes
        mode_cible: Mode de régulation cible
        manoeuvre_en_cours: True si une manœuvre est déjà en cours

    Returns:
        CommandesVannes contenant les commandes à envoyer (None = pas de changement)

    Examples:
        >>> from core.etat_vannes import EtatVannes, PositionDiaphragme, PositionBypass, PositionPDC
        >>> from core.gestion_modes import ModeRegulation
        >>> etat = EtatVannes(
        ...     diaphragme=PositionDiaphragme.ISO,
        ...     bypass=PositionBypass.FERME,
        ...     pdc=PositionPDC.OUVERT,
        ...     coherent=True
        ... )
        >>> calculer_commandes_vannes(etat, ModeRegulation.NORMAL_LOW, False)
        CommandesVannes(diaphragme='miller', bypass=True, pdc=None)
    """
    # Étape préalable : Si manœuvre en cours, ne rien commander
    if manoeuvre_en_cours:
        log.debug("Manœuvre en cours, commandes suspendues")
        return CommandesVannes(diaphragme=None, bypass=None, pdc=None)

    # DEBUG : Logger l'état actuel et le mode cible
    log.debug(f"calculer_commandes_vannes: etat={etat_actuel}, mode_cible={mode_cible}")

    # Cas 1 : Mode cible = NORMAL_HIGH (ISO souhaité)
    if mode_cible == ModeRegulation.NORMAL_HIGH:
        # Sous-cas 1.1 : Diaphragme déjà ISO
        if etat_actuel.diaphragme == PositionDiaphragme.ISO:
            # Vérifier bypass
            if etat_actuel.bypass == PositionBypass.FERME:
                # Déjà en position, rien à faire
                return CommandesVannes(diaphragme=None, bypass=None, pdc=None)
            elif etat_actuel.bypass == PositionBypass.OUVERT:
                # Fermer juste le bypass
                log.info("ISO atteint, fermeture bypass")
                return CommandesVannes(diaphragme=None, bypass=False, pdc=None)
            else:
                # Bypass en transit ou inconnu, attendre
                return CommandesVannes(diaphragme=None, bypass=None, pdc=None)

        # Sous-cas 1.2 : Diaphragme Miller (transition BAS → HAUT)
        elif etat_actuel.diaphragme == PositionDiaphragme.MILLER:
            # Logique séquentielle : fermer bypass d'abord, puis diaphragme
            if etat_actuel.bypass != PositionBypass.FERME:
                # Étape 1 : Fermer bypass
                log.info("Transition Miller→ISO : fermeture bypass d'abord")
                return CommandesVannes(diaphragme=None, bypass=False, pdc=None)
            else:
                # Étape 2 : Bypass fermé, commander diaphragme vers ISO
                log.info("Transition Miller→ISO : bypass fermé, commande diaphragme ISO")
                return CommandesVannes(diaphragme="iso", bypass=None, pdc=None)

        # Sous-cas 1.3 : Diaphragme en transit ou inconnu
        else:
            # Attendre fin de mouvement
            return CommandesVannes(diaphragme=None, bypass=None, pdc=None)

    # Cas 2 : Mode cible = NORMAL_LOW ou PDC_CONTROL (Miller souhaité)
    elif mode_cible in (ModeRegulation.NORMAL_LOW, ModeRegulation.PDC_CONTROL):
        # Sous-cas 2.1 : Diaphragme déjà Miller
        if etat_actuel.diaphragme == PositionDiaphragme.MILLER:
            # Vérifier bypass (doit rester OUVERT pour modes Miller)
            if etat_actuel.bypass == PositionBypass.OUVERT:
                # Déjà en position correcte, rien à faire
                return CommandesVannes(diaphragme=None, bypass=None, pdc=None)
            elif etat_actuel.bypass == PositionBypass.FERME:
                # Ouvrir le bypass (transition terminée)
                log.info("Miller atteint, ouverture bypass")
                return CommandesVannes(diaphragme=None, bypass=True, pdc=None)
            else:
                # Bypass en transit ou inconnu, attendre
                return CommandesVannes(diaphragme=None, bypass=None, pdc=None)

        # Sous-cas 2.2 : Diaphragme ISO (transition HAUT → BAS)
        elif etat_actuel.diaphragme == PositionDiaphragme.ISO:
            # Commande simultanée : diaphragme vers Miller ET bypass ouvert
            log.info("Transition ISO→Miller : commande simultanée diaphragme Miller + bypass ouvert")
            return CommandesVannes(diaphragme="miller", bypass=True, pdc=None)

        # Sous-cas 2.3 : Diaphragme en transit ou inconnu
        else:
            # Attendre fin de mouvement
            return CommandesVannes(diaphragme=None, bypass=None, pdc=None)

    # Cas par défaut (ne devrait jamais arriver)
    log.error("Mode cible inconnu : %s", mode_cible)
    return CommandesVannes(diaphragme=None, bypass=None, pdc=None)
