# Notice d'utilisation — pilotage du banc aéraulique

Cette notice explique comment **utiliser** le banc au quotidien : régler un
nombre de Reynolds, lancer un profil, arrêter, réagir à une alerte.

Les apprentissages et la maintenance ne sont pas traités ici : ils sont
réservés au responsable du banc (voir `MISE_EN_SERVICE.md`). Le banc doit
avoir été caractérisé (apprentissages faits) avant un essai.

---

## 1. Démarrer

1. Lancer le logiciel.
2. Une fenêtre **« Initialisation du banc »** s'ouvre : les vannes se mettent
   en position de repos (diaphragme Miller, by-pass ouvert, vanne de perte
   de charge ouverte). Attendre les trois ✓ (environ 30 s), ou cliquer
   « Ne plus attendre » : les vannes finissent seules leur course.
3. Vérifier en bas à gauche le **voyant de liaison vert** (« Wago 750-362 »
   suivi de son adresse). Rouge = coupleur injoignable : vérifier le câble
   réseau et l'alimentation du coffret.

---

## 2. L'écran en un coup d'œil

| Zone | Ce qu'on y trouve |
|---|---|
| **Colonne de gauche** | les mesures en temps réel : débit Qv, masse volumique ρ, viscosités μ et ν, pression amont, ΔP ISO, ΔP Miller ; l'état des actionneurs (variateur, vannes) |
| **Centre** | les onglets **Graphique** (courbe du Re mesuré et de la consigne), **Voies** (état des capteurs), **Séquence** (profils automatiques), Configuration ; Apprentissage et Maintenance sont réservés |
| **Colonne de droite** | le panneau **PILOTAGE** (Re mesuré, état, mode, consigne, boutons) et le **JOURNAL D'ACTION** (tout ce qui se passe, alertes comprises) |
| **Bandeau du bas** | liaison, fréquence du variateur, journal d'essai en cours, état du banc, durée de la régulation |

**Le grand chiffre** du panneau PILOTAGE est le Re mesuré. Dessous :
« moy. … ± … (…%) · … s » = moyenne, dispersion et durée de stabilité.

**Le badge d'état** à côté du Re :

| Badge | Signification |
|---|---|
| ARRÊT | ventilateur arrêté |
| MANŒUVRE | des vannes se déplacent (changement de diaphragme) : ventilateur tenu à 0, c'est normal |
| RÉGULATION… | le banc cherche la consigne |
| **RÉGULÉ ✓** | consigne atteinte et stable : **la mesure peut commencer** |
| SÉQUENCE 2/5 | profil automatique en cours, palier 2 sur 5 |
| MANUEL | commande à la main du ventilateur |

---

## 3. Choisir la référence : Veine ou Conduite

Le bouton **Référence Re** indique dans quelle référence on **saisit** la
consigne et on **lit** le Re :

- **Veine** (choix habituel) : Reynolds dans la veine d'essai.
- **Conduite** : Reynolds dans la conduite des diaphragmes.

Pour un même débit, Re conduite ≈ 1,87 × Re veine. Changer de référence
convertit automatiquement la consigne saisie.

---

## 4. Réguler sur une consigne (mode Asservi)

C'est l'usage normal.

1. Cliquer sur **Asservi**.
2. Choisir la référence (Veine ou Conduite).
3. Taper le Reynolds voulu dans **Consigne Reynolds**.
4. Cliquer sur **✓ APPLIQUER**.
5. Attendre le badge **RÉGULÉ ✓** (de quelques secondes à environ une
   minute selon l'écart).

Pour changer de consigne : taper la nouvelle valeur, puis **APPLIQUER**.
Inutile d'arrêter avant.

**Le logiciel choisit tout seul** le bon circuit selon le Re demandé :
diaphragme ISO pour les Re élevés, diaphragme Miller pour les Re moyens,
vanne de perte de charge pour les très bas Re. Au passage d'un circuit à
l'autre, les vannes se déplacent (badge MANŒUVRE, environ 15 s, Re affiché
« en cours ») : c'est normal, la régulation reprend ensuite d'elle-même.

**Plage atteignable** : d'environ 100 à environ 14 000 Re veine (selon la
température). Une consigne hors plage est ramenée à la limite, et le journal
affiche « ⚠ Consigne limitée à la plage atteignable ».

---

## 5. Enchaîner des paliers (mode Séquence)

Pour un essai à plusieurs paliers sans intervention.

**Préparer le fichier** (tableur ou bloc-notes, enregistré en CSV avec
point-virgule) : une ligne par palier, **durée en secondes ; Reynolds**.

```
duree_s;re_cible
60;3000
60;6000
120;9000
```

→ 3 000 pendant 60 s, puis 6 000 pendant 60 s, puis 9 000 pendant 120 s,
puis **arrêt automatique** du banc.

Les Reynolds sont lus dans la référence choisie (Veine ou Conduite). Un
exemple est fourni : `exemple_sequence.csv`.

**Lancer** : onglet **Séquence** → **📂 Charger CSV…** (le profil s'affiche
en courbe) → **▶ Démarrer**. Le suivi indique le palier en cours et le temps
écoulé.

- **■ Arrêter** (onglet Séquence) : le profil s'arrête de défiler, mais le
  banc **reste régulé** sur le palier en cours.
- À la fin du dernier palier : « séquence terminée — banc arrêté ».
- En cas de besoin : **ARRÊT VARIATEUR** arrête tout immédiatement.

---

## 6. Commande à la main (mode Manuel)

Pour un essai ponctuel, **sans régulation** : rien ne corrige le Re.

1. Cliquer sur **Manuel** (le ventilateur passe à 0 V).
2. Régler si besoin les vannes :
   - **Vanne Diaphragme** : ISO ou Miller (agit tout de suite) ;
   - **Vanne By-pass** : Ouvert ou Fermée (agit tout de suite) ;
   - **Vanne Perte de charge** : en **% de fermeture** (0 = grande ouverte).
3. Saisir la **tension du variateur** (0 à 10 V).
4. Cliquer sur **✓ APPLIQUER**.

---

## 7. Arrêter

| Bouton | Effet |
|---|---|
| **✕ ARRÊT VARIATEUR** | arrêt **immédiat et total** : ventilateur coupé, régulation et séquence arrêtées. À utiliser au moindre doute. |
| **⏻ QUITTER** | ferme le logiciel proprement : ventilateur coupé d'abord, puis vannes en position de repos |

---

## 8. En cas de problème

**Bandeau rouge « ARRÊT DE SÉCURITÉ »** : le logiciel a coupé le ventilateur
lui-même (capteur, vanne, variateur ou liaison en défaut). Le motif est
écrit dans le bandeau.

1. Lire le motif et corriger la cause.
2. Cliquer sur **Acquitter**.
3. Relancer la consigne (APPLIQUER).

Tant que l'arrêt n'est pas acquitté, aucune relance n'est possible.

**Messages courants du journal d'action** :

| Message | Que faire |
|---|---|
| ⚠ Consigne limitée à la plage atteignable | la consigne dépasse ce que le banc peut faire : elle est ramenée à la limite |
| ⚠ ΔP saturé | Re au-delà de la plage du capteur : baisser la consigne |
| ⚠ Communication | liaison avec le coupleur perdue : vérifier câble et coffret |
| ⚠ Défaut vanne | une vanne n'a pas atteint sa position à temps : vérifier la vanne, prévenir le responsable |
| ⚠ Défaut variateur | le variateur signale un défaut : voir son afficheur |
| ⚠ Table de gains à refaire | la régulation fonctionne mais peine : prévenir le responsable |

Un message suivi de « — rétabli » signifie que le problème a disparu.

---

## 9. Bon à savoir

- **Journal des essais** : pendant chaque régulation, les conditions
  (Re, ΔP, température, pression…) sont enregistrées chaque seconde dans un
  fichier CSV du dossier `journaux`. Son nom s'affiche dans le bandeau du bas.
- **Onglet Voies** : chaque capteur doit être « ● Active ». « Absente » ou
  « Liaison perdue » = problème de câblage ou de liaison.
- **Température de l'air** : par défaut celle de la sonde PT100, en temps
  réel. Le choix (sonde ou valeur saisie) se fait dans l'onglet
  Configuration, par le responsable du banc.
- **Avant de mesurer**, attendre **RÉGULÉ ✓** : c'est la garantie que le Re
  est stable autour de la consigne.

---

## 10. L'onglet Configuration

Il regroupe les paramètres du calcul et du pilotage (enregistrés dans
`config.json`). **Les valeurs sont verrouillées par défaut**, pour éviter
une modification par erreur.

### Modifier une valeur

1. **Arrêter la régulation** (ARRÊT VARIATEUR) : on ne peut pas modifier les
   paramètres pendant qu'elle tourne.
2. Cocher **« Déverrouiller les paramètres »** : les champs deviennent
   modifiables.
3. Changer la ou les valeurs (virgule ou point décimal acceptés).
4. Cliquer sur **💾 Enregistrer**, puis confirmer.
   - Une valeur incohérente est refusée avec un message qui explique
     pourquoi ; rien n'est enregistré.
   - Sinon : « ✓ configuration enregistrée et appliquée ». La modification
     agit **tout de suite**, sans redémarrer le logiciel, et l'onglet se
     reverrouille.
5. **↺ Annuler** remet les valeurs affichées avant modification.

Les valeurs en texte simple (non modifiables), comme la section S et le
diamètre hydraulique Dh, sont calculées automatiquement.

### Ce que l'opérateur peut régler

| Carte | Réglage | Quand y toucher |
|---|---|---|
| **Fluide** | Pression atmosphérique [Pa] | en début de journée d'essais : saisir la pression **absolue du local** lue au baromètre (pas la pression « ramenée au niveau de la mer » des bulletins météo). 10 hPa d'écart ≈ 0,5 % d'erreur sur le Re. |
| **Fluide** | Humidité relative [%] | si l'on veut tenir compte de l'humidité de l'air (0 = air sec) |
| **Température de l'air** | Offset PT100 (+ ou −) | si la sonde PT100 s'écarte d'un thermomètre de référence : saisir la correction (ex. −0,4 °C). 0 par défaut. |
| **Température de l'air** | Température saisie + case « Utiliser la température saisie » | si la sonde est absente ou douteuse : cocher la case et saisir la température. Décochée = sonde en temps réel. |
| **Bascule ISO / Miller** | Seuil et hystérésis (en Re conduite) | pour déplacer le passage entre les deux diaphragmes. Le titre de la carte rappelle les équivalents en Re veine. |

La carte **Caractéristiques de l'air (temps réel)** affiche, sans rien
modifier, la température utilisée et les propriétés de l'air qui en
découlent (ρ, μ, ν).

### Ce qui est réservé au responsable du banc

Géométries (veine, diaphragmes ISO et Miller), coefficients de viscosité,
réglages de régulation, de surveillance, du très bas Re et des temps de
course des vannes. Ces valeurs viennent de la mise en service et des
apprentissages : **une mauvaise valeur fausse le Re ou la régulation**.
Une valeur marquée « à ajuster » est une valeur d'usine jamais mesurée sur
le banc. La communication Modbus (adresse IP, port) se modifie dans
`config.json`, puis en redémarrant le logiciel.
