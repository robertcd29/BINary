"""Test cap-coada: backend-ul (simulat aici) scrie in trashes, iar rutarea reactioneaza.

ATENTIE: sterge si recreeaza tabelele din baza data in TEST_DATABASE_URL. Nu folosi baza echipei.
Rulare, din folderul routing:
    createdb binary_test
    TEST_DATABASE_URL=postgresql://localhost/binary_test python -m pytest test_api.py -q
"""
import os

import psycopg
import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="seteaza TEST_DATABASE_URL")
CAMERA = (45.73738, 21.23222)


@pytest.fixture(scope="module")
def client():
    with psycopg.connect(TEST_DB) as conn:
        conn.execute("DROP TABLE IF EXISTS trashes, rutare_pubele, rutare_citiri, rutare_camere, "
                     "rutare_golire, rutare_alerte")
        # exact tabela si datele din server.py-ul lui Robert
        conn.execute("CREATE TABLE trashes (id TEXT PRIMARY KEY, lat DOUBLE PRECISION, "
                     "lng DOUBLE PRECISION, umplere INTEGER, actualizat TIMESTAMPTZ DEFAULT now())")
        conn.execute("INSERT INTO trashes (id, lat, lng, umplere) VALUES "
                     "('TM-001', 45.7489, 21.2087, 73), ('TM-002', 45.7537, 21.2257, 41)")
        # TM-003 nu a mai trimis nimic de 2 ore
        conn.execute("INSERT INTO trashes VALUES ('TM-003', 45.7601, 21.2290, 90, now() - interval '2 hours')")
    os.environ["DATABASE_URL"] = TEST_DB
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app)       # fara "with": bucla de fundal nu porneste, apelam poll_once direct


def robert(bin_id, value):
    """Ce face backend-ul cand primeste o valoare de la senzor."""
    with psycopg.connect(TEST_DB) as conn:
        conn.execute("UPDATE trashes SET umplere = %s, actualizat = now() WHERE id = %s", (value, bin_id))
    import api
    return api.poll_once()


def trashes_snapshot():
    with psycopg.connect(TEST_DB) as conn:
        cols = [r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns "
                                           "WHERE table_name = 'trashes' ORDER BY ordinal_position")]
        return cols, conn.execute("SELECT count(*) FROM trashes").fetchone()[0]


def test_pubelele_lui_robert_intra_in_rute_si_trashes_ramane_neatinsa(client):
    plan = client.get("/api/rute").json()
    everywhere = {s for r in plan["routes"] for s in r["stops"]} | set(plan["skipped"])
    assert {"TM-001", "TM-002", "TM-003", "bin_000"} <= everywhere
    assert trashes_snapshot() == (["id", "lat", "lng", "umplere", "actualizat"], 3)


def test_senzor_care_nu_mai_transmite_e_estimat(client):
    plan = client.post("/api/rute/planifica").json()
    assert plan["surse"]["TM-002"]["sursa"] == "senzor"
    assert plan["surse"]["TM-003"]["sursa"] == "estimat"
    assert any(w.startswith("TM-003") for w in plan["warnings"])


def test_golire_si_mediana(client):
    robert("TM-001", 20)
    events = robert("TM-001", 21)                 # doua valori mici la rand dupa 73 = golire
    assert any(e["status"] == "golita" for e in events)
    for v in (95, 20, 21):                        # 95 = varf izolat, nu golire si nu urgenta
        assert not any(e["status"] in ("golita", "urgenta", "assigned") for e in robert("TM-001", v))
    plan = client.post("/api/rute/planifica").json()
    assert plan["surse"]["TM-001"]["umplere"] == 21


def test_o_singura_citire_zero_nu_e_golire(client):
    for v in (60, 60, 60, 0, 60):
        assert not any(e["status"] == "golita" for e in robert("TM-002", v))


def test_pubela_urgenta_intra_o_singura_data_in_ruta(client):
    plan = client.post("/api/rute/planifica").json()
    assert "TM-001" in plan["skipped"], "TM-001 la 21% ar trebui sa fie sarita"
    for v in (85, 86, 84, 79, 81, 78, 82, 85):    # oscileaza in jurul pragului
        robert("TM-001", v)
    plan = client.get("/api/rute").json()
    inserted = [e for e in plan["evenimente"] if e["id"] == "TM-001" and e["status"] == "assigned"]
    assert len(inserted) == 1
    assert sum("TM-001" in r["stops"] for r in plan["routes"]) == 1


def test_alerta_de_la_camera_deduplicata(client):
    first = client.post("/api/alerte", json={"lat": CAMERA[0], "lng": CAMERA[1], "sursa": "camera"}).json()
    assert first["status"] == "assigned" and first["sursa"] == "camera"
    for _ in range(3):                            # modelul detecteaza din nou la fiecare poza
        again = client.post("/api/alerte", json={"lat": CAMERA[0], "lng": CAMERA[1], "sursa": "camera"}).json()
        assert again["status"] == "duplicat" and again["id"] == first["id"]
    alerts = {a["id"]: a for a in client.get("/api/alerte").json()}
    assert alerts[first["id"]]["nr_raportari"] == 4 and alerts[first["id"]]["sursa"] == "camera"


def test_replanificare_si_colectare(client):
    plan = client.post("/api/rute/planifica").json()
    assert plan["predictions"]["bin_001"]["hours_to_full"] < 500
    assert "AL-0001" in {s for r in plan["routes"] for s in r["stops"]}
    assert client.post("/api/alerte/AL-0001/colectata").json()["status"] == "ok"
    plan = client.post("/api/rute/planifica").json()
    assert "AL-0001" not in {s for r in plan["routes"] for s in r["stops"]}
    assert client.post("/api/pubele/bin_005/golita").json()["status"] == "ok"
    assert client.post("/api/pubele/TM-001/golita").status_code == 404
    assert trashes_snapshot()[1] == 3
