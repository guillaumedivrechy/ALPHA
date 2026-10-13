# Prompt pour Claude Code — Pilotage d'une veine d'essai aéraulique (PIV)

## Rôle et objectif

Tu vas développer le logiciel de pilotage d'une boucle aéraulique fermée servant à des essais PIV.
L'objectif est d'atteindre et de maintenir une **consigne de nombre de Reynolds** (de part et d'autre de Re = 5000) avec :
- une bonne précision de mesure du débit ;
- le moins de bagotage possible (pas de commutations TOR répétées, pas d'oscillations du régulateur) ;
- aucune saturation des capteurs de pression.

### Méthode de travail

1. Lis la section « Informations à compléter ». S'il manque une information indispensable, pose-moi les questions groupées en une seule fois. Pour les paramètres non critiques, prends une valeur par défaut raisonnable, mets-la dans le fichier de configuration et signale-la.
2. **Avant de coder**, présente un plan court (architecture, liste des fichiers, hypothèses retenues) et attends ma validation.
3. Développe dans cet ordre, en testant chaque étape avant de passer à la suivante :
   - chaîne de mesure ;
   - simulateur ;
   - régulation et machine d'états ;
   - sécurités ;
   - interface.

---

## 1. Description de l'installation

Parcours de l'air, en boucle fermée :

1. **Chambre d'ensemencement**, où les particules PIV sont ajoutées.
2. **Veine d'essai**, équipée d'un générateur de vortex et d'une mesure PIV. Le logiciel ne pilote pas la PIV.
3. **Sortie de veine** :
   - une sonde **PT100** mesure la température T ;
   - un capteur **P_amont** mesure la pression statique relative.
4. **Mesure de débit** sur deux branches en parallèle :
   - le **diaphragme Miller**, utilisé pour Re < 5000, avec un capteur ΔP_Miller ;
   - le **diaphragme ISO** (ISO 5167-2), utilisé pour Re > 5000, avec un capteur ΔP_ISO.
   - Une **vanne TOR 3 voies** sélectionne la branche active. La branche inactive n'a pas de débit, donc son ΔP vaut environ 0 si la vanne est étanche.
5. **Ventilateur**, en aspiration, piloté par un **variateur U/f** avec une consigne 0-10 V.
6. **By-pass TOR** entre le refoulement et l'aspiration du ventilateur. Son piquage est **en aval** des diaphragmes, donc il ne fausse pas la mesure de débit.
7. **Vanne proportionnelle 0-10 V** (perte de charge réglable) sur le retour vers la chambre. Elle est en série avec le circuit d'essai.

### Capteurs de pression (FCO432)

- **P_amont** et **ΔP_Miller** : chacun est un couple de capteurs 50 Pa + 500 Pa **chaînés en maître-esclave**. Le changement de gamme est interne au capteur. Le logiciel lit **un seul signal 0-10 V, linéaire sur 0-500 Pa** (20 mV/Pa) et n'a **pas** à gérer le changement de gamme.
- **ΔP_ISO** : un seul capteur 500 Pa, en 0-10 V linéaire sur 0-500 Pa. Sa précision est celle d'un capteur 500 Pa, y compris pour de faibles ΔP.
- **Limite de saturation** : 500 Pa, soit 10 V. **Seuil d'alarme logiciel : 450 Pa**, configurable.
- **Signe de P_amont** : la veine est a priori en dépression. Le capteur 0-500 Pa est peut-être monté prises inversées pour lire la dépression en positif. Ce signe est un paramètre de configuration (`p_amont_signe`), à confirmer.

### Entrées / sorties

| Signal | Sens | Type |
|---|---|---|
| P_amont | Entrée | Analogique 0-10 V → 0-500 Pa |
| ΔP_Miller | Entrée | Analogique 0-10 V → 0-500 Pa |
| ΔP_ISO | Entrée | Analogique 0-10 V → 0-500 Pa |
| T (PT100) | Entrée | À préciser (transmetteur 4-20 mA / 0-10 V, ou module PT100) |
| Consigne variateur | Sortie | Analogique 0-10 V → 0-f_max Hz |
| Vanne proportionnelle | Sortie | Analogique 0-10 V → 0-100 % d'ouverture (sens à confirmer) |
| Vanne 3 voies | Sortie | TOR (Miller / ISO) |
| By-pass | Sortie | TOR (ouvert / fermé) |

---

## 2. Chaîne de mesure

### 2.1 Acquisition

- **Période de la boucle de contrôle** : configurable, 100 ms par défaut.
- **Suréchantillonnage** des entrées analogiques, avec une moyenne à chaque cycle. Une fenêtre de moyenne égale à un multiple de 20 ms rejette le parasite secteur à 50 Hz.
- **Conversion** : P [Pa] = 50 × U [V], puis correction de zéro.
- **Température** en kelvin : T = T_°C + 273,15.

### 2.2 Propriétés de l'air

- **Pression absolue** : P_abs = P_atm + p_amont_signe × P_amont. P_atm est une valeur configurable, ou une mesure si elle est disponible.
- **Masse volumique** : ρ = P_abs / (R_air × T), avec R_air = 287,05 J/(kg·K). La correction d'humidité est optionnelle (effet inférieur à 1 %).
- **Viscosité**, par la loi de Sutherland : μ = μ₀ × (T/T₀)^1,5 × (T₀ + S)/(T + S), avec μ₀ = 1,716e-5 Pa·s, T₀ = 273,15 K, S = 110,4 K.

### 2.3 Deux nombres de Reynolds à ne pas confondre

- **Re_D**, Reynolds de la conduite du diaphragme actif : Re_D = 4 q_m / (π μ D).
  - Il sert au calcul du coefficient C et au contrôle du domaine de validité du diaphragme.
- **Re_ref**, Reynolds de référence : Re_ref = q_m × L_ref / (μ × A_ref).
  - Il sert pour la consigne, la régulation et l'affichage.
  - L_ref et A_ref sont définis dans la configuration (a priori la veine d'essai, à confirmer).
- Les seuils de régime (4500 / 5500) sont exprimés en Re_ref. Les domaines de validité des diaphragmes sont exprimés en Re_D.

### 2.4 Débit massique

q_m = C × ε × (π/4) × d² × √(2 ρ ΔP) / √(1 − β⁴), avec β = d/D.

**Résolution itérative** (méthode ISO 5167-1) :
1. Calculer l'invariant A₁ = ε × d² × √(2 ρ ΔP) / (μ × D × √(1 − β⁴)).
2. Partir de C = 0,6 et poser Re_D = C × A₁.
3. Recalculer C(Re_D), puis Re_D, et répéter jusqu'à une variation relative de C inférieure à 1e-6.
4. Limiter à 50 itérations ; lever une alarme en cas de non-convergence.
5. Si ΔP est sous un seuil de bruit configurable (par exemple 0,3 Pa), considérer le débit comme nul. On évite ainsi la racine d'un nombre négatif et une itération sans objet.

**Diaphragme ISO** (ISO 5167-2:2003 ; vérifie ces formules dans la norme) :

- **Coefficient C** par l'équation de Reader-Harris/Gallagher :
  C = 0,5961 + 0,0261β² − 0,216β⁸ + 0,000521(10⁶β/Re_D)^0,7 + (0,0188 + 0,0063A)β^3,5(10⁶/Re_D)^0,3 + (0,043 + 0,080e^(−10L₁) − 0,123e^(−7L₁))(1 − 0,11A)β⁴/(1 − β⁴) − 0,031(M′₂ − 0,8M′₂^1,1)β^1,3
  - A = (19000β/Re_D)^0,8 et M′₂ = 2L′₂/(1 − β).
  - Si D < 71,12 mm, ajouter le terme 0,011(0,75 − β)(2,8 − D/25,4), avec D en mm.
  - Valeurs selon les prises de pression :
    - prises dans les angles : L₁ = L′₂ = 0 ;
    - prises à D et D/2 : L₁ = 1, L′₂ = 0,47 ;
    - prises à la bride : L₁ = L′₂ = 25,4/D, avec D en mm.
- **Coefficient de détente** : ε = 1 − (0,351 + 0,256β⁴ + 0,93β⁸) × [1 − (p₂/p₁)^(1/κ)].
  - κ = 1,4, p₁ ≈ P_abs et p₂ = p₁ − ΔP.
- **Domaine de validité**, à contrôler au chargement de la configuration et en fonctionnement :
  - d ≥ 12,5 mm ;
  - 50 mm ≤ D ≤ 1000 mm ;
  - 0,1 ≤ β ≤ 0,75 ;
  - Re_D ≥ 5000, et en plus Re_D ≥ 16000β² pour les prises dans les angles ou à D et D/2, ou Re_D ≥ 170β²D pour les prises à la bride (D en mm).
  - Hors domaine : lever une alarme et signaler que C n'est plus garanti par la norme.

**Diaphragme Miller** : la loi C(Re_D) et son domaine de validité sont fournis dans la configuration, sous forme de formule ou de table interpolée. ε est calculé comme pour l'ISO, sauf indication contraire.

### 2.5 Filtrage

- **Filtre court** pour la régulation : constante de temps d'environ 1 s, configurable.
- **Filtre long** pour l'affichage et le critère de stabilité : fenêtre de quelques secondes.
- Les valeurs brutes sont toujours enregistrées.

### 2.6 Correction de zéro

- **Au démarrage**, ventilateur arrêté : moyenner chaque voie pendant N secondes pour obtenir les offsets. Lever une alarme si un offset dépasse une limite configurable (par exemple 2 Pa).
- **En fonctionnement** : mettre à jour lentement l'offset de la branche inactive.
  - Seulement après stabilisation.
  - Avec une variation maximale par mise à jour.

---

## 3. Stratégie de pilotage

### 3.1 Principe

- Les **décisions discrètes** (régime, palier) sont prises à partir de la **consigne**, jamais à partir de la mesure instantanée. Chaque décision a son hystérésis.
- Le **variateur** fonctionne par **paliers fixes** et ne régule pas.
- La **vanne proportionnelle** est le **seul actionneur régulé**, par un régulateur PI sur Re_ref filtré.

### 3.2 Régimes

| | Régime BAS | Régime HAUT |
|---|---|---|
| Vanne 3 voies | Miller | ISO |
| By-pass | Ouvert | Fermé |
| Variateur | Paliers BAS (liste configurable, souvent un seul palier au-dessus de la fréquence minimale stable) | Paliers HAUT (liste configurable, par exemple [20, 30, 40, 50] Hz) |
| Mesure du débit | ΔP_Miller | ΔP_ISO |
| Régulation | PI sur la vanne proportionnelle | PI sur la vanne proportionnelle |

**Contraintes de recouvrement**, à vérifier par la cartographie (section 6) :
- le régime BAS doit atteindre Re_ref = 5500 (plus une marge) avec ΔP_Miller < 450 Pa ;
- le régime HAUT doit atteindre Re_ref = 4500 (moins une marge) avec :
  - un ΔP_ISO lisible, par exemple ≥ 50 Pa, soit 10 % de la pleine échelle (seuil configurable) ;
  - un Re_D dans le domaine de validité ISO.

Si une contrainte n'est pas tenue, signale-le et propose de déplacer la bande d'hystérésis.

### 3.3 Choix du régime

- Passage en HAUT si la consigne dépasse 5500.
- Passage en BAS si la consigne passe sous 4500.
- Entre les deux, on reste dans le régime courant. Ces seuils sont configurables.
- Au démarrage, si la consigne est dans la bande : BAS si elle est inférieure à 5000, HAUT sinon.

### 3.4 Choix du palier

- **Par anticipation (règle principale)** :
  - à chaque nouvelle consigne, choisir dans la table de cartographie le palier dont la position de vanne prévue est la plus proche de 50 % ;
  - garder le palier courant tant que sa position prévue reste entre 25 et 75 % (hystérésis).
- **Par secours** (dérive de température, table absente ou imprécise) :
  - si l'ouverture reste > 85 % avec Re_ref < consigne pendant t_palier, monter d'un palier ;
  - si l'ouverture reste < 15 % avec Re_ref > consigne pendant t_palier, descendre d'un palier ;
  - respecter un délai minimal entre deux changements, et interdire le changement inverse pendant ce délai.
- **Recouvrement des paliers** : les plages de Re_ref couvertes par deux paliers consécutifs, avec la vanne entre 20 et 80 %, doivent se chevaucher. Sinon, deux paliers voisins peuvent se renvoyer la main en boucle. À vérifier par la cartographie.

### 3.5 Régulateur

- **Sens d'action** : une ouverture de vanne fait augmenter le débit. Ce sens est configurable.
- **PI** : pas d'action dérivée sur une mesure bruitée. Une dérivée optionnelle sur la mesure filtrée est possible.
- **Gain non linéaire** : le procédé combine la loi de la vanne et des pertes quadratiques. Utiliser des gains par régime et par palier, ou linéariser la commande par la courbe débit / ouverture issue de la cartographie.
- **Bande morte** de ±0,5 à 1 % de la consigne, avec l'intégrale figée dans la bande morte.
- **Limitation de vitesse** de la commande (en %/s), **anti-windup** et sortie bornée.
- **Reprise sans à-coup** : initialiser l'intégrale à la position courante de la vanne.

### 3.6 Séquence générique d'une manœuvre discrète

Elle s'applique à tout changement de régime, de palier ou de branche :

1. **Geler** le PI (mode manuel) et marquer la mesure comme « non établie ».
2. **Amener la vanne à min(position actuelle, position cible)**.
   - La position cible est lue dans la table de prépositionnement.
   - Sans table, utiliser une valeur par défaut prudente et configurable.
   - Cette règle garantit qu'aucune manœuvre ne crée de surdébit transitoire.
3. **Effectuer les manœuvres** décrites en 3.7, avec une rampe sur la fréquence du variateur.
4. **Attendre la stabilisation** :
   - temporisation minimale, puis écart-type de Re_ref sous un seuil ;
   - au-delà d'un délai maximal, lever une alarme.
5. Amener la vanne à sa **position cible**, puis attendre de nouveau la stabilisation.
6. **Relâcher le PI** sans à-coup.

Règles complémentaires :
- Une manœuvre en cours n'est **jamais interrompue**, sauf par une sécurité. Une consigne reçue pendant une manœuvre est traitée à la fin de celle-ci.
- Pendant le basculement de la vanne 3 voies, les mesures de ΔP sont **invalides** : elles sont ignorées pendant le temps de manœuvre plus une marge.

### 3.7 Ordre des manœuvres entre régimes

**Principe** : la branche Miller est a priori la plus restrictive, donc celle qui donne le ΔP le plus élevé à débit égal (à confirmer). Elle n'est raccordée qu'**à faible débit**. Les variations de débit importantes se font sur la branche ISO, qui ne risque pas la saturation.

**De HAUT vers BAS** (toutes les manœuvres de débit se font sur la branche ISO) :
1. Vanne à min(actuelle, cible).
2. Ouvrir le by-pass, puis rampe du variateur vers le palier BAS.
3. Attendre la stabilisation. Re_ref doit être inférieur à 4500.
4. Basculer la vanne 3 voies sur Miller, en ignorant les mesures pendant la manœuvre.
5. Vanne à sa cible, stabilisation, reprise du PI.

**De BAS vers HAUT** :
1. Vanne à min(actuelle, cible).
2. Basculer la vanne 3 voies sur ISO, en ignorant les mesures pendant la manœuvre.
3. Fermer le by-pass, puis rampe du variateur vers le palier HAUT.
4. Stabilisation, vanne à sa cible, reprise du PI.

Pendant ces séquences, le PI est gelé. Une mesure obtenue hors du domaine de validité du diaphragme n'est pas utilisée pour réguler, mais la surveillance anti-saturation reste active.

### 3.8 Régime établi (pour la PIV)

Le drapeau « régime établi » est vrai si toutes ces conditions sont réunies depuis au moins t_établi :
- aucune manœuvre en cours et aucune alarme ;
- |Re_ref − consigne| inférieur à une tolérance configurable ;
- écart-type de Re_ref sur la fenêtre longue inférieur à un seuil.

Ce drapeau est journalisé et peut servir à déclencher l'acquisition PIV.

---

## 4. Sécurités (prioritaires sur la régulation)

- **Anti-saturation**, sur P_amont et sur le ΔP de la branche active :
  - au-delà de 450 Pa :
    - la commande de vanne est réduite par une rampe rapide, et le PI passe en suivi pour reprendre sans à-coup ;
    - si cela ne suffit pas après X s, réduire la fréquence du variateur (palier inférieur) ;
    - une alarme est levée.
  - Retour au fonctionnement normal sous 400 Pa (hystérésis).
  - Au-delà de 490 Pa, ou si le signal atteint 10 V : **action d'urgence**, on ouvre le by-pass et on passe le variateur au minimum.
- **Défaut capteur** : tension < −0,2 V ou > 10,2 V, signal figé (variance nulle alors que les actionneurs bougent), ou PT100 hors plage. On passe alors en repli sûr et on lève une alarme.
- **Diaphragme hors domaine** : alarme, et le drapeau « régime établi » passe à faux.
- **Séquence de démarrage** :
  1. ventilateur arrêté, relevé des zéros ;
  2. choix du régime ;
  3. prépositionnement : by-pass ouvert, vanne partiellement fermée ;
  4. rampe du variateur.
- **Séquence d'arrêt** : rampe de descente du variateur, by-pass ouvert.
- **Plantage du logiciel** : beaucoup de cartes d'acquisition gardent la dernière valeur en sortie si le programme s'arrête. Utilise le watchdog matériel de la carte s'il existe ; sinon, documente ce risque. L'arrêt d'urgence matériel reste indépendant du logiciel.

---

## 5. Architecture logicielle attendue

- **Couche d'abstraction matérielle** : une interface `IO` avec une implémentation réelle (carte d'acquisition, à préciser) et une implémentation **simulateur**.
- **Simulateur de boucle** pour tester sans matériel :
  - courbe de ventilateur avec les lois de similitude : ΔP_vent(Q, f) = (f/f₀)² × ΔP₀(Q × f₀/f) ;
  - pertes quadratiques, avec la vanne et le by-pass en parallèle ;
  - résistance différente selon la branche, et temps de manœuvre de la vanne 3 voies ;
  - réponse du premier ordre sur le débit ;
  - bruit et saturation des capteurs à 500 Pa ;
  - échauffement de l'air, en option.
  - Le simulateur connaît le débit réel : compare-le au débit calculé par la chaîne de mesure.
- **Modules séparés** : acquisition, propriétés de l'air, calcul des diaphragmes (ISO / Miller), filtrage, régulateur, machine d'états, sécurités, journalisation.
- **Configuration** dans un fichier unique (YAML ou JSON) :
  - contenu : géométries, lois C(Re_D), seuils, hystérésis, temporisations, paliers, gains, table de prépositionnement ;
  - **validation au chargement** : cohérence des seuils, recouvrements, domaines de validité.
- **Journal horodaté** en CSV :
  - mesures brutes et filtrées, Re_D, Re_ref, état, commandes, alarmes, drapeau « régime établi » ;
  - version de la configuration utilisée.
- **Interface minimale** :
  - saisie de la consigne de Reynolds ;
  - affichage de l'état, des mesures et des alarmes ;
  - bouton d'arrêt ;
  - **mode manuel** de chaque actionneur pour la mise en service, avec les sécurités actives.

---

## 6. Mode cartographie

Prévoir un mode automatique qui balaie les combinaisons palier × by-pass × branche × ouverture de vanne. Pour chaque combinaison, il attend la stabilisation puis enregistre Re_ref, Re_D et les ΔP. Il s'arrête sur la sécurité de saturation à 450 Pa.

Il produit :
- la **table de prépositionnement** utilisée par les manœuvres ;
- la **courbe débit / ouverture** de la vanne, souvent non linéaire ;
- un **rapport de vérification** :
  - recouvrement des régimes dans la bande 4500-5500 (section 3.2) ;
  - ΔP_Miller à Re_ref = 5500 ;
  - ΔP_ISO et Re_D à Re_ref = 4500 ;
  - recouvrement des paliers consécutifs.

---

## 7. Tests à fournir

**Tests unitaires** :
- propriétés de l'air ;
- convergence de l'itération C(Re_D), et cas ΔP nul ou négatif ;
- coefficient C de l'ISO comparé à des valeurs de référence calculées indépendamment ;
- PI : bande morte, anti-windup, limitation de vitesse, reprise sans à-coup ;
- toutes les transitions de la machine d'états, y compris l'hystérésis et la règle min(actuelle, cible).

**Tests en simulation** :
- échelons de consigne traversant 5000 dans les deux sens (par exemple 3000 → 8000 → 3000) ;
- consigne fixée dans la bande 4500-5500 ;
- rampe lente de consigne autour de 5000, puis autour d'un seuil de palier ;
- nouvelle consigne reçue pendant une manœuvre ;
- dérive de température ;
- déclenchement de chaque sécurité.

**Critères de réussite** :
- **aucune commutation TOR répétée** ;
- **aucun ΔP au-delà de 450 Pa** hors scénario de sécurité ;
- **écart entre débit calculé et débit réel du simulateur** dans la tolérance attendue.

Produis un rapport de simulation (graphes de Re_ref, des commandes et des états) pour chaque scénario.

---

## Informations à compléter (me les demander si absentes)

- [ ] Langage et plateforme cible : Python + carte d'acquisition ? Automate ? LabVIEW ? Modèle de la carte, résolution du convertisseur, existence d'un watchdog matériel.
- [ ] Période de boucle souhaitée.
- [ ] Diaphragme ISO : d, D, β, type de prises de pression (angle, D et D/2, bride).
- [ ] Diaphragme Miller : d, D, loi C(Re_D) et domaine de validité. Lequel des deux diaphragmes est le plus restrictif ?
- [ ] Définition du Reynolds de référence : section et longueur caractéristique de la veine, ou conduite des diaphragmes.
- [ ] Plage de Reynolds visée (min / max).
- [ ] Signe de P_amont : la veine est-elle en dépression, et le capteur est-il monté prises inversées ? Pression atmosphérique mesurée ou fixée ?
- [ ] Variateur : f_max correspondant à 10 V, fréquence minimale stable du ventilateur, rampes internes.
- [ ] Vanne proportionnelle : temps de course, sens de la commande (10 V = ouverte ou fermée).
- [ ] Vanne 3 voies : temps de manœuvre, et passe-t-elle par une position où les deux voies sont fermées ?
- [ ] Interface de la PT100 et plage de température.
