"""Leaga senzorii, camera si raportarile cetatenilor de motorul de rutare.

Ruleaza ca server separat (routing/main.py), fara sa modifice nimic din backend:
are tabelele lui (rutare_*) si doar citeste tabela trashes, daca exista.
Rutele sunt sub /api, in formatul in care le cheama firmware-ul ESP32
(POST /api/sensors, POST /api/image).

Fluxul:
- dimineata: GET /api/rute (sau POST /api/rute/planifica) face planul zilei;
- in timpul zilei camioanele sunt pe drum, deci NU refacem planul: o pubela sarita care
  devine urgenta si fiecare depozitare ilegala confirmata sunt inserate cu cost minim
  in ruta existenta (routing_engine.add_alert).
"""
import os
import sys
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))       # modulele din routing/ se importa intre ele fara pachet

import psycopg
from dotenv import load_dotenv
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from psycopg.rows import dict_row
from pydantic import BaseModel

import dumping
import routing_engine as engine
import sensor_filter as sf
from explain import explain_plan
from predict import estimate

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]

# cheile din JSON-ul trimis de sensors.ino -> pubelele marcate "physical" in bins.json
SENSOR_MAP = {"bin1": "bin_000", "bin2": "bin_001", "bin3": "bin_002"}
# unde "sta" fiecare camera; cam.ino nu trimite un id, deci primeste DEFAULT_CAMERA
CAMERAS = {"CAM-01": (45.73738, 21.23222)}   # la ~450 m de rute: in demo se vede ocolul
DEFAULT_CAMERA = "CAM-01"
DUMP_CLASSES = None             # None = orice clasa a modelului Dariei; altfel ex. {"gunoi"}
HISTORY_SLOT = "1 minute"       # citirile se mediaza pe intervale, pentru regresie
MIN_HISTORY_H = 1.0             # istoricul real conteaza doar daca acopera cel putin o ora
CAPTURES = HERE / "captures"    # ultima poza de la fiecare camera

router = APIRouter(prefix="/api")
_lock = threading.Lock()        # planul e unul singur si e modificat din mai multe cereri
_state = {"plan": None, "bins": None, "surse": {}, "urgent": {}}
_confirmer = dumping.FrameConfirmer()
_detector = {"loaded": False, "fn": None}

with open(HERE / "history.json") as f:
    _SIM_HISTORY = json.load(f)       # istoric simulat pentru pubelele fara senzor


def _connect():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    with open(HERE / "bins.json") as f:
        sim = json.load(f)["bins"]
    with _connect() as conn:
        # tabele proprii, prefixate cu rutare_; tabela trashes a backend-ului doar se citeste
        conn.execute("""CREATE TABLE IF NOT EXISTS rutare_pubele (
            id TEXT PRIMARY KEY, lat DOUBLE PRECISION, lng DOUBLE PRECISION,
            umplere INTEGER, actualizat TIMESTAMPTZ DEFAULT now(),
            capacitate_l INTEGER DEFAULT 1100, golita_la TIMESTAMPTZ)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS rutare_citiri (
            id BIGSERIAL PRIMARY KEY, bin_id TEXT NOT NULL,
            umplere REAL, ts TIMESTAMPTZ NOT NULL DEFAULT now())""")   # umplere NULL = eroare senzor
        conn.execute("CREATE INDEX IF NOT EXISTS rutare_citiri_bin_ts ON rutare_citiri (bin_id, ts)")
        conn.execute("""CREATE TABLE IF NOT EXISTS rutare_camere (
            id TEXT PRIMARY KEY, lat DOUBLE PRECISION, lng DOUBLE PRECISION)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS rutare_alerte (
            id TEXT PRIMARY KEY, sursa TEXT, camera_id TEXT,
            lat DOUBLE PRECISION, lng DOUBLE PRECISION, marime TEXT, volum_l INTEGER,
            nr_raportari INTEGER DEFAULT 1, status TEXT DEFAULT 'deschisa', camion INTEGER,
            creata TIMESTAMPTZ DEFAULT now(), inchisa TIMESTAMPTZ)""")
        with conn.cursor() as cur:
            # bins.json e sursa comuna de adevar; pubelele existente nu sunt suprascrise
            cur.executemany(
                """INSERT INTO rutare_pubele (id, lat, lng, umplere, capacitate_l)
                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
                [(b["id"], b["lat"], b["lon"], round(b["fill_level"] * 100), b["capacity_l"])
                 for b in sim])
            cur.executemany(
                """INSERT INTO rutare_camere (id, lat, lng) VALUES (%s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET lat = EXCLUDED.lat, lng = EXCLUDED.lng""",   # CAMERAS e sursa de adevar
                [(cid, lat, lng) for cid, (lat, lng) in CAMERAS.items()])


init_db()


# ---------- pregatirea datelor pentru rutare ----------

def _db_history(conn):
    """Istoricul real al pubelelor cu senzor, de la ultima golire, in formatul din predict.py."""
    rows = conn.execute(f"""
        SELECT c.bin_id, extract(epoch FROM date_bin('{HISTORY_SLOT}', c.ts, timestamptz '2000-01-01') - now()) / 3600 AS t_h,
               avg(c.umplere) / 100 AS level
        FROM rutare_citiri c JOIN rutare_pubele t ON t.id = c.bin_id
        WHERE c.umplere IS NOT NULL AND c.ts >= coalesce(t.golita_la, '-infinity')
          AND c.ts > now() - interval '48 hours'
        GROUP BY 1, 2 ORDER BY 1, 2""").fetchall()
    hist = {}
    for r in rows:
        hist.setdefault(r["bin_id"], []).append({"t_h": float(r["t_h"]), "level": float(r["level"])})
    return hist


def _current_bins(conn):
    """Pubelele din DB, cu umplerea corectata pentru senzorii defecti sau muti."""
    rows = conn.execute(
        "SELECT id, lat, lng, umplere, capacitate_l FROM rutare_pubele ORDER BY id").fetchall()
    # pubelele din backend (tabela trashes), daca exista: doar citite, niciodata modificate
    if conn.execute("SELECT to_regclass('trashes') IS NOT NULL AS ok").fetchone()["ok"]:
        own = {r["id"] for r in rows}
        rows += [{**r, "capacitate_l": None} for r in conn.execute(
            "SELECT id, lat, lng, umplere FROM trashes ORDER BY id").fetchall() if r["id"] not in own]
    stats = {r["bin_id"]: r for r in conn.execute("""
        SELECT c.bin_id, max(c.ts) FILTER (WHERE c.umplere IS NOT NULL) AS last_good, max(c.ts) AS last_any
        FROM rutare_citiri c JOIN rutare_pubele t ON t.id = c.bin_id
        WHERE c.ts >= coalesce(t.golita_la, '-infinity') GROUP BY c.bin_id""").fetchall()}
    db_hist = _db_history(conn)
    history = {k: v for k, v in _SIM_HISTORY.items()}
    # cateva minute de citiri nu spun nimic despre rata de umplere (regresia iese plata si
    # predictia "se umple in 8000 de ore"); pana se aduna o ora de date ramane istoricul simulat
    history.update({k: v for k, v in db_hist.items()
                    if len(v) >= 3 and v[-1]["t_h"] - v[0]["t_h"] >= MIN_HISTORY_H})

    now = datetime.now(timezone.utc)
    bins, surse = [], {}
    for r in rows:
        b = {"id": r["id"], "lat": r["lat"], "lng": r["lng"], "umplere": r["umplere"],
             "capacitate_l": r["capacitate_l"] or engine.DEFAULT_BIN_CAPACITY_L}
        if r["id"] in SENSOR_MAP.values():
            s = stats.get(r["id"], {})
            rate, _ = estimate(history.get(r["id"], []))
            fill = sf.routing_fill(r["umplere"] if s.get("last_good") else None,
                                   s.get("last_good"), s.get("last_any"), now,
                                   None if rate is None else rate * 100)
            b["umplere"] = fill["umplere"]
            surse[r["id"]] = fill
        bins.append(b)
    return bins, history, surse


def _public(plan):
    return {k: v for k, v in plan.items() if k != "_nodes"}


def _build_plan(conn):
    bins, history, surse = _current_bins(conn)
    plan = engine.plan_routes(bins, history, compare_baseline=True)
    for bid, s in surse.items():
        if s["sursa"] != "senzor":
            plan["warnings"].append(f"{bid}: {s['motiv']}")
    _state.update(plan=plan, bins=bins, surse=surse)
    # alertele inca necolectate (si cele venite inainte sa existe un plan) intra in planul nou
    for a in conn.execute("""SELECT * FROM rutare_alerte WHERE status IN ('deschisa', 'asignata')
                             ORDER BY creata""").fetchall():
        _assign(conn, a)
    return plan


def _assign(conn, alert):
    """Insereaza o alerta in planul curent si salveaza rezultatul in DB."""
    plan = _state["plan"]
    if plan is None:
        return {"id": alert["id"], "status": "deschisa", "reason": "inca nu exista un plan al zilei"}
    r = engine.add_alert(plan, {"id": alert["id"], "lat": alert["lat"], "lng": alert["lng"],
                                "volum_l": alert["volum_l"]})
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
    return {**_assign(conn, alert), "marime": marime}


# ---------- rute ----------

@router.get("/rute")
def rute(explicatie: bool = False):
    """Planul curent; il calculeaza daca nu exista inca."""
    with _lock, _connect() as conn:
        plan = _state["plan"] or _build_plan(conn)
        out = {**_public(plan), "surse": _state["surse"]}
        if explicatie:
            out["explicatie"] = explain_plan(out, _state["bins"])
        return out


@router.post("/rute/planifica")
def planifica():
    """Planul de dimineata: recalculeaza tot din datele actuale."""
    with _lock, _connect() as conn:
        plan = _build_plan(conn)
        return {**_public(plan), "surse": _state["surse"]}


# ---------- senzori (format sensors.ino) ----------

def _became_urgent(conn, bin_id, level):
    plan = _state["plan"]
    if plan is None:
        return {"id": bin_id, "status": "urgenta", "reason": "inca nu exista un plan al zilei"}
    if bin_id not in plan["skipped"]:
        return {"id": bin_id, "status": "urgenta", "reason": "e deja pe o ruta"}
    row = conn.execute("SELECT lat, lng, capacitate_l FROM rutare_pubele WHERE id = %s", (bin_id,)).fetchone()
    cap = row["capacitate_l"] or engine.DEFAULT_BIN_CAPACITY_L
    return engine.add_alert(plan, {"id": bin_id, "lat": row["lat"], "lng": row["lng"],
                                   "volum_l": round(cap * level / 100)}, kind="bin")


@router.post("/sensors")
def sensors(payload: dict = Body(...)):
    """Primeste {"bin1": 73.5, "bin2": -1, ...}. Valori negative sau null = senzor fara raspuns."""
    result, events = {}, []
    with _lock, _connect() as conn:
        for key, bin_id in SENSOR_MAP.items():
            if key not in payload:
                continue
            conn.execute("INSERT INTO rutare_citiri (bin_id, umplere) VALUES (%s, %s)",
                         (bin_id, sf.valid(payload[key])))
            recent = conn.execute("""
                SELECT c.umplere FROM rutare_citiri c JOIN rutare_pubele t ON t.id = c.bin_id
                WHERE c.bin_id = %s AND c.ts >= coalesce(t.golita_la, '-infinity')
                ORDER BY c.id DESC LIMIT 15""", (bin_id,)).fetchall()
            level = sf.smooth([r["umplere"] for r in reversed(recent)])
            if level is not None:
                conn.execute("UPDATE rutare_pubele SET umplere = %s, actualizat = now() WHERE id = %s",
                             (round(level), bin_id))
            was = _state["urgent"].get(bin_id, False)
            urgent = sf.next_urgent(was, level)
            _state["urgent"][bin_id] = urgent
            if urgent and not was:
                events.append(_became_urgent(conn, bin_id, level))
            result[key] = {"pubela": bin_id, "umplere": None if level is None else round(level),
                           "urgenta": urgent}
    return {"status": "ok", "pubele": result, "evenimente": events}


@router.post("/pubele/{bin_id}/golita")
def golita(bin_id: str):
    """Camionul a golit pubela: istoricul de umplere incepe de la zero."""
    with _lock, _connect() as conn:
        found = conn.execute("""UPDATE rutare_pubele SET umplere = 0, actualizat = now(), golita_la = now()
                                WHERE id = %s""", (bin_id,)).rowcount
        if not found:
            raise HTTPException(404, "Pubela nu exista in rutare (pubelele din trashes sunt doar citite)")
        conn.execute("INSERT INTO rutare_citiri (bin_id, umplere) VALUES (%s, 0)", (bin_id,))
        _state["urgent"][bin_id] = False
    return {"status": "ok"}


# ---------- depozitari ilegale ----------

def _process_frame(camera_id, latime, inaltime, detectii):
    dets = dumping.relevant(detectii, DUMP_CLASSES)
    with _lock, _connect() as conn:
        if not _confirmer.update(camera_id, bool(dets)):
            return {"status": "detectie" if dets else "nimic",
                    "cadre_consecutive": _confirmer.streak.get(camera_id, 0),
                    "necesare": _confirmer.frames}
        cam = conn.execute("SELECT lat, lng FROM rutare_camere WHERE id = %s", (camera_id,)).fetchone()
        if cam is None:
            return {"status": "ignorata", "reason": f"camera {camera_id} nu are coordonate in tabela camere"}
        return _register_alert(conn, "camera", cam["lat"], cam["lng"],
                               dumping.size_from_boxes(dets, latime, inaltime), camera_id)


class Cadru(BaseModel):
    camera_id: str = DEFAULT_CAMERA
    latime: int
    inaltime: int
    detectii: list[dict] = []        # [{"clasa": "gunoi", "conf": 0.87, "bbox": [x1, y1, x2, y2]}]


@router.post("/detectii")
def detectii(c: Cadru):
    """Rezultatul modelului Dariei pentru un cadru."""
    return _process_frame(c.camera_id, c.latime, c.inaltime, c.detectii)


def _load_detector():
    """Daca exista vision.py cu detect(jpeg_bytes) -> {"latime", "inaltime", "detectii"}, il folosim."""
    if not _detector["loaded"]:
        _detector["loaded"] = True
        try:
            from vision import detect
            _detector["fn"] = detect
        except ImportError:
            _detector["fn"] = None
    return _detector["fn"]


@router.post("/image")
async def image(request: Request, camera_id: str = DEFAULT_CAMERA):
    """Poza JPEG trimisa de cam.ino. Se salveaza si, daca exista detectorul, se analizeaza."""
    data = await request.body()
    if not data:
        raise HTTPException(400, "Imagine goala")
    CAPTURES.mkdir(exist_ok=True)
    (CAPTURES / f"{camera_id}.jpg").write_bytes(data)
    detect = _load_detector()
    if detect is None:
        return {"status": "salvata", "detector": False}
    out = await run_in_threadpool(detect, data)
    return await run_in_threadpool(_process_frame, camera_id, out["latime"], out["inaltime"],
                                   out.get("detectii", []))


class Raportare(BaseModel):
    lat: float
    lng: float
    marime: str = "medie"            # mica / medie / mare


@router.post("/alerte")
def raportare_cetatean(r: Raportare):
    with _lock, _connect() as conn:
        return _register_alert(conn, "cetatean", r.lat, r.lng, r.marime)


@router.get("/alerte")
def lista_alerte():
    with _connect() as conn:
        return conn.execute("SELECT * FROM rutare_alerte ORDER BY creata DESC").fetchall()


@router.post("/alerte/{alert_id}/colectata")
def alerta_colectata(alert_id: str):
    with _lock, _connect() as conn:
        found = conn.execute("""UPDATE rutare_alerte SET status = 'colectata', inchisa = now()
                                WHERE id = %s AND status <> 'colectata'""", (alert_id,)).rowcount
        if not found:
            raise HTTPException(404, "Alerta nu exista sau e deja colectata")
    return {"status": "ok"}
