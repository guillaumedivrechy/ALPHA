# -*- coding: utf-8 -*-
"""Tests unitaires pour le module air_humide.

Valeurs de référence issues du fichier Excel fourni par l'utilisateur.
Tolérance relative : 1e-6 (0.0001%).
"""

import pytest
from core.air_humide import calculer_proprietes_air_humide


class TestProprietesAirHumide:
    """Tests avec valeurs de référence Excel."""

    def test_air_sec_20degC(self):
        """Cas 1 : Air sec à 20°C, 101355 Pa, HR=0%."""
        P = 101355.0
        t = 20.0
        HR = 0.0

        resultat = calculer_proprietes_air_humide(P, t, HR)

        # Valeurs de référence Excel
        assert resultat["Pvs"] == pytest.approx(2355.0014981235, rel=1e-6)
        assert resultat["x"] == pytest.approx(0.0, abs=1e-10)  # exactement 0
        assert resultat["v"] == pytest.approx(0.8297782914706, rel=1e-6)
        assert resultat["rho"] == pytest.approx(1.2051411928694, rel=1e-6)
        assert resultat["mu"] == pytest.approx(1.806e-5, rel=1e-6)
        assert resultat["nu"] == pytest.approx(1.4985795943959e-5, rel=1e-6)
        assert resultat["h"] == pytest.approx(20.12, rel=1e-6)

    def test_air_50pct_humidite_20degC(self):
        """Cas 2 : Air 50% HR à 20°C, 101355 Pa."""
        P = 101355.0
        t = 20.0
        HR = 0.5

        resultat = calculer_proprietes_air_humide(P, t, HR)

        # Valeurs de référence Excel
        assert resultat["Pvs"] == pytest.approx(2355.0014981235, rel=1e-6)
        assert resultat["x"] == pytest.approx(0.0073110775513, rel=1e-6)
        assert resultat["v"] == pytest.approx(0.8395316249743, rel=1e-6)
        assert resultat["rho"] == pytest.approx(1.1998488771428, rel=1e-6)
        assert resultat["mu"] == pytest.approx(1.806e-5, rel=1e-6)
        assert resultat["nu"] == pytest.approx(1.5051895571221e-5, rel=1e-6)
        assert resultat["h"] == pytest.approx(38.6706310253319, rel=1e-6)

    def test_temperature_negative(self):
        """Cas 3 : Température négative (t < 0) — branche glace."""
        P = 101355.0
        t = -10.0
        HR = 0.3

        resultat = calculer_proprietes_air_humide(P, t, HR)

        # Vérifier que Pvs utilise la formule avec 9.756 et 272.7
        # Pvs = 10^(9.756*(-10)/(272.7-10) + 2.7877)
        #     = 10^(-97.56/262.7 + 2.7877)
        #     = 10^(-0.37140... + 2.7877)
        #     = 10^2.41629...
        #     ≈ 260.77 Pa
        expected_Pvs = 10 ** (9.756 * (-10) / (272.7 + (-10)) + 2.7877)
        assert resultat["Pvs"] == pytest.approx(expected_Pvs, rel=1e-6)

        # Vérifier que les autres grandeurs sont cohérentes
        assert resultat["Pv"] == pytest.approx(HR * expected_Pvs, rel=1e-6)
        assert resultat["rho"] > 0  # masse volumique positive
        assert resultat["mu"] > 0  # viscosité positive
        assert resultat["nu"] > 0  # viscosité cinématique positive

    def test_air_100pct_humidite(self):
        """Cas 4 : Air saturé (HR = 100%)."""
        P = 101355.0
        t = 25.0
        HR = 1.0

        resultat = calculer_proprietes_air_humide(P, t, HR)

        # À saturation, Pv = Pvs
        assert resultat["Pv"] == pytest.approx(resultat["Pvs"], rel=1e-10)
        assert resultat["x"] > 0  # humidité spécifique non nulle
        assert resultat["rho"] < 1.2  # air humide moins dense que air sec


class TestValidationEntrees:
    """Tests de validation des paramètres d'entrée."""

    def test_pression_negative(self):
        """P ≤ 0 doit lever une ValueError."""
        with pytest.raises(ValueError, match="pression doit être > 0"):
            calculer_proprietes_air_humide(P=0, t=20, HR=0.5)

        with pytest.raises(ValueError, match="pression doit être > 0"):
            calculer_proprietes_air_humide(P=-100, t=20, HR=0.5)

    def test_humidite_hors_plage(self):
        """HR hors [0, 1] doit lever une ValueError."""
        with pytest.raises(ValueError, match="humidité relative doit être entre 0 et 1"):
            calculer_proprietes_air_humide(P=101355, t=20, HR=-0.1)

        with pytest.raises(ValueError, match="humidité relative doit être entre 0 et 1"):
            calculer_proprietes_air_humide(P=101355, t=20, HR=1.5)

        with pytest.raises(ValueError, match="humidité relative doit être entre 0 et 1"):
            calculer_proprietes_air_humide(P=101355, t=20, HR=101)  # en %

    def test_valeur_non_numerique(self):
        """Valeurs non numériques doivent lever une ValueError."""
        with pytest.raises(ValueError, match="doivent être des nombres"):
            calculer_proprietes_air_humide(P="101355", t=20, HR=0.5)

        with pytest.raises(ValueError, match="doivent être des nombres"):
            calculer_proprietes_air_humide(P=101355, t="20", HR=0.5)

        with pytest.raises(ValueError, match="doivent être des nombres"):
            calculer_proprietes_air_humide(P=101355, t=20, HR="0.5")


class TestCoherencePhysique:
    """Tests de cohérence physique des résultats."""

    def test_coherence_rho_v(self):
        """ρ = (1+x)/v doit être vérifié."""
        P = 101355.0
        t = 20.0
        HR = 0.5

        r = calculer_proprietes_air_humide(P, t, HR)
        rho_calcule = (1 + r["x"]) / r["v"]

        assert r["rho"] == pytest.approx(rho_calcule, rel=1e-10)

    def test_coherence_nu_mu_rho(self):
        """ν = μ/ρ doit être vérifié."""
        P = 101355.0
        t = 20.0
        HR = 0.5

        r = calculer_proprietes_air_humide(P, t, HR)
        nu_calcule = r["mu"] / r["rho"]

        assert r["nu"] == pytest.approx(nu_calcule, rel=1e-10)

    def test_coherence_Pv_HR_Pvs(self):
        """Pv = HR · Pvs doit être vérifié."""
        P = 101355.0
        t = 20.0
        HR = 0.5

        r = calculer_proprietes_air_humide(P, t, HR)
        Pv_calcule = HR * r["Pvs"]

        assert r["Pv"] == pytest.approx(Pv_calcule, rel=1e-10)

    def test_humidite_croissante_densite_decroissante(self):
        """Plus d'humidité → masse volumique plus faible (vapeur plus légère)."""
        P = 101355.0
        t = 20.0

        rho_sec = calculer_proprietes_air_humide(P, t, HR=0.0)["rho"]
        rho_50 = calculer_proprietes_air_humide(P, t, HR=0.5)["rho"]
        rho_sat = calculer_proprietes_air_humide(P, t, HR=1.0)["rho"]

        assert rho_sec > rho_50 > rho_sat

    def test_protection_saturation(self):
        """Test protection contre saturation (P ≤ Pv × 1.01)."""
        # À 20°C, Pvs ≈ 2355 Pa
        # Seuil protection : 2355 × 1.01 = 2378.55 Pa
        # Test avec P = 2370 Pa (< seuil) → doit lever ValueError
        P = 2370.0  # Pression juste au-dessus de Pvs mais < seuil
        t = 20.0
        HR = 1.0  # Saturation (Pv = Pvs = 2355 Pa)

        with pytest.raises(ValueError, match="trop proche de la saturation"):
            calculer_proprietes_air_humide(P, t, HR)

    def test_protection_pression_nulle(self):
        """Test protection contre pression nulle ou négative."""
        with pytest.raises(ValueError, match="doit être > 0"):
            calculer_proprietes_air_humide(P=0.0, t=20.0, HR=0.5)

        with pytest.raises(ValueError, match="doit être > 0"):
            calculer_proprietes_air_humide(P=-101325.0, t=20.0, HR=0.5)

    def test_saturation_limite_acceptable(self):
        """Test cas limite : HR=100% avec pression normale (doit passer)."""
        P = 101355.0
        t = 20.0
        HR = 1.0  # 100% saturation

        # À pression normale, P >> Pv donc pas de problème
        r = calculer_proprietes_air_humide(P, t, HR)

        # Vérifier que les calculs sont cohérents
        assert r["Pv"] == pytest.approx(r["Pvs"], rel=1e-6)
        assert r["rho"] > 0
        assert r["nu"] > 0
