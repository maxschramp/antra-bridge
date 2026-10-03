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

### Lidarr plugin interface surfaces (verified against reference repos)

Plugin marker (`Plugin.cs`, namespace `NzbDrone.Core.Plugins`):
- `class AntraPlugin : Plugin { Name; Owner; GithubUrl }` - trivial.

Indexer (`NzbDrone.Core.Indexers`, base `HttpIndexerBase<TIndexerSettings>`):
- ctor: (httpClient, indexerStatusService, configService, parsingService, logger)
- overrides: `Name`; `Protocol => nameof(AntraDownloadProtocol)`; `SupportsRss=false`;
  `SupportsSearch=true`; `PageSize`; `RateLimit`.
- `GetRequestGenerator()` -> `IIndexerRequestGenerator` with `GetRecentRequests()`,
  `GetSearchRequests(AlbumSearchCriteria)`, `GetSearchRequests(ArtistSearchCriteria)`.
  `AlbumSearchCriteria` exposes ArtistQuery + AlbumQuery + the Album (with tracks):
  the bridge call source.
- `GetParser()` -> `IParseIndexerResponse.ParseResponse(IndexerResponse)` building
  `ReleaseInfo` items: Guid, Artist, Album, DownloadUrl, InfoUrl, PublishDate,
  DownloadProtocol, Codec="FLAC", Container="Lossless", Size, Title
  (TrevTV style: "Artist - Album (Year) [FLAC] [WEB]").
- Settings class : `IIndexerSettings` (FluentValidation validator; `[FieldDefinition]`
  fields; `BaseUrl` property required by the interface).

Download client (`NzbDrone.Core.Download.Clients`, base `DownloadClientBase<TSettings>`):
- ctor: (configService, diskProvider, remotePathMappingService, localizationService, logger)
- overrides: `Protocol`, `Name`; `GetItems()` -> `IEnumerable<DownloadClientItem>`
  (DownloadId, Title, Status Queued/Downloading/Completed/Failed,
  TotalSize/RemainingSize/RemainingTime, OutputPath, CanMoveFiles/CanBeRemoved);
  `RemoveItem(item, deleteData)`; `Download(RemoteAlbum, IIndexer)` -> Task<string>
  returns the download id; throw `ReleaseUnavailableException` /
  `DownloadClientRejectedReleaseException` for clean user-visible failures;
  `GetStatus()` -> `DownloadClientInfo { IsLocalhost, OutputRootFolders }`;
  `Test(failures)`.
- Remote path mapping: `_remotePathMappingService.RemapRemoteToLocal(Settings.Host, path)`
  (Slskd pattern). Our settings carry the bridge host as the mapping key; staging
  path is the OutputRootFolder Lidarr imports from.
- Settings : `IProviderConfig` (`[FieldDefinition]`, `Privacy = PrivacyLevel.ApiKey`
  for secrets; `IsValidPath()` / `ValidHost()` / `ValidRootUrl()` / `NotEmpty()`).

Protocol: `class AntraDownloadProtocol : IDownloadProtocol {}` in NzbDrone.Core.Indexers;
both classes reference `nameof(AntraDownloadProtocol)`; users enable it in Delay
Profiles (the new protocol shows up as a tab after install).

### Packaging / release conventions (copy the TrevTV model)

- net8.0 class lib; ProjectReference to a Lidarr source submodule
  (`ext/Lidarr` -> github.com/Lidarr/Lidarr, branch **plugins**) with
  Private=false + ExcludeAssets=runtime (mirrors TrevTV build.yml + csproj).
- Build output lands in `_plugins/net8.0/<PluginName>/`; GitHub Actions (ubuntu) zips
  `_plugins/net8.0/<PluginName>/<PluginName>.*` as `<PluginName>.net8.0.zip` and
  attaches it to a GitHub release. Version scheme <major>.<minor>.<patch>.<run_number>.
- ILRepack merges 3rd-party deps into the single dll (we can likely avoid extra deps).
- Install verified end-to-end: extract to config/plugins/<Owner>/<Name>/ (dll + pdb +
  deps.json) -> restart -> `/api/v1/system/plugins` lists it -> the plugin's indexer
  AND download client appear in `/api/v1/indexer/schema` + `/api/v1/downloadclient/schema`
  (confirmed: Deezer appears in both).

### Dev environment notes

- dotnet SDK 10.0.101 is on the dev machine; net8.0 targets build with it (CI pins
  8.0.405 + global.json; local dev stays on 10).
- Dev Lidarr: 3.1.6.5078 native on the dev machine, port 8688, data in `dev/config/`,
  API key `<dev-api-key>` (dev only), branch nightly. Launched via
  `Lidarr.Console.exe '--data=C:\...\dev\config' --nobrowser`. Reference plugin
  (TrevTV Deezer) installed and loaded; /system/plugins + schemas verified.

## Phase 1: bridge service (antra_bridge package)

Built and proven. FastAPI + SQLite; runs the Antra engine in-process.
Endpoints: /health, /resolve, /download, /jobs, /jobs/{id}, DELETE /jobs/{id}.

Design decisions:
- Engine runs in-process (AntraService from the clone), driven by RuntimeOptions
  (staging dir, format, source preference). No subprocess/JSON-CLI parsing.
- Deezer URLs carry an "exclusive" source intent inside Antra (Deezer family
  only). The bridge clears source_service/source_rule on the fetched tracks so
  every grab runs Antra's full multi-source cascade; the Deezer link is
  metadata identity, not a source lock.
- /resolve caches in SQLite keyed on the exact request identity; failures are
  cached too, so Lidarr search retries do not hammer the APIs. Refresh bypasses.
  Availability probe (search-only) samples N tracks per adapter and reports
  hit/miss/rate_limited/unavailable per source.
- One worker thread by default (BRIDGE_CONCURRENCY=1) plus a shared semaphore
  serializing engine operations, to respect free-tier rate limits. Rate-limit
  shaped failures set job status `rate_limited` and cool the worker down
  (BRIDGE_RATE_LIMIT_COOLDOWN_S); /health surfaces cooldown_until.
- Cancel is cooperative (Antra's DownloadController): between tracks, not
  mid-track. Job statuses: queued/running/completed/failed/cancelled/rate_limited.
- Optional BRIDGE_API_TOKEN gates every endpoint except /health (empty in dev).

## Phase 2: plugin + E2E (dev instance)

Built: `lidarr-plugin-antra` (net8.0; HttpIndexerBase + DownloadClientBase +
AntraDownloadProtocol; schemas verified in dev Lidarr 3.1.6.5078). E2E wiring
on the dev instance (port 8688) with both test albums.

Interface/contract updates found while wiring the E2E:
- Lidarr searches with the MONITORED release's tracklist and stores every
  edition as a separate release row. RAM's monitored release carries 30
  tracks across 3 media; the first failing search sent a 22-track list from
  another edition while the validated Deezer original is 13 tracks. Fix: the
  plugin sends up to 6 de-duplicated release tracklists ("tracks" +
  "alternates"); /resolve accepts a candidate whose count matches ANY set and
  validates against that set. Verified offline against live Deezer
  (evidence/test_multiset.py: 30-track primary + 13-track alternate ->
  original 13-track edition, 13/13).
- Edition regex updated: the keyword may appear anywhere inside brackets
  ("(10th Anniversary Edition)" now normalizes to the base title; previously
  it was dropped by title similarity, which made 22-track resolution fail).
- Jobs carry an "origin" tag; the download client reads /jobs?origin=lidarr,
  so jobs triggered outside Lidarr never appear in Lidarr's queue.

Build gotchas (hit, fixed):
- The vendored ext/Lidarr builds with AssemblyVersion `10.0.0.*`; a plugin
  compiled against that fails to load in Lidarr nightly (FileLoadException:
  Lidarr.Core "not an absolute path"). CI patches it to 3.0.0.4855;
  scripts/build-local.sh applies the same patch before building.
- ext/Lidarr expects `stylecop.json` at the solution root: `src/stylecop.json`
  must exist in the plugin repo.
- Referenced projects build via the sln only with Build.0 config entries for
  them; SDK 10 generates .slnx by default (classic .sln kept for CI).
- Lidarr API path validation on Windows wants backslash paths ("C:/..." is
  rejected as Invalid Path for root folders / staging paths).

E2E results (2026-10-03):
- Daft Punk - Random Access Memories: search -> synthetic Antra release ->
  auto-grab -> bridge job `645a698c` completed 13/13 -> Lidarr imported all
  13 tracks into the library. Imported file: 24-bit/44.1k FLAC with correct
  artist/album/title/date/genre tags. (That job exercised the resume/skip
  path - files were already staged by the bridge proof run; the fresh
  download path is evidenced by the bridge full proof and smoke test.)
- Susumu Yokota - Sakura: search -> grab -> bridge job `27e06d48` running,
  resuming past 3 staged tracks and downloading the rest fresh through the
  Lidarr-triggered path. [completion + import recorded in git history]

## Decisions

- Soulseek disabled (explicit user decision): SLSKD_AUTO_BOOTSTRAP=false, no
  credentials; the community-mirror tier covers the sources.
- Public GitHub repos under maxschramp approved for antra-bridge and
  lidarr-plugin-antra.
- Dev Lidarr runs NATIVE on the dev machine (no Docker available on this host;
  WSL2 has systemd but no Docker). A Windows build from the nightly update
  channel was used; isolated data dir; port 8688.
- Bridge will consume Antra as an external dependency (clone + venv), never
  modifying upstream (Elastic License 2.0).

## Open questions

Resolved (2026-10-03): test albums confirmed (Daft Punk - Random Access
Memories; Susumu Yokota - Sakura); public GitHub repos approved; Soulseek
disabled; Phase 1 green-lit.
Open: production hosting for the bridge (runbook phase) - the NAS Docker with
staging on the share proposed vs the dev machine service.
