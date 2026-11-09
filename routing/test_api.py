"""Test cap-coada al serverului, cu payload-urile exacte trimise de sensors.ino si cam.ino.

ATENTIE: sterge si recreeaza tabelele din baza data in TEST_DATABASE_URL. Nu folosi baza de demo.
Rulare, din folderul routing:
    createdb binary_test
    TEST_DATABASE_URL=postgresql://localhost/binary_test python -m pytest test_api.py -q
"""
import os

import psycopg
import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="seteaza TEST_DATABASE_URL")


@pytest.fixture(scope="module")
def client():
    with psycopg.connect(TEST_DB) as conn:
        conn.execute("DROP TABLE IF EXISTS trashes, rutare_pubele, rutare_citiri, rutare_camere, rutare_alerte")
        # tabela backend-ului, ca in server.py: trebuie doar citita
        conn.execute("CREATE TABLE trashes (id TEXT PRIMARY KEY, lat DOUBLE PRECISION, "
                     "lng DOUBLE PRECISION, umplere INTEGER, actualizat TIMESTAMPTZ DEFAULT now())")
        conn.execute("INSERT INTO trashes (id, lat, lng, umplere) VALUES ('TM-001', 45.7489, 21.2087, 73)")
    os.environ["DATABASE_URL"] = TEST_DB
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app)


def firmware_json(a, b, c):
    """Exact stringul construit de sendData() din sensors.ino."""
    return f'{{"bin1":{a:.1f},"bin2":{b:.1f},"bin3":{c:.1f}}}'


def post_sensors(client, a, b, c):
    r = client.post("/api/sensors", content=firmware_json(a, b, c),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    return r.json()


def test_trashes_e_doar_citita(client):
    plan = client.get("/api/rute").json()
    stops = {s for r in plan["routes"] for s in r["stops"]} | set(plan["skipped"])
    assert {"TM-001", "bin_000", "bin_059"} <= stops
    with psycopg.connect(TEST_DB) as conn:
        cols = [r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns "
                                           "WHERE table_name = 'trashes' ORDER BY ordinal_position")]
        assert cols == ["id", "lat", "lng", "umplere", "actualizat"]
        assert conn.execute("SELECT count(*) FROM trashes").fetchone()[0] == 1


def test_fara_citiri_senzorul_e_necunoscut(client):
    plan = client.post("/api/rute/planifica").json()
    assert plan["surse"]["bin_000"]["sursa"] == "necunoscut"
    assert any("bin_000" in w for w in plan["warnings"])


def test_mediana_si_float(client):
    for a in (20.0, 21.5, 95.0, 20.5, 21.0):     # 95 = varf izolat
        out = post_sensors(client, a, 15.0, 30.0)
    assert out["pubele"]["bin1"]["umplere"] == 21
    assert out["pubele"]["bin1"]["urgenta"] is False


def test_senzor_fara_raspuns_nu_devine_gol(client):
    out = post_sensors(client, 21.0, 15.0, -1.0)      # valoare invalida (negativ sau null) = eroare de senzor
    assert out["pubele"]["bin3"]["umplere"] == 30     # mediana citirilor bune ramane


def test_pubela_urgenta_intra_o_singura_data_in_ruta(client):
    plan = client.post("/api/rute/planifica").json()
    assert plan["surse"]["bin_000"]["sursa"] == "senzor"
    skipped = [b for b in ("bin_000", "bin_001", "bin_002") if b in plan["skipped"]]
    assert skipped, "nicio pubela fizica nu e sarita la umplere mica"
    key = {"bin_000": "bin1", "bin_001": "bin2", "bin_002": "bin3"}[skipped[0]]

    events = []
    for level in (85, 86, 84, 79, 81, 78, 82, 85):   # oscileaza in jurul pragului
        vals = {"bin1": 21.0, "bin2": 15.0, "bin3": 30.0, key: level}
        events += post_sensors(client, vals["bin1"], vals["bin2"], vals["bin3"])["evenimente"]
    inserted = [e for e in events if e.get("status") == "assigned"]
    assert len(inserted) == 1 and inserted[0]["id"] == skipped[0]

    plan = client.get("/api/rute").json()
    assert sum(skipped[0] in r["stops"] for r in plan["routes"]) == 1


def test_camera_confirma_dupa_trei_cadre(client):
    frame = {"camera_id": "CAM-01", "latime": 160, "inaltime": 120,
             "detectii": [{"clasa": "gunoi", "conf": 0.9, "bbox": [20, 20, 80, 70]}]}
    statuses = [client.post("/api/detectii", json=frame).json()["status"] for _ in range(4)]
    assert statuses[:2] == ["detectie", "detectie"]
    assert statuses[2] == "assigned"
    assert statuses[3] == "detectie"              # a patra poza nu mai creeaza alerta
    alerts = client.get("/api/alerte").json()
    assert len(alerts) == 1 and alerts[0]["marime"] == "medie" and alerts[0]["status"] == "asignata"


def test_raportare_cetatean_deduplicata(client):
    near = client.post("/api/alerte", json={"lat": 45.74777, "lng": 21.2395, "marime": "mare"}).json()
    assert near["status"] == "duplicat"
    far = client.post("/api/alerte", json={"lat": 45.7600, "lng": 21.2300}).json()
    assert far["status"] in ("assigned", "pending")
    alerts = {a["id"]: a for a in client.get("/api/alerte").json()}
    assert alerts[near["id"]]["nr_raportari"] == 2


def test_imagine_bruta_de_la_esp32_cam(client):
    r = client.post("/api/image", content=b"\xff\xd8\xff fake jpeg",
                    headers={"Content-Type": "image/jpeg"})
    assert r.status_code == 200 and r.json()["status"] in ("salvata", "nimic", "detectie")


def test_replanificare_pastreaza_alertele_si_golirea(client):
    plan = client.post("/api/rute/planifica").json()
    stops = {s for r in plan["routes"] for s in r["stops"]}
    assert {"AL-0001", "AL-0002"} <= stops | {a["id"] for a in plan["alerts"] if a["status"] == "pending"}
    assert client.post("/api/pubele/bin_000/golita").json()["status"] == "ok"
    assert client.post("/api/alerte/AL-0001/colectata").json()["status"] == "ok"
    plan = client.post("/api/rute/planifica").json()
    assert "AL-0001" not in {s for r in plan["routes"] for s in r["stops"]}
    assert plan["surse"]["bin_000"]["umplere"] == 0
