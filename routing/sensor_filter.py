"""Curata citirile senzorilor inainte sa ajunga la rutare.

Senzorii ultrasonici sunt zgomotosi si ESP32 trimite o citire la 2 secunde, deci:
- o singura citire nu e de incredere: folosim mediana ultimelor citiri;
- pragul de urgenta are histerezis, ca o pubela care oscileaza 79/81% sa nu fie
  inserata in ruta de mai multe ori;
- un senzor care nu raspunde sau tace de mult NU inseamna pubela goala. Pubela devine
  "estimata" (ultima valoare buna + rata de umplere) sau "necunoscuta" (vizitata, ca sa
  nu riscam o pubela plina pe care nu o vedem).
"""
import math
from statistics import median

WINDOW = 5                  # cate citiri intra in mediana (5 x 2 s = 10 s intarziere)
URGENT_ON = 80              # devine urgenta la 80%, la fel ca MANDATORY_FILL din motor
URGENT_OFF = 70             # redevine normala doar sub 70% (sau dupa golire)
STALE_MIN = 30              # dupa 30 de minute fara citire buna, senzorul e considerat mut
DEFAULT_RATE_PCT_H = 2.5    # rata de umplere cand nu avem istoric (media din simulare)
UNKNOWN_FILL = 80           # pubela fara nicio citire: o tratam ca obligatorie


def valid(value):
    """Valoarea in procente, sau None daca citirea e eroare de senzor (null, negativ, NaN)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or v < 0:
        return None
    return min(v, 100.0)


def smooth(values, window=WINDOW):
    """Mediana ultimelor `window` citiri valide (cele mai noi la final), sau None."""
    good = [v for v in (valid(x) for x in values) if v is not None]
    if not good:
        return None
    return float(median(good[-window:]))


def next_urgent(was_urgent, level, on=URGENT_ON, off=URGENT_OFF):
    """Starea de urgenta dupa o noua valoare filtrata."""
    if level is None:
        return was_urgent           # fara date noi nu schimbam starea
    if was_urgent:
        return level >= off
    return level >= on


def routing_fill(level, last_good_ts, last_any_ts, now, rate_pct_h=None, stale_min=STALE_MIN):
    """Ce umplere dam motorului de rutare pentru o pubela cu senzor.

    level         - ultima valoare filtrata buna (procente) sau None
    last_good_ts  - momentul ultimei citiri bune, last_any_ts - al ultimei citiri de orice fel
    Intoarce {"umplere", "sursa", "motiv"}; sursa e "senzor", "estimat" sau "necunoscut".
    """
    if level is not None and last_good_ts is not None:
        age_h = (now - last_good_ts).total_seconds() / 3600
        if age_h * 60 <= stale_min:
            return {"umplere": round(level), "sursa": "senzor", "motiv": None}
        rate = rate_pct_h if rate_pct_h is not None else DEFAULT_RATE_PCT_H
        estimated = min(100, level + rate * age_h)
        broken = last_any_ts is not None and (now - last_any_ts).total_seconds() / 60 <= stale_min
        reason = "senzor defect" if broken else "senzorul nu mai transmite"
        return {"umplere": round(estimated), "sursa": "estimat",
                "motiv": f"{reason} de {age_h:.1f} h; estimat din ultima valoare ({level:.0f}%)"}
    return {"umplere": UNKNOWN_FILL, "sursa": "necunoscut",
            "motiv": "nicio citire buna de la senzor; o vizitam ca sa nu ratam o pubela plina"}
