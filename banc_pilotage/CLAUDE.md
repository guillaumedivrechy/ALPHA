# CLAUDE.md — Pilotage du banc aéraulique (diaphragmes ISO 5167 / Miller)

> Contexte projet pour Claude Code. Voir `README.md` pour la description
> fonctionnelle complète.
> Voir `claude-code-2.1.172-fable-5.md`
> Voir `code-review.md`

## Vue d'ensemble

Application **desktop Windows** de pilotage automatique d'une soufflerie :
asservissement du nombre de Reynolds via un variateur 0-10 V, mesures par
diaphragmes (ISO 5167 ou Miller selon le Re), E/S sur coupleur
**Wago 750-362 (Modbus TCP)**.

- **Langage** : Python ≥ 3.10
- **GUI** : PySide6 + pyqtgraph (`gui/app.py`, thème dans `ui/theme.py`)
- **E/S** : `pymodbus` (mode matériel) ou simulateur (`--simulation`)

## Architecture (à respecter)

```
main.py                point d'entrée (--simulation, --config)
config.json            TOUTES les constantes du banc — aucun code à modifier
core/
  diaphragmes.py       équations ISO 5167 & Miller (direct + inverse) —
                       reproduisent les classeurs Excel, NE PAS "corriger"
                       une formule sans re-valider contre le README
  fluide.py            rho, mu de l'air (gaz parfait, Sutherland)
  banc.py              consigne Re <-> qm, sélection diaphragme
  regulation.py        PI (anti-windup, rampe, bande morte)
  sequence.py          chargement/validation des profils CSV (mode séquence)
  pilote.py            boucle temps réel (thread), manœuvres de vannes,
                       suivi de séquence, feed-forward appris (etat_banc.json)
  apprentissage.py     séquences de caractérisation (thread dédié)
io_banc/
  wago.py              EntreesSorties (interface) + WagoModbus
  simulateur.py        banc virtuel (mêmes équations)
gui/app.py             IHM — lit pilote.etat via QTimer, ne calcule rien
tests/                 pytest — sans matériel ni Qt
```

## Règles

1. **Threading** : la boucle du pilote et l'apprentissage tournent dans des
   threads ; l'IHM ne fait que lire `pilote.etat` (dict publié) sur un
   `QTimer`. Pas d'I/O bloquante dans le thread Qt.
2. **Toutes les commandes matériel passent par les méthodes publiques de
   `Pilote`** (`commander_diaphragme`, `commander_bypass`,
   `commander_vanne_perte_charge`, `arreter_regulation`…) — jamais
   d'écriture directe `es.*` depuis l'IHM. Le client pymodbus est protégé
   par un verrou interne à `WagoModbus`.
3. **Sécurité** : toute nouvelle branche de la boucle doit préserver les
   replis sûrs (variateur 0 V + RUN off sur défaut, chien de garde des
   vannes). `arreter_regulation()` doit rester un arrêt TOTAL (régulation
   ET manuel).
4. **Config, pas de constantes en dur** : adresses Modbus, géométries,
   gains, temps de course → `config.json`.
5. **Moteur de calcul verrouillé par les tests** : `python -m pytest tests/`
   doit passer avant tout commit touchant `core/`.
6. Clés commençant par `_` dans `config.json` = commentaires : toute
   itération générique sur la config doit les ignorer.

## Commandes

```bash
pip install -r requirements.txt
python main.py --simulation      # sans matériel
python main.py                   # coupleur Wago (config.json)
python -m pytest tests/ -q
```
