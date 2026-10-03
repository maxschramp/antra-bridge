# Antra Bridge - Notes

Working notes for the Lidarr <-> Antra integration. Phase 0 findings first.

## Phase 0 findings

### Antra engine (upstream @ 07aeef1, 2026-09-26, v1.17+ tree)

- Repo layout: `antra/` (Python engine), `antra-wails/` (GUI), `antra_shared/`.
  The Python engine is real and standalone: `python -m antra <url> ...` works.
- Requirements install clean on Windows with Python 3.11 (`uv venv` + 
  `uv pip install -r requirements-runtime.txt`). ffmpeg comes from
  `imageio-ffmpeg` (bundled binary); pywidevine is pinned to 1.8.0 + construct
  2.8.8; playwright/websockets ship for the Amazon/Apple browser-login flows.
- CLI surface (`python -m antra --help`): `-o/--output`, `--source`
  (auto|apple|hifi|amazon|qobuz|deezer|soulseek|youtube|jiosaavn + priority
  groups), `--format` (source|flac|alac|m4a|aac|mp3|lossless-16|lossless-24|...),
  `--debug`, `--preview`, `--no-lyrics`, `--no-enrich`.
- Service layer for the bridge: `antra.core.service.AntraService` -
  `build_runtime_config(RuntimeOptions)` -> `Config` (dataclass),
  `build_engine(cfg, event_callback=...)` -> `DownloadEngine`,
  `download_playlist(url, options=...)`, `download_tracks(tracks, ...)`,
  `fetch_playlist_tracks(url, ...)`. `RuntimeOptions(output_dir,
  source_preference, output_format, fetch_lyrics, enrich_album_data)`.
- Config is env-var driven (`antra.core.config.load_config()`); every knob has
  an env name (`SLSKD_BASE_URL`, `OUTPUT_FORMAT`, `SOURCE_PREFERENCES`,
  `FETCH_LYRICS`, `SOURCES_ENABLED`, `ANTRA_MAX_WORKERS`, ...). The bridge can
  either pass env vars to a subprocess or construct `Config` directly.
- Resolver reports per-adapter search outcomes via
  `SourceResolver.last_resolve_report()` - useful for the /resolve endpoint's
  "which sources can serve this" detail.

**Gotcha G1 (headless)**: on first run the CLI calls `ensure_slskd()`, which
interactively prompts for Soulseek setup and crashes with `EOFError` when
stdin is not a TTY. Headless runs MUST set `SLSKD_AUTO_BOOTSTRAP=false` (or
provide `SLSKD_BASE_URL` + `SLSKD_API_KEY` of an existing slskd). This was the
only interactive blocker hit.

**Gotcha G2 (rate limits)**: free tier runs a single worker
(`ANTRA_MAX_WORKERS=1` unless a supporter key is set). The bridge must
serialize/limit downloads and surface 429/cooldown states instead of
retrying hot.

- Smoke test result: `python -m antra <spotify track url> --format flac` ->
  downloaded via the Apple community source (FLAC 24-bit), landed at
  `staging/<Artist>/<Album (Year)>/NN - Title.flac`, tags + synced lyrics
  written. Log: `../evidence/antra-smoke-run2.log`.

### Lidarr plugins (verified on v3.1.6.5078, "nightly" branch)

- The plugins build loads plugin assemblies and exposes `/api/v1/system/plugins`
  (list + install state). Settings UI: System -> Plugins; install = paste
  GitHub repo URL -> Install -> restart.
- Install flow observed (works): picks release asset named
  `<Plugin>.<target>.zip` - the TrevTV Deezer plugin resolved
  `Lidarr.Plugin.Deezer.net8.0.zip` from its GitHub releases; extracts to
  `config/plugins/<Owner>/<PluginName>/` containing main dll + pdb + deps.json.
- After restart the plugin shows in `/api/v1/system/plugins` (name, owner,
  githubUrl, installedVersion, availableVersion) and its schemas appear in
  `/api/v1/downloadclient/schema` (e.g. Deezer with fields downloadPath,
  saveSyncedLyrics, useLRCLIB). Plugin-provided indexers appear in
  `/api/v1/indexer/schema` likewise.
- Dev instance: Lidarr 3.1.6.5078 (develop-tier build served on the nightly
  update channel) at `http://127.0.0.1:8688`, data dir
  `dev/config/` (isolated from any other instance). Launch:
  `Lidarr.Console.exe '--data=C:\...\dev\config' --nobrowser` (path MUST be a
  Windows-style path - forward slashes make Lidarr reject config.xml as
  "not a valid Windows path").

### Matching strategy ({artist, album, year, tracks} -> Deezer link)

- Deezer public API (no auth) works; field-qualified queries
  (`artist:"x" album:"y"`) returned ZERO results for known albums on
  /search/album, so the matcher searches plain keywords and scores locally.
- Real hazard: editions (original 13-track RAM vs 10th Anniversary 22-track vs
  Drumless vs tribute albums). Validation = exact track count + per-track
  normalized title similarity + duration tolerance, all against the expected
  tracklist supplied by the caller. If nothing validates: clean failure with a
  candidate report (never guess).
- Track-level ISRC is not present in /album/{id}/tracks responses (only on
  /track/{id}); Phase 1 may fetch ISRCs for disambiguation later if needed.

## Decisions

- Dev Lidarr runs NATIVE on the dev machine (no Docker available on this host;
  WSL2 has systemd but no Docker). A Windows build from the nightly update
  channel was used; isolated data dir; port 8688.
- Bridge will consume Antra as an external dependency (clone + venv), never
  modifying upstream (Elastic License 2.0).

## Open questions (for Max)

- Confirm the two test albums (mainstream: Daft Punk - Random Access Memories;
  obscure: Susumu Yokota - Sakura) or pick others for the E2E.
- Public GitHub repos OK for lidarr-plugin-antra (needed so Lidarr can install
  from the repo URL)? 
- Soulseek/slskd credentials for the fallback tier: provide later via vault,
  or keep Soulseek disabled in the bridge?
