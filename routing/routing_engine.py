"""Modulul de rutare apelat de backend.

Intrare (formatul din tabela trashes a backend-ului):
    bins = [{"id": "TM-001", "lat": 45.74, "lng": 21.23, "umplere": 73,
             "capacitate_l": 1100}]          # capacitate_l e optional
    history = {"TM-001": [{"t_h": -2, "level": 0.69}, ...]}   # optional

Iesire: rute cu opriri identificate prin id ("depot" sau id-ul pubelei/alertei),
geometria fiecarei rute, predictii, metrici si avertismente. Functiile nu arunca
exceptii pentru date proaste: le raporteaza in "warnings", ca demo-ul sa nu pice.
"""
import heapq
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
MANDATORY_FILL = 0.55            # de la 55% in sus ghena se colecteaza sigur (si intra din mers in ruta)
MIN_FILL = 0.3                   # sub acest prag pubela nu merita drumul (afara de risc de revarsare)
HORIZON_H = 24
PENALTY_SCALE = 4000             # ocolul acceptat (m) pentru o pubela optionala = PENALTY_SCALE * umplere^2
MANDATORY_PENALTY = 10_000_000   # "obligatoriu": sarirea costa cat 10.000 km
DEFAULT_BIN_CAPACITY_L = 1100
MAX_SNAP_M = 300                 # peste aceasta distanta, punctul e in afara hartii
SOLVER_SECONDS = 3               # suficient pentru ~60 de pubele; 30 pentru cifre de pitch
BASELINE_SECONDS = 5             # referinta e doar prima solutie, gata in sub o secunda
ALERT_SIZES_L = {"mica": 100, "medie": 500, "mare": 1500}   # raportari de la cetateni
# unde descarca un camion plin (statia de descarcare / drumul spre groapa de gunoi); trebuie sa fie in harta.
# Capatul de nord al Caii Torontalului, spre iesirea din oras: de inlocuit cu locul real, daca e altul.
GROAPA = {"lat": 45.7846838, "lng": 21.2019655, "nume": "Groapa de gunoi", "adresa": "Calea Torontalului, Timișoara"}
CAPACITATE_UTILA_L = int(TRUCK_CAPACITY_L * (1 - RESERVE))   # peste atat camionul merge sa descarce
# Camionul de gunoi compacteaza ce strange: deseurile menajere stau afanate in ghena (~100-150 kg/m3),
# iar presa unui camion compactor le aduce la ~350-500 kg/m3, adica de ~3-4 ori mai putin volum.
# Luam 3, capatul prudent. Volumul unei ghene se imparte la COMPACTARE cand intra in camion
# (depozitarile ilegale raman cu volumul lor: mobila, moloz, saci voluminosi nu se compacteaza la fel).
COMPACTARE = 3
MAX_CURSE = 3                    # de cate ori se poate umple un camion intr-o zi (cu descarcari intre ele)

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


def _bearing(a, b):
    """Directia de mers de la a la b, in grade (0 = nord, 90 = est)."""
    la1, la2 = np.radians(a[0]), np.radians(b[0])
    dl = np.radians(b[1] - a[1])
    x = np.sin(dl) * np.cos(la2)
    y = np.cos(la1) * np.sin(la2) - np.sin(la1) * np.cos(la2) * np.cos(dl)
    return float(np.degrees(np.arctan2(x, y)) % 360)


# ---------- drumul dintre doua opriri, pe strazi, cu cost pentru viraje ----------
#
# Drumul se cauta din strada in strada (nu din intersectie in intersectie), ca sa stim cum vireaza
# camionul la fiecare intersectie: o intoarcere la 180 de grade e practic interzisa (un camion de
# gunoi nu se intoarce in mijlocul intersectiei), virajul la stanga costa putin mai mult decat cel
# la dreapta, iar inainte nu costa nimic. Costurile de viraj conteaza doar la ALEGEREA drumului;
# kilometrii raportati sunt mereu cei reali (vezi _leg_m).

VIRAJ_STANGA_M = 20        # un viraj la stanga "costa" cat 20 m in plus (asteptare, traversarea sensului)
VIRAJ_DREAPTA_M = 6
INTOARCERE_M = 5000        # intoarcere la 180 de grade intr-o intersectie: doar daca altfel nu se poate
INTOARCERE_FUNDATURA_M = 40  # la capatul unei fundaturi camionul se intoarce oricum, n-are alta iesire
PARTEA_GRESITA_M = 40      # ghena pe partea stanga a strazii: echipajul traverseaza cu ea (se evita, dar nu cu orice pret)

_E_LEN = [cum[-1] for _, _, _, cum, _ in _EDGES]
_E_IDX = {(u, v): i for i, (u, v, _, _, _) in enumerate(_EDGES)}
_OUT, _IN = {}, {}
for _i, (_u, _v, _, _, _) in enumerate(_EDGES):
    _OUT.setdefault(_u, []).append(_i)
    _IN.setdefault(_v, []).append(_i)


def _capete(pts, cum):
    """Directia strazii la inceput si la capat (masurata pe primii/ultimii ~10 m, nu pe un punct izolat)."""
    k = next((j for j in range(1, len(pts)) if cum[j] >= 10), len(pts) - 1)
    m = next((j for j in range(len(pts) - 2, -1, -1) if cum[-1] - cum[j] >= 10), 0)
    return _bearing(pts[0], pts[k]), _bearing(pts[m], pts[-1])


_START_B, _END_B = zip(*[_capete(pts, cum) for _, _, pts, cum, _ in _EDGES])


def _cost_viraj(f, g):
    """Cat costa trecerea de pe strada f pe strada g (f se termina unde incepe g)."""
    d = (_START_B[g] - _END_B[f] + 540) % 360 - 180          # pozitiv = spre dreapta
    intoarcere = _EDGES[g][1] == _EDGES[f][0] or abs(d) > 150
    if intoarcere:
        return INTOARCERE_FUNDATURA_M if all(_EDGES[h][1] == _EDGES[f][0] for h in _OUT[_EDGES[f][1]]) \
            else INTOARCERE_M
    if d < -40:
        return VIRAJ_STANGA_M
    if d > 40:
        return VIRAJ_DREAPTA_M
    return 0


_NEXT = [[(g, _cost_viraj(f, g)) for g in _OUT.get(_EDGES[f][1], [])] for f in range(len(_EDGES))]
_INF = float("inf")


@lru_cache(maxsize=256)                 # ~150 KB fiecare (tablouri numpy), deci cel mult ~40 MB
def _din_strada(s):
    """Plecand de la capatul strazii s: costul (cu viraje), metrii reali si strada dinainte, pana la capatul
    fiecarei strazi din oras. Harta nu se schimba, deci rezultatul se refoloseste."""
    n = len(_EDGES)
    cost, prev = [_INF] * n, [-1] * n
    cost[s] = 0.0
    heap = [(0.0, s)]
    ordine = []
    while heap:
        c, f = heapq.heappop(heap)
        if c > cost[f]:
            continue
        ordine.append(f)
        for g, t in _NEXT[f]:
            nc = c + t + _E_LEN[g]
            if nc < cost[g]:
                cost[g] = nc
                prev[g] = f
                heapq.heappush(heap, (nc, g))
    metri = [_INF] * n
    metri[s] = 0.0
    for f in ordine[1:]:                     # in ordinea costului: strada dinainte e deja calculata
        metri[f] = metri[prev[f]] + _E_LEN[f]
    return np.array(cost), np.array(metri), np.array(prev, dtype=np.int32)


def _intrare(a, b):
    """Cum ajunge camionul de la capatul strazii lui a la inceputul strazii lui b:
    (cost, metri reali, ultima strada dinainte de b) sau None daca nu exista drum legal."""
    cost, metri, prev = _din_strada(a["e"])
    if b["e"] != a["e"]:
        if cost[b["e"]] == _INF:
            return None
        return (float(cost[b["e"]] - _E_LEN[b["e"]]), float(metri[b["e"]] - _E_LEN[b["e"]]), int(prev[b["e"]]))
    # b e pe aceeasi strada, dar in spatele lui a: trebuie facut un ocol si revenit pe ea
    best = None
    for p in _IN.get(_EDGES[b["e"]][0], []):
        if cost[p] == _INF:
            continue
        t = next((c for g, c in _NEXT[p] if g == b["e"]), INTOARCERE_M)
        if best is None or cost[p] + t < best[0]:
            best = (float(cost[p] + t), float(metri[p]), p)
    return best


def _same_edge_ahead(a, b):
    return a["e"] is not None and a["e"] == b["e"] and b["off"] >= a["off"]


def _leg(a, b):
    """Costul drumului legal de la oprirea a la oprirea b (metri + costul virajelor), pentru ALES drumul:
    restul strazii lui a, strazile dintre ele, inceputul strazii lui b. Ghena lasata pe partea stanga
    a strazii costa in plus."""
    lat = PARTEA_GRESITA_M if b.get("dreapta") is False else 0
    if a is b:
        return 0.0
    if _same_edge_ahead(a, b):
        return max(0.0, b["off"] - a["off"]) + lat
    x = _intrare(a, b)
    if x is None:
        return _FARA_DRUM
    return (a["len"] - a["off"]) + x[0] + b["off"] + lat


def _leg_m(a, b):
    """Metrii reali pe drumul ales de _leg (fara costurile de viraj), pentru kilometrii raportati."""
    if a is b:
        return 0.0
    if _same_edge_ahead(a, b):
        return max(0.0, b["off"] - a["off"])
    x = _intrare(a, b)
    if x is None:
        return _FARA_DRUM
    return (a["len"] - a["off"]) + x[1] + b["off"]


def _matrix(stops):
    return np.array([[0 if i == j else round(_leg(a, b)) for j, b in enumerate(stops)]
                     for i, a in enumerate(stops)], dtype=int)


def _variante(lat, lng):
    """Locurile de pe strada unde poate opri camionul pentru un punct: cel cu punctul pe dreapta primul,
    apoi, pe o strada cu doua sensuri, si sensul celalalt (camionul nu mai trebuie sa treaca de ghena,
    sa se intoarca si sa revina doar ca sa o aiba pe dreapta). Intoarce ([opriri], distanta pana la strada)."""
    toate = _snap_all(lat, lng)
    off = toate[0][0]
    dreapta = [s for d, s in toate if s["dreapta"]]
    stanga = [s for d, s in toate if not s["dreapta"]]
    alese = (dreapta[:1] + stanga[:1]) if dreapta else [toate[0][1]]
    return alese, off


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
    """Drumul de la a la b ca bucati [(puncte, nume strada, (u, v))], fiecare strada cu forma ei reala."""
    if a is b or _same_edge_ahead(a, b):
        return [(_sub(a, a["off"], b["off"]), _EDGES[a["e"]][4], (a["u"], a["v"]))]
    x = _intrare(a, b)
    if x is None:
        return []
    _, _, prev = _din_strada(a["e"])
    strazi, f = [], x[2]
    while f != a["e"] and f != -1:            # strazile dintre a si b, de la b inapoi spre a
        strazi.append(f)
        f = int(prev[f])
    parts = [(_sub(a, a["off"], a["len"]), _EDGES[a["e"]][4], (a["u"], a["v"]))]
    for e in reversed(strazi):
        u, v, pts, _, name = _EDGES[e]
        parts.append((pts, name, (u, v)))
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

_VIRAJ = {"inainte": "Continuă înainte", "usor-dreapta": "Virează ușor la dreapta",
          "dreapta": "Virează la dreapta", "usor-stanga": "Virează ușor la stânga",
          "stanga": "Virează la stânga", "intoarcere": "Întoarce-te"}


# ---------- sensuri giratorii ----------

# strazile care fac parte dintr-un sens giratoriu (OpenStreetMap: junction=roundabout / circular)
_GIRATORIU = {(u, v) for u, v, d in _G.edges(data=True) if str(d.get("junction")) in ("roundabout", "circular")}
# centrul fiecarui giratoriu (media nodurilor lui legate intre ele), pentru desenul iesirii
_CENTRU_GIRATORIU = {}
_vecini = {}
for _u, _v in _GIRATORIU:
    _vecini.setdefault(_u, set()).add(_v)
    _vecini.setdefault(_v, set()).add(_u)
for _n in _vecini:
    if _n in _CENTRU_GIRATORIU:
        continue
    _comp, _stiva = {_n}, [_n]
    while _stiva:
        for _w in _vecini[_stiva.pop()]:
            if _w not in _comp:
                _comp.add(_w)
                _stiva.append(_w)
    _c = (float(np.mean([_G.nodes[k]["y"] for k in _comp])), float(np.mean([_G.nodes[k]["x"] for k in _comp])))
    for _k in _comp:
        _CENTRU_GIRATORIU[_k] = _c
del _vecini

_ORDINAL = {1: "prima", 2: "a doua", 3: "a treia", 4: "a patra", 5: "a cincea", 6: "a șasea",
            7: "a șaptea", 8: "a opta"}


def _iesiri_numarate(muchii):
    """A cata iesire ia soferul: numaram nodurile din giratoriu prin care trece (dupa intrare) din
    care pleaca o strada care iese din giratoriu. Nodurile unde strazile doar intra nu se numara."""
    n = 0
    for _, v in muchii:
        if any((v, w) not in _GIRATORIU for w in _G.successors(v)):
            n += 1
    return n


def _text_giratoriu(n, strada):
    pe = f" pe {strada}" if strada and strada != "strada fără nume" else ""
    if not n:
        return f"Intră în sensul giratoriu{pe}"
    return f"La sensul giratoriu, ia {_ORDINAL.get(n, f'ieșirea {n}')}{' ieșire' if n in _ORDINAL else ''}{pe}"


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


def _directie_la(stop):
    """Incotro merge camionul cand e oprit in locul dat (directia strazii, pe sensul ei)."""
    pts = _sub(stop, max(0.0, stop["off"] - 10), min(stop["len"], stop["off"] + 10))
    return _bearing(pts[0], pts[-1]) if len(pts) >= 2 and _m(pts[0], pts[-1]) > 1 else None


def etapa_navigatie(start, dest, nume_dest, start_oprire=None, dest_oprire=None, directie=None):
    """Drumul de la start la dest ([lat, lng]) cu instructiuni de condus, ca un GPS.

    Intoarce {"geometrie", "distanta_m", "pasi"}; fiecare pas are "tip" (start, stanga, dreapta,
    usor-*, inainte, intoarcere, sosire), "text", "strada", "la_m" (unde incepe, in metri de la
    inceputul etapei), "lat"/"lng" (punctul manevrei) si "distanta_m" (cat tine pana la urmatorul pas).
    """
    # unde opreste: locul ales de plan (cu sensul strazii), altfel oricare sens, cel mai ieftin
    destinatii = [dest_oprire] if dest_oprire else _variante(*dest)[0]
    if start_oprire:
        plecari = [start_oprire]               # pleaca de la oprirea dinainte, pe sensul pe care a venit
    else:
        plecari = [s for _, s in _snap_all(*start)]
        if directie is not None:
            # stim incotro merge camionul (GPS): doar sensul strazii pe care chiar merge, nu cel opus
            pe_sens = [s for s in plecari
                       if (d := _directie_la(s)) is not None and abs((d - directie + 540) % 360 - 180) < 100]
            plecari = pe_sens or plecari
    a, b = min(((x, y) for x in plecari for y in destinatii), key=lambda p: _leg(*p))
    parts = _leg_parts(a, b)
    if not parts:
        raise ValueError("nu exista drum pe harta intre cele doua puncte")

    # bucati de drum grupate pe strazi: fiecare schimbare de strada e o manevra.
    # Un sens giratoriu e un singur grup (oricate bucati si nume ar avea), cu muchiile lui pastrate
    # ca sa numaram iesirile.
    grupuri = []                                  # [nume, [puncte], ultima strada (u, v), giratoriu, muchii]
    for pts, name, edge in parts:
        if len(pts) < 2:                          # bucata de lungime zero
            continue
        rb = edge in _GIRATORIU
        if grupuri and grupuri[-1][3] == rb and (rb or grupuri[-1][0] == name):
            g = grupuri[-1]
            g[1].extend(pts[1:])
            g[2] = edge
            g[4].append(edge)
            if rb and g[0] == "strada fără nume":
                g[0] = name
        else:
            grupuri.append([name, list(pts), edge, rb, [edge]])
    # bucatile scurte fara nume (benzi de viraj, intersectii) nu sunt manevre de anuntat;
    # giratoriile raman separate, chiar daca sunt mici
    curat = []
    for name, pts, edge, rb, muchii in grupuri:
        scurt = sum(_m(p, q) for p, q in zip(pts, pts[1:])) < 40
        if curat and not rb and not curat[-1][3] and (scurt and name == "strada fără nume" or curat[-1][0] == name):
            curat[-1][1].extend(pts[1:])
            curat[-1][2] = edge
        else:
            curat.append([name, pts, edge, rb, muchii])
    grupuri = curat

    coords = [list(start)]
    pasi = []
    for i, (name, pts, _, rb, muchii) in enumerate(grupuri):
        benzi, extra = None, {}
        urm = grupuri[i + 1] if i + 1 < len(grupuri) else None
        if i == 0:
            tip, text = "start", ("Pornește prin sensul giratoriu" if rb else f"Pornește pe {name}")
        elif rb:
            # intrarea in giratoriu: o singura instructiune, cu numarul iesirii si strada pe care iese
            n = _iesiri_numarate(muchii) if urm else 0
            strada_iesire = urm[0] if urm else None
            tip, text = "giratoriu", _text_giratoriu(n, strada_iesire)
            name = strada_iesire if strada_iesire and strada_iesire != "strada fără nume" else (
                name if name != "strada fără nume" else "sensul giratoriu")
            extra = {"iesire": n}
            # in ce directie iese, fata de cum a intrat (pentru desenul de pe ecran)
            # in ce directie iese, pentru desen: cat din cerc parcurge (in sens invers acelor de ceasornic)
            # de la intrare la iesire, vazut din centrul giratoriului; 0 = drept inainte, + = dreapta
            centru = _CENTRU_GIRATORIU.get(muchii[0][1]) or _CENTRU_GIRATORIU.get(muchii[0][0])
            if urm and centru:
                arc = (_bearing(centru, pts[0]) - _bearing(centru, pts[-1])) % 360
                extra["unghi"] = round(180 - arc)
            else:
                extra["unghi"] = 0
        elif grupuri[i - 1][3]:
            # iesirea din giratoriu: anuntata cand e aproape, ca soferul sa nu o rateze
            n = _iesiri_numarate(grupuri[i - 1][4])
            tip, text = "iesire-giratoriu", f"Ieși din sensul giratoriu pe {name}"
            extra = {"iesire": n}
        else:
            prev = grupuri[i - 1][1]
            tip = _viraj(_bearing(prev[-2], prev[-1]), _bearing(pts[0], pts[1]))
            text = f"{_VIRAJ[tip]} pe {name}"
            benzi = _benzi(*grupuri[i - 1][2], tip)
        la = sum(_m(p, q) for p, q in zip(coords, coords[1:])) + _m(coords[-1], pts[0])
        pas = {"tip": tip, "text": text, "strada": name, "la_m": round(la), "lat": pts[0][0], "lng": pts[0][1], **extra}
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
        variante, off = _variante(lat, lng)
        if off > MAX_SNAP_M:
            warnings.append(f"{bid} e la {off:.0f} m de cea mai apropiata strada din harta; ignorata")
            continue
        seen.add(bid)
        clean.append({**b, "umplere": fill, "_stop": variante[0], "_variante": variante})
    return clean


# ---------- solver ----------

def _solve(M, demands, groups, capacity, seconds, greedy=False, zone=None):
    """VRP cu capacitate. groups = [([noduri], penalitate)]: fiecare pubela are 1-2 noduri (cate un sens al
    strazii pe care poate opri camionul); se viziteaza cel mult unul, iar pubela poate fi sarita contra penalitatii.

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
    for nodes, penalty in groups:
        routing.AddDisjunction([manager.NodeToIndex(n) for n in nodes], penalty, 1)
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
            dist += int(M[node][manager.IndexToNode(nxt)])   # doar metrii reali, fara costul fix
            idx = nxt
        stops.append(0)
        total += dist
        routes.append({"truck": v, "stops": stops, "distance_m": dist, "load_l": load})
    return routes, total, visited


def _empty_plan(depot, warnings):
    routes = [{"truck": v, "stops": ["depot", "depot"], "distance_m": 0, "load_l": 0,
               "geometry": [], "descarcari": 0} for v in range(NUM_TRUCKS)]
    return {"total_distance_m": 0, "routes": routes, "skipped": [], "predictions": {},
            "metrics": {"total_km": 0, "trucks_used": 0, "bins_total": 0, "bins_collected": 0,
                        "bins_skipped": 0, "overflows_prevented": 0},
            "alerts": [], "warnings": warnings, "_stops": {"depot": depot, "groapa": _snap(GROAPA["lat"], GROAPA["lng"])[0]},
            "_vol": {}}


# ---------- descarcari: camionul plin merge la groapa, apoi continua ----------

def e_groapa(sid):
    """Opririle de descarcare au id-ul "groapa-<camion>-<nr>"."""
    return isinstance(sid, str) and sid.startswith("groapa")


def _descarcari(ordine, vol, incarcat=0):
    """Opririle in ordinea data, cu None (= mergi la groapa) inainte de oprirea care nu mai incape
    in camion si la final, daca a ramas ceva in camion. incarcat = litrii deja in camion."""
    out, load = [], incarcat
    for s in ordine:
        v = vol(s)
        if load > 0 and load + v > CAPACITATE_UTILA_L:
            out.append(None)
            load = 0
        out.append(s)
        load += v
    if load > 0:
        out.append(None)
    return out


def _cu_descarcari(plan, truck, ordine, incarcat=0, nr=1):
    """Ca _descarcari, pe id-uri: fiecare descarcare primeste id-ul ei, numerotat de la nr."""
    st, vol = plan["_stops"], plan.setdefault("_vol", {})
    out = []
    for s in _descarcari(ordine, lambda x: vol.get(x, 0), incarcat):
        if s is None:
            s = f"groapa-{truck}-{nr}"
            nr += 1
            st[s] = st["groapa"]
        out.append(s)
    return out


def _seteaza_ruta(plan, r, stops):
    """Pune noua lista de opriri pe ruta si actualizeaza distanta, desenul si metricile."""
    st = plan["_stops"]
    noua = sum(_leg_m(st[a], st[b]) for a, b in zip(stops, stops[1:])) if len(stops) > 2 else 0
    plan["total_distance_m"] += int(noua) - r["distance_m"]
    r.update(stops=stops, geometry=_draw(plan, stops) if len(stops) > 2 else [], distance_m=int(noua),
             descarcari=sum(1 for s in stops if e_groapa(s)))
    m = plan["metrics"]
    m["total_km"] = round(plan["total_distance_m"] / 1000, 2)
    if m.get("baseline_km"):
        m["saving_pct"] = round(100 * (1 - m["total_km"] / m["baseline_km"]), 1)


# ---------- API public ----------

def plan_routes(bins, history=None, compare_baseline=False, seconds=SOLVER_SECONDS, zone=None, rezerva=None):
    """zone = {id pubela: [camioane]}, optional: fiecare pubela e colectata doar de camioanele zonei ei.
    rezerva = {id pubela: [camioane]}, optional: camioanele (de acelasi tip) care pot lua o pubela
    obligatorie care nu mai incape in camionul zonei ei, inainte ca vreun camion sa faca mai multe curse.
    Un pubela poate avea "compactare": False (depozitarile ilegale): volumul ei nu se compacteaza."""
    history = history or {}
    warnings = []
    depot, _ = _snap(DEPOT["lat"], DEPOT["lng"])
    used = _clean_bins(bins, warnings)
    if not used:
        warnings.append("Nicio pubela utilizabila: toate camioanele raman in depozit")
        return _empty_plan(depot, warnings)

    # fiecare pubela intra cu 1-2 noduri (cate un sens al strazii); solverul il alege pe cel mai bun
    ids, stops, nodes_of = ["depot"], [depot], []
    for b in used:
        nodes_of.append(list(range(len(stops), len(stops) + len(b["_variante"]))))
        ids += [b["id"]] * len(b["_variante"])
        stops += b["_variante"]
    M = _matrix(stops)
    fills = [b["umplere"] / 100 for b in used]
    demands = [0] * len(stops)
    for nodes, f, b in zip(nodes_of, fills, used):
        k = COMPACTARE if b.get("compactare", True) else 1     # cat ocupa in camion, dupa presa
        for n in nodes:
            demands[n] = round(f * b.get("capacitate_l", DEFAULT_BIN_CAPACITY_L) / k)

    predictions, groups, overflow_risk, mandatory = {}, [], set(), set()
    for nodes, b, fill in zip(nodes_of, used, fills):
        rate, _ = estimate(history.get(b["id"], []))
        # orele pana la plin se socotesc din umplerea de ACUM (nu din ultima citire din istoric),
        # ca predictia sa fie aceeasi cu ce vede dispecerul pe harta
        hours_to_full = None if rate is None else max(0.0, (1.0 - fill) / rate)
        predictions[b["id"]] = {
            "hours_to_full": None if hours_to_full is None else round(hours_to_full, 1),
            "predicted_fill_24h": round(min(1.0, fill + (rate or 0) * HORIZON_H), 2),
        }
        # o ghena aproape goala nu trimite camionul doar pe baza predictiei: istoricul unui senzor
        # umplut si golit la teste ar parea o umplere foarte rapida
        will_overflow = hours_to_full is not None and hours_to_full <= HORIZON_H and fill >= MIN_FILL
        if will_overflow:
            overflow_risk.add(b["id"])
        if fill >= MANDATORY_FILL or will_overflow:
            # plina sau se umple in urmatoarele 24 h: se colecteaza sigur
            mandatory.add(b["id"])
            penalty = MANDATORY_PENALTY             # o sare doar daca nu incape in camioane
        elif fill >= MIN_FILL:
            # optionala: o ia doar daca ocolul e mic fata de cat de plina e
            # (50% -> pana la ~1 km ocol, 70% -> pana la ~2 km)
            penalty = int(PENALTY_SCALE * fill ** 2)
        else:
            penalty = 0                             # aproape goala: nu merita drumul
        groups.append((nodes, penalty))

    zone_idx = {n: [t for t in zone[b["id"]] if 0 <= t < NUM_TRUCKS]
                for nodes, b in zip(nodes_of, used) if zone and b["id"] in zone for n in nodes}

    def nepreluate(visited):
        return [(nodes, b) for nodes, b in zip(nodes_of, used) if b["id"] in mandatory and not visited & set(nodes)]

    routes, _, visited = _solve(M, demands, groups, CAPACITATE_UTILA_L, seconds, zone=zone_idx)
    mutate = set()
    # 1) o ghena obligatorie care nu incape in camionul zonei ei poate fi luata de alt camion de
    #    acelasi tip care mai are loc (cate o runda, pana nu mai ajuta)
    for _ in range(3):
        lipsa = [(nodes, b) for nodes, b in nepreluate(visited) if rezerva and rezerva.get(b["id"])]
        if not lipsa:
            break
        for nodes, b in lipsa:
            extra = [t for t in rezerva[b["id"]] if 0 <= t < NUM_TRUCKS]
            for n in nodes:
                if n in zone_idx:
                    zone_idx[n] = sorted(set(zone_idx[n]) | set(extra))
            mutate.add(b["id"])
        inainte = len(nepreluate(visited))
        routes, _, visited = _solve(M, demands, groups, CAPACITATE_UTILA_L, seconds, zone=zone_idx)
        if len(nepreluate(visited)) >= inainte:
            break
    if nepreluate(visited):
        # 2) ultima solutie: nici flota nu are loc intr-o singura cursa, deci camioanele fac mai multe
        #    curse (cand se umplu merg la groapa si continua); solverul nu vede ocolul pana la groapa,
        #    deci il lasam sa faca asta doar cand chiar e nevoie
        routes, _, visited = _solve(M, demands, groups, CAPACITATE_UTILA_L * MAX_CURSE, seconds, zone=zone_idx)

    # locul ales pentru fiecare pubela vizitata (sensul strazii); cele sarite raman pe varianta preferata
    groapa, _ = _snap(GROAPA["lat"], GROAPA["lng"])
    st = {"depot": depot, "groapa": groapa, **{b["id"]: b["_stop"] for b in used}}
    for n in visited:
        if n:
            st[ids[n]] = stops[n]
    vol = {ids[n]: demands[n] for nodes in nodes_of for n in nodes}
    tmp = {"_stops": st, "_vol": vol}
    for r in routes:
        # descarcarile: inainte de ghena care nu mai incape si la final, inainte de depou
        r["stops"] = ["depot"] + _cu_descarcari(tmp, r["truck"], [ids[i] for i in r["stops"][1:-1]]) + ["depot"]
        r["descarcari"] = sum(1 for s in r["stops"] if e_groapa(s))
        r["distance_m"] = round(sum(_leg_m(st[a], st[b]) for a, b in zip(r["stops"], r["stops"][1:])))
        r["geometry"] = _geometry([st[x] for x in r["stops"]])
    total = sum(r["distance_m"] for r in routes)
    vizitate = {ids[n] for n in visited if n}

    unserved = sorted(mandatory - vizitate)
    if unserved:
        warnings.append(f"Nu incap azi in camioane, desi sunt obligatorii: {', '.join(unserved)}")

    metrics = {
        "total_km": round(total / 1000, 2),
        "trucks_used": sum(1 for r in routes if len(r["stops"]) > 2),
        "bins_total": len(used),
        "bins_collected": len(vizitate),
        "bins_skipped": len(used) - len(vizitate),
        "overflows_prevented": len(overflow_risk & vizitate),
        "mandatory_unserved": len(unserved),
        # ghene obligatorii luate de alt camion de acelasi tip, fiindca al zonei era plin
        "bins_reassigned": sum(1 for r in routes for s in r["stops"]
                               if s in mutate and r["truck"] not in (zone or {}).get(s, [])),
    }
    if compare_baseline:
        # referinta = colectarea tuturor pubelelor fara optimizare (din aproape in aproape, cum ar
        # face un sofer fara aplicatie); o calculam o data pentru acelasi set de pubele
        key = (tuple(ids), tuple(sorted((i, tuple(t)) for i, t in zone_idx.items())))
        if key not in _baseline_cache:
            base = _solve(M, demands, [(nodes, MANDATORY_PENALTY) for nodes in nodes_of],
                          TRUCK_CAPACITY_L * MAX_CURSE, BASELINE_SECONDS, greedy=True, zone=zone_idx)
            # si referinta merge la groapa cand se umple, ca sa comparam la fel
            dist = 0
            for r in base[0]:
                pts = [stops[0]] + [groapa if n is None else stops[n]
                                    for n in _descarcari(r["stops"][1:-1], lambda n: demands[n])] + [stops[0]]
                dist += sum(_leg_m(a, b) for a, b in zip(pts, pts[1:]))
            _baseline_cache[key] = dist
        metrics["baseline_km"] = round(_baseline_cache[key] / 1000, 2)
        metrics["saving_pct"] = round(100 * (1 - total / _baseline_cache[key]), 1)

    return {
        "total_distance_m": total,
        "routes": routes,
        "skipped": [b["id"] for b in used if b["id"] not in vizitate],
        "predictions": predictions,
        "metrics": metrics,
        "alerts": [],
        "warnings": warnings,
        "_stops": st,                            # intern: id -> locul de pe strada, pentru alerte si desen
        "_vol": vol,                             # intern: id -> litrii colectati acolo, pentru descarcari
    }


def insertion_cost(plan, lat, lng, volume_l=0):
    """Cati metri in plus ar costa sa adaugi un punct in cea mai buna ruta (None daca nu incape)."""
    variante, off = _variante(lat, lng)
    if off > MAX_SNAP_M:
        return None
    st = plan["_stops"]
    costs = [_leg_m(st[s[k]], x) + _leg_m(x, st[s[k + 1]]) - _leg_m(st[s[k]], st[s[k + 1]])
             for x in variante
             for r in plan["routes"] if r["load_l"] + volume_l <= CAPACITATE_UTILA_L * MAX_CURSE
             for s in [r["stops"]] for k in range(len(s) - 1)]
    return int(min(costs)) if costs else None


def add_alert(plan, alert, kind="alert"):
    """alert = {"id", "lat", "lng"} plus "volum_l" (camera) sau "marime": mica/medie/mare (cetatean).

    kind="alert" pentru depozitari ilegale, kind="bin" pentru o pubela sarita care a devenit
    urgenta in timpul zilei (camioanele sunt deja pe drum, deci nu refacem tot planul).
    Modifica planul pe loc. Daca niciun camion nu are loc, punctul ramane "pending"
    pentru urmatoarea planificare, in loc sa dea eroare. Descarcarile de pe ruta nu se refac aici:
    apelantul cheama reordoneaza(), care stie ce e deja in camion.
    """
    volume = alert.get("volum_l")
    if volume is None:
        volume = ALERT_SIZES_L.get(alert.get("marime", "medie"), ALERT_SIZES_L["medie"])
    # cat ocupa in camion: o ghena se compacteaza, o depozitare ilegala nu
    incarcare = round(volume / COMPACTARE) if kind == "bin" else volume
    try:
        variante, off = _variante(float(alert["lat"]), float(alert["lng"]))
    except (KeyError, TypeError, ValueError):
        return {"id": alert.get("id"), "status": "rejected", "reason": "coordonate invalide"}
    if off > MAX_SNAP_M:
        return {"id": alert["id"], "status": "rejected", "reason": f"la {off:.0f} m de harta"}

    st = plan["_stops"]

    def cel_mai_bun(camioane, limita):
        best = None
        for r in plan["routes"]:
            if r["load_l"] + incarcare > limita:
                continue
            if camioane is not None and r["truck"] not in camioane:
                continue
            s = r["stops"]
            for k in range(len(s) - 1):
                a, b = st[s[k]], st[s[k + 1]]
                for x in variante:           # oricare sens al strazii, cel mai ieftin (cu tot cu viraje)
                    cost = _leg(a, x) + _leg(x, b) - _leg(a, b)
                    if best is None or cost < best[0]:
                        best = (cost, r, k + 1, x)
        return best

    # ordinea: camionul zonei daca mai are loc; altfel alt camion de acelasi tip care mai are loc;
    # abia apoi o cursa in plus pana la groapa (camionul zonei, apoi celelalte de acelasi tip)
    zona = alert.get("camioane") or None             # None = oricare camion
    rezerva = alert.get("camioane_rezerva") or None
    trepte = [(zona, CAPACITATE_UTILA_L)]
    if rezerva:
        trepte.append((rezerva, CAPACITATE_UTILA_L))
    trepte.append((zona, CAPACITATE_UTILA_L * MAX_CURSE))
    if rezerva:
        trepte.append((rezerva, CAPACITATE_UTILA_L * MAX_CURSE))
    best = None
    for camioane, limita in trepte:
        best = cel_mai_bun(camioane, limita)
        if best is not None:
            break

    result = {"id": alert["id"], "kind": kind, "lat": alert["lat"], "lng": alert["lng"],
              "volum_l": volume}
    if best is None:
        result.update(status="pending", reason="niciun camion nu mai are loc azi")
        plan["alerts"].append(result)
        plan["warnings"].append(f"Alerta {alert['id']} asteapta: niciun camion nu mai are loc azi")
        return result

    _, route, pos, x = best
    a, b = st[route["stops"][pos - 1]], st[route["stops"][pos]]
    extra = _leg_m(a, x) + _leg_m(x, b) - _leg_m(a, b)     # metrii reali in plus
    dedicated = len(route["stops"]) == 2
    st[alert["id"]] = x
    plan.setdefault("_vol", {})[alert["id"]] = incarcare
    route["stops"].insert(pos, alert["id"])
    route["geometry"] = _draw(plan, route["stops"])
    route["distance_m"] += int(extra)
    route["load_l"] += incarcare
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


def reordoneaza(plan, truck, facute, start=None, incarcat=0):
    """Reordoneaza opririle ramase ale camionului pornind de unde e acum si reface descarcarile.

    Ordinea din planul de dimineata e optima pentru tot traseul pornit din depou; daca soferul
    bifeaza mai devreme, ia alt drum sau apare o oprire noua, urmatoarea oprire din lista poate sa
    nu mai fie cea mai buna. start = (lat, lng) de la GPS (optional); fara el, pleaca de la ultima
    oprire facuta. incarcat = litrii deja in camion: daca nu mai incape urmatoarea oprire (sau e
    plin), prima oprire devine groapa de gunoi.
    """
    r = next((x for x in plan["routes"] if x["truck"] == truck), None)
    if r is None:
        return
    st = plan["_stops"]
    facute_aici = [s for s in r["stops"] if s != "depot" and s in facute]
    ramase = [s for s in r["stops"] if s != "depot" and s not in facute and not e_groapa(s)]
    ordine = ramase
    if len(ramase) >= 2:
        x = st[facute_aici[-1]] if facute_aici else st["depot"]
        if start is not None:
            gps, off = _snap(float(start[0]), float(start[1]))
            if off <= MAX_SNAP_M:
                x = gps
        puncte = [x] + [st[s] for s in ramase] + [st["depot"]]
        M = _matrix(puncte)
        n = len(puncte)
        manager = pywrapcp.RoutingIndexManager(n, 1, [0], [n - 1])   # pleaca de unde e, se termina la depou
        routing = pywrapcp.RoutingModel(manager)
        cb = routing.RegisterTransitCallback(lambda a, b: int(M[manager.IndexToNode(a)][manager.IndexToNode(b)]))
        routing.SetArcCostEvaluatorOfAllVehicles(cb)
        params = pywrapcp.DefaultRoutingSearchParameters()
        params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        params.time_limit.FromMilliseconds(500)
        sol = routing.SolveWithParameters(params)
        if sol is not None:
            ordine, idx = [], sol.Value(routing.NextVar(routing.Start(0)))
            while not routing.IsEnd(idx):
                ordine.append(ramase[manager.IndexToNode(idx) - 1])
                idx = sol.Value(routing.NextVar(idx))
    # descarcarile facute raman cu id-ul lor; cele noi se numeroteaza dupa ele
    nr = 1 + max([int(s.rsplit("-", 1)[1]) for s in facute_aici if e_groapa(s)], default=0)
    _seteaza_ruta(plan, r, ["depot"] + facute_aici + _cu_descarcari(plan, truck, ordine, incarcat, nr) + ["depot"])


def scoate_oprire(plan, sid):
    """Scoate o oprire din ruta (de exemplu o depozitare care a disparut), fara sa o considere facuta.
    Intoarce camionul de pe a carui ruta a iesit (None daca nu era pe nicio ruta)."""
    for a in plan.get("alerts", []):
        if a["id"] == sid:
            a["status"] = "gone"
    r = next((x for x in plan["routes"] if sid in x["stops"]), None)
    if r is None:
        return None
    _seteaza_ruta(plan, r, [s for s in r["stops"] if s != sid])
    r["load_l"] -= plan.get("_vol", {}).pop(sid, 0)
    return r["truck"]               # apelantul reface descarcarile cu reordoneaza()
