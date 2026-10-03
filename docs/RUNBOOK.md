# Runbook: Antra for Lidarr

Everything here was exercised on the dev instance except the NAS sections,
which are the rollout plan (run with approval).

Ports used by the dev setup: bridge `8890`, dev Lidarr `8688`.

## 1. Bridge setup (verified)

```bash
# once: engine environment
git clone https://github.com/anandprtp/Antra antra      # consumed as a dependency, never modified
cd antra && uv venv --python 3.11 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements-runtime.txt
uv pip install --python .venv/Scripts/python.exe fastapi uvicorn python-dotenv

# bridge
cd ../antra-bridge
cp .env.example .env        # adjust BRIDGE_STAGING_DIR, ANTRAA_REPO_DIR, port
python -m antra_bridge      # serves http://127.0.0.1:8890
```

Headless notes:
- `SLSKD_AUTO_BOOTSTRAP=false` is applied automatically (first-run Soulseek
  prompt would otherwise crash headless runs). Set `SLSKD_BASE_URL` +
  `SLSKD_API_KEY` to use an existing slskd.
- Free tier runs one worker; keep `BRIDGE_CONCURRENCY=1`.
- Job state lives in `bridge.db` (SQLite) and survives restarts.

### Autostart

Linux (NAS or any host): systemd unit

```ini
[Unit]
Description=Antra Bridge
After=network-online.target

[Service]
User=antra
WorkingDirectory=/opt/antra-bridge
ExecStart=/opt/antra-lidarr/antra/.venv/bin/python -m antra_bridge
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Windows dev box: Task Scheduler entry running
`...\antra\.venv\Scripts\python.exe -m antra_bridge` from the bridge directory,
"At log on".

Container option (for the NAS rollout): a small image `FROM python:3.11-slim`
that clones Antra + bridge, installs both requirement sets, exposes 8890, and
mounts staging + music library. FFmpeg comes from imageio-ffmpeg, no system
packages needed.

## 2. Plugin install (verified)

Build locally: `bash scripts/build-local.sh` (patches the vendored Lidarr
`AssemblyVersion` to 3.0.0.4855 exactly like CI; a wildcard-version build
fails to load). Output: `_plugins/net8.0/Lidarr.Plugin.Antra/`.

Dev install (file copy):
1. copy `Lidarr.Plugin.Antra.dll`, `.pdb`, `.deps.json` to
   `<config>/plugins/maxschramp/Lidarr.Plugin.Antra/`
2. restart Lidarr
3. verify: System -> Plugins lists "Antra"

Release install (Settings -> System -> Plugins): paste
`https://github.com/maxschramp/lidarr-plugin-antra` and Install; Lidarr
fetches the `<PluginName>.net8.0.zip` release asset.

### Lidarr configuration

1. Settings -> Indexers -> Add -> Antra: Bridge URL, (optional token).
2. Settings -> Download Clients -> Add -> Antra: Bridge URL, Staging Path
   (as this Lidarr sees it), Host (`localhost` when same filesystem), format.
3. Settings -> Profiles -> Delay Profiles: enable the "Antra" protocol.
4. Remote path mapping only when Lidarr and the bridge see different paths to
   the staging dir (e.g. container vs host).

## 3. NAS rollout plan (run with approval)

Preconditions: explicit user sign-off; dev E2E green.

1. Backup first (docker appdata): `config.xml`, `lidarr.db` (+ wal/shm), and
   the compose file. Note the downgrade caveat: once Lidarr runs the plugins
   branch, rolling back requires restoring this DB backup (per the servarr
   wiki).
2. Choose the path:
   - A. switch the existing container to `lscr.io/linuxserver/lidarr:nightly`
     (one-way door without the DB backup), or
   - B. run a second nightly container on another port with its own appdata
     (trial without touching production; two instances must not manage the
     same library concurrently).
   Proposed default: B for a one-album trial, then A once satisfied.
3. Bridge on the NAS (docker, section 1) with:
   - staging on the array (e.g. `/mnt/user/antra-staging`),
   - the music library mounted read-write for nothing (the bridge only writes
     staging; Lidarr imports),
   - reachable from the Lidarr container (same docker network or host IP).
4. Plugin install on the nightly instance (section 2). Download client
   Staging Path = the container-side staging path; add a remote path mapping
   if the bridge and Lidarr see it differently.
5. Test one album end to end (search -> grab -> import). Keep qBittorrent
   untouched: Antra is additive (new indexer/download client + protocol).
6. Rollback: stop antivra bridge, remove plugin dir, restore the backed-up
   DB + revert the image tag (or delete the trial container).

## 4. Failure modes (dev-tested) and responses

- Bridge down: indexer test fails, downloads error with "bridge unreachable";
  Lidarr keeps working for all other indexers/clients. Check `/health`.
- Album cannot be matched/validated: search returns no Antra release; reason
  in `/resolve` (`reasons` list). Nothing is grabbed (no wrong editions).
- All sources miss some tracks: job ends `failed` with per-track errors; no
  partial import (staging keeps whatever completed; re-grab resumes).
- Rate limited: job status `rate_limited`, worker cools down
  (`BRIDGE_RATE_LIMIT_COOLDOWN_S`), `/health` shows `cooldown_until`.
- Cancel: Lidarr remove -> `DELETE /jobs/{id}` (cooperative; stops between
  tracks).
