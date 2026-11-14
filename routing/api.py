"""Leaga datele echipei de motorul de rutare, fara sa modifice nimic din partea lor.

Cum circula datele (cum a stabilit echipa):
- senzorii lui Mihai trimit la backend-ul lui Robert, care scrie umplerea in tabela trashes;
  aici doar CITIM tabela, la cateva secunde (watch / poll_once), si de acolo facem
  filtrarea, istoricul pentru predictie si inserarea pubelelor care devin urgente;
- camera: Daria trimite coordonatele camerei la backend, iar backend-ul cheama
  POST /api/alerte cu {"lat", "lng", "sursa": "camera"};
- datele noastre stau in tabele separate (rutare_*); pe langa pubelele reale din trashes,
  planul include si cele 60 de pubele simulate din bins.json, ca orasul sa arate realist.

Fluxul zilei:
- dimineata: GET /api/rute (sau POST /api/rute/planifica) face planul;
- in timpul zilei camioanele sunt pe drum, deci NU refacem planul: o pubela sarita care
  devine urgenta si fiecare depozitare ilegala sunt inserate cu cost minim in ruta existenta.
"""
import os
import sys
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
import io

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))       # modulele din routing/ se importa intre ele fara pachet

import psycopg
from dotenv import load_dotenv
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from PIL import Image
from psycopg.rows import dict_row
from pydantic import BaseModel

import dumping
import routing_engine as engine
import sensor_filter as sf
from explain import explain_plan
from predict import estimate
from filtru_gemini import verifica

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]

POLL_S = 3                      # la cate secunde citim tabela trashes
EMPTY_DROP = 30                 # o scadere brusca de 30+ puncte = pubela a fost golita
HISTORY_SLOT = "1 minute"       # citirile se mediaza pe intervale, pentru regresie
MIN_HISTORY_H = 1.0             # istoricul real conteaza doar daca acopera cel putin o ora
MAX_EVENTS = 50
RAPORTARE_PCT = 5               # o schimbare de umplere de cel putin 5 puncte apare in fluxul live
FOLOSESTE_SIMULATE = False      # True = pe langa ghenele din trashes, si cele 60 de pubele din bins.json

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


def _connect():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    with open(HERE / "bins.json") as f:
        sim = json.load(f)["bins"]
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
                                "volum_l": round(engine.DEFAULT_BIN_CAPACITY_L * level / 100)}, kind="bin")
    return _event(r)


def _poll(conn):
    """Citeste trashes; fiecare valoare noua (actualizat schimbat) devine o citire. Fara lock."""
    if not _has_trashes(conn):
        return []
    if _state["seen"] is None:      # dupa repornire nu reluam citirile deja salvate
        _state["seen"] = {r["bin_id"]: r["ts"] for r in conn.execute(
            "SELECT bin_id, max(ts) AS ts FROM rutare_citiri GROUP BY bin_id").fetchall()}
    seen, events = _state["seen"], []
    rows = conn.execute("SELECT id, lat, lng, umplere, actualizat FROM trashes ORDER BY id").fetchall()
    if _state["raportat"] is None:
        # dupa repornire pornim de la valorile curente: planul le ia oricum, deci fara evenimente false
        vals = {r["id"]: sf.valid(r["umplere"]) for r in rows}
        _state["raportat"] = {k: v for k, v in vals.items() if v is not None}
        _state["urgent"] = {k: v > engine.MIN_FILL * 100 for k, v in _state["raportat"].items()}
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
        # orice ghena care primeste deseuri intra pe ruta, la fel ca in planul de dimineata
        was = _state["urgent"].get(bid, False)
        urgent = value > engine.MIN_FILL * 100
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
    real = conn.execute("SELECT id, lat, lng, umplere, actualizat FROM trashes ORDER BY id").fetchall() \
        if _has_trashes(conn) else []
    real_ids = {r["id"] for r in real}
    sim = [r for r in conn.execute(
        "SELECT id, lat, lng, umplere, capacitate_l FROM rutare_pubele ORDER BY id").fetchall()
        if r["id"] not in real_ids] if FOLOSESTE_SIMULATE else []
    stats = {r["bin_id"]: r for r in conn.execute(f"""
        SELECT c.bin_id, max(c.ts) FILTER (WHERE c.umplere IS NOT NULL) AS last_good, max(c.ts) AS last_any
        FROM rutare_citiri c {SINCE_EMPTY} GROUP BY c.bin_id""").fetchall()}

    history = {k: v for k, v in _SIM_HISTORY.items()}
    # cateva minute de citiri nu spun nimic despre rata de umplere (regresia iese plata si
    # predictia "se umple in 8000 de ore"); pana se aduna o ora de date ramane istoricul simulat
    history.update({k: v for k, v in _db_history(conn).items()
                    if len(v) >= 3 and v[-1]["t_h"] - v[0]["t_h"] >= MIN_HISTORY_H})

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
                         "capacitate_l": engine.DEFAULT_BIN_CAPACITY_L})
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
                     "capacitate_l": engine.DEFAULT_BIN_CAPACITY_L})
    for r in sim:
        bins.append({"id": r["id"], "lat": r["lat"], "lng": r["lng"], "umplere": r["umplere"],
                     "capacitate_l": r["capacitate_l"] or engine.DEFAULT_BIN_CAPACITY_L})
    return bins, history, surse


def _public(plan):
    return {k: v for k, v in plan.items() if not k.startswith("_")}


def _build_plan(conn):
    _poll(conn)                     # planul porneste de la cele mai noi valori din trashes
    bins, history, surse = _current_bins(conn)
    plan = engine.plan_routes(bins, history, compare_baseline=True)
    for bid, s in surse.items():
        if s["sursa"] != "senzor":
            plan["warnings"].append(f"{bid}: {s['motiv']}")
    _state.update(plan=plan, bins=bins, surse=surse, facute=set())
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


@router.post("/rute/planifica")
def planifica():
    """Planul de dimineata: recalculeaza tot din datele actuale."""
    with _lock, _connect() as conn:
        return _out(_build_plan(conn))


@router.post("/pubele/{bin_id}/golita")
def golita(bin_id: str):
    """Pentru pubelele simulate. Cele reale sunt detectate singure, din scaderea brusca a senzorului."""
    with _lock, _connect() as conn:
        found = conn.execute("UPDATE rutare_pubele SET umplere = 0, actualizat = now() WHERE id = %s",
                             (bin_id,)).rowcount
        if not found:
            # ghena reala (din trashes): nu scriem in tabela lui Robert, doar marcam golirea la noi;
            # istoricul de umplere porneste de la zero, iar senzorul confirma golirea la urmatoarea citire
            real = _has_trashes(conn) and conn.execute(
                "SELECT 1 FROM trashes WHERE id = %s", (bin_id,)).fetchone()
            if not real:
                raise HTTPException(404, "Ghena nu exista")
        conn.execute("""INSERT INTO rutare_golire (bin_id, golita_la) VALUES (%s, now())
                        ON CONFLICT (bin_id) DO UPDATE SET golita_la = EXCLUDED.golita_la""", (bin_id,))
        _state["urgent"][bin_id] = False
        _state["facute"].add(bin_id)
        _event({"id": bin_id, "status": "golita"})
    return {"status": "ok"}


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
def alerta_colectata(alert_id: str):
    with _lock, _connect() as conn:
        found = conn.execute("""UPDATE rutare_alerte SET status = 'colectata', inchisa = now()
                                WHERE id = %s AND status <> 'colectata'""", (alert_id,)).rowcount
        if not found:
            raise HTTPException(404, "Alerta nu exista sau e deja colectata")
        _state["facute"].add(alert_id)
    return {"status": "ok"}


@router.get("/pubele-fizice")
def pubele_fizice():
    """Pubelele reale din trashes, pentru harta din dashboard."""
    with _connect() as conn:
        if not _has_trashes(conn):
            return []
        return conn.execute("SELECT id, lat, lng, umplere FROM trashes ORDER BY id").fetchall()


@router.get("/pubele")
def toate_pubelele():
    """Pubelele reale si cele simulate, cu umplerea curenta, pentru harta din dashboard."""
    with _connect() as conn:
        bins, _, surse = _current_bins(conn)
        info = {r["id"]: r for r in conn.execute("SELECT * FROM trashes").fetchall()} \
            if _has_trashes(conn) else {}
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
                    "umplere": r.get("umplere", b["umplere"]),
                    "umplere_rutare": b["umplere"],
                    "nume": r.get("nume"), "adresa": r.get("adresa"), "tip": r.get("tip")})
    return out


# ---------- raportari de la cetateni (aplicatia lui Mihai) ----------

CAPTURES = HERE / "captures"
CAPTURES.mkdir(exist_ok=True)
STATUS_APLICATIE = {"assigned": "pe_ruta", "duplicat": "duplicat"}


@router.post("/rapoarte")
def raport_cetatean(poza: UploadFile = File(...), lat: float = Form(...), lng: float = Form(...)):
    """Gemini verifica poza; daca e gunoi, intra ca orice alerta (duplicate, inserare in ruta)."""
    try:
        imagine = Image.open(io.BytesIO(poza.file.read())).convert("RGB")
    except Exception:
        raise HTTPException(400, "Fișierul trimis nu e o imagine")
    try:
        verdict = verifica(imagine)     # in afara lock-ului: Gemini raspunde in 1-3 secunde
    except Exception:
        raise HTTPException(503, "Verificarea pozei nu merge acum, încearcă din nou")
    if not verdict["e_gunoi"]:
        return {"acceptat": False, "motiv": verdict["descriere"]}

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
def pozitie(p: Pozitie):
    """Aplicatia soferului o trimite periodic; dashboard-ul o citeste din GET /pozitii."""
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
        return conn.execute("SELECT id, lat, lng, umplere FROM trashes ORDER BY id").fetchall()


@compat.post("/rapoarte")
def compat_rapoarte(poza: UploadFile = File(...), lat: float = Form(...), lng: float = Form(...),
                    descriere: str = Form("")):
    """Aplicatia asteapta {id, status}; filtrul Gemini si alertele sunt cele din /api/rapoarte."""
    r = raport_cetatean(poza, lat, lng)
    if not r["acceptat"]:
        return {"id": "-", "status": "Respinsă", "motiv": r["motiv"]}
    return {"id": r["alerta"]["id"], "status": "Acceptată", "descriere": r["descriere"]}


@compat.post("/pozitie")
def compat_pozitie(p: Pozitie):
    return pozitie(p)
