# Check-list de mise en service — banc réel

Ce que les tests automatiques ne peuvent pas vérifier : le matériel, le
câblage, le paramétrage du variateur et du coupleur, la métrologie. À
dérouler dans l'ordre ; ne pas passer à une section tant que la précédente
n'est pas entièrement cochée. Noter les valeurs relevées dans la marge.

## 0. Avant d'aller au banc (PC)

- [ ] `python -m pytest tests/ -q` : tout passe.
- [ ] `python repetition_generale.py` : « RÉPÉTITION GÉNÉRALE : tout est passé ».
- [ ] `python main.py --simulation` : l'IHM démarre, fenêtre d'initialisation
      fermée en moins d'une seconde, aucun message d'erreur.
- [ ] Sauvegarder `config.json`, `etat_banc.json` et `apprentissages/` (copie
      ou commit) : point de retour si un réglage tourne mal.

## 1. Sécurité, ventilateur hors tension

- [ ] Arrêt d'urgence physique : coupe bien le moteur, indépendamment du PC.
- [ ] Variateur 3G3MV : référence de fréquence par la borne FR (n004 = 2),
      marche par S1 (n003 = 1), fréquence max 50,0 Hz (n011).
- [ ] **Watchdog Modbus du coupleur 750-362 activé** (doc WAGO du coupleur) :
      sur perte de liaison, les sorties doivent retomber (référence 0 V, RUN
      ouvert). Indispensable : le logiciel ne peut plus rien écrire quand la
      liaison est coupée, et un test sur faux coupleur a montré qu'un
      coupleur sans watchdog garde sa dernière consigne et RUN actif.
- [ ] Comportement du 3G3MV sur perte de référence / ouverture de S1 :
      arrêt du moteur (manuel du variateur).
- [ ] IP du coupleur : `config.json` → `modbus.ip` = 192.168.1.80, PC sur le
      même sous-réseau (`ping`). Le pied de page de l'IHM affiche cette IP.

## 2. Entrées / sorties (WAGO I/O CHECK, puis page Maintenance)

Adresses de `config.json`. Chaque voie : provoquer l'état, vérifier la
lecture dans la page Maintenance (image process et valeur convertie).

| Type | Adr. | Voie | Vérification |
|---|---|---|---|
| AI | 0 | `pdc_recopie` | suit la consigne PdC (0 V fermée, 10 V ouverte) |
| AI | 1 | `dp_iso_500` | ≈ 0 Pa ventilateur arrêté ; répond seul en butée ISO |
| AI | 2 | `dp_diaph_500` | ≈ 0 Pa ventilateur arrêté ; répond seul en butée Miller |
| AI | 3 | `p_amont_500` | ≈ 0 Pa arrêté, monte avec le débit |
| AI | 4 | `frequence_variateur` | image de la fréquence (n065 = 0, n066 = 0) |
| AI | 16 | `temperature` | égale à un thermomètre de référence à ±0,5 °C |
| DI | 0 / 1 | `fc_bypass_ouvert` / `_ferme` | une seule active en butée, aucune en course |
| DI | 2 / 3 | `fc_pdc_ouvert` / `_ferme` | idem |
| DI | 4 / 6 | `fc_3voies_0` / `_90` | une seule active (0° ISO, 90° Miller) |
| DI | 5 | `fc_3voies_180` | jamais active |
| DI | 7 | `defaut_variateur` | contact NF : pas de défaut en marche ; **fil débranché = défaut** |
| DI | 8-10 | `defaut_act_*` | bandeau orange dans l'IHM quand le défaut est forcé |
| AO | 0 | `vanne_perte_charge` | 0 V = fermée, 10 V = grande ouverte |
| AO | 1 | `variateur` | 5 V commandés → mi-fréquence au variateur |
| Coil | 0 / 1 | `vanne_diaphragme_0deg` / `_90deg` | jamais les deux ensemble |
| Coil | 2 | `vanne_bypass` | ouvre / ferme le bypass |
| Coil | 3 | `variateur_run` | RUN du 3G3MV |

## 3. Actionneurs (page Maintenance, ventilateur arrêté)

- [ ] Vanne 3 voies : « Ordre F → 0° » d'abord (vanne d'usine à 0° : elle ne
      doit pas bouger), puis « Ordre I → 90° », puis retour. Temps de course
      relevé : ______ s.
- [ ] Bypass : ouvrir / fermer ; les deux fins de course commutent, aucune
      active pendant la course. Temps : ______ s.
- [ ] Vanne PdC : 0 V → FC fermée, 10 V → FC ouverte, 5 V → mi-course ; la
      recopie suit. Commander la butée haute de la recopie
      (`actionneurs.pdc_recopie_max_v`) et vérifier que la FC « ouverte » est
      atteinte (cf. README, échelle de la consigne PdC). Temps : ______ s.
- [ ] Indicateur de la vanne 3 voies : « Manœuvre… → … (en course) » pendant
      la course, puis la position lue ; le grand affichage Re indique
      « en cours » pendant la course.

## 4. Capteurs, ventilateur arrêté puis à faible régime

- [ ] Zéro ΔP (apprentissage « Zéro capteurs », soufflerie arrêtée) :
      offsets de quelques Pa au plus.
- [ ] Bruit ΔP à débit stable (page Voies) : écart-type > 0,1 Pa, sinon la
      détection de signal figé arrêterait le banc à tort.
- [ ] Capteur ΔP **affiché = butée lue** : en butée ISO le capteur ISO est
      exploité, en butée Miller le capteur Miller.

## 5. Apprentissages, dans l'ordre de l'onglet

Vérifier dans la confirmation « Appliquer » les valeurs ancienne → nouvelle.

- [ ] Zéro capteurs → Appliquer.
- [ ] Calibration recopie PdC → Appliquer (fermée ≈ 0 V, ouverte ≈ 10 V).
- [ ] Vanne 3 voies, bypass, vanne PdC → Appliquer (chiens de garde).
- [ ] Contrôle câblage E/S : « câblage conforme » (ou mapping ISO/Miller
      corrigé par Appliquer).
- [ ] Re mapping Miller → Appliquer. Relever u_min : ______ V. **La valeur
      actuelle de `strategie_bas_re.u_min_ventilateur_v` (0,3 V) ne vient pas
      de ce banc : elle doit être remplacée par la mesure.**
- [ ] Re mapping ISO → Appliquer.
- [ ] Essai indiciel → Appliquer (kp, ki, τ).
- [ ] Au redémarrage suivant, plus de message « Stratégie bas-Re
      indisponible ».

## 6. Sécurités en marche (ventilateur à faible régime)

Pour chaque essai : ventilateur coupé, bandeau rouge avec la cause, aucune
relance possible avant « Acquitter », puis reprise normale.

- [ ] Débrancher le câble Ethernet du coupleur : le **moteur s'arrête**
      (watchdog / 3G3MV, §1) ; bandeau « liaison Modbus perdue ». Rebrancher :
      rien ne repart seul ; acquitter, relancer.
- [ ] Débrancher la PT100 : repli en quelques secondes.
- [ ] Débrancher un capteur ΔP en régulation : repli en ≤ 12 s.
- [ ] Ouvrir le contact MB du variateur (ou provoquer un défaut 3G3MV) : repli
      immédiat.
- [ ] ARRÊT VARIATEUR pendant un apprentissage (Re mapping) : arrêt
      immédiat, aucun redémarrage du palier suivant.
- [ ] Fermer l'application pendant une régulation : ventilateur à 0 V, puis
      vannes au repos (Miller, bypass et PdC ouverts).

## 7. Régulation (asservi, référence veine)

Temps mesurés sur le banc simulé, à titre de repère.

| Consigne | Attendu | Repère simulé | Relevé |
|---|---|---|---|
| 12 000 Re veine depuis le repos | ISO, bypass fermé | « RÉGULÉ ✓ » ≤ 30 s | |
| 3 000 depuis 12 000 | reste en ISO, aucune manœuvre | ≤ 15 s | |
| 2 000 depuis 3 000 | Miller, bypass ouvert, variateur | ≤ 30 s | |
| 300 depuis 2 000 | mode PdC, ventilateur fixe, sans creux sous la consigne | ≤ 40 s | |
| 15 000 | limitée ≈ 13 700 (« consigne limitée ») | ≤ 30 s | |
| `exemple_sequence.csv` | chaque échelon converge, « Arrêter » fige | — | |

- [ ] « RÉGULÉ ✓ » ne s'allume que Re stabilisé (jamais pendant une
      transition) et ne clignote pas en régime établi.
- [ ] Bruit du Re à 300 Re veine en mode PdC : ______ %.

## 8. Métrologie

- [ ] Comparer débit / Re à un instrument de référence en plusieurs points
      ISO et Miller.
- [ ] Continuité Miller / ISO dans la zone de recouvrement (≈ 4 500 à
      5 500 Re conduite) : écart entre les deux diaphragmes : ______ %.
- [ ] Facteur conduite / veine (1,870 avec la géométrie de `config.json`)
      cohérent avec la veine réelle (`veine.hauteur_m`, `largeur_m`).

## 9. Clôture

- [ ] Sauvegarder (commit) `config.json`, `etat_banc.json`, `apprentissages/`
      avec les valeurs de ce banc.
- [ ] Reporter ici les valeurs relevées et les écarts constatés.
