"""Verificare vizuala: deseneaza rutele din example_output.json pe harta."""
import json
import matplotlib.pyplot as plt
import osmnx as ox

G = ox.load_graphml("timisoara_drive.graphml")
with open("example_output.json") as f:
    plan = json.load(f)

fig, ax = ox.plot_graph(G, node_size=0, edge_color="#444", show=False, close=False)
colors = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4"]
for r in plan["routes"]:
    if len(r["stops"]) <= 2:
        continue                      # camion ramas in depozit
    lats, lngs = zip(*r["geometry"])
    c = colors[r["truck"] % len(colors)]
    ax.plot(lngs, lats, color=c, linewidth=2.5, alpha=0.85,
            label=f"Camion {r['truck']}: {r['distance_m'] / 1000:.1f} km")
ax.legend(loc="lower left", facecolor="white")
m = plan["metrics"]
title = f"{m['total_km']} km, {m['bins_collected']} colectate, {m['bins_skipped']} sarite"
if "saving_pct" in m:
    title += f", economie {m['saving_pct']}% fata de baseline"
ax.set_title(title, color="white")
plt.show()
