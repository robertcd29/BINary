"""Test al modulului de rutare, cu datele in formatul backend-ului."""
import json
import routing_engine

with open("bins.json") as f:
    data = json.load(f)
with open("history.json") as f:
    history = json.load(f)

# conversie din formatul nostru de simulare in formatul tabelei trashes
bins = [{"id": b["id"], "lat": b["lat"], "lng": b["lon"],
         "umplere": round(b["fill_level"] * 100), "capacitate_l": b["capacity_l"]}
        for b in data["bins"]]
# cele 3 pubele de test din server.py
bins += [{"id": "TM-001", "lat": 45.7489, "lng": 21.2087, "umplere": 73},
         {"id": "TM-002", "lat": 45.7537, "lng": 21.2257, "umplere": 41},
         {"id": "TM-003", "lat": 45.7601, "lng": 21.2290, "umplere": 90}]

plan = routing_engine.plan_routes(bins, history, compare_baseline=True)
for w in plan["warnings"]:
    print("ATENTIE:", w)
for r in plan["routes"]:
    print(f"Camion {r['truck']}: {len(r['stops']) - 2} opriri, {r['distance_m'] / 1000:.1f} km, {r['load_l']} L")
print("Metrici:", plan["metrics"])

print(routing_engine.add_alert(plan, {"id": "AL-1", "lat": 45.7475, "lng": 21.2395, "volum_l": 500}))

plan.pop("_nodes")
with open("example_output.json", "w") as f:
    json.dump(plan, f, indent=2)
print("Exemplu de raspuns salvat in example_output.json")
