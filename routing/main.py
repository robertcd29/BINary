"""Serverul modulului de rutare, separat de backend.

Pornire, din folderul routing:
    uvicorn main:app --host 0.0.0.0 --port 5000
Portul 5000 e cel la care trimit acum sensors.ino si cam.ino.
"""
from fastapi import FastAPI

from api import router

app = FastAPI(title="BINary - rutare")
app.include_router(router)
