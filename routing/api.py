"""Leaga datele echipei de motorul de rutare, fara sa modifice nimic din partea lor.

Cum circula datele (cum a stabilit echipa):
- senzorii lui Mihai trimit la backend-ul lui Robert, care scrie umplerea in tabela trashes;
  aici doar CITIM tabela, la cateva secunde (watch / poll_once), si de acolo facem
  filtrarea, istoricul pentru predictie si inserarea pubelelor care devin urgente;
- camera: Daria trimite coordonatele camerei la backend, iar backend-ul cheama
  POST /api/alerte cu {"lat", "lng", "sursa": "camera"};
- datele noastre stau in tabele separate (rutare_*); pe langa pubelele reale din trashes,
  planul include si cele 60 de ghene simulate din sim_bins.json, ca orasul sa arate realist.

Fluxul zilei:
- dimineata: GET /api/rute (sau POST /api/rute/planifica) face planul;
- in timpul zilei camioanele sunt pe drum, deci NU refacem planul: o pubela sarita care
  devine urgenta si fiecare depozitare ilegala sunt inserate cu cost minim in ruta existenta.
"""
import os
import sys
import json
import math
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
import io

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))       # modulele din routing/ se importa intre ele fara pachet

import psycopg
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from PIL import Image
from psycopg.rows import dict_row
from pydantic import BaseModel

import dumping
from auth import CAMION, ROLURI, cere_rol
import routing_engine as engine
import sensor_filter as sf
from explain import explain_plan
from predict import estimate
from filtru_gemini import pregateste, verifica

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]

POLL_S = 3                      # la cate secunde citim tabela trashes
EMPTY_DROP = 30                 # o scadere brusca de 30+ puncte = pubela a fost golita
HISTORY_SLOT = "1 minute"       # citirile se mediaza pe intervale, pentru regresie
MIN_HISTORY_H = 1.0             # istoricul real conteaza doar daca acopera cel putin o ora
MAX_EVENTS = 50
RAPORTARE_PCT = 5               # o schimbare de umplere de cel putin 5 puncte apare in fluxul live
FOLOSESTE_SIMULATE = True       # True = pe langa ghenele din trashes, si cele 60 de ghene simulate din sim_bins.json

# ---------- zone si pubele cu formula (doar pentru prototip) ----------
# Orasul e impartit in 3 sectoare pornind din centru (ca feliile unui tort), fiecare cu
# camioanele lui si cu un senzor real. O ghena intra in sectorul directiei in care se afla
# fata de centru, deci fiecare zona e compacta si rutele nu traverseaza orasul.
CENTRU = (45.748, 21.235)
ZONE = {   # unghiul se masoara de la nord, in sensul acelor de ceasornic
    0: {"nume": "Vest",     "senzor": "TM-011", "de_la": 210, "pana_la": 330},
    1: {"nume": "Nord-Est", "senzor": "TM-016", "de_la": 330, "pana_la": 90},
    2: {"nume": "Sud",      "senzor": "TM-015", "de_la": 90,  "pana_la": 210},
}
SENZORI = {d["senzor"] for d in ZONE.values()}   # doar ele au citiri reale, deci predictie de umplere
# 6 camioane: TM 01-03 strang deseurile menajere, TM 04-06 reciclabilele; cate unul din fiecare pe zona
CAMION_MENAJER = {z: z for z in ZONE}
CAMION_RECICLABIL = {z: z + len(ZONE) for z in ZONE}
TIP_CAMION = {**{c: "menajer" for c in CAMION_MENAJER.values()}, **{c: "reciclabil" for c in CAMION_RECICLABIL.values()}}
CAMIOANE_MENAJERE = sorted(CAMION_MENAJER.values())   # depozitarile ilegale le iau doar camioanele menajere
# soferii isi recunosc camionul dupa numarul de inmatriculare (numere inventate, pentru prototip)
NUMERE_CAMIOANE = ["TM 01 BIN", "TM 02 BIN", "TM 03 BIN", "TM 04 BIN", "TM 05 BIN", "TM 06 BIN"]


def zona_pentru(lat, lng):
    """Sectorul in care cade un punct, dupa directia lui fata de centru."""
    dy = lat - CENTRU[0]
    dx = (lng - CENTRU[1]) * math.cos(math.radians(CENTRU[0]))
    unghi = (math.degrees(math.atan2(dx, dy)) + 360) % 360
    for z, d in ZONE.items():
        if d["de_la"] <= d["pana_la"]:
            if d["de_la"] <= unghi < d["pana_la"]:
                return z
        elif unghi >= d["de_la"] or unghi < d["pana_la"]:   # sectorul care trece prin nord
            return z
    return 0


# Unele pubele hardcodate isi iau umplerea dintr-o formula aplicata pe senzorul zonei lor,
# ca sa se miste odata cu senzorii reali. Formule diferite, ca sa nu se miste toate la fel.
FORMULE = {
    "TM-012": ("TM-011", lambda x: 100 - x),          # Vest
    "TM-005": ("TM-011", lambda x: 0.6 * x + 30),     # Vest
    "TM-006": ("TM-011", lambda x: 0.4 * x + 50),     # Vest
    "TM-017": ("TM-011", lambda x: 0.7 * x + 20),     # Vest
    "TM-001": ("TM-016", lambda x: 100 - x),          # Nord-Est
    "TM-008": ("TM-016", lambda x: 0.5 * x + 40),     # Nord-Est
    "TM-002": ("TM-015", lambda x: 100 - x),          # Sud
}

# ghenele simulate, raspandite pe toata harta (vezi generate_sim_bins.py)
with open(HERE / "sim_bins.json") as f:
    SIM = {b["id"]: b for b in json.load(f)["bins"]}
_TM_POZ = {"TM-001": (45.774718, 21.2633061), "TM-002": (45.744593, 21.267267), "TM-003": (45.7121271, 21.198657),
           "TM-004": (45.77612, 21.2135825), "TM-005": (45.76, 21.197), "TM-006": (45.766, 21.218),
           "TM-007": (45.779, 21.226), "TM-008": (45.7785, 21.2255), "TM-009": (45.778, 21.207),
           "TM-010": (45.7588, 21.2135), "TM-011": (45.746, 21.224), "TM-012": (45.7482, 21.2045),
           "TM-013": (45.748, 21.204), "TM-014": (45.7485, 21.205), "TM-015": (45.743, 21.237),
           "TM-016": (45.755, 21.245), "TM-017": (45.7585, 21.224)}
ZONA_CAMION = {bid: zona_pentru(*p) for bid, p in _TM_POZ.items()}           # ghena -> zona
ZONA_CAMION.update({bid: zona_pentru(b["lat"], b["lon"]) for bid, b in SIM.items()})
# ce strange fiecare ghena: o ghena de reciclabile o goleste doar camionul de reciclabile al zonei
RECICLABILE = {bid for bid, b in SIM.items() if b.get("deseu") == "reciclabil"} | {"TM-015"}
CAMIOANE_PUBELA = {bid: [CAMION_RECICLABIL[z] if bid in RECICLABILE else CAMION_MENAJER[z]]
                   for bid, z in ZONA_CAMION.items()}

router = APIRouter(prefix="/api")
_lock = threading.Lock()        # planul e unul singur si e modificat din mai multe fire
_state = {"plan": None, "bins": None, "surse": {}, "urgent": {}, "seen": None, "evenimente": [],
          "raportat": None,     # ultima umplere aratata in fluxul live, pe ghena
          "facute": set(),      # opririle bifate azi de soferi (pubele golite, depozitari colectate)
          "pozitii": {}}        # ultima pozitie GPS a fiecarui camion; doar in memorie, nu in DB

with open(HERE / "history.json") as f:
    _SIM_HISTORY = json.load(f)       # istoric simulat pentru pubelele simulate

SINCE_EMPTY = """LEFT JOIN rutare_golire g ON g.bin_id = c.bin_id
                 WHERE c.ts >= coalesce(g.golita_la, '-infinity')"""


def _trashes(conn):
    """Randurile din trashes, cu umplerea pubelelor din FORMULE calculata din senzorul lor."""
    if not _has_trashes(conn):
        return []
    rows = [dict(r) for r in conn.execute("SELECT * FROM trashes ORDER BY id").fetchall()]
    by_id = {r["id"]: r for r in rows}
    for r in rows:
        if r["id"] not in FORMULE:
            continue
        sid, f = FORMULE[r["id"]]
        src = by_id.get(sid)
        x = None if src is None else sf.valid(src["umplere"])
        if x is None:
            continue                     # senzor lipsa sau citire invalida: ramane valoarea fixa
        noua = round(min(max(f(x), 0), 100))
        if noua != r["umplere"]:
            # valoarea calculata ajunge si in baza de date, ca toata echipa sa vada aceeasi umplere
            conn.execute("UPDATE trashes SET umplere = %s, actualizat = %s WHERE id = %s",
                         (noua, src["actualizat"], r["id"]))
        r["umplere"] = noua
        r["actualizat"] = src["actualizat"]   # o citire noua a senzorului = o citire noua aici
    return rows


def _connect():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    sim = list(SIM.values())
    with _connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS rutare_pubele (
            id TEXT PRIMARY KEY, lat DOUBLE PRECISION, lng DOUBLE PRECISION,
            umplere INTEGER, actualizat TIMESTAMPTZ DEFAULT now(),
            capacitate_l INTEGER DEFAULT 1100, golita_la TIMESTAMPTZ)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS rutare_citiri (
            id BIGSERIAL PRIMARY KEY, bin_id TEXT NOT NULL,
            umplere REAL, ts TIMESTAMPTZ NOT NULL DEFAULT now())""")   # umplere NULL = valoare invalida
        conn.execute("CREATE INDEX IF NOT EXISTS rutare_citiri_bin_ts ON rutare_citiri (bin_id, ts)")
        conn.execute("""CREATE TABLE IF NOT EXISTS rutare_golire (
            bin_id TEXT PRIMARY KEY, golita_la TIMESTAMPTZ NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS rutare_alerte (
            id TEXT PRIMARY KEY, sursa TEXT, camera_id TEXT,
            lat DOUBLE PRECISION, lng DOUBLE PRECISION, marime TEXT, volum_l INTEGER,
            nr_raportari INTEGER DEFAULT 1, status TEXT DEFAULT 'deschisa', camion INTEGER,
            creata TIMESTAMPTZ DEFAULT now(), inchisa TIMESTAMPTZ)""")
        conn.execute("ALTER TABLE rutare_alerte ADD COLUMN IF NOT EXISTS descriere TEXT")
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO rutare_pubele (id, lat, lng, umplere, capacitate_l)
                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
                [(b["id"], b["lat"], b["lon"], round(b["fill_level"] * 100), b["capacity_l"])
                 for b in sim])
            # si in tabela trashes a backend-ului, ca ghenele simulate sa arate peste tot ca unele reale
            if conn.execute("SELECT to_regclass('trashes') IS NOT NULL AS ok").fetchone()["ok"]:
                cur.executemany(
                    """INSERT INTO trashes (id, lat, lng, umplere, actualizat, nume, adresa, tip)
                       VALUES (%s, %s, %s, %s, now(), %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
                    [(b["id"], b["lat"], b["lon"], round(b["fill_level"] * 100), b["nume"], b["adresa"],
                      "ghena_reciclabile" if b.get("deseu") == "reciclabil" else "ghena") for b in sim])
                cur.executemany("UPDATE trashes SET tip = %s WHERE id = %s",
                                [("ghena_reciclabile" if b.get("deseu") == "reciclabil" else "ghena", b["id"])
                                 for b in sim])


init_db()


def _has_trashes(conn):
    return conn.execute("SELECT to_regclass('trashes') IS NOT NULL AS ok").fetchone()["ok"]


def _recent(conn, bin_id, limit=15):
    """Ultimele citiri de la golire incoace, cele mai vechi primele: [{"umplere", "ts"}]."""
    rows = conn.execute(f"""SELECT c.umplere, c.ts FROM rutare_citiri c {SINCE_EMPTY} AND c.bin_id = %s
                            ORDER BY c.ts DESC, c.id DESC LIMIT {limit}""", (bin_id,)).fetchall()
    return list(reversed(rows))


def _recent_levels(conn, bin_id):
    return [r["umplere"] for r in _recent(conn, bin_id)]


def _emptied_at(before, value):
    """Momentul golirii, daca ultimele doua valori sunt ambele cu EMPTY_DROP sub mediana de dinainte.

    Doua valori la rand, nu una: o singura citire gresita (senzorul trimite 0 cand nu raspunde)
    nu trebuie sa stearga istoricul pubelei.
    """
    good = [r for r in before if r["umplere"] is not None]
    if value is None or len(good) < 2:
        return None
    base = sf.smooth([r["umplere"] for r in good[:-1]])
    if good[-1]["umplere"] <= base - EMPTY_DROP and value <= base - EMPTY_DROP:
        return good[-1]["ts"]
    return None


def _event(e):
    e = {**e, "la": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _state["evenimente"] = (_state["evenimente"] + [e])[-MAX_EVENTS:]
    return e


# ---------- urmarirea tabelei trashes (senzorii reali) ----------

def _became_urgent(bin_row, level):
    plan = _state["plan"]
    bid = bin_row["id"]
    if plan is None:
        return _event({"id": bid, "status": "urgenta", "reason": "inca nu exista un plan al zilei"})
    if bid not in plan["skipped"]:
        return _event({"id": bid, "status": "urgenta", "reason": "e deja pe o ruta"})
    r = engine.add_alert(plan, {"id": bid, "lat": bin_row["lat"], "lng": bin_row["lng"],
                                "volum_l": round(engine.DEFAULT_BIN_CAPACITY_L * level / 100),
                                "camioane": CAMIOANE_PUBELA.get(bid)}, kind="bin")
    return _event(r)


def _poll(conn):
    """Citeste trashes; fiecare valoare noua (actualizat schimbat) devine o citire. Fara lock."""
    if not _has_trashes(conn):
        return []
    if _state["seen"] is None:      # dupa repornire nu reluam citirile deja salvate
        _state["seen"] = {r["bin_id"]: r["ts"] for r in conn.execute(
            "SELECT bin_id, max(ts) AS ts FROM rutare_citiri GROUP BY bin_id").fetchall()}
    seen, events = _state["seen"], []
    rows = _trashes(conn)
    if _state["raportat"] is None:
        # dupa repornire pornim de la valorile curente: planul le ia oricum, deci fara evenimente false
        vals = {r["id"]: sf.valid(r["umplere"]) for r in rows}
        _state["raportat"] = {k: v for k, v in vals.items() if v is not None}
        _state["urgent"] = {k: v >= engine.MANDATORY_FILL * 100 for k, v in _state["raportat"].items()}
    for r in rows:
        ts, bid = r["actualizat"], r["id"]
        if ts is None or (bid in seen and ts <= seen[bid]):
            continue
        seen[bid] = ts
        value = sf.valid(r["umplere"])
        emptied = _emptied_at(_recent(conn, bid), value)
        if emptied is not None:
            # golire: istoricul de umplere si starea de urgenta incep de la zero
            conn.execute("""INSERT INTO rutare_golire (bin_id, golita_la) VALUES (%s, %s)
                            ON CONFLICT (bin_id) DO UPDATE SET golita_la = EXCLUDED.golita_la""",
                         (bid, emptied))
            _state["urgent"][bid] = False
            _state["facute"].add(bid)
            events.append(_event({"id": bid, "status": "golita"}))
        conn.execute("INSERT INTO rutare_citiri (bin_id, umplere, ts) VALUES (%s, %s, %s)", (bid, value, ts))
        if value is None:
            continue
        # fiecare schimbare vizibila de umplere apare in fluxul din dashboard (pragul de 5 puncte
        # tine deoparte zgomotul senzorului, care trimite o citire la 2 secunde)
        last = _state["raportat"].get(bid)
        if last is None or abs(value - last) >= RAPORTARE_PCT or (value > 0) != (last > 0):
            _state["raportat"][bid] = value
            if last is not None and emptied is None:
                events.append(_event({"id": bid, "status": "citire", "umplere": round(value),
                                      "inainte": round(last)}))
        # o ghena care trece de pragul de umplere obligatorie intra pe ruta din mers
        was = _state["urgent"].get(bid, False)
        urgent = value >= engine.MANDATORY_FILL * 100
        _state["urgent"][bid] = urgent
        if urgent and not was and emptied is None:
            events.append(_became_urgent(r, value))
    return events


def poll_once():
    with _lock, _connect() as conn:
        return _poll(conn)


def watch(stop):
    """Bucla pornita de main.py: citeste trashes la fiecare POLL_S secunde."""
    while not stop.wait(POLL_S):
        try:
            poll_once()
        except Exception as e:          # o eroare de retea nu trebuie sa opreasca urmarirea
            print("rutare: citirea trashes a esuat:", e)


# ---------- pregatirea datelor pentru rutare ----------

def _db_history(conn):
    """Istoricul real (de la ultima golire), in formatul din predict.py."""
    rows = conn.execute(f"""
        SELECT c.bin_id, extract(epoch FROM date_bin('{HISTORY_SLOT}', c.ts, timestamptz '2000-01-01') - now()) / 3600 AS t_h,
               avg(c.umplere) / 100 AS level
        FROM rutare_citiri c {SINCE_EMPTY}
          AND c.umplere IS NOT NULL AND c.ts > now() - interval '48 hours'
        GROUP BY 1, 2 ORDER BY 1, 2""").fetchall()
    hist = {}
    for r in rows:
        hist.setdefault(r["bin_id"], []).append({"t_h": float(r["t_h"]), "level": float(r["level"])})
    return hist


def _current_bins(conn):
    """Pubelele reale (trashes, cu umplerea filtrata) plus cele simulate."""
    real = _trashes(conn)
    real_ids = {r["id"] for r in real}
    sim = [r for r in conn.execute(
        "SELECT id, lat, lng, umplere, capacitate_l FROM rutare_pubele WHERE id = ANY(%s) ORDER BY id",
        (list(SIM),)).fetchall()
        if r["id"] not in real_ids] if FOLOSESTE_SIMULATE else []
    stats = {r["bin_id"]: r for r in conn.execute(f"""
        SELECT c.bin_id, max(c.ts) FILTER (WHERE c.umplere IS NOT NULL) AS last_good, max(c.ts) AS last_any
        FROM rutare_citiri c {SINCE_EMPTY} GROUP BY c.bin_id""").fetchall()}

    history = {k: v for k, v in _SIM_HISTORY.items()}
    # cateva minute de citiri nu spun nimic despre rata de umplere (regresia iese plata si
    # predictia "se umple in 8000 de ore"); pana se aduna o ora de date ramane istoricul simulat
    # predictia de umplere doar pentru pubelele cu senzor: la cele hardcodate valorile sunt
    # schimbate de mana sau din formula, deci o "rata de umplere" calculata din ele ar fi falsa
    history.update({k: v for k, v in _db_history(conn).items()
                    if k in SENZORI and len(v) >= 3 and v[-1]["t_h"] - v[0]["t_h"] >= MIN_HISTORY_H})

    now = datetime.now(timezone.utc)
    bins, surse = [], {}
    for r in real:
        s = stats.get(r["id"])
        raw = sf.valid(r["umplere"])
        if raw is not None:
            # rutam dupa valoarea din baza de date, exact cea de pe harta
            fill = {"umplere": round(raw), "sursa": "senzor", "motiv": None}
            surse[r["id"]] = fill
            bins.append({"id": r["id"], "lat": r["lat"], "lng": r["lng"], "umplere": fill["umplere"],
                         "capacitate_l": SIM.get(r["id"], {}).get("capacity_l", engine.DEFAULT_BIN_CAPACITY_L)})
            continue
        # citire invalida in baza de date: estimam din istoric, ca sa nu ratam o pubela plina
        if s:
            level, last_good, last_any = sf.smooth(_recent_levels(conn, r["id"])), s["last_good"], s["last_any"]
        else:   # inca necitita de bucla: folosim direct valoarea din trashes
            level = sf.valid(r["umplere"])
            last_good = r["actualizat"] if level is not None else None
            last_any = r["actualizat"]
        rate, _ = estimate(history.get(r["id"], []))
        fill = sf.routing_fill(level, last_good, last_any, now, None if rate is None else rate * 100)
        surse[r["id"]] = fill
        bins.append({"id": r["id"], "lat": r["lat"], "lng": r["lng"], "umplere": fill["umplere"],
                     "capacitate_l": SIM.get(r["id"], {}).get("capacity_l", engine.DEFAULT_BIN_CAPACITY_L)})
    for r in sim:
        bins.append({"id": r["id"], "lat": r["lat"], "lng": r["lng"], "umplere": r["umplere"],
                     "capacitate_l": r["capacitate_l"] or engine.DEFAULT_BIN_CAPACITY_L})
    return bins, history, surse


def _public(plan):
    return {k: v for k, v in plan.items() if not k.startswith("_")}


def _build_plan(conn):
    _poll(conn)                     # planul porneste de la cele mai noi valori din trashes
    bins, history, surse = _current_bins(conn)
    # depozitarile inca necolectate intra direct in optimizare, ca opriri obligatorii, ca solverul
    # sa le puna unde se potrivesc in ruta (nu doar lipite dupa); oricare camion le poate lua
    alerte = conn.execute("""SELECT * FROM rutare_alerte WHERE status IN ('deschisa', 'asignata')
                             ORDER BY creata""").fetchall()
    puncte = [{"id": a["id"], "lat": a["lat"], "lng": a["lng"], "umplere": 100,
               "capacitate_l": a["volum_l"] or engine.ALERT_SIZES_L["medie"]} for a in alerte]
    zone = {**CAMIOANE_PUBELA, **{a["id"]: CAMIOANE_MENAJERE for a in alerte}}
    plan = engine.plan_routes(bins + puncte, history, compare_baseline=True, zone=zone)
    for bid, s in surse.items():
        if s["sursa"] != "senzor":
            plan["warnings"].append(f"{bid}: {s['motiv']}")
    _state.update(plan=plan, bins=bins, surse=surse, facute=set())
    pe_ruta = {s: r["truck"] for r in plan["routes"] for s in r["stops"]}
    for a in alerte:
        plan["predictions"].pop(a["id"], None)
        if a["id"] in pe_ruta:
            plan["alerts"].append({"id": a["id"], "kind": "alert", "lat": a["lat"], "lng": a["lng"],
                                   "volum_l": a["volum_l"], "status": "assigned", "truck": pe_ruta[a["id"]]})
            conn.execute("UPDATE rutare_alerte SET status = 'asignata', camion = %s WHERE id = %s",
                         (pe_ruta[a["id"]], a["id"]))
        else:   # nu incape azi: ramane deschisa si intra din mers, daca se elibereaza loc
            if a["id"] in plan["skipped"]:
                plan["skipped"].remove(a["id"])
            _assign(conn, a)
    return plan


def _assign(conn, alert):
    """Insereaza o alerta in planul curent si salveaza rezultatul in DB."""
    plan = _state["plan"]
    if plan is None:
        return {"id": alert["id"], "status": "deschisa", "reason": "inca nu exista un plan al zilei"}
    r = engine.add_alert(plan, {"id": alert["id"], "lat": alert["lat"], "lng": alert["lng"],
                                "volum_l": alert["volum_l"], "camioane": CAMIOANE_MENAJERE})
    status = {"assigned": "asignata", "rejected": "respinsa"}.get(r["status"], "deschisa")
    conn.execute("UPDATE rutare_alerte SET status = %s, camion = %s WHERE id = %s",
                 (status, r.get("truck"), alert["id"]))
    return r


def _register_alert(conn, sursa, lat, lng, marime, camera_id=None):
    if marime not in engine.ALERT_SIZES_L:
        marime = "medie"
    open_alerts = conn.execute(
        "SELECT id, lat, lng FROM rutare_alerte WHERE status IN ('deschisa', 'asignata')").fetchall()
    dup = dumping.find_duplicate(open_alerts, lat, lng)
    if dup:
        conn.execute("UPDATE rutare_alerte SET nr_raportari = nr_raportari + 1 WHERE id = %s", (dup["id"],))
        return {"id": dup["id"], "status": "duplicat",
                "reason": f"exista deja o alerta deschisa la sub {dumping.MERGE_RADIUS_M} m"}
    n = conn.execute("SELECT count(*) AS n FROM rutare_alerte").fetchone()["n"]
    alert = {"id": f"AL-{n + 1:04d}", "lat": lat, "lng": lng, "volum_l": engine.ALERT_SIZES_L[marime]}
    conn.execute("""INSERT INTO rutare_alerte (id, sursa, camera_id, lat, lng, marime, volum_l)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                 (alert["id"], sursa, camera_id, lat, lng, marime, alert["volum_l"]))
    return {**_assign(conn, alert), "sursa": sursa, "marime": marime}


def precalculeaza():
    """Pornit de main.py la startup: planul e gata pana deschide cineva dashboard-ul."""
    try:
        with _lock, _connect() as conn:
            if _state["plan"] is None:
                _build_plan(conn)
                print("rutare: planul zilei e calculat")
    except Exception as e:
        print("rutare: planul initial nu s-a putut calcula:", e)


# ---------- rute ----------

def _out(plan):
    return {**_public(plan), "surse": _state["surse"], "evenimente": _state["evenimente"],
            "facute": sorted(_state["facute"])}     # opririle bifate azi, pentru progresul camioanelor


@router.get("/rute")
def rute(explicatie: bool = False):
    """Planul curent (cu inserarile facute de la planificare); il calculeaza daca nu exista inca."""
    with _lock, _connect() as conn:
        out = _out(_state["plan"] or _build_plan(conn))
        if explicatie:
            out["explicatie"] = explain_plan(out, _state["bins"])
        return out


@router.get("/camioane")
def camioane():
    return [{"camion": i, "nr": nr, "tip": TIP_CAMION.get(i, "menajer")} for i, nr in enumerate(NUMERE_CAMIOANE)]


def _camionul_meu(date):
    """Camionul soferului logat (None = fara restrictie: login oprit sau dispecer)."""
    if date is None or "admin" in date.get(ROLURI, []):
        return None
    if date.get(CAMION) is None:
        raise HTTPException(403, "Contul tău nu are un camion atribuit. Cere-i dispecerului.")
    return int(date[CAMION])


def _verifica_oprire(date, sid):
    """Un sofer poate bifa doar opririle de pe ruta camionului lui."""
    c = _camionul_meu(date)
    if c is None or _state["plan"] is None:
        return
    r = next((x for x in _state["plan"]["routes"] if x["truck"] == c), None)
    if r is None or sid not in r["stops"]:
        raise HTTPException(403, "Oprirea nu e pe ruta camionului tău")


@router.get("/auth-config")
def auth_config():
    """Ce le trebuie paginilor ca sa porneasca loginul Auth0 (nimic secret: domeniul si Client ID sunt publice)."""
    return {"activ": os.environ.get("AUTH_ACTIV", "false").strip().lower() == "true",
            "domain": os.environ.get("AUTH0_DOMAIN", ""), "client_id": os.environ.get("AUTH0_CLIENT_ID", ""),
            "audience": os.environ.get("AUTH0_AUDIENCE", "https://binary-api")}


@router.post("/demo/reset", dependencies=[Depends(cere_rol("admin"))])
def reset_demo():
    """Butonul de reset pentru demo: aduce orasul la starea de dimineata si replanifica.

    Ghenele simulate revin la umplerea din sim_bins.json, depozitarile si raportarile de test se
    inchid, iar opririle bifate, fluxul live si pozitiile GPS se sterg. Senzorii reali raman cum sunt.
    """
    with _lock, _connect() as conn:
        with conn.cursor() as cur:
            cur.executemany("""UPDATE rutare_pubele SET umplere = %s, actualizat = now(), golita_la = NULL
                               WHERE id = %s""",
                            [(round(b["fill_level"] * 100), b["id"]) for b in SIM.values()])
        if _has_trashes(conn):
            with conn.cursor() as cur:
                cur.executemany("UPDATE trashes SET umplere = %s, actualizat = now() WHERE id = %s",
                                [(round(b["fill_level"] * 100), b["id"]) for b in SIM.values()])
        conn.execute("DELETE FROM rutare_golire WHERE bin_id = ANY(%s)", (list(SIM),))
        conn.execute("""UPDATE rutare_alerte SET status = 'colectata', inchisa = now()
                        WHERE status IN ('deschisa', 'asignata')""")
        _state.update(plan=None, facute=set(), evenimente=[], pozitii={})
        return _out(_build_plan(conn))


@router.post("/rute/planifica", dependencies=[Depends(cere_rol("admin"))])
def planifica():
    """Planul de dimineata: recalculeaza tot din datele actuale."""
    with _lock, _connect() as conn:
        return _out(_build_plan(conn))


@router.post("/pubele/{bin_id}/golita")
def golita(bin_id: str, date: dict | None = Depends(cere_rol("sofer", "admin"))):
    """Pentru pubelele simulate. Cele reale sunt detectate singure, din scaderea brusca a senzorului."""
    with _lock, _connect() as conn:
        _verifica_oprire(date, bin_id)
        found = conn.execute("UPDATE rutare_pubele SET umplere = 0, actualizat = now() WHERE id = %s",
                             (bin_id,)).rowcount
        if _has_trashes(conn):
            if bin_id in SENZORI:
                # ghena cu senzor real: golirea o confirma senzorul, nu scriem peste citirea lui
                found = found or conn.execute("SELECT 1 FROM trashes WHERE id = %s", (bin_id,)).fetchone()
            else:
                # ghena simulata sau hardcodata: golirea se vede in baza de date, ca la una reala
                found = conn.execute("UPDATE trashes SET umplere = 0, actualizat = now() WHERE id = %s",
                                     (bin_id,)).rowcount or found
        if not found:
            raise HTTPException(404, "Ghena nu exista")
        conn.execute("""INSERT INTO rutare_golire (bin_id, golita_la) VALUES (%s, now())
                        ON CONFLICT (bin_id) DO UPDATE SET golita_la = EXCLUDED.golita_la""", (bin_id,))
        _state["urgent"][bin_id] = False
        _state["facute"].add(bin_id)
        _event({"id": bin_id, "status": "golita"})
        _dupa_bifare(bin_id)
    return {"status": "ok"}


def _dupa_bifare(sid):
    """Soferul a bifat o oprire: opririle ramase ale camionului lui se reordoneaza de unde e acum."""
    plan = _state["plan"]
    if plan is None:
        return
    r = next((x for x in plan["routes"] if sid in x["stops"]), None)
    if r is None:
        return
    poz = _state["pozitii"].get(r["truck"])
    proaspat = poz and (datetime.now(timezone.utc) - datetime.fromisoformat(poz["la"])).total_seconds() < 120
    try:
        engine.reordoneaza(plan, r["truck"], _state["facute"], (poz["lat"], poz["lng"]) if proaspat else None)
    except Exception as e:          # reordonarea e un bonus: daca pica, ruta ramane cea de dimineata
        print("rutare: reordonarea a esuat:", e)



# ---------- depozitari ilegale (camera prin backend, sau cetateni) ----------

class Raportare(BaseModel):
    lat: float
    lng: float
    sursa: Literal["camera", "cetatean"] = "cetatean"
    marime: str = "medie"            # mica / medie / mare
    camera_id: str | None = None


@router.post("/alerte")
def alerta(r: Raportare):
    """Backend-ul o cheama cand modelul Dariei detecteaza gunoi; acelasi punct de mai multe ori = o singura alerta."""
    with _lock, _connect() as conn:
        return _register_alert(conn, r.sursa, r.lat, r.lng, r.marime, r.camera_id)


@router.get("/alerte")
def lista_alerte():
    with _connect() as conn:
        return conn.execute("SELECT * FROM rutare_alerte ORDER BY creata DESC").fetchall()


@router.post("/alerte/{alert_id}/colectata")
def alerta_colectata(alert_id: str, date: dict | None = Depends(cere_rol("sofer", "admin"))):
    with _lock, _connect() as conn:
        _verifica_oprire(date, alert_id)
        found = conn.execute("""UPDATE rutare_alerte SET status = 'colectata', inchisa = now()
                                WHERE id = %s AND status <> 'colectata'""", (alert_id,)).rowcount
        if not found:
            raise HTTPException(404, "Alerta nu exista sau e deja colectata")
        _state["facute"].add(alert_id)
        _dupa_bifare(alert_id)
    return {"status": "ok"}


@router.get("/pubele-fizice")
def pubele_fizice():
    """Pubelele reale din trashes, pentru harta din dashboard."""
    with _connect() as conn:
        if not _has_trashes(conn):
            return []
        return [{k: r[k] for k in ("id", "lat", "lng", "umplere")} for r in _trashes(conn)]


@router.get("/pubele")
def toate_pubelele():
    """Pubelele reale si cele simulate, cu umplerea curenta, pentru harta din dashboard."""
    with _connect() as conn:
        bins, _, surse = _current_bins(conn)
        info = {**SIM, **{r["id"]: r for r in _trashes(conn)}}
    out = []
    for b in bins:
        try:
            _, dist_m = engine._snap(float(b["lat"]), float(b["lng"]))
        except Exception:
            continue
        r = info.get(b["id"], {})     # numele si adresa vin din tabela trashes a lui Robert
        out.append({**b, "reala": b["id"] in surse,
                    # departe de orice strada din harta: o aratam, dar nu poate fi pusa pe o ruta
                    "pe_ruta_posibil": dist_m <= engine.MAX_SNAP_M,
                    # pe harta aratam exact valoarea din baza de date; rutarea foloseste in continuare
                    # valoarea filtrata (b["umplere"]), ca o citire gresita sa nu trimita un camion degeaba
                    "umplere": b["umplere"] if b["id"] in SIM else r.get("umplere", b["umplere"]),
                    "umplere_rutare": b["umplere"],
                    "nume": r.get("nume"), "adresa": r.get("adresa"), "tip": r.get("tip"),
                    "zona": ZONE[ZONA_CAMION[b["id"]]]["nume"] if b["id"] in ZONA_CAMION else None,
                    "formula": b["id"] in FORMULE,
                    "deseu": "reciclabil" if b["id"] in RECICLABILE else "menajer"})
    return out


# ---------- raportari de la cetateni (aplicatia lui Mihai) ----------

CAPTURES = HERE / "captures"
CAPTURES.mkdir(exist_ok=True)
STATUS_APLICATIE = {"assigned": "pe_ruta", "duplicat": "duplicat"}


@router.post("/rapoarte")  # deschis si vizitatorilor: poza trece oricum prin filtrul Gemini
def raport_cetatean(poza: UploadFile = File(...), lat: float = Form(...), lng: float = Form(...)):
    """Gemini verifica poza; daca e gunoi, intra ca orice alerta (duplicate, inserare in ruta)."""
    try:
        # intoarsa dupa EXIF inainte de orice conversie (altfel orientarea de pe telefon se pierde)
        imagine = pregateste(Image.open(io.BytesIO(poza.file.read())))
    except Exception:
        raise HTTPException(400, "Fișierul trimis nu e o imagine")
    try:
        verdict = verifica(imagine)     # in afara lock-ului: Gemini raspunde in 1-3 secunde
    except Exception:
        raise HTTPException(503, "Verificarea pozei nu merge acum, încearcă din nou")
    if not verdict["e_gunoi"]:
        return {"acceptat": False, "motiv": verdict.get("motiv") or verdict["descriere"]}

    with _lock, _connect() as conn:
        r = _register_alert(conn, "cetatean", lat, lng, verdict["marime"])
        if r["status"] != "duplicat":   # la duplicat ramane descrierea primului raport
            conn.execute("UPDATE rutare_alerte SET descriere = %s WHERE id = %s",
                         (verdict["descriere"], r["id"]))
    cale = CAPTURES / f"{r['id']}.jpg"
    if not cale.exists():
        imagine.save(cale, "JPEG")
    return {"acceptat": True, "descriere": verdict["descriere"],
            "alerta": {"id": r["id"], "status": STATUS_APLICATIE.get(r["status"], "deschisa")}}


# ---------- soferi (aplicatia lui Mihai) ----------

@router.get("/ruta/{camion}")
def ruta_camion(camion: int):
    """Ruta zilei pentru un camion, cu coordonatele fiecarei opriri, in ordinea in care le ia."""
    with _lock:
        plan = _state["plan"]
        if plan is None:
            raise HTTPException(404, "Nu exista inca un plan al zilei. Dispecerul apasa Planifica pe dashboard.")
        ruta = next((r for r in plan["routes"] if r["truck"] == camion), None)
        if ruta is None:
            raise HTTPException(404, f"Camionul {camion} nu are ruta azi")
        pubele = {b["id"]: b for b in _state["bins"] or []}
        alerte = {a["id"]: a for a in plan["alerts"]}
        stops, geometrie, distanta = list(ruta["stops"]), ruta["geometry"], ruta["distance_m"]
        incarcare, facute = ruta["load_l"], set(_state["facute"])
    with _connect() as conn:            # numele si adresa ghenelor, din tabela lui Robert
        info = {r["id"]: r for r in conn.execute("SELECT id, nume, adresa FROM trashes").fetchall()} \
            if _has_trashes(conn) else {}
    info = {**SIM, **info}              # ghenele simulate au numele strazii din sim_bins.json
    opriri = []
    for sid in stops:
        if sid == "depot":
            continue
        if sid in alerte and alerte[sid].get("kind") == "alert":
            a = alerte[sid]
            opriri.append({"id": sid, "tip": "depozitare", "lat": a["lat"], "lng": a["lng"],
                           "nume": "Depozitare ilegală", "adresa": None, "facuta": sid in facute})
        elif sid in pubele:
            b, r = pubele[sid], info.get(sid, {})
            opriri.append({"id": sid, "tip": "pubela", "lat": b["lat"], "lng": b["lng"],
                           "nume": r.get("nume") or sid, "adresa": r.get("adresa"),
                           "umplere": b["umplere"], "facuta": sid in facute})
    return {"camion": camion, "distanta_m": distanta, "incarcare_l": incarcare, "opriri": opriri,
            "traseu": geometrie}


@router.get("/navigatie/{camion}")
def navigatie(camion: int, lat: float | None = None, lng: float | None = None):
    """Ca un GPS: drumul pas cu pas spre fiecare oprire ramasa, apoi inapoi la depou.

    lat/lng = pozitia curenta a soferului; fara ele, pornim de la ultima oprire facuta (sau depou).
    etape[i] duce la opriri[i]; ultima etapa duce la depou.
    """
    with _lock:
        plan = _state["plan"]
        if plan is None:
            raise HTTPException(404, "Nu exista inca un plan al zilei. Dispecerul apasa Planifica pe dashboard.")
        ruta = next((r for r in plan["routes"] if r["truck"] == camion), None)
        if ruta is None:
            raise HTTPException(404, f"Camionul {camion} nu are ruta azi")
        pubele = {b["id"]: b for b in _state["bins"] or []}
        alerte = {a["id"]: a for a in plan["alerts"]}
        facute = set(_state["facute"])
        stops = [s for s in ruta["stops"] if s != "depot"]
    with _connect() as conn:
        info = {r["id"]: r for r in conn.execute("SELECT id, nume, adresa FROM trashes").fetchall()} \
            if _has_trashes(conn) else {}
    info = {**SIM, **info}              # ghenele simulate au numele strazii din sim_bins.json

    depou = (engine.DEPOT["lat"], engine.DEPOT["lng"])
    opriri, start = [], depou
    for sid in stops:
        if sid in alerte and alerte[sid].get("kind") == "alert":
            a = alerte[sid]
            o = {"id": sid, "tip": "depozitare", "nume": "Depozitare ilegală", "adresa": None,
                 "lat": float(a["lat"]), "lng": float(a["lng"])}
        elif sid in pubele:
            b, r = pubele[sid], info.get(sid, {})
            o = {"id": sid, "tip": "pubela", "nume": r.get("nume") or sid, "adresa": r.get("adresa"),
                 "lat": float(b["lat"]), "lng": float(b["lng"]), "umplere": b["umplere"]}
        else:
            continue
        if sid in facute:
            start = (o["lat"], o["lng"])     # ultima oprire facuta: de aici pleaca soferul
        else:
            opriri.append(o)
    if lat is not None and lng is not None:
        start = (lat, lng)
    opriri.append({"id": "depot", "tip": "depou", "nume": "depou", "adresa": None,
                   "lat": depou[0], "lng": depou[1]})

    etape, prev = [], start
    for o in opriri:
        etape.append(engine.etapa_navigatie(prev, (o["lat"], o["lng"]), o["nume"]))
        prev = (o["lat"], o["lng"])
    return {"camion": camion, "opriri": opriri, "etape": etape}


class Pozitie(BaseModel):
    camion: int
    lat: float
    lng: float


@router.post("/pozitie")
def pozitie(p: Pozitie, date: dict | None = Depends(cere_rol("sofer", "admin"))):
    """Aplicatia soferului o trimite periodic; dashboard-ul o citeste din GET /pozitii."""
    c = _camionul_meu(date)
    if c is not None and p.camion != c:
        raise HTTPException(403, "Poți trimite doar poziția camionului tău")
    _state["pozitii"][p.camion] = {"camion": p.camion, "lat": p.lat, "lng": p.lng,
                                   "la": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return {"status": "ok"}


@router.get("/pozitii")
def pozitii():
    return list(_state["pozitii"].values())


# ---------- compatibilitate cu aplicatia lui Mihai (exact caile si raspunsurile pe care le cere ea) ----------

compat = APIRouter()        # montat fara /api in main.py


@compat.get("/trashes")
def compat_trashes():
    """Aceleasi pubele reale ca /trashes de la Robert; doar citire."""
    with _connect() as conn:
        if not _has_trashes(conn):
            return []
        return [{k: r[k] for k in ("id", "lat", "lng", "umplere")} for r in _trashes(conn)]


@compat.post("/rapoarte")  # deschis si vizitatorilor: poza trece oricum prin filtrul Gemini
def compat_rapoarte(poza: UploadFile = File(...), lat: float = Form(...), lng: float = Form(...),
                    descriere: str = Form("")):
    """Aplicatia asteapta {id, status}; filtrul Gemini si alertele sunt cele din /api/rapoarte."""
    r = raport_cetatean(poza, lat, lng)
    if not r["acceptat"]:
        return {"id": "-", "status": "Respinsă", "motiv": r["motiv"]}
    return {"id": r["alerta"]["id"], "status": "Acceptată", "descriere": r["descriere"]}


@compat.post("/pozitie")
def compat_pozitie(p: Pozitie, date: dict | None = Depends(cere_rol("sofer", "admin"))):
    return pozitie(p, date)
