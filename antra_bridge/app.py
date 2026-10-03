"""
FastAPI application: the Antra bridge HTTP surface.

Endpoints (see README):
  GET    /health        engine + staging + queue state
  POST   /resolve       {artist, album, year?, tracks?} -> validated Deezer link
  POST   /download      start a job (staging dir, default format FLAC)
  GET    /jobs          recent jobs
  GET    /jobs/{id}     job status, per-track progress, result paths
  DELETE /jobs/{id}     cancel

All heavy engine calls run in FastAPI's worker thread pool (plain `def`
endpoints) and are serialized through a semaphore so free-tier rate limits are
respected. Optional BRIDGE_API_TOKEN gates every endpoint except /health.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import __version__, engine_adapter
from .config import BridgeConfig, REPO_ROOT
from .resolve import resolve_request
from .store import Store
from .worker import BridgeWorker

cfg = BridgeConfig.from_env()
cfg.ensure_dirs()


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(cfg.log_level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = _JsonFormatter()
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)
    try:
        file_handler = logging.FileHandler(REPO_ROOT / "logs" / "bridge.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        pass


_setup_logging()
logger = logging.getLogger("antra_bridge.app")

store = Store(cfg.db_path)
gate = threading.Semaphore(max(1, cfg.concurrency))
worker = BridgeWorker(cfg, store, gate)

app = FastAPI(title="Antra Bridge", version=__version__)


@app.on_event("startup")
def _startup() -> None:
    worker.start()
    try:
        engine_adapter.ensure_antra(cfg)
    except Exception as exc:  # noqa: BLE001
        logger.error("engine not ready at startup: %s", exc)
    logger.info("antra bridge up (staging=%s, format=%s)", cfg.staging_dir, cfg.output_format)


@app.middleware("http")
async def _auth(request: Request, call_next):
    if cfg.api_token and request.url.path != "/health":
        token = request.headers.get("x-bridge-token", "")
        if not token:
            auth = request.headers.get("authorization", "")
            token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if token != cfg.api_token:
            return JSONResponse({"detail": "invalid or missing bridge token"}, status_code=401)
    return await call_next(request)


class TrackIn(BaseModel):
    title: str
    duration_s: Optional[float] = None


class ResolveRequest(BaseModel):
    artist: str
    album: str
    year: Optional[int] = None
    tracks: Optional[list[TrackIn]] = None
    alternates: Optional[list[list[TrackIn]]] = None
    refresh: bool = False


class DownloadRequest(BaseModel):
    artist: str
    album: str
    year: Optional[int] = None
    tracks: Optional[list[TrackIn]] = None
    alternates: Optional[list[list[TrackIn]]] = None
    deezer_url: Optional[str] = None
    format: Optional[str] = None
    sources: Optional[str] = None
    origin: Optional[str] = None


@app.get("/health")
def health() -> dict:
    engine: dict = {"repo_dir": str(cfg.antra_repo_dir), "staging_dir": str(cfg.staging_dir)}
    try:
        engine_adapter.ensure_antra(cfg)
        engine["import_ok"] = True
    except Exception as exc:  # noqa: BLE001
        engine["import_ok"] = False
        engine["error"] = str(exc)[:300]
    engine["staging_writable"] = os.access(cfg.staging_dir, os.W_OK)
    return {
        "status": "ok" if engine.get("import_ok") else "degraded",
        "version": __version__,
        "engine": engine,
        "format": cfg.output_format,
        "sources": cfg.source_preference,
        "queue": worker.status(),
        "jobs": store.count_jobs(),
        "now": time.time(),
    }


@app.post("/resolve")
def resolve(req: ResolveRequest) -> dict:
    return resolve_request(cfg, store, req.model_dump(), allow_probe=True)


@app.post("/download")
def download(req: DownloadRequest) -> dict:
    job_id = uuid.uuid4().hex
    store.create_job(job_id, req.model_dump())
    worker.enqueue(job_id)
    return {"job_id": job_id, "status": "queued", "deezer_url": req.deezer_url}


@app.get("/jobs")
def jobs(status: Optional[str] = None, limit: int = 50, origin: Optional[str] = None) -> dict:
    effective_limit = min(200, max(1, limit))
    if origin:
        # Filter on the request's origin tag (e.g. "lidarr") so the download
        # client never surfaces jobs triggered by other bridge users.
        rows = store.list_jobs(status=status, limit=500)
        rows = [row for row in rows if (row.get("request") or {}).get("origin") == origin]
        return {"jobs": rows[:effective_limit]}
    return {"jobs": store.list_jobs(status=status, limit=effective_limit)}


@app.get("/jobs/{job_id}")
def job_detail(job_id: str) -> dict:
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.delete("/jobs/{job_id}")
def cancel_job(job_id: str) -> dict:
    outcome = worker.cancel(job_id)
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="job not found")
    if outcome == "already_done":
        raise HTTPException(status_code=409, detail="job already finished")
    return {"job_id": job_id, "status": "cancelled"}
