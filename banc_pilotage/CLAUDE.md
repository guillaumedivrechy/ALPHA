# CLAUDE.md — Logiciel d'acquisition de données capteur

> Fichier de contexte lu automatiquement par Claude Code à la racine du dépôt.
> Objectif : produire du code cohérent, sûr et conforme aux designs d'interface fournis.

---

## 1. Vue d'ensemble du projet

Application **desktop** d'acquisition, de visualisation et d'enregistrement de
données capteur en temps réel.

- **Langage** : Python 3.11+
- **GUI** : **PySide6** (par défaut — licence LGPL, adaptée à la distribution).
  Si le projet bascule sur PyQt6, garder une API compatible Qt6.
- **Cible** : Windows / Linux (préciser ici la plateforme principale).

> ⚠️ Protocoles capteur : **non figés**. Le code DOIT rester agnostique du
> protocole grâce à la couche d'abstraction décrite en §4.

---

## 2. Règles d'or (à ne jamais enfreindre)

1. **Jamais d'I/O bloquante sur le thread GUI.** Toute lecture capteur,
   ouverture de port, ou attente réseau se fait dans un `QThread`/worker.
   La communication worker → UI passe **uniquement** par signaux/slots Qt.
2. **Séparer acquisition et affichage.** L'UI ne parle jamais directement au
   matériel : elle passe par la couche `drivers/` (§4).
3. **Pas de logique métier dans les fichiers de vue.** Les widgets affichent,
   les contrôleurs orchestrent, les drivers acquièrent.
4. **Respecter le design system (§6)** avant d'inventer un style.
5. Toujours proposer un plan avant une modification touchant >2 fichiers.

---

## 3. Structure du projet

```
src/
  app/
    main.py            # point d'entrée, instancie QApplication
    main_window.py     # fenêtre principale (vue uniquement)
  ui/
    widgets/           # widgets réutilisables (graphes, jauges, panneaux)
    views/             # écrans complets assemblant des widgets
    theme.py           # tokens de design (couleurs, espacements, polices)
  core/
    controller.py      # orchestration acquisition <-> UI (signaux/slots)
    acquisition.py     # worker QThread : boucle de lecture
    buffer.py          # buffer circulaire thread-safe des échantillons
  drivers/
    base.py            # interface abstraite SensorDriver (§4)
    registry.py        # enregistrement/découverte des drivers
    # serial_driver.py, modbus_driver.py, tcp_driver.py ... ajoutés au besoin
  storage/
    recorder.py        # écriture CSV / HDF5 / Parquet
  config/
    settings.py        # chargement config (pydantic / dataclasses)
tests/
docs/
  designs/             # mockups d'interface fournis (PNG/Figma export)
```

---

## 4. Couche d'abstraction matérielle (point critique)

Comme les protocoles ne sont pas figés, **toute communication passe par une
interface commune**. Pour ajouter un capteur, on crée un driver — sans jamais
modifier l'UI ni le contrôleur.

```python
# drivers/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass(frozen=True)
class Sample:
    timestamp: float          # epoch monotone
    channel: str
    value: float
    unit: str

class SensorDriver(ABC):
    """Contrat commun à tout capteur, quel que soit le protocole."""

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def read(self) -> list[Sample]:
        """Lecture non bloquante d'un lot d'échantillons."""

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self): self.open(); return self
    def __exit__(self, *exc): self.close()
```

Règles :
- Un nouveau protocole = un nouveau fichier `drivers/<nom>_driver.py` qui
  implémente `SensorDriver`, enregistré dans `drivers/registry.py`.
- Le worker d'acquisition ne connaît QUE `SensorDriver`, jamais `pyserial`,
  `pymodbus`, sockets, etc. directement.
- Dépendances protocole isolées et optionnelles (extras) :
  `pyserial`, `pymodbus`, `paho-mqtt`… importées seulement dans leur driver.

---

## 5. Modèle de threading temps réel

```
[QThread worker]  driver.read() en boucle  --signal newSamples-->  [GUI thread]
                                                                     |-> buffer
                                                                     |-> graphes
                                                                     |-> recorder
```

- Acquisition dans une sous-classe de `QObject` déplacée via `moveToThread`,
  OU un `QThread` dédié. Émettre `Signal(list)` (lots), pas un signal par point.
- Rafraîchir l'affichage à cadence fixe (timer ~30–60 ms) en lisant le buffer,
  **découplé** de la cadence d'acquisition.
- Arrêt propre : flag + `quit()`/`wait()` du thread avant fermeture fenêtre.

---

## 6. Design system & interfaces

Les maquettes de référence sont dans `docs/designs/`. **Suivre la maquette
fournie** ; en cas d'écart, demander avant d'improviser.

Tokens centralisés dans `ui/theme.py` (ne jamais coder une couleur en dur
ailleurs) :

```python
# ui/theme.py  -- à ajuster selon la maquette
COLORS = {
    "bg": "#0F1419", "surface": "#1A2129", "accent": "#3DDC97",
    "text": "#E6EDF3", "muted": "#8B98A5",
    "ok": "#3DDC97", "warn": "#FFB454", "error": "#FF5C5C",
}
SPACING = {"xs": 4, "sm": 8, "md": 16, "lg": 24, "xl": 32}  # px
FONT = {"family": "Inter", "size_base": 13, "size_title": 18}
RADIUS = 8
```

Conventions UI :
- Réutiliser le vocabulaire de la maquette dans les noms de classes/widgets
  (ex. mockup « panneau capteurs » → `SensorPanel`).
- Styles via QSS centralisé ou propriétés, jamais de `setStyleSheet` dispersés.
- Graphes temps réel : préciser la lib (**pyqtgraph** recommandé pour la perf
  temps réel, plutôt que matplotlib).

---

## 7. Conventions de code

- **PEP 8**, formatage **ruff** (`ruff format` + `ruff check`).
- **Type hints obligatoires** sur fonctions publiques ; docstrings courtes.
- Pas de variable globale mutable ; config via `config/settings.py`.
- Logging via le module `logging` (pas de `print`), niveau configurable.
- Gestion d'erreur explicite sur l'I/O matériel (timeout, déconnexion,
  reconnexion) — ne jamais laisser une exception tuer le thread silencieusement.

---

## 8. Données & enregistrement

- Buffer circulaire borné (taille configurable) pour éviter de saturer la RAM.
- Export : CSV par défaut ; HDF5/Parquet pour gros volumes (préciser).
- Horodatage monotone côté acquisition + horodatage mural pour l'enregistrement.

---

## 9. Tests

- `pytest`. Drivers testés via un `FakeSensorDriver` qui rejoue des `Sample`.
- Pas besoin de matériel réel pour la CI : tout passe par `SensorDriver`.
- Tester : buffer thread-safe, parsing des trames, logique du contrôleur.

---

## 10. Commandes utiles

```bash
# Installation
python -m venv .venv && source .venv/bin/activate   # (.venv\Scripts\activate sous Windows)
pip install -e ".[dev]"

# Lancer l'app
python -m app.main

# Qualité & tests
ruff format . && ruff check .
pytest -q
```

---

## 11. Travailler avec les maquettes (rappel de workflow)

- Déposer la maquette dans le chat (Ctrl+V même sur macOS, ou glisser-déposer)
  ou la référencer : `@docs/designs/ecran_principal.png`.
- Demander d'abord une **version statique** de l'écran conforme à la maquette,
  puis brancher l'acquisition réelle.
- Itérer par retours ciblés (« espacement trop serré », « jauge à droite »).
