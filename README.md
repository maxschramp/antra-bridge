# antra-bridge

Headless HTTP service that lets Lidarr drive Antra's multi-source download
pipeline. Lidarr thinks in MusicBrainz identities; Antra consumes service
links. The bridge maps one to the other, validated, and runs the engine with
per-track job progress.

Status: Phase 1 complete (service + proofs). Phase 2 (Lidarr plugin) next.

## Endpoints

- `GET /health` - engine import state, staging dir, queue, cooldowns
- `POST /resolve` - `{artist, album, year?, tracks?, refresh?}` -> validated
  Deezer link + candidate report + optional source availability probe. Cached;
  never downloads.
- `POST /download` - starts a job (`{artist, album, year?, tracks?, deezer_url?,
  format?, sources?}`), returns `job_id`. Downloads to the staging dir
  (default format FLAC).
- `GET /jobs` - recent jobs (filter `?status=`, `?limit=`)
- `GET /jobs/{id}` - status, per-track progress, engine event tail, result paths
- `DELETE /jobs/{id}` - cancel (cooperative; between tracks)

## Run (dev)

The bridge runs the Antra engine in-process, so it needs the Antra clone's
`requirements-runtime.txt` in the same environment. Dev setup used here:

```bash
# once: antra venv (see ../antra) then
uv pip install --python ../antra/.venv/Scripts/python.exe fastapi uvicorn python-dotenv

cd antra-bridge
python -m antra_bridge          # serves http://127.0.0.1:8890
```

Configuration is via env vars (`.env.example` documents all of them):
staging dir, format/quality, source preferences, concurrency, probe depth,
cache TTL, rate-limit cooldown, optional API token, and the Antra repo path.
See `NOTES.md` for design decisions and known failure modes.

## Proofs

- `scripts/prove_matching.py` - Phase 0: MusicBrainz tracklist -> Deezer
  edition matching with per-track validation (2/2 test albums).
- `scripts/prove_bridge.py` - Phase 1: health, resolves, availability probe,
  bogus-album clean failure, cancel semantics, full album downloads with file
  verification. Run with the bridge up.

## Layout

- `antra_bridge/matching.py` - Deezer matcher (search, scoring, validation)
- `antra_bridge/engine_adapter.py` - all contact with the Antra engine
- `antra_bridge/resolve.py` - resolve pipeline + SQLite cache
- `antra_bridge/worker.py` - job queue, progress, cancellation, cool-downs
- `antra_bridge/app.py` - FastAPI surface
- `antra_bridge/store.py` - SQLite jobs + cache
- `antra_bridge/mb.py` - MusicBrainz helper (ground truth tracklists)
