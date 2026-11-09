"""Serverul modulului de rutare, separat de backend.

Pornire, din folderul routing (alt port decat backend-ul):
    uvicorn main:app --host 0.0.0.0 --port 8001
La pornire incepe sa urmareasca tabela trashes (senzorii reali, scrisi de backend).
"""
import threading
from contextlib import asynccontextmanager
from fastapi.staticfiles import StaticFiles

from fastapi import FastAPI

import api


@asynccontextmanager
async def lifespan(app):
    stop = threading.Event()
    threading.Thread(target=api.watch, args=(stop,), daemon=True).start()
    yield
    stop.set()


app = FastAPI(title="BINary - rutare", lifespan=lifespan)
app.include_router(api.router)
app.mount("/", StaticFiles(directory="static", html=True), name="static")