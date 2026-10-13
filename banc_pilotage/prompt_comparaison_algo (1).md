# Prompt pour Claude Code — Comparer l'algorithme de pilotage existant à une stratégie de référence

## Rôle et objectif

Tu es ingénieur contrôle-commande. Un **algorithme de pilotage existe déjà** pour une boucle aéraulique fermée servant à des essais PIV. Ta mission est de le **comparer objectivement** à la stratégie de référence décrite plus bas, puis de produire un rapport d'analyse avec des recommandations.

Tu ne présupposes pas que la stratégie de référence est meilleure. L'algorithme existant a peut-être été éprouvé sur l'installation, et certains de ses choix peuvent être justifiés par des contraintes que la référence ignore. Tu signales les points forts des deux approches.

### Règles de travail

- **Ne modifie pas le code existant.** Si tu dois l'instrumenter ou le porter, travaille sur une copie dans un dossier séparé (`comparaison/`).
- **Cite systématiquement tes sources** dans le code : fichier et ligne, ou nom de fonction ou de bloc.
- **Distingue les faits des déductions** : « le code fait X (fichier:ligne) » n'est pas la même chose que « je déduis que X, car… ».
- Si un comportement est ambigu ou dépend du matériel, **pose-moi les questions groupées** plutôt que de deviner.
- Si le fichier `prompt_pilotage_veine.md` est présent, c'est la description détaillée de la stratégie de référence. **En cas de divergence avec le résumé ci-dessous, c'est ce fichier qui fait foi.**

---

## 1. Installation pilotée (rappel)

Boucle fermée : chambre d'ensemencement → veine d'essai (PIV) → PT100 + P_amont → **deux diaphragmes en parallèle** (Miller pour Re < 5000, ISO 5167 pour Re > 5000), sélectionnés par une **vanne TOR 3 voies** → aspiration du **ventilateur** (variateur U/f, 0-10 V) → refoulement → **vanne proportionnelle 0-10 V** (perte de charge) → retour à la chambre.
Un **by-pass TOR** relie le refoulement et l'aspiration du ventilateur, en aval des diaphragmes.

Capteurs FCO432 :
- **P_amont** et **ΔP_Miller** : couples 50 Pa + 500 Pa chaînés en maître-esclave, avec un seul signal 0-10 V linéaire sur 0-500 Pa ;
- **ΔP_ISO** : un seul capteur 500 Pa, en 0-10 V sur 0-500 Pa.
- Saturation à 500 Pa, alarme recommandée à 450 Pa.
- La veine est a priori en dépression : le signe de P_amont dépend du montage du capteur.

Actionneurs disponibles : fréquence du variateur, vanne 3 voies, by-pass, vanne proportionnelle.

---

## 2. Stratégie de référence (résumé)

1. **Séparation discret / continu**
   - Les décisions discrètes (régime, palier) sont prises à partir de la **consigne** de Reynolds, jamais à partir de la mesure instantanée. Chaque décision a son hystérésis.
   - Le variateur fonctionne par **paliers fixes** et ne régule pas.
   - La **vanne proportionnelle** est le **seul actionneur régulé**, par un PI sur le Reynolds de référence filtré.
2. **Régimes**
   - BAS (Re < 5000) : branche Miller, by-pass ouvert, paliers BAS (souvent un seul, au-dessus de la fréquence minimale stable).
   - HAUT (Re > 5000) : branche ISO, by-pass fermé, paliers HAUT.
   - Contraintes de recouvrement : BAS atteint Re = 5500 sans saturer ΔP_Miller ; HAUT atteint Re = 4500 avec un ΔP_ISO lisible et dans le domaine ISO.
3. **Hystérésis de régime sur la consigne** : passage en HAUT si la consigne dépasse 5500, en BAS si elle passe sous 4500. Au démarrage dans la bande, BAS sous 5000, HAUT sinon.
4. **Choix du palier**
   - Règle principale, par anticipation : à partir de la consigne et de la table de cartographie, on choisit le palier qui place la vanne près de 50 %. On le garde tant que la position prévue reste entre 25 et 75 %.
   - Règle de secours : vanne > 85 % avec Re < consigne, ou < 15 % avec Re > consigne, pendant t_palier. Délai minimal entre deux changements, changement inverse interdit pendant ce délai.
   - Les plages couvertes par deux paliers consécutifs doivent se chevaucher.
5. **Séquence générique de manœuvre**
   - Gel du PI, puis vanne amenée à **min(position actuelle, position cible)** : aucune manœuvre ne crée de surdébit transitoire.
   - Manœuvres avec rampe sur le variateur, attente de stabilisation (avec délai maximal), vanne à sa cible, reprise du PI sans à-coup.
   - Une manœuvre en cours n'est jamais interrompue, sauf par une sécurité. Une consigne reçue pendant une manœuvre est traitée à la fin.
   - Mesures de ΔP ignorées pendant le basculement de la vanne 3 voies.
6. **Ordre des manœuvres entre régimes**
   - Principe : le diaphragme Miller (a priori le plus restrictif) n'est raccordé qu'à faible débit ; les grosses variations de débit se font sur la branche ISO.
   - HAUT → BAS : sur ISO, ouverture du by-pass et passage au palier BAS, stabilisation, **puis** bascule sur Miller.
   - BAS → HAUT : bascule sur ISO **d'abord**, puis fermeture du by-pass et passage au palier HAUT.
7. **Régulateur**
   - PI sans dérivée sur mesure bruitée, gains par régime et par palier ou linéarisation par la courbe de vanne.
   - Bande morte de ±0,5 à 1 % avec intégrale figée, limitation de vitesse, anti-windup.
8. **Mesure**
   - Deux Reynolds distincts : **Re_D** (conduite du diaphragme, pour le coefficient C et le domaine de validité) et **Re_ref** (veine, pour la consigne et la régulation).
   - ISO 5167 : C par Reader-Harris/Gallagher, coefficient de détente ε, itération sur C(Re_D), contrôle du domaine de validité (notamment D ≥ 50 mm et 0,1 ≤ β ≤ 0,75). Miller : loi C(Re_D) fournie.
   - ρ à partir de P_atm, de P_amont (avec son signe) et de T ; μ par la loi de Sutherland.
   - Débit nul sous un seuil de bruit de ΔP ; moyenne sur un multiple de 20 ms pour rejeter le 50 Hz.
   - Zéros relevés au démarrage, puis mis à jour lentement sur la branche inactive.
9. **Sécurités** (prioritaires)
   - Anti-saturation : au-delà de 450 Pa, réduction de la vanne puis du variateur, retour à la normale sous 400 Pa ; au-delà de 490 Pa, ouverture du by-pass et variateur au minimum.
   - Défaut capteur, diaphragme hors domaine, séquences de démarrage et d'arrêt, watchdog matériel.
10. **Exploitation**
    - Drapeau « régime établi » (pas de manœuvre ni d'alarme, Re dans la tolérance et stable) pour déclencher la PIV.
    - Configuration unique validée au chargement, mode manuel de mise en service, mode cartographie produisant la table de prépositionnement.

---

## 3. Travail demandé

### Étape 1 — Inventaire de l'existant

- Localise le code de pilotage : langage, plateforme, fichiers, point d'entrée, période de la boucle de contrôle.
- Liste les entrées / sorties utilisées et leur mise à l'échelle.
- Liste tous les **paramètres** (seuils, temporisations, gains, paliers) avec leurs valeurs et leur emplacement.

### Étape 2 — Reconstruction de l'algorithme existant

- Décris l'algorithme existant sous forme de **machine d'états** (ou de pseudo-code s'il n'en a pas), avec pour chaque bascule sa condition de déclenchement. Précise si elle s'appuie sur une **consigne** ou sur une **mesure**.
- Fournis un diagramme Mermaid de cette machine d'états.
- Décris la chaîne de mesure : conversion des signaux, filtrage, calcul de ρ, μ, débit et Reynolds, coefficients des diaphragmes.

### Étape 3 — Grille de comparaison

Pour chaque thème ci-dessous, remplis le tableau suivant :

| Thème | Existant (avec réf. code) | Référence | Écart | Impact (précision / bagotage / saturation / sécurité) | Gravité (haute / moyenne / basse) |
|---|---|---|---|---|---|

Thèmes à couvrir :

1. Choix de la branche de diaphragme : critère (consigne ou mesure), hystérésis.
2. Gestion du by-pass : critère, hystérésis, séquencement.
3. Usage du variateur : régulation continue ou paliers ; logique de choix du palier (anticipation ou réaction) ; recouvrement des paliers.
4. Usage de la vanne proportionnelle : régulée ou fixe, zone d'autorité.
5. Nombre de boucles de régulation et risque de **conflit entre régulateurs**.
6. Réglage du régulateur : structure (PI, PID), filtrage, bande morte, anti-windup, limitation de vitesse, reprise sans à-coup, prise en compte de la non-linéarité.
7. Transitions :
   - gel du régulateur et règle min(actuelle, cible) ;
   - ordre des manœuvres (Miller raccordé à faible débit) ;
   - mesures ignorées pendant le basculement de la vanne 3 voies ;
   - attente de stabilisation ;
   - consigne reçue pendant une manœuvre.
8. Protection contre la saturation des capteurs : seuils, hystérésis, action d'urgence.
9. Calcul du débit :
   - distinction entre Re_D et Re_ref ;
   - conformité à ISO 5167 (C, ε, itération, domaine de validité) ;
   - loi Miller ;
   - calcul de ρ et μ, signe de P_amont ;
   - correction de zéro et traitement d'un ΔP quasi nul.
10. Précision au bas de l'échelle : ΔP_ISO près de Re = 4500, filtrage du bruit, rejet du 50 Hz.
11. Gestion des défauts et états sûrs : démarrage, arrêt, watchdog.
12. Journalisation et repérage du « régime établi » pour l'acquisition PIV.
13. Maintenabilité : paramètres centralisés, validation de la configuration, mode manuel, testabilité.

### Étape 4 — Comparaison dynamique en simulation

1. Construis un **simulateur de boucle commun**, dans la copie `comparaison/` :
   - courbe de ventilateur avec les lois de similitude ;
   - pertes quadratiques, avec la vanne et le by-pass en parallèle ;
   - résistance différente selon la branche, et temps de manœuvre de la vanne 3 voies ;
   - réponse du premier ordre sur le débit ;
   - bruit sur les capteurs et saturation à 500 Pa.
2. Fais tourner **les deux algorithmes** sur ce même simulateur.
   - Si l'existant ne peut pas être exécuté tel quel (code automate, LabVIEW…), **porte fidèlement sa logique** en Python. Liste chaque hypothèse de portage et chaque point où la fidélité est incertaine.
3. Scénarios à jouer, avec les mêmes conditions initiales et le même bruit (graine aléatoire fixe) pour les deux algorithmes :
   - échelons de consigne traversant 5000 dans les deux sens (par exemple 3000 → 8000 → 3000) ;
   - consigne fixée dans la bande 4500-5500 ;
   - rampe lente de consigne autour de 5000 ;
   - consigne proche d'un seuil de changement de palier ;
   - nouvelle consigne reçue pendant une manœuvre ;
   - dérive lente de température (+5 °C en 30 min) ;
   - bruit renforcé sur ΔP à très bas Reynolds ;
   - défaut capteur (signal figé, puis hors plage).
4. Indicateurs à calculer pour chaque scénario et chaque algorithme :
   - nombre de commutations TOR (vanne 3 voies, by-pass) et de changements de palier ;
   - ΔP maximal et temps passé au-dessus de 450 Pa et de 500 Pa ;
   - temps d'établissement, dépassement, erreur statique ;
   - écart-type du Reynolds en régime établi ;
   - **écart entre le débit calculé par l'algorithme et le débit réel du simulateur** ;
   - débattement cumulé de la vanne proportionnelle (indicateur d'usure) ;
   - temps passé hors du domaine de validité du diaphragme actif.
5. Produis les graphes Re, commandes et états, en superposant les deux algorithmes pour chaque scénario.

### Étape 5 — Rapport

Produis `comparaison/rapport_comparaison.md` contenant :

1. **Synthèse** en une demi-page : verdict global et 3 à 5 constats majeurs.
2. Description de l'algorithme existant, avec le diagramme d'états.
3. La grille de comparaison complète.
4. Les résultats de simulation : tableau des indicateurs et graphes.
5. **Points forts de l'existant** à conserver.
6. **Recommandations classées par priorité**, avec pour chacune le problème, la modification proposée, le gain attendu et l'effort estimé.
7. **Plan de migration incrémental** : quels changements apporter dans quel ordre, en ne changeant qu'une chose à la fois et en validant chaque étape.
8. Limites de l'analyse et questions ouvertes.

---

## Informations utiles (me les demander si absentes)

- [ ] Emplacement du code existant et langage / plateforme.
- [ ] Documentation ou commentaires décrivant l'intention de l'existant.
- [ ] Problèmes connus constatés sur l'installation : bagotage, saturation, instabilités, temps de réglage.
- [ ] Dimensions des diaphragmes (d, D, type de prises de pression), loi C(Re) du Miller, et lequel est le plus restrictif.
- [ ] Définition du Reynolds de référence utilisée par l'existant.
- [ ] Signe de P_amont (veine en dépression ? capteur monté prises inversées ?).
- [ ] Données d'essais réels (journaux) éventuellement disponibles pour caler le simulateur.
- [ ] Fréquence minimale stable du ventilateur et f_max.
