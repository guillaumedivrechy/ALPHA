# -*- coding: utf-8 -*-
"""Rend les paquets core/ et io_banc/ importables depuis tests/."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


@pytest.fixture(autouse=True)
def _donnees_hors_sources(tmp_path_factory, monkeypatch):
    """Journaux, état du banc, apprentissages et log de diagnostic écrits
    par le pilote vont dans un dossier temporaire : les tests n'écrivent
    plus dans l'arborescence des sources, et n'y lisent plus les
    apprentissages locaux de la machine (résultats non reproductibles)."""
    import core.pilote
    monkeypatch.setattr(core.pilote, "RACINE",
                        tmp_path_factory.mktemp("racine_banc"))
