"""Genereaza ghenele simulate (SIM-01 ... SIM-60), raspandite pe toata harta, pe strazi reale."""
import json, math, random
import osmnx as ox

random.seed(14)
N = 60
G = ox.load_graphml("timisoara_drive.graphml")
TM = [(45.774718,21.2633061),(45.744593,21.267267),(45.7121271,21.198657),(45.77612,21.2135825),(45.76,21.197),(45.766,21.218),(45.779,21.226),(45.7785,21.2255),(45.778,21.207),(45.7588,21.2135),(45.746,21.224),(45.7482,21.2045),(45.748,21.204),(45.7485,21.205),(45.743,21.237),(45.755,21.245),(45.7585,21.224)]
def m(a, b):
    return math.hypot((a[0]-b[0])*111320, (a[1]-b[1])*111320*math.cos(math.radians(a[0])))
# doar strazile din zona ghenelor reale (cu putina margine), nu marginea hartii
lat0, lat1 = min(p[0] for p in TM) - .004, max(p[0] for p in TM) + .004
lng0, lng1 = min(p[1] for p in TM) - .006, max(p[1] for p in TM) + .006
noduri = [n for n, d in G.nodes(data=True) if lat0 <= d["y"] <= lat1 and lng0 <= d["x"] <= lng1]
random.shuffle(noduri)
ales = []
for n in noduri:
    p = (G.nodes[n]["y"], G.nodes[n]["x"])
    if all(m(p, q) > 350 for q in TM + [a["p"] for a in ales]):   # ghene raspandite, nu gramada
        ales.append({"n": n, "p": p})
    if len(ales) == N:
        break
def strada(n):
    for _, _, d in list(G.out_edges(n, data=True)) + list(G.in_edges(n, data=True)):
        nume = d.get("name")
        if nume:
            return nume[0] if isinstance(nume, list) else nume
    return None
bins = []
for i, a in enumerate(ales, 1):
    r = random.random()
    # cum arata un oras real dimineata: cele mai multe pe la jumatate, cateva pline
    fill = random.uniform(.86, .98) if r < .15 else random.uniform(.55, .85) if r < .45 else random.uniform(.05, .55)
    s = strada(a["n"])
    bins.append({"id": f"SIM-{i:02d}", "lat": round(a["p"][0], 7), "lon": round(a["p"][1], 7),
                 "capacity_l": random.choice([240, 1100, 1100]), "fill_level": round(fill, 2),
                 "nume": f"Ghena {s}" if s else f"Ghena SIM-{i:02d}", "adresa": s})
with open("sim_bins.json", "w") as f:
    json.dump({"bins": bins}, f, indent=1, ensure_ascii=False)
print(len(bins), "ghene simulate;", sum(b["fill_level"] >= .85 for b in bins), "peste 85%")
