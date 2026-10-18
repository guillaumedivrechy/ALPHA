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
   Ø76,6 mm ou veine d'essai) et démarre la régulation.
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
   pas rejoint la commande. Sous le seuil bas-Re (mode PdC), le variateur
   est fixé un peu au-dessus de sa tension mini (u_min ×
   `strategie_bas_re.marge_ventilateur_pdc`, 1,15 par défaut) et un PI règle
   la vanne perte de charge. Le seuil automatique est placé à mi-chemin
   entre le Re minimal du mode Miller (variateur à u_min) et le Re maximal
   du mode PdC (PdC ouverte) : les deux modes couvrent ainsi la bande
   d'hystérésis. La table PdC est apprise à cette même tension.
4. Chaque voie ΔP et P_amont fournit un signal **0-10 V = 0-500 Pa**. Les
   voies **ΔP Miller** et **P_amont** embarquent deux capteurs montés
   **maître/esclave (50 Pa + 500 Pa)** : sous ~50 Pa la mesure provient du
   capteur fin (meilleure précision aux bas ΔP), au-dessus du capteur
   500 Pa. Cette bascule est **matérielle** et transparente pour le
   logiciel — la sortie reste 0-10 V = 0-500 Pa. La voie **ΔP ISO**
   (débits plus élevés, ΔP plus grands) n'a que le capteur 500 Pa.
5. ρ et μ de l'air sont **recalculés en continu** à partir de P_amont et de
   la PT100 : gaz parfait + Sutherland pour l'air sec, formules d'air humide
   si `fluide.humidite_relative` > 0 (50 % par défaut, valeur fixe : le banc
   n'a pas d'hygromètre) — plus précis que les tables figées
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

## Gestion des vannes à chaque consigne

| Mode | Vanne 3 voies | Bypass | Vanne PdC | Organe régulé | Table |
|---|---|---|---|---|---|
| ISO (Re > seuil ISO/Miller) | ISO | fermé | 100 % | variateur | ISO (bypass fermé) |
| Miller (Re_freq_min < Re < seuil) | Miller | ouvert | 100 % | variateur | Miller (bypass ouvert) |
| PdC (Re < Re_freq_min) | Miller | ouvert | 100 % à l'entrée, puis régulée | vanne PdC (variateur fixe) | PdC |

À chaque cycle, la boucle lit les fins de course des trois vannes et ne
commande que celles qui ne sont pas en position, **toutes simultanément**.
Aucune consigne n'est envoyée au variateur ni à la PdC avant que toutes
soient en butée : variateur à 0 V pendant les manœuvres des vannes TOR ;
si seule la PdC rejoint sa butée (sortie du mode PdC), la tension du
variateur est tenue, sans nouvelle consigne. La consigne initiale vient de la
table apprise du mode (interpolation entre paliers), puis la régulation
corrige en continu l'organe du mode. Une PdC déjà régulée en mode PdC n'est
pas rouverte pour une nouvelle consigne de mode PdC (elle l'est après un
arrêt). Une nouvelle consigne reçue pendant un déplacement redirige aussitôt
les vannes vers leurs nouvelles positions.

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
| `fluide` | γ, Sutherland, humidité relative, valeurs forcées ρ/μ optionnelles |
| `zero_capteurs` | offsets de zéro des capteurs ΔP (apprentissage « zéro capteurs ») |
| `selection_diaphragme` | seuil Re = 5000 et hystérésis (± hystérésis autour du seuil) |
| `actionneurs` | temps de course des vannes (chiens de garde), butées de la recopie PdC |
| `regulation` | période, gains, rampe, tolérance, saturation ΔP (< 1), bornes PT100 |
| `modbus` | IP du coupleur, **mapping des voies**, diaphragme de chaque butée 3 voies |
| `strategie_bas_re` | mode PdC (très bas Re) : activation, seuil, u_min, gains |
| `variateur` | fréquence max (affichage), tolérance de suivi de la recopie AM |
| `journal` | journal CSV des essais ; log de diagnostic de la boucle (désactivé) |
| `apprentissage` | paramètres des séquences de caractérisation |
| `simulation` | paramètres du banc simulé |

### Mapping Modbus Wago 750-362

L'image process du coupleur dépend de l'**ordre physique des modules** sur
le nœud. Les adresses par défaut de `config.json` supposent :

- entrées analogiques 750-497 (FC4, mots 0…) : recopie vanne PdC (0),
  ΔP ISO (1), ΔP Miller (2), P_amont (3), recopie AM du variateur (4) — les
  voies ΔP et P_amont en 0-10 V = 0-500 Pa ; ΔP Miller et P_amont sont des
  capteurs maître/esclave 50+500 Pa (bascule matérielle, cf. § Principe 4) ;
- PT100 sur module RTD 750-450 (mot 16, valeur en 0,1 °C) ;
- entrées TOR 750-1405 (FC2) : fins de course bypass, PdC, vanne 3 voies
  (0°/180°/90°), défaut variateur (contact NF, lecture inversée), défauts
  actionneurs ;
- sorties analogiques 750-597 (FC6/16) : vanne perte de charge (0),
  variateur (1) ;
- relais 750-515 (FC5, coils 0…3) : ordre 0° et ordre 90° de la vanne 3
  voies, bypass, RUN du variateur.

Le diaphragme alimenté à chaque butée de la vanne 3 voies est donné par
`modbus.positions_vanne_3voies` (contrôle de câblage + « Appliquer » le
corrigent si la plomberie est croisée).

**Vérifiez avec WAGO I/O CHECK** et ajustez les adresses dans
`config.json` — aucun code à modifier. Échelle analogique : 0-10 V ↔
0…32767 (0x7FFF).

## Sécurités intégrées

- Tout défaut (communication Modbus, PT100, pression, ΔP, variateur,
  vanne, erreur interne de la boucle) → repli sûr **latché** : variateur
  0 V, ordre RUN retiré, régulation, séquence **et mode manuel** coupés.
  Rien ne redémarre seul quand le défaut disparaît.
- Chien de garde par vanne (1,5 × temps de course de `actionneurs`), et
  délai global d'attente des vannes dérivé des mêmes temps de course.
- Vanne 3 voies sans butée franche hors manœuvre (fil coupé, 180°) →
  repli sûr.
- Saturation du capteur ΔP (`saturation_dp_relative` × pleine échelle,
  borné sous 1 car la lecture est écrêtée) → commande plafonnée ; consigne
  hors plage bornée au maximum mesurable.
- Limitation de rampe du variateur (`regulation.rampe_v_par_s`), anti-windup,
  bande morte paramétrable.
- Bouton « ARRÊT VARIATEUR » → coupe régulation, mode manuel **et** toute
  séquence d'apprentissage (qui ne relance jamais le ventilateur).
- Fermeture de l'application → variateur à 0 V, puis vannes en position de
  repos (Miller, bypass et PdC ouverts).

> ⚠️ Si le lien Modbus lui-même est rompu, le logiciel ne peut plus écrire
> 0 V : la mise en sécurité repose alors sur le paramétrage du variateur
> 3G3MV (comportement sur perte de référence / perte du contact RUN).
> À vérifier à la mise en service.

### À vérifier à la mise en service

Points que le logiciel ne peut pas trancher seul :

- **Échelle de la consigne de la vanne PdC.** Les commandes en % passent
  par les butées de la *recopie* (`actionneurs.pdc_recopie_min_v` /
  `max_v`, environ 2,4 et 9,2 V) : 100 % commande ~9,2 V, 0 % ~2,4 V. Si
  la carte P6 attend 0-10 V linéaire sur toute la course, « 100 % »
  n'ouvrirait qu'à ~91 % et « 0 % » laisserait ~24 % d'ouverture (la
  valeur apprise `pdc_min_exploitable_pct` vaut justement 24 %). Test :
  commander 9,2 V et vérifier que la fin de course « ouverte » est atteinte.
- **Repli du variateur sur perte de liaison** (paramétrage 3G3MV), cf.
  avertissement ci-dessus.
- **Bruit des capteurs ΔP à débit stable.** La détection de signal figé
  arrête le banc si l'écart-type du ΔP reste sous 0,1 Pa pendant 10 s en
  régulation : un capteur très peu bruité pourrait la déclencher à tort.
- **Domaine de validité.** La corrélation RHG (prises d'angle, β = 0,535)
  est normalisée pour ReD ≥ 5000 ; l'hystérésis garde l'ISO jusqu'à 4500.
  En Miller, un ΔP sous 0,3 Pa (Re conduite ≈ 210) est lu comme un débit
  nul.
- **Viscosité.** Avec `fluide.humidite_relative` > 0, μ suit la
  corrélation linéaire du classeur d'air humide (≈ 0,6 % sous Sutherland à
  20 °C, soit ≈ 0,6 % sur le Re) ; avec 0, Sutherland.

## Structure du code

```
banc_pilotage/
├── main.py                  point d'entrée (--simulation)
├── config.json              toutes les constantes du banc
├── core/
│   ├── diaphragmes.py       équations ISO 5167 & Miller (direct + inverse)
│   ├── fluide.py            ρ, μ de l'air (gaz parfait, Sutherland)
│   ├── air_humide.py        propriétés de l'air humide
│   ├── banc.py              consigne Re ↔ qm, sélection diaphragme, gammes
│   ├── regulation.py        feed-forward + correction PI, PI vanne PdC
│   ├── etat_vannes.py       décodage des fins de course des vannes
│   ├── gestion_modes.py     modes normal_high / normal_low / pdc_control
│   ├── commandes_vannes.py  ordres TOR pour rejoindre un mode
│   ├── table_gains_pdc.py   interpolation de la table PdC (% ↔ Re)
│   ├── sequence.py          chargement des profils CSV (mode séquence)
│   ├── pilote.py            boucle temps réel, feed-forward adaptatif
│   ├── apprentissage.py     séquences de caractérisation (thread dédié)
│   ├── gestionnaire_apprentissages.py  apprentissages/*.json
│   ├── journal.py           journal CSV des essais
│   ├── logger_regulation.py log CSV de diagnostic de la boucle
│   └── fichiers.py          écriture JSON atomique
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
| **T** | Température | °C | Mesurée par PT100 (module 750-450), utilisée pour ρ et μ |
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
| **Diaphragme ISO** | Diaphragme normalisé ISO 5167-1 (NFX 10-102), d=41,018 mm, pour Re > 5000 (vanne 3V à 0° par défaut) |
| **Diaphragme Miller** | Diaphragme corrélation Miller, d=21,113 mm, pour Re < 5000 (vanne 3V à 90° par défaut) |
| **Vanne 3 voies** | Vanne TOR (carte GF3) : 0° → diaphragme ISO ; 90° → diaphragme Miller |
| **Vanne bypass** | Vanne TOR : ouverte pendant manœuvre 3 voies pour éviter surpression |
| **Re conduite** | Reynolds calculé dans la conduite Ø76,6 mm (critère de sélection diaphragme) |
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
| **750-497** | 8 entrées analogiques 0-10 V | 0…32767 (0x7FFF) pour 10 V |
| **750-450** | 4 entrées RTD (PT100 3 fils) | Valeur signée en 0,1 °C (ex: 235 = 23,5 °C) |
| **750-597** | 8 sorties analogiques 0-10 V | 0…32767 pour 10 V |
| **750-515** | 4 relais NO libres de potentiel | coils 0…3 (vanne 3 voies 0°/90°, bypass, RUN) |
| **750-1405** | 16 entrées TOR 24 V | fins de course, défauts variateur et actionneurs |
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
| **Chien de garde vannes** | Par vanne : 1,5 × temps de course (`actionneurs`) ; défaut vanne + repli sûr si la fin de course n'arrive pas |
| **Trim saturé** | Alarme si abs(intégrale) > trim_max-1e-6 pendant >5s → table de gains obsolète |
| **Consigne limitée** | ΔP attendu > plage capteur → Re plafonné au maximum mesurable |
| **Défaut capteur** | P_abs hors 800-1200 mbar → arrêt régulation avec message explicite |
| **Saturation air** | P ≤ Pv × 1,01 → ValueError (risque condensation, calculs invalides) |

## Documentation complémentaire

- **CLAUDE.md** : Contexte projet pour Claude Code (règles architecture, threading)
- **apprentissages/*.json** : tables de gains apprises (Re mapping ISO/Miller, PdC), écrites par « Appliquer »
- **etat_banc.json** : état de session (gain Re/V appris en fonctionnement), écrit à l'arrêt
