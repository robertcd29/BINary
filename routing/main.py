"""Serverul modulului de rutare, separat de backend.

Pornire, din folderul routing (alt port decat backend-ul):
    uvicorn main:app --host 0.0.0.0 --port 8001
La pornire incepe sa urmareasca tabela trashes (senzorii reali, scrisi de backend).
"""
import threading
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi.staticfiles import StaticFiles

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import api


@asynccontextmanager
async def lifespan(app):
    stop = threading.Event()
    threading.Thread(target=api.watch, args=(stop,), daemon=True).start()
    threading.Thread(target=api.precalculeaza, daemon=True).start()   # planul se face in fundal
    yield
    stop.set()


app = FastAPI(title="BINary - rutare", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(api.router)
app.include_router(api.compat)      # caile fara /api pe care le foloseste aplicatia lui Mihai
app.mount("/captures", StaticFiles(directory="captures", check_dir=False), name="captures")
class FaraCache(StaticFiles):
    """Browserul verifica mereu versiunea noua, deci un refresh simplu arata ultimele modificari."""
    async def get_response(self, path, scope):
        r = await super().get_response(path, scope)
        r.headers["Cache-Control"] = "no-cache, must-revalidate"
        return r


# dashboard-ul vine direct din repo (routing/static), deci se actualizeaza singur la fiecare push
app.mount("/dispecer", FaraCache(directory=Path(__file__).parent / "static", html=True), name="static")
app.mount("/", StaticFiles(directory="/home/ubuntu/frontend/mobil", html=True, check_dir=False), name="mobil")