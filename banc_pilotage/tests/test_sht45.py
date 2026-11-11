# -*- coding: utf-8 -*-
"""Capteur d'air Sensirion SHT45 sur SEK-SensorBridge (io_banc/sht45.py) et
son usage par le pilote : sources de température et d'humidité, maintien
de la dernière mesure, repli, refus de mise en route. Sans matériel."""

import time

import pytest

from io_banc.sht45 import (CMD_MESURE_HAUTE_PRECISION, CMD_NUMERO_SERIE,
                           ConfigSht45, Sht45SensorBridge, Sht45Simule, crc8,
                           decoder_mesure, decoder_numero_serie)


def _mots(*mots):
    """Réponse SHT4x : chaque mot de 16 bits suivi de son CRC."""
    rx = []
    for m in mots:
        octets = [m >> 8, m & 0xFF]
        rx += octets + [crc8(octets)]
    return bytes(rx)


def _reponse_mesure(t_c, hr_pct):
    return _mots(round((t_c + 45.0) / 175.0 * 65535),
                 round((hr_pct + 6.0) / 125.0 * 65535))


# ---- décodage (fiche technique SHT4x) -------------------------------------

def test_crc_exemple_de_la_fiche_technique():
    assert crc8([0xBE, 0xEF]) == 0x92


def test_decodage_mesure_et_numero_de_serie():
    t, hr = decoder_mesure(_reponse_mesure(23.45, 41.2))
    assert t == pytest.approx(23.45, abs=0.01)
    assert hr == pytest.approx(41.2, abs=0.01)
    assert decoder_numero_serie(_mots(0x1234, 0xABCD)) == 0x1234ABCD
    # humidité hors 0-100 (formule) ramenée dans la plage
    assert decoder_mesure(_mots(0x6666, 0xFFFF))[1] == 100.0


def test_trame_corrompue_ou_tronquee_refusee():
    rx = bytearray(_reponse_mesure(20.0, 50.0))
    rx[4] ^= 0x01
    with pytest.raises(ValueError, match="CRC"):
        decoder_mesure(rx)
    with pytest.raises(ValueError, match="octets"):
        decoder_mesure(rx[:5])


def test_configuration():
    c = ConfigSht45.depuis({"adresse_i2c": "0x45", "port_bridge": "two"})
    assert c.adresse_i2c == 0x45 and c.port_bridge == "TWO"
    assert ConfigSht45.depuis(None).adresse_i2c == 0x44
    with pytest.raises(ValueError, match="1,08 à 3,6 V"):
        ConfigSht45.depuis({"tension_v": 5.0})        # le SHT45 serait détruit
    with pytest.raises(ValueError, match="port_bridge"):
        ConfigSht45.depuis({"port_bridge": "THREE"})


# ---- boîtier simulé au niveau des méthodes ---------------------------------

class FauxBridge:
    """SEK-SensorBridge : SHT45 sur le port `port_capteur`, alimenté."""

    def __init__(self, port_capteur=1, t=21.0, hr=45.0):
        self.port_capteur = port_capteur
        self.t, self.hr = t, hr
        self.alimente = set()
        self.panne = False
        self.ferme = False

    def alimenter(self, port, tension_v, actif):
        assert tension_v == 3.3
        (self.alimente.add if actif else self.alimente.discard)(port)

    def echanger_i2c(self, port, adresse, tx, n_rx):
        if self.panne:
            raise OSError("port série fermé")
        if port != self.port_capteur or port not in self.alimente \
                or adresse != 0x44:
            raise IOError("NACK")
        assert n_rx == 6
        if tx == [CMD_NUMERO_SERIE]:
            return _mots(0x0102, 0x0304)
        assert tx == [CMD_MESURE_HAUTE_PRECISION]
        return _reponse_mesure(self.t, self.hr)

    def fermer(self):
        self.ferme = True


def test_detection_du_port_du_boitier():
    bridge = FauxBridge(port_capteur=1)
    c = Sht45SensorBridge({}, ouvrir_bridge=lambda p, d: bridge,
                          lister_ports=lambda: ["COM7"])
    c.connecter()
    assert c.numero_serie == 0x01020304
    assert "COM7, port TWO" in c.description
    assert bridge.alimente == {1}              # port ONE remis hors tension
    m = c.mesurer()
    assert (m.temperature_c, m.humidite_pct) == pytest.approx((21.0, 45.0),
                                                              abs=0.01)


def test_messages_de_connexion_explicites():
    c = Sht45SensorBridge({}, ouvrir_bridge=lambda p, d: FauxBridge(),
                          lister_ports=lambda: [])
    with pytest.raises(ConnectionError, match="aucun port série USB"):
        c.connecter()

    def sans_bibliotheque(p, d):
        raise ImportError("No module named 'sensirion_shdlc_driver'")
    c = Sht45SensorBridge({}, ouvrir_bridge=sans_bibliotheque,
                          lister_ports=lambda: ["COM3"])
    with pytest.raises(ConnectionError, match="pip install"):
        c.connecter()
    # boîtier présent, capteur absent (mauvaise adresse)
    c = Sht45SensorBridge({"adresse_i2c": 0x45},
                          ouvrir_bridge=lambda p, d: FauxBridge(),
                          lister_ports=lambda: ["COM3"])
    with pytest.raises(ConnectionError, match="0x45"):
        c.connecter()


def test_thread_de_lecture_et_reconnexion():
    """Lecture périodique, erreur signalée, reconnexion automatique."""
    bridge = FauxBridge(port_capteur=0, t=19.0)
    c = Sht45SensorBridge({"periode_s": 0.2, "reconnexion_s": 0.5},
                          ouvrir_bridge=lambda p, d: bridge,
                          lister_ports=lambda: ["COM3"])
    c.demarrer()
    try:
        fin = time.monotonic() + 3.0
        while c.derniere_mesure() is None and time.monotonic() < fin:
            time.sleep(0.05)
        assert c.derniere_mesure().temperature_c == pytest.approx(19.0, abs=0.01)
        assert c.erreur is None
        bridge.panne = True
        fin = time.monotonic() + 3.0
        while c.erreur is None and time.monotonic() < fin:
            time.sleep(0.05)
        assert "lecture impossible" in c.erreur and bridge.ferme
        avant = c.derniere_mesure().t
        bridge.panne, bridge.t = False, 24.0
        fin = time.monotonic() + 4.0
        while (c.derniere_mesure().t == avant or c.erreur) \
                and time.monotonic() < fin:
            time.sleep(0.05)
        assert c.erreur is None
        assert c.derniere_mesure().temperature_c == pytest.approx(24.0, abs=0.01)
    finally:
        c.arreter()


# ---- vraie bibliothèque Sensirion, port série simulé au niveau SHDLC -------

def test_chaine_avec_la_bibliotheque_sensirion(monkeypatch):
    """Les appels faits à la bibliothèque (commandes SHDLC 0xD0, 0x00, 0x01,
    0x11, ports ONE/TWO, tension 3,3 V) sont ceux qu'elle accepte, et sa
    réponse I²C est bien décodée."""
    driver = pytest.importorskip("sensirion_shdlc_driver")
    pytest.importorskip("sensirion_shdlc_sensorbridge")
    from struct import unpack

    trames = []

    class FauxPortSerie:
        def __init__(self, port, baudrate, additional_response_time=0.1):
            assert (port, baudrate) == ("FAKE0", 460800)
            self.alimente = set()

        description = "FAKE0@460800"
        is_open = True

        def transceive(self, adresse, cmd, data, timeout):
            data = bytes(data)
            trames.append((cmd, data))
            if cmd == 0xD0:                              # nom du produit
                return adresse, cmd, 0, b"SensorBridge\0"
            if cmd == 0x00:                              # tension du port
                assert data == bytes([0x00, 0x02])       # port ONE, 3,3 V
                return adresse, cmd, 0, b""
            if cmd == 0x01:                              # alimentation
                if data[1]:
                    self.alimente.add(data[0])
                return adresse, cmd, 0, b""
            assert cmd == 0x11 and data[0] == 0x00       # 1re trame I²C
            port, i2c = data[1], data[2]
            n_tx, n_rx, _ = unpack(">III", data[3:15])
            tx = list(data[15:15 + n_tx])
            if port not in self.alimente or i2c != 0x44:
                return adresse, cmd, 0x23, b""           # erreur I²C (NACK)
            assert n_rx == 6
            if tx == [CMD_NUMERO_SERIE]:
                return adresse, cmd, 0, _mots(0x0A0B, 0x0C0D)
            return adresse, cmd, 0, _reponse_mesure(22.5, 38.0)

        def close(self):
            pass

    monkeypatch.setattr(driver, "ShdlcSerialPort", FauxPortSerie)
    c = Sht45SensorBridge({"port_bridge": "ONE"}, lister_ports=lambda: ["FAKE0"])
    c.connecter()
    m = c.mesurer()
    assert (m.temperature_c, m.humidite_pct) == pytest.approx((22.5, 38.0),
                                                              abs=0.01)
    assert c.numero_serie == 0x0A0B0C0D
    assert [t[0] for t in trames[:4]] == [0xD0, 0x00, 0x01, 0x11]
    c.arreter()


# ---- pilote : sources de l'air ----------------------------------------------

def _banc(monkeypatch, tmp_path, temperature=None, humidite=None, **air):
    from banc_simule import BancSimule
    cfg = {}
    if temperature:
        cfg["temperature"] = temperature
    if humidite:
        cfg["humidite"] = humidite
    b = BancSimule(monkeypatch, tmp_path, caracterise=True, cfg_sur=cfg)
    b.air.bruit_t = b.air.bruit_hr = 0.0
    for k, v in air.items():
        setattr(b.air, k, v)
    return b


def test_humidite_mesuree_utilisee_dans_les_calculs(monkeypatch, tmp_path):
    b = _banc(monkeypatch, tmp_path, humidite={"source": "sht45",
                                               "offset_sht45_pct": 2.0},
              humidite_pct=30.0)
    p = b.p
    b.cycles(1.0, verifier=False)
    e = p.etat
    assert e["humidite"] == pytest.approx(32.0) and e["humidite_source"] == "sht45"
    assert p.banc.fluide.humidite_relative == pytest.approx(32.0)
    assert e["alerte_sht45"] is None
    rho_mesuree = p.banc.fluide.rho(101325.0, 20.0)
    # saisie : 50 %HR (config du dépôt), air plus humide donc plus léger
    b.cfg["humidite"]["source"] = "manuelle"
    p.appliquer_config(b.cfg)
    b.cycles(1.0, verifier=False)
    assert e["humidite"] == 50.0 and e["humidite_source"] == "manuelle"
    assert p.banc.fluide.rho(101325.0, 20.0) < rho_mesuree


def test_humidite_perdue_repli_sur_la_saisie_sans_arret(monkeypatch, tmp_path):
    """Humidité du SHT45, température de la PT100 : SHT45 perdu, pas
    d'arrêt."""
    b = _banc(monkeypatch, tmp_path, temperature={"source": "pt100"},
              humidite={"source": "sht45"}, humidite_pct=30.0)
    p = b.p
    b.consigne(6000, "veine")
    assert b.jusqu_a_convergence(200) is not None
    b.air.debrancher()
    b.cycles(5.0, verifier=False)
    assert p.etat["humidite"] == 30.0                # dernière mesure tenue
    b.cycles(p.sht45_maintien_s, verifier=False)
    e = p.etat
    assert e["humidite"] == 50.0 and e["humidite_source"] == "manuelle"
    assert "SHT45 indisponible" in e["alerte_sht45"]
    assert "humidité saisie (50 %HR)" in e["alerte_sht45"]
    assert not e.get("arret_securite") and p.regulation_active
    b.air.rebrancher()
    b.cycles(1.0, verifier=False)
    assert e["humidite"] == 30.0 and e["alerte_sht45"] is None


def test_temperature_du_sht45_et_arret_si_perdue(monkeypatch, tmp_path):
    b = _banc(monkeypatch, tmp_path,
              temperature={"source": "sht45", "offset_sht45_c": -0.5})
    p = b.p
    b.sim.temperature = 24.0
    b.cycles(1.0, verifier=False)
    e = p.etat
    assert e["temperature"] == pytest.approx(23.5)
    assert e["temperature_source"] == "sht45"
    assert e["temperature_sht45"] == pytest.approx(23.5)
    assert e["temperature_pt100"] == pytest.approx(
        e["mesures_brutes"]["temperature"], abs=1e-9)
    b.consigne(6000, "veine")
    assert b.jusqu_a_convergence(200) is not None
    b.air.debrancher()
    b.cycles(p.sht45_maintien_s - 1.0, verifier=False)
    assert not e.get("arret_securite") and p.regulation_active
    b.cycles(2.0, verifier=False)
    assert e.get("arret_securite") and not p.regulation_active
    assert "SHT45 indisponible" in e["arret_motif"]
    assert e["u_variateur"] == 0.0


def test_temperature_sht45_absente_mise_en_route_refusee(monkeypatch, tmp_path):
    from core.apprentissage import Apprentissage
    b = _banc(monkeypatch, tmp_path, temperature={"source": "sht45"})
    p = b.p
    b.air.debrancher()
    b.cycles(p.sht45_maintien_s + 1.0, verifier=False)
    e = p.etat
    assert "consignes refusées" in e["alerte_sht45"]
    assert not e.get("arret_securite")                # banc à l'arrêt
    assert e["temperature"] == pytest.approx(        # affichage : PT100
        e["mesures_brutes"]["temperature"])
    assert e["temperature_source"] == "pt100"
    p.definir_consigne(6000, "veine")
    assert "SHT45" in p.demarrer_regulation()
    assert not p.regulation_active
    assert "SHT45" in p.mode_manuel_tension(2.0)
    assert "SHT45" in p.demarrer_sequence([(60, 3000)], "veine")
    app = Apprentissage(p, b.cfg, tmp_path / "config.json")
    assert "SHT45" in app.demarrer("indiciel")
    b.air.rebrancher()
    b.cycles(1.0, verifier=False)
    assert p.demarrer_regulation() is None and e["alerte_sht45"] is None


def test_mesure_invraisemblable_rejetee(monkeypatch, tmp_path):
    b = _banc(monkeypatch, tmp_path, humidite={"source": "sht45"})
    b.air._temperature = lambda: 150.0               # hors bornes PT100
    b.cycles(1.0, verifier=False)
    e = b.p.etat
    assert e["humidite_source"] == "manuelle"
    assert "invraisemblable" in e["alerte_sht45"]


def test_sans_capteur_installe(monkeypatch, tmp_path):
    """Pilote sans SHT45 (sht45.actif = false) : humidité saisie, alerte
    seulement si une source « sht45 » a été choisie."""
    from banc_simule import BancSimule
    b = BancSimule(monkeypatch, tmp_path, caracterise=True)
    b.p.capteur_air = None
    b.cfg["humidite"]["source"] = "manuelle"
    b.cfg["temperature"]["source"] = "pt100"
    b.p.appliquer_config(b.cfg)
    b.cycles(1.0, verifier=False)
    e = b.p.etat
    assert e["humidite_source"] == "manuelle" and e["alerte_sht45"] is None
    assert e["temperature_sht45"] is None
    b.cfg["humidite"]["source"] = "sht45"
    b.p.appliquer_config(b.cfg)
    b.cycles(1.0, verifier=False)
    assert "capteur désactivé" in e["alerte_sht45"]


def test_par_defaut_temperature_et_humidite_du_sht45(monkeypatch, tmp_path):
    """Sans choix de l'opérateur (clés absentes, config.json antérieur au
    SHT45) comme avec le config.json du dépôt : T et HR du SHT45 ; la PT100
    reste lue pour comparaison."""
    import json
    from pathlib import Path
    from core.banc import Banc
    from core.pilote import Pilote
    depot = json.loads((Path(__file__).resolve().parent.parent / "config.json")
                       .read_text(encoding="utf-8"))
    assert depot["temperature"]["source"] == "sht45"
    assert depot["humidite"]["source"] == "sht45"
    ancien = json.loads(json.dumps(depot))
    ancien["temperature"].pop("source")
    ancien.pop("humidite")
    for cfg in (depot, ancien):
        p = Pilote(Banc(cfg), object(), cfg, Sht45Simule())
        assert (p.t_source, p.hr_source) == ("sht45", "sht45")
    # sans capteur installé (sht45.actif = false), défaut PT100 et humidité
    # saisie : un banc sans SEK-SensorBridge reste utilisable
    p = Pilote(Banc(ancien), object(), ancien)
    assert (p.t_source, p.hr_source) == ("pt100", "manuelle")
    # choix explicite du SHT45 sans capteur : respecté, motif explicite
    p = Pilote(Banc(depot), object(), depot)
    assert p.t_source == "sht45"
    assert "sht45.actif = false" in p.motif_temperature_indisponible()
    b = _banc(monkeypatch, tmp_path, humidite_pct=35.0)
    b.sim.temperature = 23.0
    b.cycles(1.0, verifier=False)
    e = b.p.etat
    assert (e["temperature_source"], e["humidite_source"]) == ("sht45", "sht45")
    assert (e["temperature"], e["humidite"]) == pytest.approx((23.0, 35.0))
    assert e["temperature_pt100"] is not None


def test_source_d_humidite_inconnue_signalee(caplog):
    """Faute de frappe (« saisie ») : défaut du banc, avec un avertissement
    dans le log (elle passait sans trace)."""
    import json
    import logging
    from pathlib import Path
    from core.banc import Banc
    from core.pilote import Pilote
    cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json")
                     .read_text(encoding="utf-8"))
    cfg["humidite"]["source"] = "saisie"
    with caplog.at_level(logging.WARNING):
        p = Pilote(Banc(cfg), object(), cfg, Sht45Simule())
    assert p.hr_source == "sht45"
    assert "humidite.source = 'saisie' inconnue" in caplog.text


def test_apprentissage_interrompu_si_le_ventilateur_est_refuse(monkeypatch,
                                                               tmp_path):
    """SHT45 perdu entre deux phases (ventilateur à 0 V, donc sans arrêt de
    sécurité) : la commande suivante du ventilateur est refusée, et la
    séquence s'arrête au lieu de continuer ventilateur arrêté."""
    from core.apprentissage import Apprentissage
    b = _banc(monkeypatch, tmp_path, temperature={"source": "sht45"})
    p = b.p
    app = Apprentissage(p, b.cfg, tmp_path / "config.json")
    app._arrets_debut = p.arrets
    app._variateur(2.0)
    assert p.tension_manuelle == 2.0 and not app._interrompu()
    app._variateur(0.0)
    b.air.debrancher()
    b.cycles(p.sht45_maintien_s + 1.0, verifier=False)
    assert not p.etat.get("arret_securite")          # ventilateur à 0 V
    app._variateur(3.0)
    assert app._interrompu()
    assert app.etat["statut"].startswith("interrompu : mode manuel refusé")
    assert "SHT45" in app.etat["statut"]
    assert p.tension_manuelle == 0.0
