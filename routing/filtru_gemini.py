"""Filtrul pentru pozele trimise de cetateni: Gemini decide daca poza arata gunoi aruncat ilegal.

Test din terminal:  python filtru_gemini.py poza1.jpg poza2.jpg
"""
import os
import sys
import time
from typing import Literal

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from PIL import Image
from pydantic import BaseModel

load_dotenv()
client = genai.Client(
    api_key=os.environ["GEMINI_API_KEY"],
    http_options=types.HttpOptions(api_version="v1beta"),   # raspunsul in JSON merge doar pe v1beta
)
MODEL = "gemini-2.5-flash-lite"
PRAG = 0.6

INSTRUCTIUNI = (
    "You check photos sent by citizens to a city waste-reporting app. "
    "Decide if the photo shows illegally dumped garbage, litter, or an "
    "overflowing trash bin in a real outdoor place. "
    "Selfies, screenshots, clean streets, animals, food, indoor rooms, memes, "
    "and empty or damaged bins with no visible garbage are NOT valid. "
    "If valid, estimate the size: mica (one bag or a few items), medie "
    "(several bags or a small pile), mare (furniture, construction debris, a truckload). "
    "Give a confidence between 0 and 1 and a short description in Romanian."
)


class Verdict(BaseModel):
    e_gunoi: bool
    incredere: float
    marime: Literal["mica", "medie", "mare"]
    descriere: str


def verifica(imagine: Image.Image) -> dict:
    for incercare in range(3):
        try:
            raspuns = client.models.generate_content(
                model=MODEL,
                contents=[INSTRUCTIUNI, imagine.convert("RGB")],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=Verdict,
                ),
            )
            break
        except (errors.ServerError, errors.ClientError) as e:
            if incercare == 2:
                raise
            print("  reîncerc după eroare", e.code)
            time.sleep(5 * (incercare + 1))

    v = raspuns.parsed
    return {
        "e_gunoi": v.e_gunoi and v.incredere >= PRAG,
        "scor": round(v.incredere, 3),
        "marime": v.marime,
        "descriere": v.descriere,
    }


if __name__ == "__main__":
    for cale in sys.argv[1:]:
        print(cale, verifica(Image.open(cale)))
        time.sleep(5)
