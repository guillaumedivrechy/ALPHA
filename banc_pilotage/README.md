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
3. Le **ΔP attendu** est calculé (modèle inverse). Une consigne dont le
   ΔP dépasserait la plage du capteur est **bornée au maximum mesurable**
   (alerte « consigne limitée »). La tension variateur est la somme :
   - d'un **feed-forward** : tension d'équilibre interpolée dans la table
     apprise (« Re mapping » ISO ou Miller), rejointe sur une rampe ;
   - d'une **correction PI en volts** : elle poursuit le Re que produirait
     ce feed-forward (modèle de référence à la constante de temps du
     procédé), pas l'échelon brut, ce qui évite le dépassement. Son
     intégrale annule l'écart table/banc, et ce biais est mémorisé pour la
     consigne suivante.

   À chaque cycle, le logiciel lit ΔP, P_amont et T, recalcule le débit
   réel (ISO 5167 ou Miller, itératif comme dans Excel) et en déduit le Re
   calculé. La correction est gelée tant que la recopie du variateur n'a
   pas rejoint la commande. Sous le Re minimal du variateur seul, celui-ci
   est fixé à sa tension mini et un PI règle la vanne perte de charge.
4. Chaque voie ΔP et P_amont fournit un signal **0-10 V = 0-500 Pa**. Les
   voies **ΔP Miller** et **P_amont** embarquent deux capteurs montés
   **maître/esclave (50 Pa + 500 Pa)** : sous ~50 Pa la mesure provient du
   capteur fin (meilleure précision aux bas ΔP), au-dessus du capteur
   500 Pa. Cette bascule est **matérielle** et transparente pour le
   logiciel — la sortie reste 0-10 V = 0-500 Pa. La voie **ΔP ISO**
   (débits plus élevés, ΔP plus grands) n'a que le capteur 500 Pa.
5. ρ et μ de l'air sont **recalculés en continu** (gaz parfait + Sutherland)
   à partir de P_amont et de la PT100 — plus précis que les tables figées
   des classeurs. Des valeurs forcées restent possibles (`config.json`).

## Installation

Python ≥ 3.10.

```bash
pip install -r requirements.txt   # PySide6, pyqtgraph, pymodbus, pytest
```

(`pymodbus` n'est nécessaire qu'en mode matériel ; le mode `--simulation`
fonctionne sans.)

## Lancement

```bash
python main.py --simulation     # banc simulé : test sans matériel
python main.py                  # connexion au coupleur Wago (config.json)
```

Le mode simulation reproduit la chaîne complète (ventilateur 1er ordre,
ΔP générés par les mêmes équations, bruit capteur) : il permet de former
les opérateurs et de régler les gains sans toucher au banc.

## Mode séquence

L'onglet **Séquence** suit automatiquement un profil chargé depuis un CSV
à deux colonnes — temps [s] ; Reynolds cible (`exemple_sequence.csv`) :

- séparateur `;`, `,` ou tabulation ; virgule décimale acceptée ; ligne
  d'en-tête optionnelle ; lignes vides ou commençant par `#` ignorées ;
- temps strictement croissants ; la consigne évolue **par échelons** aux
  échéances du profil (référence Conduite/Veine du panneau Pilotage) ;
- le temps de la dernière ligne marque la **fin de la séquence** : sa
  consigne est ensuite maintenue jusqu'à l'arrêt par l'opérateur — mettre
  `Re = 0` en dernière ligne pour arrêter la soufflerie en fin d'essai.

« ARRÊT VARIATEUR », le passage en manuel ou l'application d'une consigne
fixe interrompent la séquence.

## Configuration (`config.json`)

| Section | Contenu |
|---|---|
| `diaphragme_iso` / `diaphragme_miller` | D, d à 20 °C, coefficients de dilatation, formule de C (RHG/Stolz) |
| `veine` | **à renseigner** : section [m²] et longueur caractéristique [m] pour le Re veine |
| `fluide` | γ, et valeurs forcées ρ/μ optionnelles |
| `selection_diaphragme` | seuil Re = 5000 et hystérésis |
| `regulation` | période, gains PI, rampe du variateur, tolérance de convergence |
| `modbus` | IP du coupleur et **mapping des voies** |
| `simulation` | paramètres du banc simulé |

### Mapping Modbus Wago 750-362

L'image process du coupleur dépend de l'**ordre physique des modules** sur
le nœud. Les adresses par défaut de `config.json` supposent :

- entrées analogiques (FC4) : ΔP Miller (mot 0), P_amont (mot 2), ΔP ISO
  (mot 4) — chacune une sortie unique 0-10 V = 0-500 Pa ; les voies ΔP
  Miller et P_amont sont des capteurs maître/esclave 50+500 Pa (bascule
  matérielle interne, cf. § Principe 4). Puis recopie vanne PdC et module
  RTD PT100 (750-461, valeur en 0,1 °C) ;
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
- Bouton « ARRÊT VARIATEUR » → coupe régulation **et** mode manuel
  (tension 0 V, ordre RUN retiré).

> ⚠️ Si le lien Modbus lui-même est rompu, le logiciel ne peut plus écrire
> 0 V : la mise en sécurité repose alors sur le paramétrage du variateur
> 3G3MV (comportement sur perte de référence / perte du contact RUN).
> À vérifier à la mise en service.

## Structure du code

```
banc_pilotage/
├── main.py                  point d'entrée (--simulation)
├── config.json              toutes les constantes du banc
├── core/
│   ├── diaphragmes.py       équations ISO 5167 & Miller (direct + inverse)
│   ├── fluide.py            ρ, μ de l'air (gaz parfait, Sutherland)
│   ├── banc.py              consigne Re ↔ qm, sélection diaphragme, gammes
│   ├── regulation.py        feed-forward + correction PI, PI vanne PdC
│   ├── sequence.py          chargement des profils CSV (mode séquence)
│   └── pilote.py            boucle temps réel, feed-forward adaptatif
├── io_banc/
│   ├── wago.py              client Modbus TCP (mapping configurable)
│   └── simulateur.py        banc virtuel pour tests
├── gui/
│   └── app.py               IHM PySide6 + pyqtgraph (consigne, mesures, tendance)
├── ui/
│   └── theme.py             tokens de design (couleurs, QSS)
└── tests/                   tests pytest (moteur de calcul, PI, table de gains)
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

Ces valeurs sont verrouillées par les tests de non-régression :

```bash
python -m pytest tests/ -q
```

## Glossaire technique

### Acronymes et grandeurs

| Terme | Signification | Unité | Description |
|-------|---------------|-------|-------------|
| **qm** | Débit massique | kg/s | Débit d'air traversant le diaphragme, calculé depuis ΔP (ISO 5167 / Miller) |
| **qv** | Débit volumique | m³/s | qv = qm / ρ (conservation de la masse) |
| **Re** | Nombre de Reynolds | - | Critère d'écoulement (Re = ρ·v·Lc/μ). Re > 5000 → diaphragme ISO ; Re < 5000 → Miller |
| **ΔP** (dP) | Différence de pression | Pa | Pression différentielle au diaphragme, mesurée par capteurs FCO432 |
| **P1_abs** | Pression absolue amont | Pa | P1_abs = P_atm + P_amont (gauge). Nécessaire pour calcul ρ (gaz parfait) |
| **T** | Température | °C | Mesurée par PT100 (module 750-461), utilisée pour ρ et μ (Sutherland) |
| **ρ** (rho) | Masse volumique | kg/m³ | ρ = P/(R·T) pour l'air (gaz parfait), recalculé en continu |
| **μ** (mu) | Viscosité dynamique | Pa·s | Loi de Sutherland (fonction de T), utilisée pour Re et calculs diaphragmes |
| **ν** (nu) | Viscosité cinématique | m²/s | ν = μ / ρ |
| **PdC** | Perte de charge | - | Vanne proportionnelle (0-10 V) pour mode très bas Re (< freq_min variateur) |
| **FC** | Fin de course | - | Capteur TOR (contact NF sur GF3) indiquant position atteinte de la vanne 3 voies |
| **FF** | Feed-forward | V | Tension d'équilibre U(Re) interpolée dans la table apprise (RE mapping) |
| **Trim** | Correction PI | V | Écart en volts ajouté au feed-forward : U_total = U_FF + Trim |

### Termes métier

| Terme | Description |
|-------|-------------|
| **Diaphragme ISO** | Diaphragme normalisé ISO 5167-1 (NFX 10-102), d=49,6 mm, pour Re > 5000 (vanne 3V à 0°) |
| **Diaphragme Miller** | Diaphragme corrélation Miller, d=9,96 mm, pour Re < 5000 (vanne 3V à 90°) |
| **Vanne 3 voies** | Vanne TOR (carte GF3) : 0° → diaphragme ISO ; 90° → diaphragme Miller |
| **Vanne bypass** | Vanne TOR : ouverte pendant manœuvre 3 voies pour éviter surpression |
| **Re conduite** | Reynolds calculé dans la conduite Ø81,4 mm (critère de sélection diaphragme) |
| **Re veine** | Reynolds dans la veine d'essai rectangulaire (consigne utilisateur possible) |
| **Maître/esclave** | Montage de 2 capteurs (50 Pa + 500 Pa) avec bascule automatique pour précision bas ΔP |
| **Hystérésis** | Marge de ±500 Re autour du seuil 5000 pour éviter commutations répétées |
| **Anti-windup** | Limitation de l'intégrale du PI pour éviter dépassements lors de saturations |
| **Bande morte** | Zone ±0,5% autour de la consigne où le terme P est nul (évite oscillations bruit capteur) |
| **Séparation intégrale** | Au-delà de ±3% d'erreur, intégrale gelée tant que la mesure évolue (évite wind-up pendant montée) |
| **Rampe variateur** | Vitesse maximale de montée/descente de U (ex: 2 V/s) pour protéger mécanique |
| **RE mapping** | Apprentissage de la table U(Re) par balayage : mémorisation des tensions d'équilibre |
| **Freq min** | Apprentissage de la tension minimale du variateur (seuil de décrochage moteur) |
| **Indiciel** | Test échelon pour extraire tau (constante de temps) et Kp_procédé (réglage PI auto) |
| **IMC** | Internal Model Control : méthode de réglage PI (kp = 1/(Kp·λ), ki = kp/tau, λ=tau/3) |

### Modules matériel (Wago 750-362)

| Module | Fonction | Échelle |
|--------|----------|---------|
| **750-468** | 4 entrées analogiques 0-10 V | 0…32767 (0x7FFF) pour 10 V |
| **750-461** | 2 RTD PT100 (3 fils) | Valeur signée en 0,1 °C (ex: 235 = 23,5 °C) |
| **750-559** | 4 sorties analogiques 0-10 V | 0…32767 pour 10 V |
| **750-530** | 8 sorties TOR relais (coils) | Adressage à partir de 512 (config dépend ordre modules) |
| **FCO432** | Capteur différentiel ±500 Pa | Sortie 0-10 V = 0-500 Pa (ou ±250 Pa selon calibration) |
| **GF3** | Carte motorisation 3 voies | 2 ordres TOR (F=0°, I=90°), contact NF fin de course FC3 |

### Algorithmes et équations

| Terme | Description |
|-------|-------------|
| **ISO 5167-1** | Norme internationale pour diaphragmes à Re > 5000. Calcul itératif de C (RHG ou Stolz) |
| **Corrélation Miller** | Formule empirique pour diaphragmes à Re < 5000 (non normalisé ISO) |
| **Gaz parfait** | ρ = P/(R·T) avec R = 287,05 J/(kg·K) pour l'air sec |
| **Sutherland** | μ(T) = μ_ref · (T/T_ref)^(3/2) · (T_ref + S)/(T + S) avec S=110,4 K |
| **Magnus-Tetens** | Formule pression vapeur saturante (humidité relative → correction ρ) |
| **PI** | Régulateur Proportionnel-Intégral : U = Kp·e + Ki·∫e·dt |
| **Modèle de référence** | Consigne filtrée passe-bas (tau_consigne) pour éviter overshoot PI |

### Sécurités et alarmes

| Terme | Signification |
|-------|---------------|
| **Repli sûr** | Variateur 0 V + RUN off sur toute erreur (Modbus, capteur, calcul) |
| **Chien de garde vannes** | Timeout 45s sur manœuvre 3 voies, 15s sur bypass (alarme si FC non atteint) |
| **Trim saturé** | Alarme si abs(intégrale) > trim_max-1e-6 pendant >5s → table de gains obsolète |
| **Consigne limitée** | ΔP attendu > plage capteur → Re plafonné au maximum mesurable |
| **Défaut capteur** | P_abs hors 800-1200 mbar → arrêt régulation avec message explicite |
| **Saturation air** | P ≤ Pv × 1,01 → ValueError (risque condensation, calculs invalides) |

## Documentation complémentaire

- **TUNING_GUIDE.md** : Réglage des gains PI (kp, ki), paramètres avancés, diagnostic
- **RAPPORT_AUDIT_COMPLET.md** : Analyse architecture, problèmes identifiés, recommandations
- **CORRECTIONS_APPLIQUEES.md** : Historique des correctifs (Phases 1 et 2)
- **CLAUDE.md** : Contexte projet pour Claude Code (règles architecture, threading)
- **etat_banc.json** : Tables de gains feed-forward apprises (RE mapping ISO/Miller, freq_min)
