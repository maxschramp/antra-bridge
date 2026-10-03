"""
All contact with the Antra engine lives here. Antra is imported lazily (inside
functions) so this module can load, and /health can report engine state, before
the engine environment is ready.

Headless safety applied here (see NOTES.md gotcha G1): SLSKD_AUTO_BOOTSTRAP is
defaulted to false so the engine never reaches its interactive Soulseek setup.
Every public function calls ensure_antra() first, which sets the environment
*and then* imports, in that order.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from .config import BridgeConfig

logger = logging.getLogger("antra_bridge.engine")

_init_lock = threading.Lock()
_initialized_for: Optional[str] = None

RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate-limit",
    "ratelimited",
    "too many requests",
    "429",
    "quota",
    "throttl",
)


def is_rate_limit_error(text: Optional[str]) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in RATE_LIMIT_MARKERS)


def ensure_antra(cfg: BridgeConfig) -> None:
    """Make `import antra` work and apply headless-safe engine defaults."""
    global _initialized_for
    with _init_lock:
        repo = Path(cfg.antra_repo_dir)
        if not (repo / "antra" / "core" / "service.py").is_file():
            raise RuntimeError(
                f"Antra source not found under {repo} (expected {repo}/antra/core/service.py). "
                "Set ANTRAA_REPO_DIR to the Antra clone."
            )
        if _initialized_for == str(repo):
            return
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))
        # Engine defaults that keep the bridge headless. Must be set before the
        # first `import antra` because antra.core.config reads env at import
        # time (and again per load_config() call).
        os.environ.setdefault("SLSKD_AUTO_BOOTSTRAP", "false")
        os.environ.setdefault("ANTRA_MAX_WORKERS", "1")
        import antra  # noqa: F401  (import check only)

        _initialized_for = str(repo)
        logger.info("Antra engine ready (repo=%s)", repo)


def make_service(cfg: BridgeConfig) -> Any:
    ensure_antra(cfg)
    from antra.core.service import AntraService

    return AntraService()


def make_options(cfg: BridgeConfig, *, output_format: Optional[str] = None, source_preference: Optional[str] = None) -> Any:
    from antra.core.service import RuntimeOptions

    return RuntimeOptions(
        output_dir=str(cfg.staging_dir),
        output_format=output_format or cfg.output_format,
        source_preference=source_preference or cfg.source_preference,
        fetch_lyrics=cfg.fetch_lyrics,
        enrich_album_data=True,
    )


def make_controller(cfg: BridgeConfig) -> Any:
    ensure_antra(cfg)
    from antra.core.control import DownloadController

    return DownloadController()


def fetch_album_tracks(cfg: BridgeConfig, deezer_url: str) -> list:
    """Fetch album metadata from a Deezer link.

    Clears the per-track source intent the fetcher stamping applies for Deezer
    URLs ("exclusive" = the Deezer source family only). Lidarr-driven grabs
    want Antra's full multi-source cascade; the Deezer link is metadata
    identity, not a source lock (documented in NOTES.md).
    """
    svc = make_service(cfg)
    options = make_options(cfg)
    tracks = svc.fetch_playlist_tracks(deezer_url, options=options)
    for track in tracks:
        track.source_service = None
        track.source_rule = None
    return tracks


def probe_sources(cfg: BridgeConfig, tracks: list, limit: int) -> dict:
    """Availability probe: run each configured adapter's search() on up to
    `limit` tracks. Search-only; nothing is downloaded or streamed."""
    from antra.sources.base import RateLimitedError

    svc = make_service(cfg)
    runtime_cfg = svc.build_runtime_config(make_options(cfg))
    report: dict[str, Any] = {"sampled": [], "sources": {}}
    try:
        adapters = list(svc.build_adapters(runtime_cfg))
    except Exception as exc:  # noqa: BLE001
        report["probe_error"] = f"adapter build failed: {type(exc).__name__}: {exc}"[:300]
        return report

    for track in tracks[: max(0, limit)]:
        report["sampled"].append(
            {
                "title": getattr(track, "title", None),
                "artists": getattr(track, "artists", None) or [],
                "isrc": getattr(track, "isrc", None),
            }
        )
        for adapter in adapters:
            entry = report["sources"].setdefault(
                adapter.name,
                {"status": "miss", "detail": "not tried", "hits": 0, "tried": 0},
            )
            try:
                if not adapter.is_available():
                    entry["status"] = "unavailable"
                    entry["detail"] = "not configured"
                    continue
            except Exception:  # noqa: BLE001 - availability check is best-effort
                pass
            entry["tried"] += 1
            try:
                result = adapter.search(track)
            except RateLimitedError as exc:
                entry["status"] = "rate_limited"
                entry["detail"] = str(exc)[:200]
                continue
            except Exception as exc:  # noqa: BLE001
                if entry["status"] != "hit":
                    entry["status"] = "error"
                    entry["detail"] = f"{type(exc).__name__}: {exc}"[:200]
                continue
            if result is None:
                if entry["status"] == "miss":
                    entry["detail"] = "no match"
                continue
            entry["hits"] += 1
            entry["status"] = "hit"
            quality = []
            if getattr(result, "is_lossless", False):
                quality.append("lossless")
            if getattr(result, "bit_depth", None):
                quality.append(f"{result.bit_depth}bit")
            if getattr(result, "sample_rate_hz", None):
                quality.append(f"{result.sample_rate_hz}Hz")
            if not getattr(result, "is_lossless", False) and getattr(result, "quality_kbps", None):
                quality.append(f"{result.quality_kbps}kbps")
            sim = getattr(result, "similarity_score", 0.0)
            entry["detail"] = f"'{result.title}' via {result.source} ({'/'.join(quality) or 'unknown'}) sim={sim:.2f}"
    return report


def download_tracks(
    cfg: BridgeConfig,
    tracks: list,
    *,
    output_format: Optional[str] = None,
    source_preference: Optional[str] = None,
    event_callback: Optional[Callable] = None,
    controller: Any = None,
) -> list:
    svc = make_service(cfg)
    options = make_options(cfg, output_format=output_format, source_preference=source_preference)
    return svc.download_tracks(
        tracks,
        options=options,
        event_callback=event_callback,
        controller=controller,
    )


def result_to_dict(result: Any) -> dict:
    """Defensive DownloadResult -> plain dict (field names verified against
    antra/core/models.py DownloadResult)."""
    track = getattr(result, "track", None)
    fmt = getattr(result, "audio_format", None)
    status = getattr(result, "status", None)
    return {
        "title": getattr(track, "title", None),
        "artists": list(getattr(track, "artists", None) or []),
        "status": getattr(status, "value", None) or str(status),
        "file_path": getattr(result, "file_path", None),
        "source": getattr(result, "source_used", None),
        "format": getattr(fmt, "value", None) if fmt is not None else None,
        "error": getattr(result, "error_message", None),
        "attempts": getattr(result, "attempt_count", None),
    }
