"""Modulul de rutare apelat de backend.

Intrare (formatul din tabela trashes a backend-ului):
    bins = [{"id": "TM-001", "lat": 45.74, "lng": 21.23, "umplere": 73,
             "capacitate_l": 1100}]          # capacitate_l e optional
    history = {"TM-001": [{"t_h": -2, "level": 0.69}, ...]}   # optional

Iesire: rute cu opriri identificate prin id ("depot" sau id-ul pubelei/alertei),
geometria fiecarei rute, predictii, metrici si avertismente. Functiile nu arunca
exceptii pentru date proaste: le raporteaza in "warnings", ca demo-ul sa nu pice.
"""
import json
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
NUM_TRUCKS = 6
TRUCK_CAPACITY_L = 8000
RESERVE = 0.10
MANDATORY_FILL = 0.85            # la fel ca rosul de pe harta (peste 85%)
MIN_FILL = 0.3                   # sub acest prag pubela nu merita drumul (afara de risc de revarsare)
HORIZON_H = 24
PENALTY_SCALE = 4000             # ocolul acceptat (m) pentru o pubela optionala = PENALTY_SCALE * umplere^2
MANDATORY_PENALTY = 10_000_000   # "obligatoriu": sarirea costa cat 10.000 km
DEFAULT_BIN_CAPACITY_L = 1100
MAX_SNAP_M = 300                 # peste aceasta distanta, punctul e in afara hartii
SOLVER_SECONDS = 3               # suficient pentru ~60 de pubele; 30 pentru cifre de pitch
BASELINE_SECONDS = 5             # referinta e doar prima solutie, gata in sub o secunda
ALERT_SIZES_L = {"mica": 100, "medie": 500, "mare": 1500}   # raportari de la cetateni

_G = ox.load_graphml(GRAPH_FILE)
_baseline_cache = {}
_FARA_DRUM = 10**8              # "distanta" intre doua puncte fara drum legal intre ele
_COS = np.cos(np.radians(DEPOT["lat"]))


# ---------- harta ----------

def _m(a, b):
    """Distanta aproximativa in metri intre doua puncte [lat, lng] (suficient la scara unui oras)."""
    dy = (a[0] - b[0]) * 111_320
    dx = (a[1] - b[1]) * 111_320 * np.cos(np.radians(a[0]))
    return float(np.hypot(dx, dy))


def _edge_geom(u, v):
    """Forma reala a strazii u -> v (cu curbe), orientata de la u la v, plus lungimea si numele ei.
    Intre doua noduri pot fi mai multe strazi: luam cea mai scurta, ca Dijkstra."""
    d = min(_G.get_edge_data(u, v).values(), key=lambda e: e.get("length", 0))
    a, b = [_G.nodes[u]["y"], _G.nodes[u]["x"]], [_G.nodes[v]["y"], _G.nodes[v]["x"]]
    pts = [[y, x] for x, y in d["geometry"].coords] if "geometry" in d else [a, b]
    if _m(pts[0], a) > _m(pts[-1], a):
        pts.reverse()
    name = d.get("name")
    if isinstance(name, list):
        name = name[0]
    return pts, float(d.get("length", 0)), name or "strada fără nume"


# fiecare strada (cu sensul ei) taiata in segmente drepte, pentru gasit rapid strada cea mai apropiata
_EDGES = []                     # (u, v, puncte, distanta cumulata pe strada la fiecare punct, nume)
_seg = []                       # (index strada, punct start, punct final, distanta la start)
for _u, _v in {(u, v) for u, v, _ in _G.edges(keys=True)}:
    _pts, _len, _name = _edge_geom(_u, _v)
    _geo = [_m(p, q) for p, q in zip(_pts, _pts[1:])]
    _k = _len / sum(_geo) if sum(_geo) else 0      # lungimile din graf, ca distantele sa se potriveasca
    _cum = np.concatenate([[0], np.cumsum(_geo) * _k]).tolist()
    for _i in range(len(_pts) - 1):
        _seg.append((len(_EDGES), *_pts[_i], *_pts[_i + 1], _cum[_i], _cum[_i + 1]))
    _EDGES.append((_u, _v, _pts, _cum, _name))
_SEG = np.array(_seg)
del _seg


def _snap_all(lat, lng):
    """Strazile (cu sens) cele mai apropiate de punct: [(distanta_m, oprire)], cea mai buna prima.

    Pe o strada cu doua sensuri iese cate o varianta pentru fiecare sens; pe una cu sens unic, doar sensul legal.
    """
    ay, ax = _SEG[:, 1], _SEG[:, 2] * _COS
    by, bx = _SEG[:, 3], _SEG[:, 4] * _COS
    py, px = lat, lng * _COS
    sy, sx = by - ay, bx - ax
    l2 = sx**2 + sy**2
    t = np.clip(np.where(l2 > 0, ((px - ax) * sx + (py - ay) * sy) / np.where(l2 > 0, l2, 1), 0), 0, 1)
    d = np.hypot(ax + t * sx - px, ay + t * sy - py) * 111_320
    best = float(d.min())
    out, seen = [], set()
    for i in np.argsort(d)[:12]:
        if d[i] > best + 2:
            break
        e = int(_SEG[i, 0])
        if e in seen:
            continue
        seen.add(e)
        u, v, _, cum, _ = _EDGES[e]
        off = float(_SEG[i, 5] + t[i] * (_SEG[i, 6] - _SEG[i, 5]))
        proj = [float(ay[i] + t[i] * sy[i]), float((ax[i] + t[i] * sx[i]) / _COS)]
        # de ce parte a sensului de mers e punctul (pozitiv = stanga)
        partea = float(sx[i] * (py - ay[i]) - sy[i] * (px - ax[i]))
        out.append((float(d[i]), {"e": e, "u": u, "v": v, "off": off, "len": cum[-1],
                                  "proj": proj, "dreapta": partea <= 0}))
    return out


def _snap(lat, lng):
    """Locul de pe strada unde opreste camionul pentru punctul dat, si cat de departe e punctul de strada.

    Pe strazile cu doua sensuri alegem sensul care lasa punctul pe dreapta, ca echipajul sa nu
    traverseze strada cu pubela; pe strazile cu sens unic exista doar sensul legal.
    """
    variante = _snap_all(lat, lng)
    off, stop = next(((d, s) for d, s in variante if s["dreapta"]), variante[0])
    return stop, off


@lru_cache(maxsize=1024)
def _from(src):
    """Distantele si drumurile de la un nod spre toate celelalte (harta nu se schimba, deci se pot refolosi)."""
    return nx.single_source_dijkstra(_G, src, weight="length")


def _same_edge_ahead(a, b):
    return a["e"] is not None and a["e"] == b["e"] and b["off"] >= a["off"]


def _leg(a, b):
    """Metrii de mers legal de la oprirea a la oprirea b: restul strazii lui a, drumul pe harta, inceputul strazii lui b."""
    if _same_edge_ahead(a, b) or a is b:
        return max(0.0, b["off"] - a["off"])
    mid = _from(a["v"])[0].get(b["u"])
    if mid is None:
        return _FARA_DRUM
    return (a["len"] - a["off"]) + mid + b["off"]


def _matrix(stops):
    return np.array([[0 if i == j else round(_leg(a, b)) for j, b in enumerate(stops)]
                     for i, a in enumerate(stops)], dtype=int)


def _sub(stop, d0, d1):
    """Punctele strazii opririi intre distantele d0 si d1 (masurate de la inceputul strazii)."""
    if stop["e"] is None:
        return [stop["proj"]]
    _, _, pts, cum, _ = _EDGES[stop["e"]]

    def at(d):
        for j in range(len(pts) - 1):
            if cum[j + 1] >= d:
                f = (d - cum[j]) / (cum[j + 1] - cum[j]) if cum[j + 1] > cum[j] else 0
                return [pts[j][0] + f * (pts[j + 1][0] - pts[j][0]), pts[j][1] + f * (pts[j + 1][1] - pts[j][1])]
        return pts[-1]
    out = []
    for p in [at(d0)] + [p for p, c in zip(pts, cum) if d0 < c < d1] + [at(d1)]:
        if not out or _m(out[-1], p) > 0.5:      # fara puncte dublate (strica directia de mers)
            out.append(p)
    return out


def _leg_parts(a, b):
    """Drumul de la a la b ca bucati [(puncte, nume strada)], fiecare strada cu forma ei reala."""
    if _same_edge_ahead(a, b) or a is b:
        return [(_sub(a, a["off"], b["off"]), _EDGES[a["e"]][4] if a["e"] is not None else "strada fără nume", (a["u"], a["v"]))]
    path = _from(a["v"])[1].get(b["u"])
    if path is None:
        return []
    parts = []
    if a["e"] is not None:
        parts.append((_sub(a, a["off"], a["len"]), _EDGES[a["e"]][4], (a["u"], a["v"])))
    for u, v in zip(path, path[1:]):
        pts, _, name = _edge_geom(u, v)
        parts.append((pts, name, (u, v)))
    if b["e"] is not None:
        parts.append((_sub(b, 0, b["off"]), _EDGES[b["e"]][4], (b["u"], b["v"])))
    return parts


def _geometry(stops):
    """Coordonatele [lat, lng] ale strazilor parcurse intre opriri, pentru desenat pe harta."""
    coords = []
    for a, b in zip(stops, stops[1:]):
        for pts, _, _ in _leg_parts(a, b):
            if coords and pts and coords[-1] == pts[0]:
                pts = pts[1:]
            coords.extend(pts)
    return coords


# ---------- navigatie pas cu pas, pentru aplicatia soferului ----------

def _bearing(a, b):
    """Directia de mers de la a la b, in grade (0 = nord, 90 = est)."""
    la1, la2 = np.radians(a[0]), np.radians(b[0])
    dl = np.radians(b[1] - a[1])
    x = np.sin(dl) * np.cos(la2)
    y = np.cos(la1) * np.sin(la2) - np.sin(la1) * np.cos(la2) * np.cos(dl)
    return float(np.degrees(np.arctan2(x, y)) % 360)


_VIRAJ = {"inainte": "Continuă înainte", "usor-dreapta": "Virează ușor la dreapta",
          "dreapta": "Virează la dreapta", "usor-stanga": "Virează ușor la stânga",
          "stanga": "Virează la stânga", "intoarcere": "Întoarce-te"}


def _viraj(inainte, dupa):
    d = (dupa - inainte + 540) % 360 - 180        # -180..180, pozitiv = spre dreapta
    if abs(d) < 25:
        return "inainte"
    if abs(d) > 150:
        return "intoarcere"
    side = "dreapta" if d > 0 else "stanga"
    return f"usor-{side}" if abs(d) < 50 else side


# ---------- benzi: pe ce banda sa se incadreze soferul inainte de intersectie ----------

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "benzi_osm.json")) as _f:
    _BENZI_OSM = json.load(_f)               # din OpenStreetMap, vezi get_benzi.py

# ce marcaje de pe banda se potrivesc fiecarei manevre, in ordinea preferintei
_BANDA_BUNA = {
    "stanga": [{"left", "sharp_left"}], "usor-stanga": [{"slight_left"}, {"left"}, {"through"}],
    "dreapta": [{"right", "sharp_right"}], "usor-dreapta": [{"slight_right"}, {"right"}, {"through"}],
    "inainte": [{"through"}, {"slight_left", "slight_right"}], "intoarcere": [{"reverse"}, {"left"}],
}


_ORDINE = ["reverse", "sharp_left", "left", "slight_left", "through", "slight_right", "right", "sharp_right"]


def _ca_lista(x):
    return x if isinstance(x, list) else [x]


def _benzi(u, v, tip):
    """Benzile strazii u -> v la capatul ei (de la stanga la dreapta) si pe care sa mearga soferul.

    Intoarce {"benzi": [{"directii": [...], "buna": bool}], "benzi_estimat": bool, "benzi_text": str} sau None.
    Cand OpenStreetMap are marcajele benzilor (turn:lanes) le folosim direct; cand stim doar cate benzi
    sunt, aplicam regula din codul rutier: la stanga de pe banda din stanga, la dreapta de pe cea din dreapta.
    """
    d = min(_G.get_edge_data(u, v).values(), key=lambda e: e.get("length", 0))
    ids = [str(i) for i in _ca_lista(d.get("osmid"))]
    invers = any(str(r) == "True" for r in _ca_lista(d.get("reversed", False)))
    dublu_sens = str(d.get("oneway")) != "True"
    sens = ("backward" if invers else "forward") if dublu_sens else None

    turn, n = None, None
    for i in reversed(ids):                    # strada poate fi facuta din mai multe bucati OSM
        t = _BENZI_OSM.get(i, {})
        turn = turn or (t.get(f"turn:lanes:{sens}") if sens else t.get("turn:lanes"))
        if n is None:
            try:
                n = int(t[f"lanes:{sens}"] if sens and f"lanes:{sens}" in t else t["lanes"])
                if dublu_sens and f"lanes:{sens}" not in t:
                    n //= 2                    # "lanes" numara ambele sensuri
            except (KeyError, ValueError):
                pass
    estimat = not turn
    if turn:
        benzi = []
        for b in turn.split("|"):
            dirs = {x.strip() if x.strip() not in ("", "none") else "through" for x in b.split(";")}
            dirs = [x for x in _ORDINE if x in dirs] or ["through"]   # merge_to_* etc. = inainte
            benzi.append({"directii": dirs})
        for bune in _BANDA_BUNA[tip]:
            potrivite = [bool(bune & set(b["directii"])) for b in benzi]
            if any(potrivite):
                break
        else:
            estimat, turn = True, None         # marcajele nu se potrivesc cu manevra: regula generala
            n = len(benzi)
        if turn:
            for b, ok in zip(benzi, potrivite):
                b["buna"] = ok
    if not turn:
        if not n or n < 2:
            return None                        # o singura banda: n-ai ce alege
        stanga = tip in ("stanga", "usor-stanga", "intoarcere")
        dreapta = tip in ("dreapta", "usor-dreapta")
        sageata = {"stanga": "left", "usor-stanga": "slight_left", "intoarcere": "reverse",
                   "dreapta": "right", "usor-dreapta": "slight_right"}.get(tip, "through")
        benzi = [{"directii": [sageata if (stanga and k == 0 or dreapta and k == n - 1) else "through"],
                  "buna": (k == 0) if stanga else (k == n - 1) if dreapta else True} for k in range(n)]
        if not stanga and not dreapta:
            return None                        # inainte, fara marcaje: orice banda e buna
    bune = [k for k, b in enumerate(benzi) if b["buna"]]
    if len(bune) == len(benzi):
        return None                            # toate benzile merg: nimic de spus
    n = len(benzi)
    if bune == [0]:
        text = "Încadrează-te pe banda din stânga"
    elif bune == [n - 1]:
        text = "Încadrează-te pe banda din dreapta"
    elif bune[0] == 0:
        text = f"Încadrează-te pe {'primele ' + str(len(bune)) if len(bune) > 2 else 'primele două'} benzi din stânga"
    elif bune[-1] == n - 1:
        text = f"Încadrează-te pe {'ultimele ' + str(len(bune)) if len(bune) > 2 else 'ultimele două'} benzi din dreapta"
    else:
        text = "Încadrează-te pe banda din mijloc" if len(bune) == 1 else "Încadrează-te pe benzile din mijloc"
    return {"benzi": benzi, "benzi_estimat": estimat, "benzi_text": text}


def etapa_navigatie(start, dest, nume_dest):
    """Drumul de la start la dest ([lat, lng]) cu instructiuni de condus, ca un GPS.

    Intoarce {"geometrie", "distanta_m", "pasi"}; fiecare pas are "tip" (start, stanga, dreapta,
    usor-*, inainte, intoarcere, sosire), "text", "strada", "la_m" (unde incepe, in metri de la
    inceputul etapei), "lat"/"lng" (punctul manevrei) si "distanta_m" (cat tine pana la urmatorul pas).
    """
    b, _ = _snap(*dest)
    # nu stim incotro e intors camionul: dintre sensurile strazii pe care e, il alegem pe cel mai scurt
    a = min((s for _, s in _snap_all(*start)), key=lambda s: _leg(s, b))
    parts = _leg_parts(a, b)
    if not parts:
        raise ValueError("nu exista drum pe harta intre cele doua puncte")

    # bucati de drum grupate pe strazi: fiecare schimbare de strada e o manevra
    grupuri = []                                  # [nume, [puncte], ultima strada (u, v)]
    for pts, name, edge in parts:
        if len(pts) < 2:                          # bucata de lungime zero
            continue
        if grupuri and grupuri[-1][0] == name:
            grupuri[-1][1].extend(pts[1:])
            grupuri[-1][2] = edge
        else:
            grupuri.append([name, pts, edge])
    # bucatile scurte fara nume (benzi de viraj, intersectii) nu sunt manevre de anuntat
    curat = []
    for name, pts, edge in grupuri:
        scurt = sum(_m(p, q) for p, q in zip(pts, pts[1:])) < 40
        if curat and (scurt and name == "strada fără nume" or curat[-1][0] == name):
            curat[-1][1].extend(pts[1:])
            curat[-1][2] = edge
        else:
            curat.append([name, pts, edge])
    grupuri = curat

    coords = [list(start)]
    pasi = []
    for i, (name, pts, _) in enumerate(grupuri):
        benzi = None
        if i == 0:
            tip, text = "start", f"Pornește pe {name}"
        else:
            prev = grupuri[i - 1][1]
            tip = _viraj(_bearing(prev[-2], prev[-1]), _bearing(pts[0], pts[1]))
            text = f"{_VIRAJ[tip]} pe {name}"
            benzi = _benzi(*grupuri[i - 1][2], tip)
        la = sum(_m(p, q) for p, q in zip(coords, coords[1:])) + _m(coords[-1], pts[0])
        pas = {"tip": tip, "text": text, "strada": name, "la_m": round(la), "lat": pts[0][0], "lng": pts[0][1]}
        if benzi:
            pas.update(benzi)
        pasi.append(pas)
        coords.extend(pts)
    if not grupuri:                               # deja la oprire
        coords.append(b["proj"])
    total = sum(_m(p, q) for p, q in zip(coords, coords[1:]))
    pasi.append({"tip": "sosire", "text": f"Ai ajuns la {nume_dest}", "strada": nume_dest,
                 "la_m": round(total), "lat": dest[0], "lng": dest[1]})
    pasi[0]["la_m"] = 0                            # primul pas incepe chiar de unde e camionul
    for p, q in zip(pasi, pasi[1:]):
        p["distanta_m"] = q["la_m"] - p["la_m"]
    pasi[-1]["distanta_m"] = 0
    return {"geometrie": coords, "distanta_m": round(total), "pasi": pasi}


def _draw(plan, stops):
    return _geometry([plan["_stops"][s] for s in stops])


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
        stop, off = _snap(lat, lng)
        if off > MAX_SNAP_M:
            warnings.append(f"{bid} e la {off:.0f} m de cea mai apropiata strada din harta; ignorata")
            continue
        seen.add(bid)
        clean.append({**b, "umplere": fill, "_stop": stop})
    return clean


# ---------- solver ----------

def _solve(M, demands, penalties, capacity, seconds, greedy=False, zone=None):
    """VRP cu capacitate. penalties = {index: penalitate}; fiecare pubela poate fi sarita contra penalitatii ei.

    zone = {index: [camioane]}: pubela poate fi colectata doar de camioanele zonei ei (optional).

    greedy=True: fara optimizare, doar "mergi la cea mai apropiata pubela" (cum ar face un sofer fara aplicatie).
    """
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
    for node, trucks in (zone or {}).items():
        # -1 = sarita; altfel doar camioanele zonei
        routing.VehicleVar(manager.NodeToIndex(node)).SetValues([-1, *trucks])

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    if greedy:
        params.solution_limit = 1          # prima solutie gasita, fara nicio imbunatatire
    else:
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


def _empty_plan(depot, warnings):
    routes = [{"truck": v, "stops": ["depot", "depot"], "distance_m": 0, "load_l": 0,
               "geometry": []} for v in range(NUM_TRUCKS)]
    return {"total_distance_m": 0, "routes": routes, "skipped": [], "predictions": {},
            "metrics": {"total_km": 0, "trucks_used": 0, "bins_total": 0, "bins_collected": 0,
                        "bins_skipped": 0, "overflows_prevented": 0},
            "alerts": [], "warnings": warnings, "_stops": {"depot": depot}}


# ---------- API public ----------

def plan_routes(bins, history=None, compare_baseline=False, seconds=SOLVER_SECONDS, zone=None):
    """zone = {id pubela: [camioane]}, optional: fiecare pubela e colectata doar de camioanele zonei ei."""
    history = history or {}
    warnings = []
    depot, _ = _snap(DEPOT["lat"], DEPOT["lng"])
    used = _clean_bins(bins, warnings)
    if not used:
        warnings.append("Nicio pubela utilizabila: toate camioanele raman in depozit")
        return _empty_plan(depot, warnings)

    ids = ["depot"] + [b["id"] for b in used]
    stops = [depot] + [b["_stop"] for b in used]
    M = _matrix(stops)
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
            # plina sau se umple in urmatoarele 24 h: se colecteaza sigur
            mandatory.add(node)
            penalties[node] = MANDATORY_PENALTY     # o sare doar daca nu incape in camioane
        elif fill >= MIN_FILL:
            # optionala: o ia doar daca ocolul e mic fata de cat de plina e
            # (50% -> pana la ~1 km ocol, 70% -> pana la ~2 km)
            penalties[node] = int(PENALTY_SCALE * fill ** 2)
        else:
            penalties[node] = 0                     # aproape goala: nu merita drumul

    capacity = int(TRUCK_CAPACITY_L * (1 - RESERVE))
    zone_idx = {i: [t for t in zone[b] if 0 <= t < NUM_TRUCKS] for i, b in enumerate(ids) if zone and b in zone}
    routes, total, visited = _solve(M, demands, penalties, capacity, seconds, zone=zone_idx)
    for r in routes:
        r["stops"] = [ids[i] for i in r["stops"]]
        r["geometry"] = _geometry([stops[ids.index(s)] for s in r["stops"]])

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
        # referinta = colectarea tuturor pubelelor fara optimizare (din aproape in aproape, cum ar
        # face un sofer fara aplicatie); o calculam o data pentru acelasi set de pubele
        key = (tuple(ids), tuple(sorted((i, tuple(t)) for i, t in zone_idx.items())))
        if key not in _baseline_cache:
            base = _solve(M, demands, {i: MANDATORY_PENALTY for i in range(1, len(ids))},
                          TRUCK_CAPACITY_L, BASELINE_SECONDS, greedy=True, zone=zone_idx)
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
        "_stops": dict(zip(ids, stops)),         # intern: id -> locul de pe strada, pentru alerte si desen
    }


def insertion_cost(plan, lat, lng, volume_l=0):
    """Cati metri in plus ar costa sa adaugi un punct in cea mai buna ruta (None daca nu incape)."""
    x, off = _snap(lat, lng)
    if off > MAX_SNAP_M:
        return None
    st = plan["_stops"]
    costs = [_leg(st[s[k]], x) + _leg(x, st[s[k + 1]]) - _leg(st[s[k]], st[s[k + 1]])
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

    st = plan["_stops"]
    best = None
    for r in plan["routes"]:
        if r["load_l"] + volume > TRUCK_CAPACITY_L:
            continue
        if alert.get("camioane") and r["truck"] not in alert["camioane"]:
            continue                     # pubela dintr-o zona: doar camioanele zonei ei
        s = r["stops"]
        for k in range(len(s) - 1):
            a, b = st[s[k]], st[s[k + 1]]
            extra = _leg(a, x) + _leg(x, b) - _leg(a, b)
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
    st[alert["id"]] = x
    route["stops"].insert(pos, alert["id"])
    route["geometry"] = _draw(plan, route["stops"])
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
