"""Scenariul de demo, in trei momente, identic la fiecare rulare.

1. Planul de dimineata: pubela legata de senzorul fizic e aproape goala, deci e sarita.
2. Senzorul raporteaza ca pubela a trecut de 80%: e inserata in ruta unui camion aflat pe drum.
3. Un cetatean raporteaza o depozitare ilegala langa o ruta: alerta e preluata din mers.

Pubela legata de senzorul fizic e aleasa automat; numele ei apare la pasul 1.

Fiecare moment e salvat in demo_step<N>.json, ca dashboard-ul sa le poata afisa pe rand.
"""
import json
import routing_engine as engine
from explain import explain_plan


with open("bins.json") as f:
    data = json.load(f)
with open("history.json") as f:
    history = json.load(f)
bins = [{"id": b["id"], "lat": b["lat"], "lng": b["lon"],
         "umplere": round(b["fill_level"] * 100), "capacitate_l": b["capacity_l"]}
        for b in data["bins"]]


def save(step, plan):
    out = {k: v for k, v in plan.items() if k != "_nodes"}
    with open(f"demo_step{step}.json", "w") as f:
        json.dump(out, f, indent=2)


def summary(plan):
    m = plan["metrics"]
    line = f"{m['total_km']} km, {m['bins_collected']} colectate, {m['bins_skipped']} sarite"
    if "saving_pct" in m:
        line += f", economie {m['saving_pct']}%"
    return line


def truck_of(plan, stop_id):
    return next((r["truck"] for r in plan["routes"] if stop_id in r["stops"]), None)


# 1. planul de dimineata
plan = engine.plan_routes(bins, history, compare_baseline=True)
save(1, plan)
# pubela pe care o legam de senzorul fizic: dintre cele sarite, cea mai ieftin de adaugat,
# luand in calcul si volumul ei cand e plina, ca in demo ruta sa se schimbe vizibil,
# dar cu un ocol mic si credibil
by_id = {b["id"]: b for b in bins}
def demo_cost(s):
    volume = round(0.85 * by_id[s].get("capacitate_l", engine.DEFAULT_BIN_CAPACITY_L))
    cost = engine.insertion_cost(plan, by_id[s]["lat"], by_id[s]["lng"], volume)
    return cost if cost is not None else 10**9
PHYSICAL_BIN = min(plan["skipped"], key=demo_cost)
print("1. Planul de dimineata:", summary(plan))
print(f"   {PHYSICAL_BIN}: {'sarita' if PHYSICAL_BIN in plan['skipped'] else 'colectata'}")

# 2. senzorul fizic trece de prag, in timpul zilei: camioanele sunt pe drum,
#    deci pubela e inserata in ruta existenta, nu se reface tot planul
b = by_id[PHYSICAL_BIN]
b["umplere"] = 85
volume = round(0.85 * b.get("capacitate_l", engine.DEFAULT_BIN_CAPACITY_L))
result = engine.add_alert(plan, {"id": PHYSICAL_BIN, "lat": b["lat"], "lng": b["lng"],
                                 "volum_l": volume}, kind="bin")
save(2, plan)
print(f"\n2. Senzorul raporteaza {PHYSICAL_BIN} la 85%:", summary(plan))
print(f"   adaugata pe ruta camionului {result.get('truck')}, +{result.get('extra_m')} m")

# 3. raportare de la cetatean, la cateva sute de metri de o ruta (nu exact pe traseu)
def nearby_point(plan):
    for r in sorted(plan["routes"], key=lambda r: r["load_l"]):
        geo = r["geometry"]
        for lat, lng in geo[len(geo) // 3::5]:
            for dlat, dlng in ((0.0015, 0), (-0.0015, 0), (0, 0.002), (0, -0.002)):
                cost = engine.insertion_cost(plan, lat + dlat, lng + dlng, 500)
                if cost is not None and 100 <= cost <= 600:
                    return lat + dlat, lng + dlng
    return geo[len(geo) // 2]

lat, lng = nearby_point(plan)
result = engine.add_alert(plan, {"id": "CIT-1", "lat": lat, "lng": lng, "marime": "medie"})
save(3, plan)
print(f"\n3. Cetatean raporteaza depozitare ilegala (medie, {result['volum_l']} L):")
if result["status"] == "assigned":
    kind = "cursa separata" if result["dedicated_trip"] else "din mers, pe ruta existenta"
    print(f"   preluata de camionul {result['truck']}, {kind}, +{result['extra_m']} m")
else:
    print(f"   {result['status']}: {result.get('reason')}")

print("\nExplicatie pentru dispecer:\n" + explain_plan(plan, bins))
