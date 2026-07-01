# -*- coding: utf-8 -*-
"""Rend les paquets core/ et io_banc/ importables depuis tests/."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
