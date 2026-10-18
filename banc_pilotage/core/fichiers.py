# -*- coding: utf-8 -*-
"""Écriture sûre des fichiers de configuration et d'état.

Un fichier écrit en place et interrompu (coupure secteur, disque plein,
plantage) reste tronqué : config.json illisible empêche le démarrage
suivant, un fichier d'apprentissage tronqué perd les tables. On écrit donc
dans un fichier temporaire du même dossier, puis on le substitue d'un bloc
(remplacement atomique sous Windows comme sous POSIX).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def ecrire_json_atomique(chemin, donnees) -> None:
    """Écrit `donnees` en JSON dans `chemin`, sans jamais laisser de fichier
    partiellement écrit. Lève OSError en cas d'échec (le fichier d'origine
    est alors intact)."""
    chemin = Path(chemin)
    fd, tmp = tempfile.mkstemp(dir=chemin.parent, prefix=f".{chemin.name}.",
                               suffix=".tmp", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(donnees, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        Path(tmp).replace(chemin)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
