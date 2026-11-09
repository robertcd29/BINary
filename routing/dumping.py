"""Deduplicarea alertelor de depozitare ilegala.

Detectia o face modelul Dariei; backend-ul trimite apoi coordonatele camerei. Pentru ca
modelul ruleaza pe fiecare poza, acelasi loc ajunge de multe ori, iar mai multi cetateni
pot raporta aceeasi gramada. Tot ce e la sub MERGE_RADIUS_M de o alerta deschisa se
lipeste de ea, ca sa nu trimitem doua camioane la acelasi loc.
"""
import math

MERGE_RADIUS_M = 50


def distance_m(lat1, lng1, lat2, lng2):
    """Distanta in linie dreapta (haversine), in metri."""
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def find_duplicate(open_alerts, lat, lng, radius=MERGE_RADIUS_M):
    """Cea mai apropiata alerta deschisa la mai putin de `radius` metri, sau None."""
    best = None
    for a in open_alerts:
        d = distance_m(lat, lng, a["lat"], a["lng"])
        if d <= radius and (best is None or d < best[0]):
            best = (d, a)
    return best[1] if best else None
