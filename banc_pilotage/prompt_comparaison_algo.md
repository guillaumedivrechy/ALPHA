# Prompt pour Claude Code — Comparer l'algorithme de pilotage existant à une stratégie de référence

## Rôle et objectif

Tu es ingénieur contrôle-commande. Un **algorithme de pilotage existe déjà** pour une boucle aéraulique fermée servant à des essais PIV. Ta mission est de le **comparer objectivement** à la stratégie de référence décrite plus bas, puis de produire un rapport d'analyse avec des recommandations.

Tu ne présupposes pas que la stratégie de référence est meilleure. L'algorithme existant a peut-être été éprouvé sur l'installation, et certains de ses choix peuvent être justifiés par des contraintes que la référence ignore. Tu signales les points forts des deux approches.

### Règles de travail

- **Ne modifie pas le code existant.** Si tu dois l'instrumenter ou le porter, travaille sur une copie dans un dossier séparé (`comparaison/`).
- **Cite systématiquement tes sources** dans le code : fichier et ligne, ou nom de fonction ou de bloc.
- **Distingue les faits des déductions** : « le code fait X (fichier:ligne) » n'est pas la même chose que « je déduis que X, car… ».
- Si un comportement est ambigu ou dépend du matériel, **pose-moi les questions groupées** plutôt que de deviner.
- Si le fichier `prompt_pilotage_veine.md` est présent, utilise-le comme description détaillée de la stratégie de référence. Le résumé ci-dessous suffit sinon.

---

## 1. Installation pilotée (rappel)

Boucle fermée : chambre d'ensemencement → veine d'essai (PIV) → PT100 + P_amont → **deux diaphragmes en parallèle** (Miller pour Re < 5000, ISO 5167 pour Re > 5000), sélectionnés par une **vanne TOR 3 voies** → aspiration du **ventilateur** (variateur U/f, 0-10 V) → refoulement → **vanne proportionnelle 0-10 V** (perte de charge) → retour à la chambre.
Un **by-pass TOR** relie le refoulement et l'aspiration du ventilateur, en aval des diaphragmes.

Capteurs FCO432 :
- **P_amont** et **ΔP_Miller** : couples 50 Pa + 500 Pa chaînés en maître-esclave, avec un seul signal 0-10 V linéaire sur 0-500 Pa ;
- **ΔP_ISO** : un seul capteur 500 Pa, en 0-10 V sur 0-500 Pa.
- Saturation à 500 Pa, alarme recommandée à 450 Pa.

Actionneurs disponibles : fréquence du variateur, vanne 3 voies, by-pass, vanne proportionnelle.

---

## 2. Stratégie de référence (résumé)

1. **Séparation discret / continu**
   - La vanne 3 voies et le by-pass sont pilotés par une machine d'états qui décide à partir de la **consigne** de Reynolds, jamais à partir de la mesure.
   - Le variateur fonctionne par **paliers fixes** et ne régule pas.
   - La **vanne proportionnelle** est le **seul actionneur régulé**, par un PID sur le Reynolds filtré.
2. **Régimes**
   - BAS (Re < 5000) : branche Miller, by-pass ouvert, variateur sur un palier fixe stable.
   - HAUT (Re > 5000) : branche ISO, by-pass fermé, variateur par paliers.
3. **Hystérésis de régime sur la consigne** : passage en HAUT si la consigne dépasse 5500, en BAS si elle passe sous 4500.
4. **Changement de palier** seulement quand la vanne est en butée de son autorité : ouverture > 85 % ou < 15 % pendant plus de t_palier, avec un délai minimal entre deux changements.
5. **Transitions**
   - Le PID est gelé, la vanne est prépositionnée à partir d'une table de cartographie, puis on attend la stabilisation.
   - La reprise du PID se fait sans à-coup.
   - Dans le sens BAS → HAUT, la vanne proportionnelle est **fermée partiellement avant** la fermeture du by-pass, pour éviter un pic de débit et la saturation.
6. **Réglage du PID** : mesure filtrée, bande morte de ±0,5 à 1 %, limitation de la vitesse de variation de la commande, anti-windup.
7. **Mesure**
   - Débit calculé selon ISO 5167 (C par Reader-Harris/Gallagher, ε) ou selon la loi C(Re) du Miller, par itération sur C(Re).
   - Masse volumique ρ calculée à partir de P_amont et T, viscosité μ par la loi de Sutherland.
   - Correction de zéro sur la branche inactive.
8. **Sécurités**
   - Priorité à l'anti-saturation (> 450 Pa → ouverture du by-pass ou réduction de la vanne).
   - Détection de défaut capteur, alarme si le diaphragme sort de son domaine, rampe d'arrêt.

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

1. Choix de la branche de diaphragme : critère, hystérésis.
2. Gestion du by-pass : critère, hystérésis, séquencement.
3. Usage du variateur : régulation continue ou paliers, logique de changement.
4. Usage de la vanne proportionnelle : régulée ou fixe, zone d'autorité.
5. Nombre de boucles de régulation et risque de **conflit entre régulateurs**.
6. Réglage du PID : filtrage, bande morte, anti-windup, limitation de vitesse, reprise sans à-coup.
7. Transitions : gel du PID, prépositionnement, attente de stabilisation, ordre des manœuvres (vanne avant by-pass).
8. Protection contre la saturation des capteurs.
9. Calcul du débit : conformité à ISO 5167, loi Miller, itération sur C(Re), correction de ρ, μ et ε, correction de zéro.
10. Précision au bas de l'échelle : ΔP_ISO près de Re = 5000, filtrage du bruit.
11. Gestion des défauts et états sûrs.
12. Journalisation et repérage du « régime établi » pour l'acquisition PIV.
13. Maintenabilité : lisibilité, paramètres centralisés, testabilité.

### Étape 4 — Comparaison dynamique en simulation

1. Construis un **simulateur de boucle commun**, dans la copie `comparaison/` :
   - pertes quadratiques, avec la vanne et le by-pass en parallèle ;
   - courbe de ventilateur selon f, avec les lois de similitude ;
   - réponse du premier ordre sur le débit ;
   - bruit sur les capteurs et saturation à 500 Pa.
2. Fais tourner **les deux algorithmes** sur ce même simulateur.
   - Si l'existant ne peut pas être exécuté tel quel (code automate, LabVIEW…), **porte fidèlement sa logique** en Python. Liste chaque hypothèse de portage et chaque point où la fidélité est incertaine.
3. Scénarios à jouer, avec les mêmes conditions initiales et le même bruit (graine aléatoire fixe) pour les deux algorithmes :
   - échelons de consigne traversant 5000 dans les deux sens (par exemple 3000 → 8000 → 3000) ;
   - consigne fixée dans la bande 4500-5500 ;
   - rampe lente de consigne autour de 5000 ;
   - consigne proche d'un seuil de changement de palier ;
   - dérive lente de température (+5 °C en 30 min) ;
   - bruit renforcé sur ΔP à très bas Reynolds ;
   - défaut capteur (signal figé, puis hors plage).
4. Indicateurs à calculer pour chaque scénario et chaque algorithme :
   - nombre de commutations TOR (vanne 3 voies, by-pass) et de changements de palier ;
   - ΔP maximal et temps passé au-dessus de 450 Pa et de 500 Pa ;
   - temps d'établissement, dépassement, erreur statique ;
   - écart-type du Reynolds en régime établi ;
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
- [ ] Dimensions des diaphragmes (d, D, type de prises de pression) et loi C(Re) du Miller.
- [ ] Données d'essais réels (journaux) éventuellement disponibles pour caler le simulateur.
- [ ] Fréquence minimale stable du ventilateur et f_max.
