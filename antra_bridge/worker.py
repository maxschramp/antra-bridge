"""
Job worker: daemon thread(s) pulling job ids from an in-process queue and
driving the Antra engine. Job state lives in SQLite; progress is updated from
the engine's event callback so GET /jobs/{id} sees per-track movement.

Rate limiting: a job that fails with rate-limit-shaped errors moves to status
`rate_limited` and the worker cools down (BRIDGE_RATE_LIMIT_COOLDOWN_S) before
starting the next job. New jobs stay queued during the cooldown.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from typing import Any, Optional

from . import engine_adapter
from .config import BridgeConfig
from .resolve import resolve_request
from .store import Store

logger = logging.getLogger("antra_bridge.worker")

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_RATE_LIMITED = "rate_limited"
TERMINAL = {STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED, STATUS_RATE_LIMITED}

_DONE_MARKERS = {"completed", "skipped", "failed"}
_EVENT_TAIL = 100


class BridgeWorker:
    def __init__(self, cfg: BridgeConfig, store: Store, gate: Optional[threading.Semaphore] = None):
        self.cfg = cfg
        self.store = store
        self.gate = gate or threading.Semaphore(1)
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._controllers: dict[str, Any] = {}
        self._cancel_requested: set[str] = set()
        self._current: Optional[str] = None
        self._lock = threading.Lock()
        self.cooldown_until: float = 0.0

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        for index in range(self.cfg.concurrency):
            thread = threading.Thread(target=self._run, name=f"bridge-worker-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)
        logger.info("worker started (%d thread(s))", self.cfg.concurrency)

    def enqueue(self, job_id: str) -> None:
        self._queue.put(job_id)

    def cancel(self, job_id: str) -> str:
        """Returns one of: not_found, already_done, cancelled."""
        job = self.store.get_job(job_id)
        if job is None:
            return "not_found"
        if job["status"] in TERMINAL:
            return "already_done"
        self._cancel_requested.add(job_id)
        with self._lock:
            controller = self._controllers.get(job_id)
        if controller is not None:
            controller.cancel()
            self.store.update_job(job_id, status=STATUS_CANCELLED, error="cancelled by request")
        else:
            self.store.update_job(job_id, status=STATUS_CANCELLED, error="cancelled before start")
        return "cancelled"

    def status(self) -> dict:
        with self._lock:
            current = self._current
        cooldown = self.cooldown_until if self.cooldown_until > time.time() else None
        return {"queue_size": self._queue.qsize(), "current_job": current, "cooldown_until": cooldown}

    # -- internals ----------------------------------------------------------

    def _run(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self._handle(job_id)
            except Exception:  # noqa: BLE001
                logger.exception("worker failed handling job %s", job_id)
                try:
                    self.store.update_job(job_id, status=STATUS_FAILED, error="internal worker error")
                except Exception:  # noqa: BLE001
                    pass
            finally:
                with self._lock:
                    self._current = None
                self._queue.task_done()

    def _handle(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if job is None or job["status"] in TERMINAL or job_id in self._cancel_requested:
            return
        while True:
            wait = self.cooldown_until - time.time()
            if wait <= 0 or job_id in self._cancel_requested:
                break
            time.sleep(min(wait, 5))
        if job_id in self._cancel_requested:
            self.store.update_job(job_id, status=STATUS_CANCELLED, error="cancelled before start")
            return
        self._process(job_id)

    def _process(self, job_id: str) -> None:
        with self._lock:
            self._current = job_id
        job = self.store.get_job(job_id)
        req = job["request"] or {}
        tracks_in = req.get("tracks") or []
        self.store.update_job(
            job_id,
            status=STATUS_RUNNING,
            progress={"phase": "resolving", "tracks_total": len(tracks_in) or None, "tracks_done": 0},
        )
        self.gate.acquire()
        try:
            resolved = resolve_request(self.cfg, self.store, req, allow_probe=False)
            if not resolved.get("ok"):
                reasons = "; ".join(str(r) for r in (resolved.get("reasons") or ["no match"]))
                self.store.update_job(job_id, status=STATUS_FAILED, error=f"resolve failed: {reasons}"[:500])
                return
            deezer_url = resolved["deezer_url"]

            self.store.update_job(job_id, progress={"phase": "fetching_tracks", "deezer_url": deezer_url})
            tracks = engine_adapter.fetch_album_tracks(self.cfg, deezer_url)
            if not tracks:
                self.store.update_job(job_id, status=STATUS_FAILED, error="engine returned no tracks for the resolved album")
                return

            controller = engine_adapter.make_controller(self.cfg)
            with self._lock:
                self._controllers[job_id] = controller
            if job_id in self._cancel_requested:
                controller.cancel()

            events: list[dict] = []
            per_track = [
                {
                    "index": i + 1,
                    "title": getattr(t, "title", "") or "",
                    "artists": list(getattr(t, "artists", None) or []),
                    "status": "pending",
                }
                for i, t in enumerate(tracks)
            ]
            progress: dict = {
                "phase": "downloading",
                "deezer_url": deezer_url,
                "tracks_total": len(tracks),
                "tracks_done": 0,
                "current": None,
                "per_track": per_track,
                "events": events,
            }
            self.store.update_job(job_id, progress=progress)

            def on_event(event: Any) -> None:
                try:
                    self._apply_event(progress, per_track, events, event)
                except Exception:  # noqa: BLE001
                    logger.debug("failed to apply engine event", exc_info=True)
                self.store.update_job(job_id, progress=progress)

            results = engine_adapter.download_tracks(
                self.cfg,
                tracks,
                output_format=req.get("format"),
                source_preference=req.get("sources"),
                event_callback=on_event,
                controller=controller,
            )

            result_payload = self._result_payload(results)
            progress["phase"] = "done"

            if controller.is_cancelled() or job_id in self._cancel_requested:
                self.store.update_job(
                    job_id, status=STATUS_CANCELLED, progress=progress, result=result_payload,
                    error="cancelled by request",
                )
                return

            if not results:
                self.store.update_job(
                    job_id, status=STATUS_FAILED, progress=progress, result=result_payload,
                    error="engine returned no results",
                )
                return

            dicts = [engine_adapter.result_to_dict(r) for r in results]
            failed = [d for d in dicts if d["status"] == "failed"]
            completed = [d for d in dicts if d["status"] == "completed"]
            if not failed:
                self.store.update_job(job_id, status=STATUS_COMPLETED, progress=progress, result=result_payload)
                return

            joined = " | ".join((d.get("error") or "") for d in failed)
            if engine_adapter.is_rate_limit_error(joined):
                self.cooldown_until = time.time() + self.cfg.rate_limit_cooldown_s
                self.store.update_job(
                    job_id,
                    status=STATUS_RATE_LIMITED,
                    progress=progress,
                    result=result_payload,
                    error=(
                        f"rate limited by one or more sources; cooling down "
                        f"{self.cfg.rate_limit_cooldown_s}s. First error: {failed[0].get('error') or ''}"
                    )[:500],
                )
                return
            self.store.update_job(
                job_id,
                status=STATUS_FAILED,
                progress=progress,
                result=result_payload,
                error=f"{len(failed)} of {len(dicts)} tracks could not be sourced (completed {len(completed)})"[:500],
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("job %s crashed", job_id)
            self.store.update_job(job_id, status=STATUS_FAILED, error=f"{type(exc).__name__}: {exc}"[:500])
        finally:
            with self._lock:
                self._controllers.pop(job_id, None)
            self._cancel_requested.discard(job_id)
            self.gate.release()

    def _result_payload(self, results: list) -> dict:
        files = []
        for result in results:
            entry = engine_adapter.result_to_dict(result)
            path = entry.get("file_path")
            size = None
            if path and os.path.isfile(path):
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = None
            entry["size_bytes"] = size
            files.append(entry)
        return {
            "output_dir": str(self.cfg.staging_dir),
            "files": files,
            "completed": sum(1 for f in files if f["status"] == "completed"),
            "skipped": sum(1 for f in files if f["status"] == "skipped"),
            "failed": sum(1 for f in files if f["status"] == "failed"),
        }

    @staticmethod
    def _apply_event(progress: dict, per_track: list, events: list, event: Any) -> None:
        type_name = getattr(getattr(event, "type", None), "value", None) or str(getattr(event, "type", ""))
        track = getattr(event, "track", None)
        index = getattr(event, "track_index", None)
        entry = None
        if isinstance(index, int) and 1 <= index <= len(per_track):
            entry = per_track[index - 1]
        elif track is not None:
            title = getattr(track, "title", None)
            entry = next((e for e in per_track if e.get("title") == title and e["status"] == "pending"), None)
        if entry is not None:
            if type_name == "track_started":
                entry["status"] = "downloading"
                progress["current"] = {"index": entry["index"], "title": entry["title"]}
            elif type_name == "track_resolved":
                entry["status"] = "resolved"
                entry["source"] = getattr(event, "source", None) or entry.get("source")
                entry["quality"] = getattr(event, "quality_label", None) or entry.get("quality")
            elif type_name == "track_download_attempt":
                entry["attempts"] = getattr(event, "attempt", None)
            elif type_name == "track_completed":
                entry["status"] = "completed"
                entry["file_path"] = getattr(event, "file_path", None) or entry.get("file_path")
                entry["source"] = getattr(event, "source", None) or entry.get("source")
                entry["quality"] = getattr(event, "quality_label", None) or entry.get("quality")
            elif type_name == "track_skipped":
                entry["status"] = "skipped"
                entry["file_path"] = getattr(event, "file_path", None) or entry.get("file_path")
            elif type_name == "track_failed":
                entry["status"] = "failed"
                entry["error"] = getattr(event, "error", None)
        if type_name == "playlist_completed":
            progress["phase"] = "finishing"
        progress["tracks_done"] = sum(1 for e in per_track if e["status"] in _DONE_MARKERS)
        events.append(
            {
                "ts": round(time.time(), 1),
                "type": type_name,
                "index": index,
                "title": getattr(track, "title", None) if track is not None else None,
                "message": getattr(event, "message", None),
                "source": getattr(event, "source", None),
                "quality": getattr(event, "quality_label", None),
                "error": getattr(event, "error", None),
                "file": getattr(event, "file_path", None),
            }
        )
        if len(events) > _EVENT_TAIL:
            del events[: len(events) - _EVENT_TAIL]
