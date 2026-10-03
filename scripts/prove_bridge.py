"""
Phase 1 proof: drive the running bridge end to end.

Run with the bridge up (python -m antra_bridge):
    python scripts/prove_bridge.py [--base http://127.0.0.1:8890] [--skip-downloads]

Steps:
  1. /health
  2. /resolve for both test albums (MusicBrainz tracklists as ground truth)
     + one bogus album (must fail cleanly, ok=false, no crash)
  3. cancel test: start a download, cancel after ~12s, expect status=cancelled
  4. full download of both test albums; verify FLAC files on disk + tags
Exits 0 only if every check passes.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, ".")
from antra_bridge.mb import mb_release_tracklist  # noqa: E402

ALBUMS = [
    ("Daft Punk", "Random Access Memories", 2013),
    ("Susumu Yokota", "Sakura", 1999),
]
BOGUS = ("Zzzq Nonexistent", "Totally Unreal Album Volume Nine", 2049)

TERMINAL = {"completed", "failed", "cancelled", "rate_limited"}
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" -- {detail}" if detail else ""))
    if not ok:
        failures.append(f"{name}: {detail}")


def request_json(method: str, url: str, **kwargs) -> dict:
    resp = requests.request(method, url, timeout=120, **kwargs)
    resp.raise_for_status()
    return resp.json()


def poll_job(base: str, job_id: str, timeout_s: int = 2400, label: str = "") -> dict:
    last = None
    started = time.time()
    while time.time() - started < timeout_s:
        job = request_json("GET", f"{base}/jobs/{job_id}")
        status = job["status"]
        if status != last:
            progress = job.get("progress") or {}
            done = progress.get("tracks_done", "-")
            total = progress.get("tracks_total", "-")
            current = (progress.get("current") or {}).get("title", "")
            print(f"[{label or job_id[:8]}] status={status} phase={progress.get('phase')} {done}/{total} {current}")
            last = status
        if status in TERMINAL:
            return job
        time.sleep(6)
    raise TimeoutError(f"job {job_id} did not finish in {timeout_s}s")


def verify_files(job: dict, label: str) -> None:
    files = (job.get("result") or {}).get("files") or []
    check(f"{label}: result lists tracks", len(files) > 0, f"{len(files)} entries")
    ok_files = []
    for entry in files:
        if entry.get("status") not in ("completed", "skipped"):
            continue
        path = Path(entry["file_path"] or "")
        if not path.is_file():
            check(f"{label}: file exists {path.name}", False, str(path))
            continue
        size = path.stat().st_size
        check(f"{label}: {path.name}", size > 1024 * 1024, f"{size / 1048576:.1f} MB, via {entry.get('source')}")
        ok_files.append(path)
    if not ok_files:
        return
    try:
        from mutagen.flac import FLAC

        flac = FLAC(str(ok_files[0]))
        title = (flac.tags.get("title") or [""])[0] if flac.tags else ""
        check(
            f"{label}: FLAC tags on {ok_files[0].name}",
            bool(title),
            f"'{title}' {flac.info.bits_per_sample}bit {flac.info.sample_rate}Hz",
        )
    except Exception as exc:  # noqa: BLE001
        check(f"{label}: mutagen verification", False, str(exc))


def resolve_album(base: str, artist: str, album: str, year: int) -> dict:
    mb = mb_release_tracklist(artist, album, year)
    body = {"artist": artist, "album": album, "year": year, "tracks": mb["tracks"]}
    result = request_json("POST", f"{base}/resolve", json=body)
    detail = result.get("deezer_url") or ("; ".join(result.get("reasons") or [])[:160])
    print(f"[resolve] {artist} - {album}: ok={result.get('ok')} cached={result.get('cached')} {detail}")
    availability = result.get("availability") or {}
    if availability.get("sources"):
        hits = sorted(k for k, v in availability["sources"].items() if v.get("status") == "hit")
        ranked = ", ".join(f"{k}({availability['sources'][k].get('detail', '')[:60]})" for k in hits)
        print(f"[resolve]   availability: {len(hits)} source(s) hit -> {ranked}")
    elif availability.get("probe_error"):
        print(f"[resolve]   probe error: {str(availability['probe_error'])[:140]}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8890")
    parser.add_argument("--skip-downloads", action="store_true")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    health = request_json("GET", f"{base}/health")
    engine = health.get("engine") or {}
    check("health: engine importable", bool(engine.get("import_ok")), str(engine)[:200])
    check("health: staging writable", bool(engine.get("staging_writable")))

    for artist, album, year in ALBUMS:
        result = resolve_album(base, artist, album, year)
        check(f"resolve: {artist} - {album}", bool(result.get("ok")), "; ".join(result.get("reasons") or [])[:200])

    bogus = request_json("POST", f"{base}/resolve", json={"artist": BOGUS[0], "album": BOGUS[1], "year": BOGUS[2]})
    check("resolve: bogus album fails cleanly", bogus.get("ok") is False, "; ".join(bogus.get("reasons") or [])[:160])

    if args.skip_downloads:
        print(f"\nSUMMARY (skip-downloads): {len(failures)} failure(s)")
        sys.exit(1 if failures else 0)

    # cancel test
    artist, album, year = ALBUMS[0]
    tracks = mb_release_tracklist(artist, album, year)["tracks"]
    job = request_json("POST", f"{base}/download", json={"artist": artist, "album": album, "year": year, "tracks": tracks})
    job_id = job["job_id"]
    print(f"\n[cancel-test] started job {job_id}; cancelling in 12s")
    time.sleep(12)
    request_json("DELETE", f"{base}/jobs/{job_id}")
    job = poll_job(base, job_id, timeout_s=300, label="cancel-test")
    check("cancel: job ends cancelled", job["status"] == "cancelled", f"status={job['status']}")

    # full downloads
    for artist, album, year in ALBUMS:
        tracks = mb_release_tracklist(artist, album, year)["tracks"]
        job = request_json("POST", f"{base}/download", json={"artist": artist, "album": album, "year": year, "tracks": tracks})
        job_id = job["job_id"]
        print(f"\n[download] {artist} - {album} -> job {job_id}")
        job = poll_job(base, job_id, timeout_s=2700, label=album[:18])
        check(
            f"download: {artist} - {album}",
            job["status"] == "completed",
            f"status={job['status']} error={job.get('error')}",
        )
        verify_files(job, album[:18])

    print(f"\nSUMMARY: {len(failures)} failure(s)")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
