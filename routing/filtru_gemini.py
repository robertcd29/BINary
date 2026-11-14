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
from PIL import Image, ImageOps
from pydantic import BaseModel

load_dotenv()
client = genai.Client(
    api_key=os.environ["GEMINI_API_KEY"],
    http_options=types.HttpOptions(api_version="v1beta"),   # raspunsul in JSON merge doar pe v1beta
)
MODEL = "gemini-3.5-flash-lite"         # rapid (~1,5 s): decide singur pozele clare
MODEL_ATENT = "gemini-3.5-flash"        # mai lent (~10 s), dar mai atent: doar pentru pozele la limita
SIGUR = 0.85                            # sub aceasta incredere, poza merge si la modelul atent
PRAG = 0.6
LATURA_MAX = 1600               # pozele de 12 MP se micsoreaza: mai rapid, aceeasi precizie

INSTRUCTIUNI = """You review photos that citizens of Timișoara send to a city app to report waste
that a garbage truck or a cleaning crew must pick up. Look at the whole photo carefully first,
describe what you see, and only then decide.

VALID (e_gunoi = true): waste that needs to be collected, for example
- litter, bottles, cans, packaging, bags or other trash lying on the ground, grass, sidewalk,
  road, in bushes, in water or in a forest, even a small amount if it is clearly discarded;
- garbage bags, boxes or waste left next to bins or containers;
- bins or containers that are overflowing (the lid cannot close, waste spills out or is piled on top);
- bulky waste dumped outdoors: furniture, mattresses, appliances, tires, construction or
  renovation debris, bags of garden waste.
Photos taken from any distance or angle, at night, with flash or slightly blurry are fine,
as long as the waste can be recognized.

NOT VALID (e_gunoi = false):
- bins or containers that are closed or normally filled, with nothing outside them (that is normal);
- clean streets, parks, buildings or landscapes;
- only natural debris: fallen leaves, branches, snow, mud, puddles;
- people, selfies, animals, food on a plate, indoor rooms, cars;
- screenshots, photos of a screen, drawings, memes or text;
- photos too dark or too blurry to see anything.

Size: mica = a few items or one bag; medie = several bags or a small pile;
mare = furniture, appliances, construction debris or a truckload.
incredere = how sure you are of your e_gunoi decision, from 0 to 1.
sfat: only when not valid, one short sentence telling the citizen what to photograph instead
(for example closer, in daylight, with the waste in the center). Do not mention other app features
or sections. Leave it empty when valid.
Write every text field in Romanian, short and plain, as for a citizen."""


class Verdict(BaseModel):
    # ordinea conteaza: modelul descrie intai poza, apoi decide
    ce_se_vede: str             # descrierea obiectiva a scenei
    gunoi_vizibil: str          # ce deseuri se vad si unde, sau "niciunul"
    e_gunoi: bool
    incredere: float
    marime: Literal["mica", "medie", "mare"]
    descriere: str              # o propozitie pentru dispecer
    sfat: str                   # daca nu e valida: ce sa faca cetateanul; altfel gol


def pregateste(imagine: Image.Image) -> Image.Image:
    """Poza intoarsa corect (telefoanele o salveaza adesea culcata, cu orientarea in EXIF) si micsorata."""
    imagine = ImageOps.exif_transpose(imagine).convert("RGB")
    imagine.thumbnail((LATURA_MAX, LATURA_MAX))
    return imagine


def _intreaba(model: str, imagine: Image.Image) -> Verdict:
    for incercare in range(3):
        try:
            raspuns = client.models.generate_content(
                model=model,
                contents=[INSTRUCTIUNI, imagine],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=Verdict,
                    temperature=0,
                    # fara "gandire" lunga: descrierea din schema ii ajunge, iar cetateanul asteapta
                    thinking_config=types.ThinkingConfig(thinking_level="low"),
                ),
            )
            return raspuns.parsed
        except (errors.ServerError, errors.ClientError) as e:
            if incercare == 2:
                raise
            print("  reîncerc după eroare", e.code)
            time.sleep(5 * (incercare + 1))


def verifica(imagine: Image.Image) -> dict:
    imagine = pregateste(imagine)
    v = _intreaba(MODEL, imagine)
    if v.incredere < SIGUR:             # poza la limita: a doua parere, de la modelul atent
        try:
            v = _intreaba(MODEL_ATENT, imagine)
        except (errors.ServerError, errors.ClientError):
            pass                        # ramane verdictul modelului rapid
    valid = v.e_gunoi and v.incredere >= PRAG
    if valid:
        motiv = ""
    elif v.e_gunoi:     # pare gunoi, dar modelul nu e sigur
        motiv = "Nu suntem siguri că poza arată deșeuri. Fă o poză mai de aproape, la lumină, cu deșeurile în centru."
    else:               # ce a vazut, pe scurt, plus ce sa fotografieze
        vazut = v.ce_se_vede.split(". ")[0].rstrip(".")
        motiv = f"În poză se vede: {vazut[0].lower() + vazut[1:] if vazut else 'altceva'}. {v.sfat}".strip()
    return {
        "e_gunoi": valid,
        "scor": round(v.incredere, 3),
        "marime": v.marime,
        "descriere": v.descriere,
        "motiv": motiv,
    }


if __name__ == "__main__":
    for cale in sys.argv[1:]:
        print(cale, verifica(Image.open(cale)))
        time.sleep(2)
