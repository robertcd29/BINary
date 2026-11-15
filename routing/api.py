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
- in timpul zilei camioanele sunt pe drum, deci NU refacem planul: o ghena care trece de 55%
  si fiecare depozitare ilegala sunt inserate in ruta camionului ei, iar opririle ramase se
  reordoneaza pornind de unde e camionul acum;
- cand camionul s-ar umple, ruta il trimite la groapa de gunoi si apoi continua; la final descarca
  inainte de depou. Ce e in camion se tine minte din opririle bifate (golite / descarcat);
- la miezul noptii (ora Romaniei) planul se reface singur pentru ziua noua.
"""
import os
import sys
import json
import math
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Literal
import io
import base64
import hmac
from collections import deque

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))       # modulele din routing/ se importa intre ele fara pachet

import psycopg
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
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
FUS_ORAR = ZoneInfo("Europe/Bucharest")   # "ziua" planului e ziua din Timisoara
FOLOSESTE_SIMULATE = True       # True = pe langa ghenele din trashes, si cele 60 de ghene simulate din sim_bins.json

# ---------- zone si pubele cu formula (doar pentru prototip) ----------
# Orasul e impartit in 3 sectoare pornind din centru (ca feliile unui tort), fiecare cu
# camioanele lui si cu un senzor real. O ghena intra in sectorul directiei in care se afla
# fata de centru, deci fiecare zona e compacta si rutele nu traverseaza orasul.
CENTRU = (45.748, 21.235)
ZONE = {   # unghiul se masoara de la nord, in sensul acelor de ceasornic
    0: {"nume": "Vest",     "senzor": "TM-011", "de_la": 210, "pana_la": 330},
    1: {"nume": "Nord-Est", "senzor": "TM-015", "de_la": 330, "pana_la": 90},
    2: {"nume": "Sud",      "senzor": "TM-016", "de_la": 90,  "pana_la": 210},
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
    "TM-001": ("TM-015", lambda x: 100 - x),          # Nord-Est
    "TM-008": ("TM-015", lambda x: 0.5 * x + 40),     # Nord-Est
    "TM-002": ("TM-016", lambda x: 100 - x),          # Sud
}

# ghenele simulate, raspandite pe toata harta (vezi generate_sim_bins.py)
with open(HERE / "sim_bins.json") as f:
    SIM = {b["id"]: b for b in json.load(f)["bins"]}
_TM_POZ = {"TM-001": (45.774718, 21.2633061), "TM-002": (45.744593, 21.267267), "TM-003": (45.7121271, 21.198657),
           "TM-004": (45.77612, 21.2135825), "TM-005": (45.76, 21.197), "TM-006": (45.766, 21.218),
           "TM-007": (45.779, 21.226), "TM-008": (45.7785, 21.2255), "TM-009": (45.778, 21.207),
           "TM-010": (45.7588, 21.2135), "TM-011": (45.7667103, 21.1952331), "TM-012": (45.7482, 21.2045),
           "TM-013": (45.748, 21.204), "TM-014": (45.7485, 21.205), "TM-015": (45.7801024, 21.2200485),
           "TM-016": (45.7156528, 21.2580798), "TM-017": (45.7585, 21.224)}
# senzorii reali stau unde erau inainte ghenele simulate SIM-19, SIM-59 si SIM-28 (si invers)
SENZORI_LOC = {"TM-011": (45.7667103, 21.1952331, "Ghena Strada Locotenent Ovidiu Balea", "Strada Locotenent Ovidiu Balea, Timisoara"),
               "TM-015": (45.7801024, 21.2200485, "Ghena Calea Aradului", "Calea Aradului, Timisoara"),
               "TM-016": (45.7156528, 21.2580798, "Ghena Strada Poeniței", "Strada Poeniței, Timisoara")}
ZONA_CAMION = {bid: zona_pentru(*p) for bid, p in _TM_POZ.items()}           # ghena -> zona
ZONA_CAMION.update({bid: zona_pentru(b["lat"], b["lon"]) for bid, b in SIM.items()})
# ce strange fiecare ghena: o ghena de reciclabile o goleste doar camionul de reciclabile al zonei
RECICLABILE = {bid for bid, b in SIM.items() if b.get("deseu") == "reciclabil"} | {"TM-015"}
CAMIOANE_PUBELA = {bid: [CAMION_RECICLABIL[z] if bid in RECICLABILE else CAMION_MENAJER[z]]
                   for bid, z in ZONA_CAMION.items()}
# daca o ghena obligatorie nu mai incape in camionul zonei ei, o poate lua alt camion de acelasi tip
# (menajer sau reciclabil) care mai are loc, inainte ca vreun camion sa faca o cursa in plus la groapa
CAMIOANE_REZERVA = {bid: [c for c in (CAMION_RECICLABIL if bid in RECICLABILE else CAMION_MENAJER).values()
                          if c not in t] for bid, t in CAMIOANE_PUBELA.items()}

router = APIRouter(prefix="/api")
_lock = threading.Lock()        # planul e unul singur si e modificat din mai multe fire
_state = {"plan": None, "bins": None, "surse": {}, "urgent": {}, "seen": None, "evenimente": [],
          "raportat": None,     # ultima umplere aratata in fluxul live, pe ghena
          "facute": set(),      # opririle bifate azi de soferi (pubele golite, depozitari colectate)
          "pozitii": {},        # ultima pozitie GPS a fiecarui camion; doar in memorie, nu in DB
          "incarcare": {},      # cati litri are acum fiecare camion (de la ultima descarcare)
          "zi": None}           # ziua pentru care e facut planul

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
        conn.execute("CREATE SEQUENCE IF NOT EXISTS rutare_alerte_nr")
        # secventa porneste dupa cel mai mare id existent (alerte din baza sau poze din captures/)
        n = conn.execute("""SELECT coalesce(max(substring(id FROM 4)::int), 0) AS n
                            FROM rutare_alerte WHERE id ~ '^AL-[0-9]+$'""").fetchone()["n"]
        poze = [int(f.stem[3:]) for f in (HERE / "captures").glob("AL-*.jpg") if f.stem[3:].isdigit()]
        n = max([n, *poze])
        if n:
            conn.execute("SELECT setval('rutare_alerte_nr', greatest(%s, (SELECT last_value FROM rutare_alerte_nr)))", (n,))
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO rutare_pubele (id, lat, lng, umplere, capacitate_l)
                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET lat = EXCLUDED.lat, lng = EXCLUDED.lng""",
                [(b["id"], b["lat"], b["lon"], round(b["fill_level"] * 100), b["capacity_l"])
                 for b in sim])
            # si in tabela trashes a backend-ului, ca ghenele simulate sa arate peste tot ca unele reale
            if conn.execute("SELECT to_regclass('trashes') IS NOT NULL AS ok").fetchone()["ok"]:
                cur.executemany(
                    """INSERT INTO trashes (id, lat, lng, umplere, actualizat, nume, adresa, tip)
                       VALUES (%s, %s, %s, %s, now(), %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
                    [(b["id"], b["lat"], b["lon"], round(b["fill_level"] * 100), b["nume"], b["adresa"],
                      "ghena_reciclabile" if b.get("deseu") == "reciclabil" else "ghena") for b in sim])
                # locul, numele si tipul din sim_bins.json sunt cele valabile (umplerea ramane cea curenta)
                cur.executemany("UPDATE trashes SET tip = %s, lat = %s, lng = %s, nume = %s, adresa = %s WHERE id = %s",
                                [("ghena_reciclabile" if b.get("deseu") == "reciclabil" else "ghena",
                                  b["lat"], b["lon"], b["nume"], b["adresa"], b["id"]) for b in sim])
                cur.executemany("UPDATE trashes SET lat = %s, lng = %s, nume = %s, adresa = %s WHERE id = %s",
                                [(*v, k) for k, v in SENZORI_LOC.items()])


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
                                "camioane": CAMIOANE_PUBELA.get(bid),
                                "camioane_rezerva": CAMIOANE_REZERVA.get(bid)}, kind="bin")
    if r["status"] == "assigned":
        # inserarea poate cadea si printre opririle deja facute: refacem ordinea celor ramase
        # (si descarcarile) pornind de unde e camionul acum, ca ghena sa fie luata din drum
        _reordoneaza_camion(r["truck"])
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
            _bifeaza(bid)
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


def _nimic_nou(conn):
    """Verificare rapida, fara lacat: nicio ghena din trashes nu are o citire noua de la ultima trecere."""
    seen = _state["seen"]
    if seen is None or not _has_trashes(conn):
        return False
    return all(r["actualizat"] is None or (r["id"] in seen and r["actualizat"] <= seen[r["id"]])
               for r in _trashes(conn))


def _azi():
    return datetime.now(FUS_ORAR).date()


def poll_once():
    # lacatul tine pe loc toate cererile care modifica sau citesc planul: il luam doar cand chiar
    # e ceva nou de procesat, si cu conexiunea la baza de date deja deschisa
    with _connect() as conn:
        if _state["plan"] is not None and _state["zi"] != _azi():
            with _lock:             # zi noua: planul de ieri nu mai e bun, il refacem
                if _state["zi"] != _azi():
                    _build_plan(conn)
                    print("rutare: zi noua, planul e refacut")
        if _nimic_nou(conn):
            return []
        with _lock:
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
    puncte = [{"id": a["id"], "lat": a["lat"], "lng": a["lng"], "umplere": 100, "compactare": False,
               "capacitate_l": a["volum_l"] or engine.ALERT_SIZES_L["medie"]} for a in alerte]
    zone = {**CAMIOANE_PUBELA, **{a["id"]: CAMIOANE_MENAJERE for a in alerte}}
    plan = engine.plan_routes(bins + puncte, history, compare_baseline=True, zone=zone, rezerva=CAMIOANE_REZERVA)
    for bid, s in surse.items():
        if s["sursa"] != "senzor":
            plan["warnings"].append(f"{bid}: {s['motiv']}")
    for r in plan["routes"]:
        r["tip"] = TIP_CAMION.get(r["truck"], "menajer")   # dashboard-ul deseneaza punctat rutele de reciclabile
    _state.update(plan=plan, bins=bins, surse=surse, facute=set(), incarcare={}, zi=_azi())
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
    if r["status"] == "assigned":
        # inserarea cauta cel mai bun loc in toata ruta, inclusiv printre opririle deja facute;
        # refacem ordinea opririlor ramase pornind de unde e camionul acum
        _reordoneaza_camion(r["truck"])
    status = {"assigned": "asignata", "rejected": "respinsa"}.get(r["status"], "deschisa")
    conn.execute("UPDATE rutare_alerte SET status = %s, camion = %s WHERE id = %s",
                 (status, r.get("truck"), alert["id"]))
    return r


def _urmatorul_nr_alerta(conn):
    """Numarul alertei noi, dintr-o secventa care nu se reseteaza: dupa Reset demo (care sterge
    alertele) o alerta noua nu primeste id-ul uneia vechi (poza ei din captures/, starea din telefon)."""
    return conn.execute("SELECT nextval('rutare_alerte_nr') AS n").fetchone()["n"]


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
    alert = {"id": f"AL-{_urmatorul_nr_alerta(conn):04d}", "lat": lat, "lng": lng, "volum_l": engine.ALERT_SIZES_L[marime]}
    conn.execute("""INSERT INTO rutare_alerte (id, sursa, camera_id, lat, lng, marime, volum_l)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                 (alert["id"], sursa, camera_id, lat, lng, marime, alert["volum_l"]))
    return {**_assign(conn, alert), "sursa": sursa, "marime": marime}


def precalculeaza():
    """Pornit de main.py la startup: planul e gata pana deschide cineva dashboard-ul."""
    try:
        with _connect() as conn, _lock:   # conexiunea (lenta) se deschide inainte de lacat
            if _state["plan"] is None:
                _build_plan(conn)
                print("rutare: planul zilei e calculat")
    except Exception as e:
        print("rutare: planul initial nu s-a putut calcula:", e)


# ---------- rute ----------

def _out(plan):
    # incarcare_acum_l = cati litri are camionul acum (de la ultima descarcare), pentru dispecer
    routes = [{**r, "incarcare_acum_l": _state["incarcare"].get(r["truck"], 0)} for r in plan["routes"]]
    return {**_public(plan), "routes": routes, "surse": _state["surse"], "evenimente": _state["evenimente"],
            "facute": sorted(_state["facute"]),     # opririle bifate azi, pentru progresul camioanelor
            "capacitate_l": engine.TRUCK_CAPACITY_L,
            "groapa": {k: engine.GROAPA[k] for k in ("lat", "lng", "nume")}}


@router.get("/rute")
def rute(explicatie: bool = False):
    """Planul curent (cu inserarile facute de la planificare); il calculeaza daca nu exista inca."""
    with _lock:
        plan = _state["plan"]
    if plan is None:                    # baza de date doar cand planul inca nu exista
        with _connect() as conn, _lock:
            plan = _state["plan"] or _build_plan(conn)
    with _lock:
        out = _out(plan)
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

    Ghenele simulate revin la umplerea din sim_bins.json, depozitarile si raportarile se sterg
    (si de pe harta), iar opririle bifate, fluxul live si pozitiile GPS se sterg. Senzorii reali raman cum sunt.
    """
    with _connect() as conn, _lock:   # conexiunea (lenta) se deschide inainte de lacat
        with conn.cursor() as cur:
            cur.executemany("""UPDATE rutare_pubele SET umplere = %s, actualizat = now(), golita_la = NULL
                               WHERE id = %s""",
                            [(round(b["fill_level"] * 100), b["id"]) for b in SIM.values()])
        if _has_trashes(conn):
            with conn.cursor() as cur:
                cur.executemany("UPDATE trashes SET umplere = %s, actualizat = now() WHERE id = %s",
                                [(round(b["fill_level"] * 100), b["id"]) for b in SIM.values()])
        conn.execute("DELETE FROM rutare_golire WHERE bin_id = ANY(%s)", (list(SIM),))
        conn.execute("DELETE FROM rutare_alerte")   # punctele depozitarilor dispar de pe harta
        _state.update(plan=None, facute=set(), evenimente=[], pozitii={}, incarcare={})
        return _out(_build_plan(conn))


@router.post("/rute/planifica", dependencies=[Depends(cere_rol("admin"))])
def planifica():
    """Planul de dimineata: recalculeaza tot din datele actuale."""
    with _connect() as conn, _lock:   # conexiunea (lenta) se deschide inainte de lacat
        return _out(_build_plan(conn))


@router.post("/pubele/{bin_id}/golita")
def golita(bin_id: str, date: dict | None = Depends(cere_rol("sofer", "admin"))):
    """Pentru pubelele simulate. Cele reale sunt detectate singure, din scaderea brusca a senzorului."""
    with _connect() as conn, _lock:   # conexiunea (lenta) se deschide inainte de lacat
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
        _bifeaza(bin_id)
        _event({"id": bin_id, "status": "golita"})
    return {"status": "ok"}


def _reordoneaza_camion(truck):
    """Opririle ramase ale camionului se reordoneaza pornind de unde e acum (GPS) sau de la ultima oprire facuta."""
    plan = _state["plan"]
    if plan is None:
        return
    poz = _state["pozitii"].get(truck)
    proaspat = poz and (datetime.now(timezone.utc) - datetime.fromisoformat(poz["la"])).total_seconds() < 120
    try:
        engine.reordoneaza(plan, truck, _state["facute"], (poz["lat"], poz["lng"]) if proaspat else None,
                           incarcat=_state["incarcare"].get(truck, 0))
    except Exception as e:          # reordonarea e un bonus: daca pica, ruta ramane cum era
        print("rutare: reordonarea a esuat:", e)


def _bifeaza(sid):
    """O oprire e facuta (ghena golita, depozitare colectata, descarcare la groapa): ce e in camion
    se actualizeaza, iar opririle ramase se reordoneaza de unde e acum (cu descarcarile refacute:
    un camion plin merge intai la groapa)."""
    if sid in _state["facute"]:
        return                      # a doua bifare (reincercare) nu mai adauga nimic in camion
    _state["facute"].add(sid)
    plan = _state["plan"]
    r = plan and next((x for x in plan["routes"] if sid in x["stops"]), None)
    if r is None:
        return
    t = r["truck"]
    if engine.e_groapa(sid):
        _state["incarcare"][t] = 0
    else:
        _state["incarcare"][t] = _state["incarcare"].get(t, 0) + plan.get("_vol", {}).get(sid, 0)
    _reordoneaza_camion(t)


@router.post("/opriri/{stop_id}/descarcat")
def descarcat(stop_id: str, date: dict | None = Depends(cere_rol("sofer", "admin"))):
    """Soferul a descarcat camionul la groapa: camionul e gol si continua traseul."""
    if not engine.e_groapa(stop_id):
        raise HTTPException(404, "Oprirea nu e o descărcare")
    with _lock:
        plan = _state["plan"]
        if plan is None or not any(stop_id in r["stops"] for r in plan["routes"]):
            if stop_id in _state["facute"]:
                return {"status": "ok", "deja": True}   # reincercare dupa ce s-a salvat deja
            raise HTTPException(404, "Descărcarea nu mai e pe nicio rută")
        _verifica_oprire(date, stop_id)
        _bifeaza(stop_id)
        _event({"id": stop_id, "status": "descarcat"})
    return {"status": "ok"}



# ---------- depozitari ilegale (camera prin backend, sau cetateni) ----------

class Raportare(BaseModel):
    lat: float
    lng: float
    sursa: Literal["camera", "cetatean"] = "cetatean"
    marime: str = "medie"            # mica / medie / mare
    camera_id: str | None = None
    imagine: str | None = None       # optional: poza camerei, JPEG in base64 (cu sau fara "data:image/jpeg;base64,")


def _salveaza_poza(alert_id, b64):
    """Poza trimisa de camera, salvata ca la raportarile cetatenilor (dashboard-ul o arata in popup)."""
    cale = CAPTURES / f"{alert_id}.jpg"
    if cale.exists():
        return True                  # la duplicat ramane poza primei detectii
    try:
        date = base64.b64decode(b64.split(",", 1)[-1], validate=False)
        Image.open(io.BytesIO(date)).convert("RGB").save(cale, "JPEG", quality=85)
        return True
    except Exception:
        return False


@router.post("/alerte")
def alerta(r: Raportare):
    """Camera (modelul Dariei) sau backend-ul o cheama cand e detectat gunoi; acelasi punct de mai multe ori = o singura alerta."""
    with _connect() as conn, _lock:   # conexiunea (lenta) se deschide inainte de lacat
        rez = _register_alert(conn, r.sursa, r.lat, r.lng, r.marime, r.camera_id)
    if r.imagine:
        rez["imagine"] = "salvata" if _salveaza_poza(rez["id"], r.imagine) else "invalida"
    return rez


def _cere_cheie_camera(x_cheie_camera):
    """Doar camerele (cu CHEIE_CAMERA din .env) au voie sa scoata alerte din rute."""
    cheie = os.environ.get("CHEIE_CAMERA", "")
    if not cheie:
        raise HTTPException(503, "Cheia camerelor (CHEIE_CAMERA) nu e setata pe server")
    if not x_cheie_camera or not hmac.compare_digest(x_cheie_camera, cheie):
        raise HTTPException(401, "Cheie de cameră greșită")


def _inchide_disparute(conn, ids):
    """Depozitarile nu mai sunt acolo: alertele se inchid "disparuta" si opririle ies din rute
    (nu raman bifate). Intoarce id-urile inchise acum (cele deja inchise sunt ignorate)."""
    inchise = [r["id"] for r in conn.execute(
        """UPDATE rutare_alerte SET status = 'disparuta', inchisa = now()
           WHERE id = ANY(%s) AND status IN ('deschisa', 'asignata') RETURNING id""", (list(ids),)).fetchall()]
    for aid in inchise:
        if _state["plan"] is not None:
            t = engine.scoate_oprire(_state["plan"], aid)
            if t is not None:
                _reordoneaza_camion(t)     # poate nu mai e nevoie de o descarcare
        _event({"id": aid, "status": "disparuta"})
    return inchise


@router.post("/alerte/{alert_id}/disparuta")
def alerta_disparuta(alert_id: str, x_cheie_camera: str | None = Header(None)):
    """Camera anunta ca depozitarea cu acest id nu mai e acolo (luata de altcineva sau detectie falsa).

    Nu e o colectare: alerta se inchide cu statusul "disparuta", iar oprirea iese din ruta camionului.
    Cere headerul X-Cheie-Camera. Se poate chema de mai multe ori (a doua oara nu mai schimba nimic).
    """
    _cere_cheie_camera(x_cheie_camera)
    with _connect() as conn, _lock:
        if not conn.execute("SELECT 1 FROM rutare_alerte WHERE id = %s", (alert_id,)).fetchone():
            raise HTTPException(404, "Alerta nu există")
        inchise = _inchide_disparute(conn, [alert_id])
    return {"status": "ok", "inchise": inchise}


class LocCurat(BaseModel):
    lat: float
    lng: float
    camera_id: str | None = None


@router.post("/alerte/curat")
def loc_curat(l: LocCurat, x_cheie_camera: str | None = Header(None)):
    """Camera nu mai vede gunoi la (lat, lng): se inchid toate alertele deschise de acolo
    (pe o raza de MERGE_RADIUS_M, aceeasi ca la duplicate), fara sa trebuiasca stiut id-ul lor.

    Camera trimite aceleasi coordonate ca la POST /api/alerte. Ca un singur cadru fara gunoi
    (cineva care trece prin fata) sa nu scoata oprirea din ruta, camera trimite asta abia dupa
    ce nu mai vede gunoiul cateva cadre la rand. Cere headerul X-Cheie-Camera.
    """
    _cere_cheie_camera(x_cheie_camera)
    with _connect() as conn, _lock:
        deschise = conn.execute(
            "SELECT id, lat, lng FROM rutare_alerte WHERE status IN ('deschisa', 'asignata')").fetchall()
        aproape = [a["id"] for a in deschise
                   if dumping.distance_m(a["lat"], a["lng"], l.lat, l.lng) <= dumping.MERGE_RADIUS_M]
        inchise = _inchide_disparute(conn, aproape)
    return {"status": "ok", "inchise": inchise}


@router.get("/alerte")
def lista_alerte():
    with _connect() as conn:
        return conn.execute("SELECT * FROM rutare_alerte ORDER BY creata DESC").fetchall()


@router.post("/alerte/{alert_id}/colectata")
def alerta_colectata(alert_id: str, date: dict | None = Depends(cere_rol("sofer", "admin"))):
    with _connect() as conn, _lock:   # conexiunea (lenta) se deschide inainte de lacat
        a = conn.execute("SELECT status FROM rutare_alerte WHERE id = %s", (alert_id,)).fetchone()
        if a is None:
            raise HTTPException(404, "Alerta nu există")
        if a["status"] in ("colectata", "disparuta"):
            # deja inchisa: a doua apasare, o reincercare dupa o cadere scurta sau camera a vazut
            # inaintea soferului ca nu mai e nimic acolo. Pentru sofer e tot "gata", nu o eroare.
            return {"status": "ok", "deja": a["status"]}
        _verifica_oprire(date, alert_id)
        conn.execute("UPDATE rutare_alerte SET status = 'colectata', inchisa = now() WHERE id = %s", (alert_id,))
        _bifeaza(alert_id)
        _event({"id": alert_id, "status": "colectata"})
    return {"status": "ok"}


@router.get("/pubele-fizice")
def pubele_fizice():
    """Pubelele reale din trashes, pentru harta din dashboard."""
    with _connect() as conn:
        if not _has_trashes(conn):
            return []
        return [{k: r[k] for k in ("id", "lat", "lng", "umplere")} for r in _trashes(conn)]


_PUBELE = {"la": -1e9, "date": None}
_PUBELE_LOCK = threading.Lock()
PUBELE_CACHE_S = 3                  # dashboard-ul si aplicatia cer harta des; raspunsul e refolosit 3 secunde


@router.get("/pubele")
def toate_pubelele():
    """Pubelele reale si cele simulate, cu umplerea curenta, pentru harta din dashboard."""
    with _PUBELE_LOCK:              # cererile venite deodata asteapta un singur calcul
        if _PUBELE["date"] is None or time.monotonic() - _PUBELE["la"] > PUBELE_CACHE_S:
            _PUBELE["date"] = _calculeaza_pubele()
            _PUBELE["la"] = time.monotonic()
        return _PUBELE["date"]


def _calculeaza_pubele():
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


POZA_MAX_MB = 10
RAPOARTE_PE_MINUT = 3               # de la aceeasi adresa IP (in memorie: se pierde la repornire, e de ajuns)
_RAPOARTE_IP = {}                   # ip -> momentele ultimelor rapoarte (time.monotonic)
_RAPOARTE_LOCK = threading.Lock()


def _marime_poza(poza):
    """Marimea fisierului urcat, fara sa-l citim: e deja pe disc (sau in memorie, daca e mic)."""
    if poza.size is not None:
        return poza.size
    poza.file.seek(0, 2)
    n = poza.file.tell()
    poza.file.seek(0)
    return n


def _limita_rapoarte(ip):
    """Cel mult RAPOARTE_PE_MINUT rapoarte pe minut de la acelasi IP; altfel 429."""
    acum = time.monotonic()
    with _RAPOARTE_LOCK:
        for k in [k for k, v in _RAPOARTE_IP.items() if not v or acum - v[-1] > 60]:
            del _RAPOARTE_IP[k]          # IP-urile care n-au mai trimis nimic de un minut nu mai tin memorie
        t = _RAPOARTE_IP.setdefault(ip, deque())
        while t and acum - t[0] > 60:
            t.popleft()
        if len(t) >= RAPOARTE_PE_MINUT:
            asteapta = math.ceil(60 - (acum - t[0]))
            raise HTTPException(429, f"Ai trimis deja {RAPOARTE_PE_MINUT} rapoarte în ultimul minut. "
                                     f"Încearcă din nou peste {asteapta} secunde.")
        t.append(acum)


@router.post("/rapoarte")  # deschis si vizitatorilor: poza trece oricum prin filtrul Gemini
def raport_cetatean(request: Request, poza: UploadFile = File(...), lat: float = Form(...), lng: float = Form(...)):
    """Gemini verifica poza; daca e gunoi, intra ca orice alerta (duplicate, inserare in ruta)."""
    if _marime_poza(poza) > POZA_MAX_MB * 1024 * 1024:
        raise HTTPException(413, f"Poza e prea mare (maximum {POZA_MAX_MB} MB). Trimite o poză mai mică.")
    # in spatele lui Caddy, uvicorn ia adresa reala din X-Forwarded-For (proxy-ul e pe 127.0.0.1)
    _limita_rapoarte(request.client.host if request.client else "necunoscut")
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

    with _connect() as conn, _lock:   # conexiunea (lenta) se deschide inainte de lacat
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

_NUME_GHENE = {"la": 0.0, "date": {}}
NUME_GHENE_S = 60                   # numele si adresele se schimba rar: le citim cel mult o data pe minut


def _nume_ghene():
    """Numele si adresa ghenelor din trashes, tinute minte un minut. Soferii cer ruta si navigatia
    des; fara asta, fiecare cerere deschidea o conexiune noua la baza de date doar pentru nume."""
    if time.monotonic() - _NUME_GHENE["la"] > NUME_GHENE_S:
        with _connect() as conn:
            _NUME_GHENE["date"] = {r["id"]: r for r in conn.execute("SELECT id, nume, adresa FROM trashes").fetchall()} \
                if _has_trashes(conn) else {}
        _NUME_GHENE["la"] = time.monotonic()
    return _NUME_GHENE["date"]


@router.get("/ruta/{camion}")
def ruta_camion(camion: int):
    """Ruta zilei pentru un camion, cu coordonatele fiecarei opriri, in ordinea in care le ia."""
    with _lock:
        plan = _state["plan"]
        if plan is None:
            # dupa o repornire planul se calculeaza cateva secunde: 503 = "mai incearca", aplicatia
            # reincearca singura si soferul ramane pe ruta si navigatia pe care le are
            raise HTTPException(503, "Planul zilei se calculează, revine în câteva secunde")
        ruta = next((r for r in plan["routes"] if r["truck"] == camion), None)
        if ruta is None:
            raise HTTPException(404, f"Camionul {camion} nu are ruta azi")
        pubele = {b["id"]: b for b in _state["bins"] or []}
        alerte = {a["id"]: a for a in plan["alerts"]}
        stops, geometrie, distanta = list(ruta["stops"]), ruta["geometry"], ruta["distance_m"]
        incarcare, facute = ruta["load_l"], set(_state["facute"])
        in_camion = _state["incarcare"].get(camion, 0)
    info = _nume_ghene()                # numele si adresa ghenelor, din tabela lui Robert
    info = {**SIM, **info}              # ghenele simulate au numele strazii din sim_bins.json
    opriri = []
    for sid in stops:
        if sid == "depot":
            continue
        if engine.e_groapa(sid):
            opriri.append({**_oprire_groapa(sid), "facuta": sid in facute})
        elif sid in alerte and alerte[sid].get("kind") == "alert":
            a = alerte[sid]
            opriri.append({"id": sid, "tip": "depozitare", "lat": a["lat"], "lng": a["lng"],
                           "nume": "Depozitare ilegală", "adresa": None, "facuta": sid in facute})
        elif sid in pubele:
            b, r = pubele[sid], info.get(sid, {})
            opriri.append({"id": sid, "tip": "pubela", "lat": b["lat"], "lng": b["lng"],
                           "nume": r.get("nume") or sid, "adresa": r.get("adresa"),
                           "umplere": b["umplere"], "facuta": sid in facute})
    return {"camion": camion, "distanta_m": distanta, "incarcare_l": incarcare, "opriri": opriri,
            "traseu": geometrie, "in_camion_l": in_camion, "capacitate_l": engine.TRUCK_CAPACITY_L}


def _oprire_groapa(sid):
    g = engine.GROAPA
    return {"id": sid, "tip": "groapa", "nume": g["nume"], "adresa": g["adresa"],
            "lat": float(g["lat"]), "lng": float(g["lng"])}


@router.get("/navigatie/{camion}")
def navigatie(camion: int, lat: float | None = None, lng: float | None = None, directie: float | None = None):
    """Ca un GPS: drumul pas cu pas spre fiecare oprire ramasa, apoi inapoi la depou.

    lat/lng = pozitia curenta a soferului; fara ele, pornim de la ultima oprire facuta (sau depou).
    directie = incotro merge camionul (grade, 0 = nord), optional: drumul porneste pe sensul pe care
    chiar merge, nu pe cel opus (altfel ar trebui sa se intoarca).
    etape[i] duce la opriri[i]; ultima etapa duce la depou.
    """
    with _lock:
        plan = _state["plan"]
        if plan is None:
            # dupa o repornire planul se calculeaza cateva secunde: 503 = "mai incearca", aplicatia
            # reincearca singura si soferul ramane pe ruta si navigatia pe care le are
            raise HTTPException(503, "Planul zilei se calculează, revine în câteva secunde")
        ruta = next((r for r in plan["routes"] if r["truck"] == camion), None)
        if ruta is None:
            raise HTTPException(404, f"Camionul {camion} nu are ruta azi")
        pubele = {b["id"]: b for b in _state["bins"] or []}
        alerte = {a["id"]: a for a in plan["alerts"]}
        facute = set(_state["facute"])
        stops = [s for s in ruta["stops"] if s != "depot"]
        locuri = dict(plan.get("_stops", {}))   # unde opreste camionul la fiecare oprire (cu sensul strazii)
    info = _nume_ghene()
    info = {**SIM, **info}              # ghenele simulate au numele strazii din sim_bins.json

    depou = (engine.DEPOT["lat"], engine.DEPOT["lng"])
    opriri, start, start_loc = [], depou, locuri.get("depot")
    for sid in stops:
        if engine.e_groapa(sid):
            o = _oprire_groapa(sid)
        elif sid in alerte and alerte[sid].get("kind") == "alert":
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
            start, start_loc = (o["lat"], o["lng"]), locuri.get(sid)   # ultima oprire facuta: de aici pleaca
        else:
            opriri.append(o)
    if lat is not None and lng is not None:
        start, start_loc = (lat, lng), None
    opriri.append({"id": "depot", "tip": "depou", "nume": "depou", "adresa": None,
                   "lat": depou[0], "lng": depou[1]})

    etape, prev, prev_loc = [], start, start_loc
    for o in opriri:
        try:
            etape.append(engine.etapa_navigatie(prev, (o["lat"], o["lng"]), o["nume"], start_oprire=prev_loc,
                                                dest_oprire=locuri.get(o["id"]),
                                                directie=directie if prev_loc is None else None))
        except Exception as e:
            # o oprire fara drum pe harta nu strica toata navigatia: etapa ei e o linie dreapta
            print("rutare: etapa de navigatie a esuat:", o["id"], e)
            d = round(engine._m(list(prev), [o["lat"], o["lng"]]))
            etape.append({"geometrie": [list(prev), [o["lat"], o["lng"]]], "distanta_m": d, "pasi": [
                {"tip": "start", "text": f"Mergi spre {o['nume']}", "strada": o["nume"], "la_m": 0,
                 "lat": prev[0], "lng": prev[1], "distanta_m": d},
                {"tip": "sosire", "text": f"Ai ajuns la {o['nume']}", "strada": o["nume"], "la_m": d,
                 "lat": o["lat"], "lng": o["lng"], "distanta_m": 0}]})
        prev, prev_loc = (o["lat"], o["lng"]), locuri.get(o["id"])
    return {"camion": camion, "opriri": opriri, "etape": etape}


@router.post("/ruta/{camion}/reia")
def reia_traseu(camion: int, date: dict | None = Depends(cere_rol("sofer", "admin"))):
    """Pentru demo: soferul isi reia traseul de la capat, fara dispecer si fara sa atinga alte camioane.

    Opririle camionului nu mai sunt bifate, camionul e gol si pleaca din depou; ghenele simulate de pe
    ruta revin la umplerea de dimineata, iar depozitarile colectate de el redevin "pe ruta".
    """
    c = _camionul_meu(date)
    if c is not None and c != camion:
        raise HTTPException(403, "Poți relua doar traseul camionului tău")
    with _connect() as conn, _lock:
        plan = _state["plan"]
        ruta = plan and next((r for r in plan["routes"] if r["truck"] == camion), None)
        if ruta is None:
            raise HTTPException(404, f"Camionul {camion} nu are ruta azi")
        ids = [s for s in ruta["stops"] if s != "depot"]
        ghene = [s for s in ids if s in SIM and s not in SENZORI]   # senzorii reali raman cum sunt
        with conn.cursor() as cur:
            cur.executemany("UPDATE rutare_pubele SET umplere = %s, actualizat = now(), golita_la = NULL WHERE id = %s",
                            [(round(SIM[b]["fill_level"] * 100), b) for b in ghene])
            if _has_trashes(conn):
                cur.executemany("UPDATE trashes SET umplere = %s, actualizat = now() WHERE id = %s",
                                [(round(SIM[b]["fill_level"] * 100), b) for b in ghene])
        conn.execute("DELETE FROM rutare_golire WHERE bin_id = ANY(%s)", (ghene,))
        conn.execute("""UPDATE rutare_alerte SET status = 'asignata', camion = %s, inchisa = NULL
                        WHERE id = ANY(%s) AND status = 'colectata'""", (camion, ids))
        _state["facute"] -= set(ids)
        _state["incarcare"][camion] = 0
        _state["pozitii"].pop(camion, None)
        engine.reordoneaza(plan, camion, _state["facute"], None, incarcat=0)   # din depou, nu de la GPS
        _event({"id": f"camion-{camion}", "status": "reluat"})
    return {"status": "ok"}


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
def compat_rapoarte(request: Request, poza: UploadFile = File(...), lat: float = Form(...), lng: float = Form(...),
                    descriere: str = Form("")):
    """Aplicatia asteapta {id, status}; filtrul Gemini si alertele sunt cele din /api/rapoarte."""
    r = raport_cetatean(request, poza, lat, lng)
    if not r["acceptat"]:
        return {"id": "-", "status": "Respinsă", "motiv": r["motiv"]}
    return {"id": r["alerta"]["id"], "status": "Acceptată", "descriere": r["descriere"]}


@compat.post("/pozitie")
def compat_pozitie(p: Pozitie, date: dict | None = Depends(cere_rol("sofer", "admin"))):
    return pozitie(p, date)
