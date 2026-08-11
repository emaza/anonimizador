"""Anonimizador · beailabs — servidor local (FastAPI).

Sirve el frontend y expone la API. Hace de proxy a Ollama (sin CORS, sin red externa).
No persiste ni registra el contenido de los textos ni de los archivos.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from anonymize import OLLAMA_URL, AnonymizeError, anonymize
from extract import ExtractionError, extract_text

MODEL = os.environ.get("ANON_MODEL", "qwen3:4b")
MAX_BYTES = 20 * 1024 * 1024  # 20 MB por archivo

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Anonimizador · beailabs")


@app.middleware("http")
async def no_cache(request, call_next):
    """Evita que el navegador sirva versiones cacheadas del frontend tras actualizarlo."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


class AnonymizeRequest(BaseModel):
    text: str
    categories: list[str] | None = None


@app.get("/api/health")
def health() -> dict:
    """Indica si Ollama responde y si el modelo configurado está disponible."""
    info = {"model": MODEL, "ollama_up": False, "model_ready": False}
    try:
        with httpx.Client(timeout=4.0) as client:
            resp = client.get(f"{OLLAMA_URL}/api/tags")
        info["ollama_up"] = resp.status_code == 200
        if info["ollama_up"]:
            names = [m.get("name", "") for m in resp.json().get("models", [])]
            info["model_ready"] = any(n == MODEL or n.startswith(MODEL) for n in names)
    except httpx.HTTPError:
        pass
    return info


@app.post("/api/extract")
async def api_extract(file: UploadFile = File(...)) -> JSONResponse:
    data = await file.read()
    if len(data) > MAX_BYTES:
        return JSONResponse(status_code=413, content={"error": "El archivo supera los 20 MB."})
    try:
        text = extract_text(file.filename or "", data)
    except ExtractionError as exc:
        return JSONResponse(status_code=422, content={"error": str(exc)})
    finally:
        del data  # no conservar los bytes del archivo
    return JSONResponse({"text": text})


@app.post("/api/anonymize")
def api_anonymize(req: AnonymizeRequest) -> JSONResponse:
    try:
        result = anonymize(req.text, MODEL, req.categories)
    except AnonymizeError as exc:
        return JSONResponse(status_code=502, content={"error": str(exc)})
    return JSONResponse(result)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
