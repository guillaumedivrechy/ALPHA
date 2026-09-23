# -*- coding: utf-8 -*-
"""Cohérence entre ce que produit le cœur et ce que l'IHM va y chercher.

L'IHM n'est pas testable (pas de Qt dans les tests) : ses formateurs vont
chercher des clés dans `Apprentissage.resultats` et dans `Pilote.etat` sans
qu'aucun test ne les relie. Une clé renommée côté cœur passe donc les 115
tests et casse l'affichage au banc — c'est arrivé : la suppression des
positions 180° de la vanne 3 voies a renommé T1/T2 et laissé le formateur
sur T1_fc1_to_iso_s / T3_fc1_to_miller_s, soit un KeyError à chaque tick du
QTimer, donc l'IHM entière figée.

Ces tests lisent le source de `gui/app.py` plutôt que de l'importer : ils
restent valables sans Qt installé.
"""

import ast
import json
import re
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
SRC_GUI = (RACINE / "gui" / "app.py").read_text(encoding="utf-8")
SRC_APPR = (RACINE / "core" / "apprentissage.py").read_text(encoding="utf-8")

# clés réellement écrites par chaque séquence, relevées dans core/apprentissage.py
CLES_PRODUITES = {
    "vanne_3voies":    {"T1_0_vers_90_s", "T2_90_vers_0_s", "max_s"},
    "vanne_bypass":    {"ouverture_s", "fermeture_s", "max_s"},
    "vanne_pdc":       {"ouverture_s", "fermeture_s", "max_s"},
    "calibration_pdc": {"v_min_recopie", "v_max_recopie", "plage_v"},
    "freq_min":        {"u_min_exploitable_v", "derniere_stable_v",
                        "re_a_u_min", "paliers_V_Re"},
    "courbe_gain":     {"gain_moyen", "non_linearite_pct", "sature_a_v",
                        "verdict"},
    "indiciel":        {"u1_v", "u2_v", "re1", "re2", "gain_re_par_v",
                        "temps_mort_s", "tau_s", "t_stabilisation_s",
                        "kp_propose", "ki_propose"},
}


def _cles_lues_par_lihm(test: str) -> set:
    """Clés que les formateurs de l'IHM lisent pour cette séquence.

    On isole le fragment de source qui suit la mention du test, borné au
    premier `)),` ou `))` de fin d'entrée, puis on y cherche les r['...'].
    """
    cles = set()
    for m in re.finditer(r'"%s"\s*,\s*\n?\s*lambda r:' % re.escape(test),
                         SRC_GUI):
        reste = SRC_GUI[m.end():m.end() + 500]
        # borne : la prochaine entrée de la liste commence par une nouvelle
        # chaîne entre guillemets précédée d'une parenthèse ouvrante
        fin = reste.find('\n            ("')
        if fin == -1:
            fin = reste.find('\n        _maj(')
        cles |= set(re.findall(r"r\['(\w+)'\]", reste[:fin if fin > 0 else 500]))
    return cles


@pytest.mark.parametrize("test", sorted(CLES_PRODUITES))
def test_formateurs_ihm_alignes_sur_les_resultats(test):
    """Chaque clé lue par un formateur doit exister dans le résultat produit."""
    lues = _cles_lues_par_lihm(test)
    if not lues:
        pytest.skip(f"pas de formateur avec r['...'] pour « {test} »")
    manquantes = lues - CLES_PRODUITES[test]
    assert not manquantes, (
        f"gui/app.py lit {sorted(manquantes)} pour « {test} », "
        f"mais core/apprentissage.py produit {sorted(CLES_PRODUITES[test])}")


def test_cles_produites_reellement_ecrites():
    """Garde-fou du garde-fou : la table ci-dessus doit rester le reflet du
    code, sinon elle validerait l'IHM contre une fiction."""
    for test, cles in CLES_PRODUITES.items():
        for cle in cles:
            assert f'"{cle}"' in SRC_APPR, (
                f"« {cle} » (déclarée pour {test}) est introuvable dans "
                "core/apprentissage.py — table à remettre à jour")


def test_noms_de_sequences_connus_du_coeur():
    """Les tests proposés par l'IHM doivent exister dans le dispatch."""
    lances = set(re.findall(r'self\._lancer_test\(\s*[\'"](\w+)[\'"]', SRC_GUI))
    lances |= set(re.findall(r't=[\'"](\w+)[\'"]\s*:\s*self\._lancer_test', SRC_GUI))
    lances |= set(re.findall(r'lambda c=False, t=[\'"](\w+)[\'"]', SRC_GUI))
    connus = set(re.findall(r'[\'"](\w+)[\'"]\s*:\s*self\._test_\w+', SRC_APPR))
    assert lances, "aucun appel à _lancer_test détecté (regex à revoir)"
    assert lances <= connus, (
        f"l'IHM propose {sorted(lances - connus)}, absents du dispatch "
        f"de core/apprentissage.py ({sorted(connus)})")


def test_entrees_analogiques_et_fins_de_course_collent_a_la_config():
    """Les deux tables descriptives de l'IHM doivent nommer les mêmes voies
    que config.json — sinon la page de relevé afficherait des canaux qui
    n'existent pas, ou en tairait."""
    cfg = json.loads((RACINE / "config.json").read_text(encoding="utf-8"))
    mb = cfg["modbus"]
    attendues_ai = {k for k in mb["entrees"] if not k.startswith("_")}
    attendues_tor = {k for k in mb["entrees_tor"] if not k.startswith("_")}

    arbre = ast.parse(SRC_GUI)
    tables = {}
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.Assign) and isinstance(noeud.targets[0], ast.Name):
            nom = noeud.targets[0].id
            if nom in ("ENTREES_ANALOGIQUES", "FINS_DE_COURSE"):
                tables[nom] = {e.elts[0].value for e in noeud.value.elts}

    assert tables.get("ENTREES_ANALOGIQUES") == attendues_ai
    assert tables.get("FINS_DE_COURSE") == attendues_tor
