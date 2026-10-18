# -*- coding: utf-8 -*-
"""Décodage des fins de course des vannes (core/etat_vannes.py)."""

from core.etat_vannes import (lire_etat_vannes, PositionDiaphragme,
                              PositionBypass, PositionPDC)

BYPASS_PDC = {"fc_bypass_ferme": True, "fc_pdc_ouvert": True}


def test_correspondance_par_defaut():
    e = lire_etat_vannes({"fc_3voies_0": True, **BYPASS_PDC})
    assert e.diaphragme == PositionDiaphragme.ISO
    assert e.bypass == PositionBypass.FERME
    assert e.pdc == PositionPDC.OUVERT
    assert e.coherent
    e = lire_etat_vannes({"fc_3voies_90": True, **BYPASS_PDC})
    assert e.diaphragme == PositionDiaphragme.MILLER


def test_plomberie_croisee():
    """La butée est traduite en diaphragme par positions_vanne_3voies."""
    croisee = {"0": "miller", "90": "iso"}
    assert lire_etat_vannes({"fc_3voies_0": True, **BYPASS_PDC},
                            croisee).diaphragme == PositionDiaphragme.MILLER
    assert lire_etat_vannes({"fc_3voies_90": True, **BYPASS_PDC},
                            croisee).diaphragme == PositionDiaphragme.ISO


def test_aucune_butee_en_transit():
    e = lire_etat_vannes(dict(BYPASS_PDC))
    assert e.diaphragme == PositionDiaphragme.EN_TRANSIT


def test_butee_180_ou_double_inexploitable():
    """180° n'est atteignable qu'à la main : position inexploitable, comme
    deux butées lues ensemble."""
    for fc in ({"fc_3voies_180": True}, {"fc_3voies_0": True, "fc_3voies_90": True},
               {"fc_3voies_90": True, "fc_3voies_180": True}):
        e = lire_etat_vannes({**fc, **BYPASS_PDC})
        assert e.diaphragme == PositionDiaphragme.INCONNU
        assert not e.coherent
