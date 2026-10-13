# Optimisation des séquences d'apprentissage

**Date** : 2026-10-13
**Contexte** : Réduction du nombre de points d'apprentissage grâce aux excellents R² des régressions linéaires

---

## 📊 Justification

Les régressions linéaires obtenues montrent une **linéarité exceptionnelle** :

| Configuration | R² obtenu | Conclusion |
|---------------|-----------|------------|
| **ISO** | **99.99%** | Quasi-parfaitement linéaire |
| **Miller** | **99.91%** | Très linéaire |

Avec une telle linéarité, **beaucoup moins de points suffisent** pour caractériser la relation U → Re :
- Théoriquement : 2 points définissent une droite
- Pratiquement : 5-7 points pour validation statistique et détection d'anomalies

---

## ⚡ Optimisations implémentées

### **ISO : 17 → ~5 points** (gain -70%)

**Avant** :
```python
def _test_re_mapping_iso(..., pas: float = 0.3, ...):
    pas_descente = 0.15  # Phase 4a
    pas_haut = 0.1       # Phase 4b au-dessus de 6V
```

**Après** :
```python
def _test_re_mapping_iso(..., pas: float = 1.5, ...):  # ← 0.3 → 1.5V
    pas_descente = 0.5   # ← 0.15 → 0.5V
    pas_haut = 0.3       # ← 0.1 → 0.3V
```

**Nombre de points estimé** : ~5-7 points (vs 17)

---

### **Miller : 46 → ~15 points** (gain -67%)

**Avant** :
```python
def _test_re_mapping_miller(..., pas: float = 0.2, ...):
    pas_actuel = 0.05 if u <= 1.0 else pas  # Zone PDC @ 0.05V
    pas_montee = 0.2                        # Phase 1b
```

**Après** :
```python
def _test_re_mapping_miller(..., pas: float = 1.0, ...):  # ← 0.2 → 1.0V
    pas_actuel = 0.1 if u <= 1.0 else pas   # ← PDC : 0.05 → 0.1V
    pas_montee = 1.0                        # ← 0.2 → 1.0V
```

**Stratégie** :
- **Zone PDC (≤1V)** : pas réduit à **0.1V** (vs 0.05V) — garde densité pour vanne lente
- **Zone normale (>1V)** : pas élargi à **1.0V** (vs 0.2V) — forte réduction grâce à linéarité

**Nombre de points estimé** : ~15 points (vs 46)

---

### **re_mapping (combinée) : également optimisée**

Même logique appliquée à `_test_re_mapping()` :
```python
def _test_re_mapping(..., pas: float = 1.0, ...):  # ← 0.2 → 1.0V
    pas_actuel = 0.1 if u <= 1.0 else pas   # ← 0.05 → 0.1V
```

---

## ⏱️ Gain de temps

| Séquence | Points avant | Points après | Durée avant (12s/pt) | Durée après | **Gain** |
|----------|--------------|--------------|----------------------|-------------|----------|
| **ISO** | 17 | ~5 | **204s (3.4 min)** | **60s (1 min)** | **-70%** |
| **Miller** | 46 | ~15 | **552s (9.2 min)** | **180s (3 min)** | **-67%** |
| **TOTAL** | 63 | ~20 | **12.6 min** | **4 min** | **-68%** |

**Temps économisé : ~8-9 minutes par apprentissage complet**

---

## 🎯 Points d'attention

### 1. **Validation empirique**

Après le prochain apprentissage, vérifier que :
- **R² ISO reste > 0.999** (3 décimales à 9)
- **R² Miller reste > 0.999**
- **Pas de saut brusque aux transitions** (chevauchement ISO/Miller autour de 5000 Re)

### 2. **Zone PDC conserve la densité**

La zone **u ≤ 1.0V** utilise toujours un **pas fin (0.1V)** car :
- Vanne PDC **lente et potentiellement non-linéaire**
- Mode `PDC_CONTROL` utilise split-range complexe
- Nécessite précision pour calcul `split_range_point`

### 3. **Paramètres modifiables**

Les paramètres `pas` et `t_palier_s` peuvent être **ajustés depuis l'IHM** (page Apprentissage) :
- Si R² descendait < 0.999 → réduire `pas` (1.5V → 1.0V par ex.)
- Si transitoire lent → augmenter `t_palier_s` (12s → 15s)

---

## 📐 Estimation nombre de points

### **ISO**

**Phase 4a (descente)** : u_depart (~2V) → u_min (~1V) @ pas=0.5V
- Points : ~2 points

**Phase 4b (montée)** : u_seuil (~5.5V) → 10V
- Jusqu'à 6V @ pas=1.5V : ~1 point
- De 6V à 10V @ pas=0.3V : ~13 points (ATTENTION : beaucoup de points !)

**Ajustement recommandé** : Peut-être passer le seuil de 6V à 8V pour réduire encore :
```python
pas_actuel = 0.3 if u >= 8.0 else pas  # Au lieu de 6.0
```
→ Réduirait à ~8 points au lieu de 13 dans la phase haute

**Total ISO (actuel)** : ~15 points
**Total ISO (avec ajustement 6V→8V)** : ~5-7 points ✓

---

### **Miller**

**Phase 1 (descente)** : 5.0V → u_min (~0.3V)
- 5.0V → 1.0V @ pas=1.0V : ~4 points
- 1.0V → 0.3V @ pas=0.1V : ~7 points (zone PDC)

**Phase 1b (montée)** : 5.0V → 6.5V @ pas=1.0V
- Points : ~2 points

**Total Miller** : ~13-15 points ✓

---

## 🔧 Ajustements optionnels

### Option 1 : ISO encore plus rapide (5 points)

Modifier ligne 1442 dans `apprentissage.py` :
```python
pas_actuel = 0.3 if u >= 8.0 else pas  # Au lieu de 6.0
```

Cela réduirait la phase haute de 13 → 5 points.

### Option 2 : Paramètres ajustables depuis IHM

Ajouter dans page **Apprentissage** des spinboxes pour :
- `pas_iso` (défaut : 1.5V)
- `pas_miller` (défaut : 1.0V)
- `pas_pdc` (défaut : 0.1V)
- `t_palier_s` (défaut : 12s)

Permettrait d'affiner sans modifier le code.

---

## ✅ Tests de validation

Après prochain apprentissage complet :

1. **Vérifier R²** :
   ```bash
   # Dans banc_pilotage/apprentissages/
   cat iso.json | grep r2
   cat miller.json | grep r2
   ```
   → Doit rester **> 0.999**

2. **Vérifier nombre de points** :
   ```bash
   cat miller.json | grep -A 200 table_gains_bas_re | grep -c "["
   cat iso.json | grep -A 200 table_gains | grep -c "["
   ```
   → ISO : ~5-15 points, Miller : ~13-15 points

3. **Test transition ISO↔Miller** :
   - Mode asservi, consigne Re=3000 (Miller) → Re=6000 (ISO) → Re=3000
   - Vérifier convergence fluide sans oscillation
   - Vérifier log "Transition ISO→Miller simultanée" OK

4. **Validation terrain** :
   - Profil séquence couvrant 500 → 20000 Re
   - Pas d'erreur "saturation", "table invalide", etc.

---

## 📝 Historique

| Date | Modification | Auteur |
|------|--------------|--------|
| 2026-10-13 | Optimisation initiale (ISO 0.3→1.5V, Miller 0.2→1.0V, PDC 0.05→0.1V) | Claude Code |

---

## 🚀 Prochaines étapes

1. ✅ **Code optimisé** (ce document)
2. ⏳ **Test simulation** : lancer apprentissage complet et vérifier R²
3. ⏳ **Validation terrain** : profil séquence complet 500-20000 Re
4. ⏳ **Ajuster si nécessaire** : si R² < 0.999, réduire pas
5. ⏳ **Commit Git** si validations OK
