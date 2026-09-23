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


# ── clés d'état lues par l'IHM ────────────────────────────────────────────

SRC_PILOTE = (RACINE / "core" / "pilote.py").read_text(encoding="utf-8")


def _cles_publiees_par_le_pilote() -> set:
    """Toutes les clés que core/pilote.py écrit dans self.etat, relevées
    statiquement : self.etat["x"] = …, self.etat.update({"x": …}) et le
    dictionnaire initial. Statique plutôt qu'à l'exécution, pour couvrir
    aussi les branches de défaut qu'un cycle nominal n'atteint pas."""
    cles = set(re.findall(r'self\.etat\[\s*"(\w+)"\s*\]\s*=', SRC_PILOTE))
    cles |= set(re.findall(r'"(\w+)"\s*:', SRC_PILOTE[
        SRC_PILOTE.find("self.etat = {"):SRC_PILOTE.find("self.etat = {") + 400]))
    for m in re.finditer(r'self\.etat\.update\(\{', SRC_PILOTE):
        bloc = SRC_PILOTE[m.end():m.end() + 1600]
        fin = bloc.find("\n        })")
        cles |= set(re.findall(r'"(\w+)"\s*:', bloc[:fin if fin > 0 else 1600]))
    return cles


def _cles_detat_lues_par_lihm() -> set:
    """Clés lues sur le dict d'état dans gui/app.py (variable `e`)."""
    cles = set(re.findall(r'\be\.get\(\s*[\'"](\w+)[\'"]', SRC_GUI))
    cles |= set(re.findall(r'\be\[\s*[\'"](\w+)[\'"]\s*\]', SRC_GUI))
    bloc = re.search(r'ALERTES = \[(.*?)\n    \]', SRC_GUI, re.S)
    if bloc:
        cles |= set(re.findall(r'\(\s*"(\w+)"', bloc.group(1)))
    return cles


def test_toute_cle_detat_lue_par_lihm_est_publiee():
    """C'est la cause racine de l'IHM figée : gui/app.py allait chercher des
    clés que core/pilote.py ne produisait plus. Une clé jamais publiée vaut
    silencieusement None — au mieux un affichage vide, au pire un KeyError."""
    lues = _cles_detat_lues_par_lihm()
    publiees = _cles_publiees_par_le_pilote()
    assert publiees, "aucune clé détectée dans pilote.py (regex à revoir)"

    # publiées ailleurs que dans pilote.py, légitimement
    hors_pilote = {
        "sequence",      # écrit par la boucle de suivi de profil
        "duree_cycle_s",  # écrit par _boucle, hors _cycle
    }
    manquantes = lues - publiees - hors_pilote
    assert not manquantes, (
        f"gui/app.py lit {sorted(manquantes)} sur pilote.etat, "
        "que core/pilote.py ne publie jamais")


def test_page_mise_en_service_branchee():
    """La page doit être construite, empilée et rafraîchie : oublier le tick
    donnerait une page figée sans que rien ne le signale."""
    assert "_build_mise_en_service" in SRC_GUI
    assert "self._stack.addWidget(self._build_mise_en_service())" in SRC_GUI
    assert "self._tick_mise_en_service(e)" in SRC_GUI
    assert "Mise en service" in SRC_GUI.split("_NAV_LABELS")[1][:200]
    # l'index teste dans le tick doit correspondre a la position d'empilement
    ordre = re.findall(r'self\._stack\.addWidget\(self\._build_(\w+)\(\)\)',
                       SRC_GUI)
    assert ordre.index("mise_en_service") == 5
    assert "self._stack.currentIndex() != 5" in SRC_GUI


def test_page_mise_en_service_ne_commande_pas_le_materiel_en_direct():
    """Règle d'architecture : toute commande passe par une méthode publique
    du pilote. Une page de diagnostic est justement l'endroit où l'on serait
    tenté d'écrire directement sur les E/S."""
    debut = SRC_GUI.find("def _build_mise_en_service")
    fin = SRC_GUI.find("def _build_footer")
    assert 0 < debut < fin
    page = SRC_GUI[debut:fin]
    assert "self.pilote.es" not in page
    assert not re.search(r'\bes\.(ecrire|lire)_', page)


# ── les champs de l'onglet Configuration doivent agir ─────────────────────

def _champs_exposes_par_lihm() -> set:
    """Chemins editables depuis l'onglet Configuration, sous forme de tuples.

    Les chemins ont une ou deux composantes : `patm_pa` est a la racine de
    config.json, les autres sont dans une section. Ne reconnaitre que les
    couples laissait justement patm_pa hors de toute verification.
    """
    debut = SRC_GUI.find("def _build_configuration")
    fin = SRC_GUI.find("def _update_controls_enabled", debut)
    bloc = SRC_GUI[debut:fin if fin > debut else debut + 9000]
    champs = {(a, b) for a, b in
              re.findall(r'\(\(\s*"(\w+)"\s*,\s*"(\w+)"\s*\)', bloc)}
    champs |= {(a,) for a in
               re.findall(r'\(\(\s*"(\w+)"\s*,\s*\)', bloc)}
    return champs


def test_tout_champ_editable_est_repris_a_chaud():
    """Un réglage accessible qui n'agit qu'au redémarrage est pire qu'un
    réglage absent : on croit avoir agi. Chaque champ exposé doit être relu
    soit par Banc (géométries, seuils de sélection), soit par
    Pilote.appliquer_config (gains, tolérances, chiens de garde, bornes)."""
    src_banc = (RACINE / "core" / "banc.py").read_text(encoding="utf-8")
    deb = SRC_PILOTE.find("def appliquer_config")
    fin = SRC_PILOTE.find("\n    def ", deb + 10)
    corps = SRC_PILOTE[deb:fin if fin > deb else deb + 6000]
    assert "appliquer_config" in SRC_GUI, \
        "l'IHM n'appelle pas appliquer_config : la moitié des champs seraient inertes"

    # champs de pure communication : relus a la (re)connexion, pas a chaud
    hors_chaud = {("modbus", "ip"), ("modbus", "port"), ("modbus", "timeout_s")}
    # purement affichage, aucun effet sur la boucle
    affichage = {("variateur", "frequence_max_hz")}

    inertes = []
    for chemin in sorted(_champs_exposes_par_lihm()):
        if chemin in hors_chaud or chemin in affichage:
            continue
        cle = chemin[-1]
        if f'"{cle}"' in corps or f'"{cle}"' in src_banc:
            continue
        inertes.append(".".join(chemin))
    assert not inertes, (
        "champs editables sans effet avant redemarrage : " + ", ".join(inertes))


def test_champs_exposes_existent_dans_la_config():
    """Un champ pointant sur une clé absente de config.json afficherait un
    blanc et écrirait une clé morte."""
    cfg = json.loads((RACINE / "config.json").read_text(encoding="utf-8"))
    champs = _champs_exposes_par_lihm()
    assert ("patm_pa",) in champs, (
        "la pression atmospherique doit rester saisissable : les deux "
        "classeurs de reference la portent en cellule d'entree (ISO "
        "« Calcul de qm » D6, Miller « Interface utilisateur » D6), et le "
        "banc n'a aucun capteur de pression absolue pour la mesurer")
    for chemin in sorted(champs):
        noeud, lu = cfg, []
        for cle in chemin:
            lu.append(cle)
            assert isinstance(noeud, dict) and cle in noeud, (
                "« %s » absent de config.json" % ".".join(lu))
            noeud = noeud[cle]
