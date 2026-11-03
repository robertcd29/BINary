import numpy as np

def estimate(readings, min_points=3):
    """Estimeaza rata de umplere (pe ora) si orele ramase pana la plin."""
    if len(readings) < min_points:
        return None, None
    t = np.array([r["t_h"] for r in readings])
    y = np.array([r["level"] for r in readings])
    rate, _ = np.polyfit(t, y, 1)           # dreapta y = rate * t + b
    rate = max(rate, 1e-4)                  # o pubela nu se goleste singura
    hours_to_full = max(0.0, (1.0 - y[-1]) / rate)
    return float(rate), float(hours_to_full)
