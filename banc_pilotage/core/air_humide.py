# -*- coding: utf-8 -*-
"""Propriétés de l'air humide.

Formules issues d'un fichier Excel de référence, reproduites exactement.
À utiliser en priorité sur les formules air sec (fluide.py) partout où
l'humidité relative est connue ou paramétrable.

Références :
- Pression de vapeur saturante : formules Magnus-Tetens
- Humidité spécifique : rapport de mélange
- Volume spécifique : équation d'état gaz parfait (air + vapeur)
- Enthalpie spécifique : somme chaleur sensible + chaleur latente
- Viscosité dynamique : corrélation empirique linéaire (ne dépend que de T)
"""



def calculer_proprietes_air_humide(P: float, t: float, HR: float) -> dict:
    """Calcule les propriétés thermodynamiques de l'air humide.

    Paramètres
    ----------
    P : float
        Pression de l'air [Pa]
    t : float
        Température de l'air [°C]
    HR : float
        Humidité relative [0–1], par exemple 0.5 pour 50%

    Retourne
    --------
    dict
        - Pvs : pression de vapeur saturante [Pa]
        - Pv : pression partielle de vapeur [Pa]
        - x : humidité spécifique [kg vapeur / kg air sec]
        - v : volume spécifique [m³/kg]
        - h : enthalpie spécifique [kJ/kg]
        - rho : masse volumique [kg/m³]
        - mu : viscosité dynamique [Pa·s]
        - nu : viscosité cinématique [m²/s]

    Lève
    ----
    ValueError
        Si P ≤ 0, HR hors [0, 1], ou valeurs non numériques
    """
    # Validation des entrées
    if not isinstance(P, (int, float)) or not isinstance(t, (int, float)) or not isinstance(HR, (int, float)):
        raise ValueError("P, t et HR doivent être des nombres")
    if P <= 0:
        raise ValueError(f"La pression doit être > 0 Pa (reçu {P})")
    if not (0 <= HR <= 1):
        raise ValueError(f"L'humidité relative doit être entre 0 et 1 (reçu {HR})")

    # 1. Pression de vapeur saturante (Pa)
    if t >= 0:
        # Formule pour t ≥ 0°C
        Pvs = 10 ** (7.625 * t / (241 + t) + 2.7877)
    else:
        # Formule pour t < 0°C (glace)
        Pvs = 10 ** (9.756 * t / (272.7 + t) + 2.7877)

    # 2. Pression partielle de vapeur (Pa)
    Pv = HR * Pvs

    # Protection contre saturation (division par zéro)
    # Marge de sécurité : 1% pour éviter P - Pv ≈ 0
    if P <= Pv * 1.01:
        raise ValueError(
            f"Pression {P:.0f} Pa trop proche de la saturation "
            f"(Pv = {Pv:.0f} Pa à {t:.1f}°C, HR={HR*100:.0f}%). "
            f"Risque de condensation, calculs invalides."
        )

    # 3. Humidité spécifique (kg vapeur / kg air sec)
    x = 0.622 * Pv / (P - Pv)

    # 4. Volume spécifique (m³/kg)
    v = 461.24 * (0.622 + x) * (t + 273.15) / P

    # 5. Enthalpie spécifique (kJ/kg)
    h = 2500.8 * x + (1.006 + 1.8266 * x) * t

    # 6. Masse volumique (kg/m³)
    rho = (1 + x) / v

    # 7. Viscosité dynamique (Pa·s) — ne dépend que de t
    mu = (17.1 + 0.048 * t) * 1e-6

    # 8. Viscosité cinématique (m²/s)
    nu = mu / rho

    return {
        "Pvs": Pvs,
        "Pv": Pv,
        "x": x,
        "v": v,
        "h": h,
        "rho": rho,
        "mu": mu,
        "nu": nu,
    }
