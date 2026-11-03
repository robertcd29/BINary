"""Modulul de rutare apelat de backend.

Intrare (formatul din tabela trashes a backend-ului):
    bins = [{"id": "TM-001", "lat": 45.74, "lng": 21.23, "umplere": 73,
             "capacitate_l": 1100}]          # capacitate_l e optional
    history = {"TM-001": [{"t_h": -2, "level": 0.69}, ...]}   # optional

Iesire: rute cu opriri identificate prin id ("depot" sau id-ul pubelei/alertei).
"""
import numpy as np
import networkx as nx
import osmnx as ox
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from predict import estimate

GRAPH_FILE = "timisoara_drive.graphml"
DEPOT = {"lat": 45.7547817, "lng": 21.2506284}   # punctul de plecare al camioanelor
NUM_TRUCKS = 3
TRUCK_CAPACITY_L = 8000
RESERVE = 0.10
MANDATORY_FILL = 0.8
HORIZON_H = 24
PENALTY_SCALE = 4000
DEFAULT_BIN_CAPACITY_L = 1100
MAX_SNAP_M = 300          # peste aceasta distanta, punctul e in afara hartii
SOLVER_SECONDS = 10

_G = ox.load_graphml(GRAPH_FILE)
_REV = _G.reverse(copy=False)
_IDS = np.array(list(_G.nodes))
_LATS = np.array([_G.nodes[i]["y"] for i in _IDS])
_LONS = np.array([_G.nodes[i]["x"] for i in _IDS])


def _snap(lat, lng):
    """Intersectia cea mai apropiata si distanta pana la ea, in metri."""
    dy = (_LATS - lat) * 111_000
    dx = (_LONS - lng) * 111_000 * np.cos(np.radians(lat))
    d2 = dx**2 + dy**2
    k = int(np.argmin(d2))
    return int(_IDS[k]), float(np.sqrt(d2[k]))


def _matrix(graph_nodes):
    n = len(graph_nodes)
    M = np.zeros((n, n), dtype=int)
    for i, src in enumerate(graph_nodes):
        lengths = nx.single_source_dijkstra_path_length(_G, src, weight="length")
        for j, dst in enumerate(graph_nodes):
            M[i, j] = round(lengths[dst])
    return M


def plan_routes(bins, history=None):
    history = history or {}
    warnings = []

    depot_node, _ = _snap(DEPOT["lat"], DEPOT["lng"])
    ids, graph_nodes = ["depot"], [depot_node]
    for b in bins:
        node, off = _snap(b["lat"], b["lng"])
        if off > MAX_SNAP_M:
            warnings.append(f"{b['id']} e la {off:.0f} m de cea mai apropiata strada din harta; ignorata")
            continue
        ids.append(b["id"])
        graph_nodes.append(node)
    used = [b for b in bins if b["id"] in ids]

    M = _matrix(graph_nodes)
    fills = [b["umplere"] / 100 for b in used]
    demands = [0] + [round(f * b.get("capacitate_l", DEFAULT_BIN_CAPACITY_L))
                     for f, b in zip(fills, used)]

    manager = pywrapcp.RoutingIndexManager(len(ids), NUM_TRUCKS, 0)
    routing = pywrapcp.RoutingModel(manager)
    transit = routing.RegisterTransitCallback(
        lambda a, b: int(M[manager.IndexToNode(a)][manager.IndexToNode(b)]))
    routing.SetArcCostEvaluatorOfAllVehicles(transit)
    demand_idx = routing.RegisterUnaryTransitCallback(
        lambda a: demands[manager.IndexToNode(a)])
    routing.AddDimensionWithVehicleCapacity(
        demand_idx, 0, [int(TRUCK_CAPACITY_L * (1 - RESERVE))] * NUM_TRUCKS, True, "Capacity")

    predictions = {}
    for node, (b, fill) in enumerate(zip(used, fills), start=1):
        rate, hours_to_full = estimate(history.get(b["id"], []))
        predictions[b["id"]] = {
            "hours_to_full": None if hours_to_full is None else round(hours_to_full, 1),
            "predicted_fill_24h": round(min(1.0, fill + (rate or 0) * HORIZON_H), 2),
        }
        will_overflow = hours_to_full is not None and hours_to_full <= HORIZON_H
        if fill >= MANDATORY_FILL or will_overflow:
            continue
        routing.AddDisjunction([manager.NodeToIndex(node)], int(PENALTY_SCALE * fill**2))

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromSeconds(SOLVER_SECONDS)
    solution = routing.SolveWithParameters(params)
    if solution is None:
        return {"error": "Nicio solutie: prea mult gunoi obligatoriu pentru capacitatea camioanelor",
                "warnings": warnings}

    routes, total, visited = [], 0, set()
    for v in range(NUM_TRUCKS):
        idx, stops, dist, load = routing.Start(v), [], 0, 0
        while not routing.IsEnd(idx):
            node = manager.IndexToNode(idx)
            stops.append(ids[node])
            visited.add(node)
            load += demands[node]
            nxt = solution.Value(routing.NextVar(idx))
            dist += routing.GetArcCostForVehicle(idx, nxt, v)
            idx = nxt
        stops.append("depot")
        total += dist
        routes.append({"truck": v, "stops": stops, "distance_m": dist, "load_l": load})

    return {
        "total_distance_m": total,
        "routes": routes,
        "skipped": [ids[i] for i in range(1, len(ids)) if i not in visited],
        "predictions": predictions,
        "warnings": warnings,
        "_nodes": dict(zip(ids, graph_nodes)),   # intern: id -> nod din graf, pentru alerte
    }


def add_alert(plan, alert):
    """alert = {"id": "AL-1", "lat": ..., "lng": ..., "volum_l": 500}. Modifica planul pe loc."""
    x, off = _snap(alert["lat"], alert["lng"])
    if off > MAX_SNAP_M:
        return {"error": f"Alerta e la {off:.0f} m de harta"}
    to_x = nx.single_source_dijkstra_path_length(_REV, x, weight="length")
    from_x = nx.single_source_dijkstra_path_length(_G, x, weight="length")
    nodes = plan["_nodes"]

    best = None
    for r in plan["routes"]:
        if r["load_l"] + alert["volum_l"] > TRUCK_CAPACITY_L:
            continue
        s = r["stops"]
        for k in range(len(s) - 1):
            a, b = nodes[s[k]], nodes[s[k + 1]]
            ab = nx.shortest_path_length(_G, a, b, weight="length")
            extra = to_x[a] + from_x[b] - ab
            if best is None or extra < best[0]:
                best = (extra, r, k + 1)
    if best is None:
        return {"error": "Niciun camion nu are capacitate libera"}

    extra, route, pos = best
    dedicated = len(route["stops"]) == 2
    route["stops"].insert(pos, alert["id"])
    route["distance_m"] += int(extra)
    route["load_l"] += alert["volum_l"]
    plan["total_distance_m"] += int(extra)
    nodes[alert["id"]] = x
    return {"alert": alert["id"], "truck": route["truck"], "extra_m": int(extra),
            "dedicated_trip": dedicated}
