"""
Bridge configuration, loaded from environment variables (.env supported).

The bridge runs the Antra engine in-process; engine-specific switches are also
plain environment variables (see .env.example). Engine-unsafe defaults for
headless operation are applied in engine_adapter.ensure_antra() so that every
entry point is safe even when a .env omits them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv

    # Deterministic: <bridge repo>/.env, then fall back to discovery.
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    load_dotenv(override=False)
except ImportError:
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _path(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    return Path(raw).expanduser() if raw else default


@dataclass
class BridgeConfig:
    host: str
    port: int
    staging_dir: Path
    antra_repo_dir: Path
    db_path: Path
    output_format: str
    source_preference: str
    fetch_lyrics: bool
    concurrency: int
    probe_enabled: bool
    probe_tracks: int
    cache_ttl_s: int
    rate_limit_cooldown_s: int
    api_token: str
    log_level: str

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        return cls(
            host=os.getenv("BRIDGE_HOST", "127.0.0.1"),
            port=_int("BRIDGE_PORT", 8890),
            staging_dir=_path("BRIDGE_STAGING_DIR", REPO_ROOT.parent / "staging"),
            antra_repo_dir=_path("ANTRAA_REPO_DIR", REPO_ROOT.parent / "antra"),
            db_path=_path("BRIDGE_DB_PATH", REPO_ROOT / "bridge.db"),
            output_format=os.getenv("BRIDGE_FORMAT", "flac").strip().lower(),
            source_preference=os.getenv("BRIDGE_SOURCES", "auto").strip().lower(),
            fetch_lyrics=_bool("BRIDGE_FETCH_LYRICS", True),
            concurrency=max(1, _int("BRIDGE_CONCURRENCY", 1)),
            probe_enabled=_bool("BRIDGE_PROBE_ENABLED", True),
            probe_tracks=max(0, _int("BRIDGE_PROBE_TRACKS", 1)),
            cache_ttl_s=max(0, _int("BRIDGE_CACHE_TTL_S", 21600)),
            rate_limit_cooldown_s=max(0, _int("BRIDGE_RATE_LIMIT_COOLDOWN_S", 300)),
            api_token=os.getenv("BRIDGE_API_TOKEN", "").strip(),
            log_level=os.getenv("BRIDGE_LOG_LEVEL", "INFO").upper(),
        )

    def ensure_dirs(self) -> None:
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        (REPO_ROOT / "logs").mkdir(exist_ok=True)
