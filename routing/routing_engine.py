"""Modulul de rutare apelat de backend.

Intrare (formatul din tabela trashes a backend-ului):
    bins = [{"id": "TM-001", "lat": 45.74, "lng": 21.23, "umplere": 73,
             "capacitate_l": 1100}]          # capacitate_l e optional
    history = {"TM-001": [{"t_h": -2, "level": 0.69}, ...]}   # optional

Iesire: rute cu opriri identificate prin id ("depot" sau id-ul pubelei/alertei),
geometria fiecarei rute, predictii, metrici si avertismente. Functiile nu arunca
exceptii pentru date proaste: le raporteaza in "warnings", ca demo-ul sa nu pice.
"""
import os
from functools import lru_cache

import numpy as np
import networkx as nx
import osmnx as ox
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from predict import estimate

# cale relativa la acest fisier, ca modulul sa mearga si cand serverul porneste din radacina repo-ului
GRAPH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "timisoara_drive.graphml")
DEPOT = {"lat": 45.7547817, "lng": 21.2506284}   # punctul de plecare al camioanelor
NUM_TRUCKS = 3
TRUCK_CAPACITY_L = 8000
RESERVE = 0.10
MANDATORY_FILL = 0.8
HORIZON_H = 24
PENALTY_SCALE = 4000
MANDATORY_PENALTY = 10_000_000   # "obligatoriu": sarirea costa cat 10.000 km
DEFAULT_BIN_CAPACITY_L = 1100
MAX_SNAP_M = 300                 # peste aceasta distanta, punctul e in afara hartii
SOLVER_SECONDS = 3               # suficient pentru ~60 de pubele; 30 pentru cifre de pitch
BASELINE_SECONDS = 10            # referinta se calculeaza rar, deci isi permite mai mult timp
ALERT_SIZES_L = {"mica": 100, "medie": 500, "mare": 1500}   # raportari de la cetateni

_G = ox.load_graphml(GRAPH_FILE)
_REV = _G.reverse(copy=False)
_IDS = np.array(list(_G.nodes))
_LATS = np.array([_G.nodes[i]["y"] for i in _IDS])
_LONS = np.array([_G.nodes[i]["x"] for i in _IDS])
_baseline_cache = {}


# ---------- harta ----------

def _snap(lat, lng):
    """Intersectia cea mai apropiata si distanta pana la ea, in metri."""
    dy = (_LATS - lat) * 111_000
    dx = (_LONS - lng) * 111_000 * np.cos(np.radians(lat))
    d2 = dx**2 + dy**2
    k = int(np.argmin(d2))
    return int(_IDS[k]), float(np.sqrt(d2[k]))


@lru_cache(maxsize=512)
def _from(src):
    """Distantele si drumurile de la un nod spre toate celelalte (harta nu se schimba, deci se pot refolosi)."""
    return nx.single_source_dijkstra(_G, src, weight="length")


@lru_cache(maxsize=512)
def _to(dst):
    """Distantele de la toate nodurile spre un nod (graful inversat, din cauza sensurilor unice)."""
    return nx.single_source_dijkstra_path_length(_REV, dst, weight="length")


def _dist(a, b):
    return _from(a)[0][b]


def _matrix(graph_nodes):
    return np.array([[round(_dist(a, b)) for b in graph_nodes] for a in graph_nodes], dtype=int)


def _geometry(stop_nodes):
    """Coordonatele [lat, lng] ale strazilor parcurse intre opriri, pentru desenat pe harta."""
    coords = []
    for a, b in zip(stop_nodes, stop_nodes[1:]):
        path = _from(a)[1][b]
        pts = [[_G.nodes[n]["y"], _G.nodes[n]["x"]] for n in path]
        coords.extend(pts if not coords else pts[1:])
    return coords


# ---------- validare ----------

def _clean_bins(bins, warnings):
    """Pastreaza doar pubelele utilizabile; tot ce e ignorat apare in warnings."""
    clean, seen = [], set()
    for b in bins or []:
        bid = b.get("id")
        if bid in seen:
            warnings.append(f"{bid} apare de mai multe ori; folosim prima aparitie")
            continue
        try:
            lat, lng = float(b["lat"]), float(b["lng"])
            fill = float(b.get("umplere", 0))
        except (KeyError, TypeError, ValueError):
            warnings.append(f"{bid}: coordonate sau umplere lipsa ori invalide; ignorata")
            continue
        if not 0 <= fill <= 100:
            warnings.append(f"{bid}: umplere {fill} in afara 0-100; limitata")
            fill = min(max(fill, 0), 100)
        node, off = _snap(lat, lng)
        if off > MAX_SNAP_M:
            warnings.append(f"{bid} e la {off:.0f} m de cea mai apropiata strada din harta; ignorata")
            continue
        seen.add(bid)
        clean.append({**b, "umplere": fill, "_node": node})
    return clean


# ---------- solver ----------

def _solve(M, demands, penalties, capacity, seconds):
    """VRP cu capacitate. penalties = {index: penalitate}; fiecare pubela poate fi sarita contra penalitatii ei."""
    manager = pywrapcp.RoutingIndexManager(len(M), NUM_TRUCKS, 0)
    routing = pywrapcp.RoutingModel(manager)
    transit = routing.RegisterTransitCallback(
        lambda a, b: int(M[manager.IndexToNode(a)][manager.IndexToNode(b)]))
    routing.SetArcCostEvaluatorOfAllVehicles(transit)
    demand_idx = routing.RegisterUnaryTransitCallback(
        lambda a: demands[manager.IndexToNode(a)])
    routing.AddDimensionWithVehicleCapacity(
        demand_idx, 0, [capacity] * NUM_TRUCKS, True, "Capacity")
    for node, penalty in penalties.items():
        routing.AddDisjunction([manager.NodeToIndex(node)], penalty)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromMilliseconds(int(seconds * 1000))
    solution = routing.SolveWithParameters(params)
    if solution is None:
        return None

    routes, total, visited = [], 0, set()
    for v in range(NUM_TRUCKS):
        idx, stops, dist, load = routing.Start(v), [], 0, 0
        while not routing.IsEnd(idx):
            node = manager.IndexToNode(idx)
            stops.append(node)
            visited.add(node)
            load += demands[node]
            nxt = solution.Value(routing.NextVar(idx))
            dist += routing.GetArcCostForVehicle(idx, nxt, v)
            idx = nxt
        stops.append(0)
        total += dist
        routes.append({"truck": v, "stops": stops, "distance_m": dist, "load_l": load})
    return routes, total, visited


def _empty_plan(depot_node, warnings):
    routes = [{"truck": v, "stops": ["depot", "depot"], "distance_m": 0, "load_l": 0,
               "geometry": []} for v in range(NUM_TRUCKS)]
    return {"total_distance_m": 0, "routes": routes, "skipped": [], "predictions": {},
            "metrics": {"total_km": 0, "trucks_used": 0, "bins_total": 0, "bins_collected": 0,
                        "bins_skipped": 0, "overflows_prevented": 0},
            "alerts": [], "warnings": warnings, "_nodes": {"depot": depot_node}}


# ---------- API public ----------

def plan_routes(bins, history=None, compare_baseline=False, seconds=SOLVER_SECONDS):
    history = history or {}
    warnings = []
    depot_node, _ = _snap(DEPOT["lat"], DEPOT["lng"])
    used = _clean_bins(bins, warnings)
    if not used:
        warnings.append("Nicio pubela utilizabila: toate camioanele raman in depozit")
        return _empty_plan(depot_node, warnings)

    ids = ["depot"] + [b["id"] for b in used]
    graph_nodes = [depot_node] + [b["_node"] for b in used]
    M = _matrix(graph_nodes)
    fills = [b["umplere"] / 100 for b in used]
    demands = [0] + [round(f * b.get("capacitate_l", DEFAULT_BIN_CAPACITY_L))
                     for f, b in zip(fills, used)]

    predictions, penalties, overflow_risk, mandatory = {}, {}, set(), set()
    for node, (b, fill) in enumerate(zip(used, fills), start=1):
        rate, hours_to_full = estimate(history.get(b["id"], []))
        predictions[b["id"]] = {
            "hours_to_full": None if hours_to_full is None else round(hours_to_full, 1),
            "predicted_fill_24h": round(min(1.0, fill + (rate or 0) * HORIZON_H), 2),
        }
        will_overflow = hours_to_full is not None and hours_to_full <= HORIZON_H
        if will_overflow:
            overflow_risk.add(node)
        if fill >= MANDATORY_FILL or will_overflow:
            mandatory.add(node)
            penalties[node] = MANDATORY_PENALTY     # o sare doar daca altfel nu exista solutie
        else:
            penalties[node] = int(PENALTY_SCALE * fill**2)

    capacity = int(TRUCK_CAPACITY_L * (1 - RESERVE))
    routes, total, visited = _solve(M, demands, penalties, capacity, seconds)
    for r in routes:
        r["stops"] = [ids[i] for i in r["stops"]]
        r["geometry"] = _geometry([graph_nodes[ids.index(s)] for s in r["stops"]])

    unserved = [ids[i] for i in mandatory if i not in visited]
    if unserved:
        warnings.append(f"Nu incap azi in camioane, desi sunt obligatorii: {', '.join(sorted(unserved))}")

    metrics = {
        "total_km": round(total / 1000, 2),
        "trucks_used": sum(1 for r in routes if len(r["stops"]) > 2),
        "bins_total": len(used),
        "bins_collected": len(visited) - 1,
        "bins_skipped": len(used) - (len(visited) - 1),
        "overflows_prevented": len(overflow_risk & visited),
        "mandatory_unserved": len(unserved),
    }
    if compare_baseline:
        # referinta = colectarea tuturor pubelelor; o calculam o data pentru acelasi set de pubele,
        # ca procentul de economie sa nu sara de la o planificare la alta din cauza solver-ului
        key = tuple(ids)
        if key not in _baseline_cache:
            base = _solve(M, demands, {i: MANDATORY_PENALTY for i in range(1, len(ids))},
                          TRUCK_CAPACITY_L, max(seconds, BASELINE_SECONDS))
            _baseline_cache[key] = base[1]
        metrics["baseline_km"] = round(_baseline_cache[key] / 1000, 2)
        metrics["saving_pct"] = round(100 * (1 - total / _baseline_cache[key]), 1)

    return {
        "total_distance_m": total,
        "routes": routes,
        "skipped": [ids[i] for i in range(1, len(ids)) if i not in visited],
        "predictions": predictions,
        "metrics": metrics,
        "alerts": [],
        "warnings": warnings,
        "_nodes": dict(zip(ids, graph_nodes)),   # intern: id -> nod din graf, pentru alerte
    }


def insertion_cost(plan, lat, lng, volume_l=0):
    """Cati metri in plus ar costa sa adaugi un punct in cea mai buna ruta (None daca nu incape)."""
    x, off = _snap(lat, lng)
    if off > MAX_SNAP_M:
        return None
    nodes, to_x, from_x = plan["_nodes"], _to(x), _from(x)[0]
    costs = [to_x[nodes[s[k]]] + from_x[nodes[s[k + 1]]] - _dist(nodes[s[k]], nodes[s[k + 1]])
             for r in plan["routes"] if r["load_l"] + volume_l <= TRUCK_CAPACITY_L
             for s in [r["stops"]] for k in range(len(s) - 1)]
    return int(min(costs)) if costs else None


def add_alert(plan, alert, kind="alert"):
    """alert = {"id", "lat", "lng"} plus "volum_l" (camera) sau "marime": mica/medie/mare (cetatean).

    kind="alert" pentru depozitari ilegale, kind="bin" pentru o pubela sarita care a devenit
    urgenta in timpul zilei (camioanele sunt deja pe drum, deci nu refacem tot planul).
    Modifica planul pe loc. Daca niciun camion nu are loc, punctul ramane "pending"
    pentru urmatoarea planificare, in loc sa dea eroare.
    """
    volume = alert.get("volum_l")
    if volume is None:
        volume = ALERT_SIZES_L.get(alert.get("marime", "medie"), ALERT_SIZES_L["medie"])
    try:
        x, off = _snap(float(alert["lat"]), float(alert["lng"]))
    except (KeyError, TypeError, ValueError):
        return {"id": alert.get("id"), "status": "rejected", "reason": "coordonate invalide"}
    if off > MAX_SNAP_M:
        return {"id": alert["id"], "status": "rejected", "reason": f"la {off:.0f} m de harta"}

    nodes = plan["_nodes"]
    to_x, from_x = _to(x), _from(x)[0]
    best = None
    for r in plan["routes"]:
        if r["load_l"] + volume > TRUCK_CAPACITY_L:
            continue
        s = r["stops"]
        for k in range(len(s) - 1):
            a, b = nodes[s[k]], nodes[s[k + 1]]
            extra = to_x[a] + from_x[b] - _dist(a, b)
            if best is None or extra < best[0]:
                best = (extra, r, k + 1)

    result = {"id": alert["id"], "kind": kind, "lat": alert["lat"], "lng": alert["lng"],
              "volum_l": volume}
    if best is None:
        result.update(status="pending", reason="niciun camion nu mai are loc azi")
        plan["alerts"].append(result)
        plan["warnings"].append(f"Alerta {alert['id']} asteapta: niciun camion nu mai are loc azi")
        return result

    extra, route, pos = best
    dedicated = len(route["stops"]) == 2
    nodes[alert["id"]] = x
    route["stops"].insert(pos, alert["id"])
    route["geometry"] = _geometry([nodes[s] for s in route["stops"]])
    route["distance_m"] += int(extra)
    route["load_l"] += volume
    plan["total_distance_m"] += int(extra)
    m = plan["metrics"]
    m["total_km"] = round(plan["total_distance_m"] / 1000, 2)
    if "baseline_km" in m:
        m["saving_pct"] = round(100 * (1 - m["total_km"] / m["baseline_km"]), 1)
    result.update(status="assigned", truck=route["truck"], extra_m=int(extra), dedicated_trip=dedicated)
    plan["alerts"].append(result)
    if kind == "bin" and alert["id"] in plan["skipped"]:
        plan["skipped"].remove(alert["id"])
        plan["metrics"]["bins_collected"] += 1
        plan["metrics"]["bins_skipped"] -= 1
    return result
