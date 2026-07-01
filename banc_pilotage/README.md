# Logiciel de pilotage du banc aéraulique — diaphragmes ISO 5167 / Miller

Pilotage automatique du variateur de la soufflerie pour atteindre un nombre
de Reynolds cible, à partir des capteurs du banc (FCO432, PT100), via un
coupleur **Wago 750-362 (Modbus TCP)**.

Remplace l'usage manuel des deux classeurs Excel :
- `Calcul_diaphragme_ISO_V12` (NFX 10-102 / ISO 5167-1, Re > 5000)
- `Calcul_diaphragme_Miller_V1-13` (corrélation Miller, Re < 5000)

Les équations de ces classeurs sont reproduites à l'identique dans
`core/diaphragmes.py` et **validées numériquement contre les exemples des
deux fichiers** (qm, Re, C, ε, Y1).

## Principe de fonctionnement

1. L'opérateur saisit le **Reynolds cible** (référence au choix : conduite
   Ø81,4 mm ou veine d'essai) et démarre la régulation.
2. Le logiciel convertit la consigne en débit massique visé, en déduit le
   **Re conduite attendu**, et commande la **vanne TOR 3 voies** vers le bon
   diaphragme : Miller si Re < 5000, ISO sinon (hystérésis ±500 contre les
   commutations répétées).
3. Le **ΔP attendu** est calculé (modèle inverse) ; le variateur est
   **pré-positionné** grâce au gain du banc appris en fonctionnement
   (feed-forward), puis un **régulateur PI** affine : à chaque cycle, le
   logiciel lit ΔP, P_amont et T, recalcule le débit réel (ISO 5167 ou
   Miller, itératif comme dans Excel), en déduit le Re mesuré et corrige la
   tension 0-10 V.
4. La **gamme capteur ΔP** est choisie automatiquement : FCO432 50 Pa
   (0-1 V, plus précis) sous ~45 Pa, sinon 500 Pa (0-10 V). Idem P_amont.
5. ρ et μ de l'air sont **recalculés en continu** (gaz parfait + Sutherland)
   à partir de P_amont et de la PT100 — plus précis que les tables figées
   des classeurs. Des valeurs forcées restent possibles (`config.json`).

## Installation

Python ≥ 3.10 avec tkinter (inclus dans l'installateur Windows standard).

```bash
pip install pymodbus        # uniquement pour le mode matériel
```

## Lancement

```bash
python main.py --simulation     # banc simulé : test sans matériel
python main.py                  # connexion au coupleur Wago (config.json)
```

Le mode simulation reproduit la chaîne complète (ventilateur 1er ordre,
ΔP générés par les mêmes équations, bruit capteur) : il permet de former
les opérateurs et de régler les gains sans toucher au banc.

## Configuration (`config.json`)

| Section | Contenu |
|---|---|
| `diaphragme_iso` / `diaphragme_miller` | D, d à 20 °C, coefficients de dilatation, formule de C (RHG/Stolz) |
| `veine` | **à renseigner** : section [m²] et longueur caractéristique [m] pour le Re veine |
| `fluide` | γ, et valeurs forcées ρ/μ optionnelles |
| `selection_diaphragme` | seuil Re = 5000 et hystérésis |
| `capteurs_dp` | seuil de bascule 50 Pa / 500 Pa |
| `regulation` | période, gains PI, rampe du variateur, tolérance de convergence |
| `modbus` | IP du coupleur et **mapping des voies** |
| `simulation` | paramètres du banc simulé |

### Mapping Modbus Wago 750-362

L'image process du coupleur dépend de l'**ordre physique des modules** sur
le nœud. Les adresses par défaut de `config.json` supposent :

- entrées analogiques (FC4, mots 0…) : ΔP 500 Pa, ΔP 50 Pa, P_amont 500 Pa,
  P_amont 50 Pa, ΔP diaphragme ISO, puis module RTD PT100 (750-461,
  valeur en 0,1 °C) ;
- sorties analogiques (FC6/16, mots 0…) : variateur, vanne perte de charge ;
- sorties TOR (coils à partir de 512) : vanne 3 voies (ON = ISO),
  vanne bypass.

**Vérifiez avec WAGO I/O CHECK** et ajustez les adresses dans
`config.json` — aucun code à modifier. Échelle analogique : 0-10 V ↔
0…32767 (0x7FFF).

## Sécurités intégrées

- Perte de communication Modbus → variateur forcé à 0 V, alerte à l'IHM.
- Limitation de rampe du variateur (0,5 V/s par défaut).
- Anti-windup du PI, bande morte paramétrable.
- Fermeture de l'application → variateur remis à 0.

## Structure du code

```
banc_pilotage/
├── main.py                  point d'entrée (--simulation)
├── config.json              toutes les constantes du banc
├── core/
│   ├── diaphragmes.py       équations ISO 5167 & Miller (direct + inverse)
│   ├── fluide.py            ρ, μ de l'air (gaz parfait, Sutherland)
│   ├── banc.py              consigne Re ↔ qm, sélection diaphragme, gammes
│   ├── regulation.py        régulateur PI (anti-windup, rampe)
│   └── pilote.py            boucle temps réel, feed-forward adaptatif
├── io_banc/
│   ├── wago.py              client Modbus TCP (mapping configurable)
│   └── simulateur.py        banc virtuel pour tests
└── gui/
    └── app.py               IHM tkinter (consigne, mesures, tendance)
```

## Validation du moteur de calcul

| Grandeur | Excel | Logiciel |
|---|---|---|
| ISO — qm (ΔP=145 Pa) | 0,015729 kg/s | 0,015729 kg/s |
| ISO — ε | 0,999559 | 0,999559 |
| ISO — C (RHG) | 0,616115 | 0,616122 |
| Miller — q (ΔP=18,3 Pa) | 0,001973 kg/s | 0,001973 kg/s |
| Miller — Re | 1672,661 | 1672,660 |
| Miller — C / Y1 | 0,773839 / 0,999947 | 0,773839 / 0,999947 |

Les écarts résiduels (< 0,1 %) proviennent des arrondis d'affichage des
valeurs ρ/ν lues dans les classeurs pour le test.
