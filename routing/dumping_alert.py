import json
import sys
import numpy as np
import networkx as nx
import osmnx as ox

TRUCK_CAPACITY_L = 8000

def nearest_node(G, lat, lon):
    """Nodul de strada cel mai apropiat de coordonatele alertei."""
    ids = np.array(list(G.nodes))
    lats = np.array([G.nodes[i]["y"] for i in ids])
    lons = np.array([G.nodes[i]["x"] for i in ids])
    # distanta aproximativa in metri, suficient de precisa pe cativa km
    dy = (lats - lat) * 111_000
    dx = (lons - lon) * 111_000 * np.cos(np.radians(lat))
    return int(ids[np.argmin(dx**2 + dy**2)])

def insert_alert(G, nodes, M, plan, alert):
    """Insereaza alerta in ruta camionului unde costa cei mai putini metri in plus.

    O oprire din ruta e fie un index din matrice (0 = depozit, 1..60 = pubele),
    fie un id de alerta ("alert_0", "alert_1"...), al carei nod e in plan["alerts"].
    """
    alert_nodes = {a["id"]: a["node"] for a in plan.get("alerts", [])}
    graph_node = lambda s: alert_nodes[s] if isinstance(s, str) else nodes[s]

    x = nearest_node(G, alert["lat"], alert["lon"])
    to_x = nx.single_source_dijkstra_path_length(G.reverse(copy=False), x, weight="length")
    from_x = nx.single_source_dijkstra_path_length(G, x, weight="length")

    def dist(a, b):
        if isinstance(a, int) and isinstance(b, int):
            return M[a][b]
        return nx.shortest_path_length(G, graph_node(a), graph_node(b), weight="length")

    best = None
    for r in plan["routes"]:
        if r["load_l"] + alert["volume_l"] > TRUCK_CAPACITY_L:
            continue                     # camionul nu mai are loc
        stops = r["stops"]
        for k in range(len(stops) - 1):
            a, b = stops[k], stops[k + 1]
            extra = to_x[graph_node(a)] + from_x[graph_node(b)] - dist(a, b)
            if best is None or extra < best[0]:
                best = (extra, r, k + 1)

    if best is None:
        return None
    extra, route, pos = best
    alert_id = f"alert_{len(alert_nodes)}"
    was_empty = len(route["stops"]) == 2
    route["stops"].insert(pos, alert_id)
    route["distance_m"] += int(extra)
    route["load_l"] += alert["volume_l"]
    result = {"id": alert_id, **alert, "node": x, "truck": route["truck"],
              "extra_m": int(extra), "dedicated_trip": was_empty}
    plan.setdefault("alerts", []).append(result)
    return result

if __name__ == "__main__":
    # utilizare: python dumping_alert.py <lat> <lon> <volum_litri>
    lat = float(sys.argv[1]) if len(sys.argv) > 1 else 45.7475
    lon = float(sys.argv[2]) if len(sys.argv) > 2 else 21.2395
    volume = int(sys.argv[3]) if len(sys.argv) > 3 else 500

    G = ox.load_graphml("timisoara_drive.graphml")
    with open("bins.json") as f:
        data = json.load(f)
    # pornim de la planul cu alerte, daca exista deja, altfel de la planul zilei
    try:
        with open("routes_with_alerts.json") as f:
            plan = json.load(f)
    except FileNotFoundError:
        with open("smart_routes.json") as f:
            plan = json.load(f)
    nodes = [data["depot"]["node"]] + [b["node"] for b in data["bins"]]
    M = np.load("distance_matrix.npy")

    result = insert_alert(G, nodes, M, plan, {"lat": lat, "lon": lon, "volume_l": volume})
    if result is None:
        print("Niciun camion nu are capacitate libera: e nevoie de o cursa separata.")
        sys.exit(1)

    note = " (pleaca din depozit special pentru ea)" if result["dedicated_trip"] else ""
    print(f"{result['id']} preluata de camionul {result['truck']}{note}, "
          f"+{result['extra_m'] / 1000:.2f} km")
    with open("routes_with_alerts.json", "w") as f:
        json.dump(plan, f, indent=2)
