import json
import random

random.seed(7)
HOURS = 72          # ultimele 3 zile de masuratori, una pe ora
NOISE = 0.02        # eroarea senzorului ultrasonic (+/- 2%)

with open("bins.json") as f:
    data = json.load(f)

history = {}
for b in data["bins"]:
    rate, now = b["fill_rate_per_h"], b["fill_level"]
    # pubela a fost golita ultima data acum now/rate ore; nu avem date dinainte
    window = min(HOURS, int(now / rate))
    readings = []
    for h in range(-window, 1):
        level = now + rate * h + random.gauss(0, NOISE)
        readings.append({"t_h": h, "level": round(min(max(level, 0), 1), 3)})
    history[b["id"]] = readings

with open("history.json", "w") as f:
    json.dump(history, f)
print(f"Istoric generat pentru {len(history)} pubele in history.json")
