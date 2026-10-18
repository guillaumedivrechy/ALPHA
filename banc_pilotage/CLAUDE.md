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
  air_humide.py        propriétés de l'air humide (si humidite_relative > 0)
  banc.py              consigne Re <-> qm, sélection diaphragme
  regulation.py        PI (anti-windup, rampe, bande morte)
  etat_vannes.py       décodage des fins de course (butée -> diaphragme
                       via modbus.positions_vanne_3voies)
  gestion_modes.py     mode cible / effectif (normal_high, normal_low,
                       pdc_control) — seuils fournis par le Banc
  commandes_vannes.py  ordres TOR pour rejoindre un mode
  table_gains_pdc.py   interpolation de la table PdC (% d'ouverture <-> Re)
  sequence.py          chargement/validation des profils CSV (mode séquence)
  pilote.py            boucle temps réel (thread), manœuvres de vannes,
                       suivi de séquence, feed-forward appris
  apprentissage.py     séquences de caractérisation (thread dédié)
  gestionnaire_apprentissages.py  apprentissages/*.json (+ etat_banc.json)
  journal.py           journal CSV des essais
  logger_regulation.py log de diagnostic (journal.diagnostic_regulation)
  fichiers.py          écriture JSON atomique (config, état, apprentissages)
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
   par un verrou interne à `WagoModbus`. Seule exception : les séquences
   d'apprentissage manœuvrent les vannes en direct pour chronométrer leurs
   courses (maintien TOR de la boucle suspendu) ; le ventilateur, lui, ne
   passe que par `Apprentissage._variateur`, qui refuse toute relance après
   un arrêt.
3. **Sécurité** : toute nouvelle branche de la boucle doit préserver les
   replis sûrs : tout défaut passe par `Pilote._repli_sur()` (variateur
   0 V, RUN off, régulation ET manuel ET séquence coupés, latché ; pose
   `etat["arret_securite"]`, que seul `acquitter_arret_securite()` lève —
   l'IHM refuse toute relance avant), chien de garde par vanne.
   `arreter_regulation()` doit rester un arrêt TOTAL (régulation ET
   manuel ; il interrompt aussi les apprentissages).
4. **Config, pas de constantes en dur** : adresses Modbus, géométries,
   gains, temps de course → `config.json`.
5. **Moteur de calcul verrouillé par les tests** : `python -m pytest tests/`
   doit passer avant tout commit touchant `core/`.
6. Clés commençant par `_` dans `config.json` = commentaires : toute
   itération générique sur la config doit les ignorer.
7. **Fichiers** : toute écriture de config/état/apprentissage passe par
   `core.fichiers.ecrire_json_atomique`. Les chemins de données sont sous
   `core.pilote.RACINE`, que les tests redirigent vers un dossier temporaire
   (`tests/conftest.py`) : un test n'écrit jamais dans les sources.

## Commandes

```bash
pip install -r requirements.txt
python main.py --simulation      # sans matériel
python main.py                   # coupleur Wago (config.json)
python -m pytest tests/ -q
```
