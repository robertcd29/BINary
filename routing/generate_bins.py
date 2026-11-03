import json
import random
import osmnx as ox
import matplotlib.pyplot as plt

random.seed(42)
N_BINS = 60

G = ox.load_graphml("timisoara_drive.graphml")
chosen = random.sample(list(G.nodes), N_BINS + 1)

depot_node = chosen[0]
depot = {
    "id": "depot",
    "node": depot_node,
    "lat": G.nodes[depot_node]["y"],
    "lon": G.nodes[depot_node]["x"],
}

bins = []
for i, node in enumerate(chosen[1:]):
    bins.append({
        "id": f"bin_{i:03d}",
        "node": node,
        "lat": G.nodes[node]["y"],
        "lon": G.nodes[node]["x"],
        "capacity_l": random.choice([240, 1100]),
        "fill_level": round(random.uniform(0.1, 0.95), 2),
        "fill_rate_per_h": round(random.uniform(0.005, 0.04), 4),
        "physical": i < 3,
    })

with open("bins.json", "w") as f:
    json.dump({"depot": depot, "bins": bins}, f, indent=2)

print(f"Depozit: nodul {depot_node}")
print(f"{len(bins)} pubele salvate in bins.json")

# verificare vizuala: verde = goala, rosu = plina, albastru = depozit
fig, ax = ox.plot_graph(G, node_size=0, show=False, close=False)
sc = ax.scatter([b["lon"] for b in bins], [b["lat"] for b in bins],
                c=[b["fill_level"] for b in bins], cmap="RdYlGn_r",
                vmin=0, vmax=1, s=40, zorder=3)
ax.scatter(depot["lon"], depot["lat"], c="blue", s=120, marker="s", zorder=4)
plt.colorbar(sc, ax=ax, label="Nivel umplere")
plt.show()