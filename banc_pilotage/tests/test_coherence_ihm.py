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
    "vanne_3voies":    {"T1_0_vers_90_s", "T2_90_vers_0_s", "max_s",
                        "chronologie_T1", "chronologie_T2",
                        "chronologie_texte", "course_complete_estimee_s"},
    "vanne_bypass":    {"ouverture_s", "fermeture_s", "max_s"},
    "vanne_pdc":       {"ouverture_s", "fermeture_s", "max_s"},
    "calibration_pdc": {"v_min_recopie", "v_max_recopie", "plage_v"},
    "re_mapping_miller": {"u_min_exploitable_v", "u_seuil_bas_re_v",
                          "derniere_stable_v", "re_a_u_min",
                          "pdc_min_exploitable_pct", "re_min_exploitable",
                          "paliers_V_Re", "paliers_PDC_Re"},
    "table_pdc":       {"u_ventilateur_v", "pdc_min_exploitable_pct",
                        "re_min_exploitable", "paliers_PDC_Re"},
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
        # fenêtre large, bornée par la fin du bloc : le bloc principal de
        # publication du cycle dépasse 1 600 caractères
        bloc = SRC_PILOTE[m.end():m.end() + 4000]
        fin = bloc.find("\n        })")
        cles |= set(re.findall(r'"(\w+)"\s*:', bloc[:fin if fin > 0 else 4000]))
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
    assert "self._tick_mise_en_service" in SRC_GUI
    assert "Maintenance" in SRC_GUI.split("_NAV_LABELS")[1][:200]
    # l'index teste dans le tick doit correspondre a la position d'empilement
    ordre = re.findall(r'self\._stack\.addWidget\(self\._build_(\w+)\(\)\)',
                       SRC_GUI)
    assert ordre.index("mise_en_service") == 5
    assert "PAGE_MAINTENANCE = 5" in SRC_GUI
    assert "self._stack.currentIndex() != self.PAGE_MAINTENANCE" in SRC_GUI
    # quitter la page doit rendre la main a la boucle : sinon le maintien
    # des ordres TOR, suspendu par les commandes de vannes, le reste
    assert "self.pilote.reprendre_maintien_tor()" in SRC_GUI.split(
        "def _nav(")[1][:900]


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


# ── replis sûrs et concurrence (revue à froid) ────────────────────────────

def test_fermeture_par_la_croix_arrete_la_soufflerie():
    """RÉGRESSION : closeEvent n'appelait que pilote.arreter(), qui arrête le
    thread et ferme la liaison sans toucher aux sorties. Fermer la fenêtre
    ventilateur en marche le laissait tourner, hors supervision et sans plus
    aucun moyen de l'arrêter depuis le logiciel."""
    deb = SRC_GUI.find("def closeEvent")
    fin = SRC_GUI.find("\n    def ", deb + 10)
    corps = SRC_GUI[deb:fin]
    assert "arreter_regulation()" in corps, \
        "closeEvent doit couper la machine tournante avant d'arrêter le thread"
    assert corps.index("arreter_regulation()") < corps.index("self.pilote.arreter()")


def test_enregistrement_config_refuse_pendant_la_regulation():
    """Le déverrouillage vérifiait l'arrêt, mais rien n'empêchait de démarrer
    ENTRE le déverrouillage et la sauvegarde. appliquer_config relit des
    paramètres utilisés par la boucle temps réel."""
    deb = SRC_GUI.find("def _enregistrer_configuration")
    fin = SRC_GUI.find("\n    # ", deb + 10)
    corps = SRC_GUI[deb:fin if fin > deb else deb + 3000]
    assert 'self.pilote.etat.get("regulation")' in corps
    # comparer sur l'APPEL, pas sur la mention : le commentaire ci-dessus
    # cite appliquer_config et fausserait la position
    assert corps.index('etat.get("regulation")') \
        < corps.index("self.pilote.appliquer_config(")
    # et le verrou se referme tout seul si la régulation démarre après coup
    assert "chk_unlock.setChecked(False)" in SRC_GUI.split("def _tick(")[1][:6000] \
        or "reverrouillage vivant" in SRC_GUI


def test_config_ecrite_avec_saut_de_ligne_final():
    """Sans lui, chaque sauvegarde depuis l'IHM ou depuis « Appliquer »
    produit un fichier sans newline terminal : git le signale à chaque fois
    et pollue les diffs."""
    src_appr = (RACINE / "core" / "apprentissage.py").read_text(encoding="utf-8")
    for nom, src in (("gui/app.py", SRC_GUI), ("core/apprentissage.py", src_appr)):
        assert "ecrire_json_atomique(self.chemin_cfg" in src, nom
    assert (RACINE / "config.json").read_text(encoding="utf-8").endswith("\n")


def test_ecriture_json_atomique(tmp_path):
    """config.json et les apprentissages s'écrivent via un fichier
    temporaire substitué d'un bloc : une écriture interrompue ne laisse
    jamais un fichier tronqué (config illisible = démarrage impossible)."""
    from core.fichiers import ecrire_json_atomique
    cible = tmp_path / "config.json"
    cible.write_text('{"ancien": 1}\n', encoding="utf-8")

    class Inserialisable:
        pass

    with pytest.raises(TypeError):
        ecrire_json_atomique(cible, {"x": Inserialisable()})
    assert cible.read_text(encoding="utf-8") == '{"ancien": 1}\n'   # intact
    assert list(tmp_path.iterdir()) == [cible]                       # pas de résidu
    ecrire_json_atomique(cible, {"nouveau": "é"})
    texte = cible.read_text(encoding="utf-8")
    assert texte.endswith("\n") and "é" in texte


def test_fenetre_de_convergence_lue_par_reference_locale():
    """appliquer_config() remplace la fenêtre (sa profondeur dépend de la
    période). La lire deux fois dans le même cycle exposait à un
    fenetre_pleine sur l'ancienne et une moyenne None sur la nouvelle, donc
    un TypeError remonté en arrêt de sécurité."""
    deb = SRC_PILOTE.find("fenetre_pleine =")
    corps = SRC_PILOTE[deb - 400:deb + 300]
    assert "fenetre = self._historique_re" in corps
    assert "len(fenetre) == fenetre.maxlen" in corps
    assert "fmean(self._historique_re)" not in corps


def test_chaque_section_daffichage_est_isolee():
    """RÉGRESSION : les fonctions de rafraîchissement étaient appelées à la
    file depuis _tick. Une seule exception arrêtait le QTimer et figeait
    l'IHM entière — mesures, états de vannes et alertes de sécurité
    comprises. C'est exactement ce qu'a produit le formateur désynchronisé
    de la vanne 3 voies. Chacune doit désormais passer par _section()."""
    deb = SRC_GUI.find("def _tick(self)")
    fin = SRC_GUI.find("\n    def ", deb + 10)
    corps = SRC_GUI[deb:fin]
    appels_directs = re.findall(r'self\.(_tick_\w+)\(', corps)
    assert not appels_directs, (
        "appels non isolés depuis _tick : " + ", ".join(appels_directs))
    assert corps.count("self._section(") >= 7, \
        "toutes les sections doivent passer par _section()"
    # et _section doit journaliser une fois, pas a chaque tick
    d2 = SRC_GUI.find("def _section(")
    f2 = SRC_GUI.find("\n    def ", d2 + 10)
    sect = SRC_GUI[d2:f2]
    assert "_sections_en_defaut" in sect
    assert "self._log(" in sect


def test_couleurs_utilisees_definies_dans_le_theme():
    """COLORS['success'] et COLORS['warning'] n'existaient pas : l'onglet
    maintenance affichait « Erreur : 'success' » après un enregistrement
    réussi, et l'annulation de l'initialisation des vannes levait KeyError."""
    import re
    from ui.theme import COLORS
    utilisees = set(re.findall(r"COLORS\[['\"](\w+)['\"]\]", SRC_GUI))
    assert utilisees - set(COLORS) == set()
