import os

import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from psycopg.rows import dict_row
from pydantic import BaseModel

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]

app = FastAPI()


def conectare():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    with conectare() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trashes (
                id TEXT PRIMARY KEY,
                lat DOUBLE PRECISION,
                lng DOUBLE PRECISION,
                umplere INTEGER,
                actualizat TIMESTAMPTZ DEFAULT now()
            )
        """)
        nr = conn.execute("SELECT COUNT(*) AS nr FROM trashes").fetchone()["nr"]
        if nr == 0:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO trashes (id, lat, lng, umplere) VALUES (%s, %s, %s, %s)",
                    [
                        ("TM-001", 45.7489, 21.2087, 73),
                        ("TM-002", 45.7537, 21.2257, 41),
                        ("TM-003", 45.7601, 21.2290, 90),
                    ],
                )


init_db()


class Masuratoare(BaseModel):
    id: str
    umplere: int


@app.get("/trashes")
def lista_trashes():
    with conectare() as conn:
        return conn.execute("SELECT * FROM trashes ORDER BY id").fetchall()


@app.post("/masuratori")
def primeste_masuratoare(m: Masuratoare):
    with conectare() as conn:
        rezultat = conn.execute(
            "UPDATE trashes SET umplere = %s, actualizat = now() WHERE id = %s",
            (m.umplere, m.id),
        )
        if rezultat.rowcount == 0:
            raise HTTPException(status_code=404, detail="Ghena nu există")
    return {"status": "ok"}