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

## Decisions

- Dev Lidarr runs NATIVE on the dev machine (no Docker available on this host;
  WSL2 has systemd but no Docker). A Windows build from the nightly update
  channel was used; isolated data dir; port 8688.
- Bridge will consume Antra as an external dependency (clone + venv), never
  modifying upstream (Elastic License 2.0).

## Open questions (for Max)

- Confirm the two test albums (mainstream: Daft Punk - Random Access Memories 2013;
  obscure: Susumu Yokota - Sakura 1999) or pick others for the E2E. Both already
  match + validate cleanly in the Phase 0 proof.
- Public GitHub repos OK for lidarr-plugin-antra + antra-bridge (needed so Lidarr
  can install the plugin from the repo URL and fetch release assets)?
- Soulseek/slskd credentials for the fallback tier: provide later via vault, or
  keep Soulseek disabled in the bridge initially?
- Production hosting for the bridge (part of the the NAS runbook): propose the NAS
  Docker (staging on the share) vs the dev machine service. Decide at runbook time.
