"""
Resolve pipeline: {artist, album, year, tracks} -> validated Deezer link.

Order of preference:
 1. If the caller supplied a deezer_url (the plugin always echoes back what
    /resolve returned), verify it cheaply (album exists, track count agrees)
    and trust it.
 2. Otherwise run the Deezer matcher (search + scoring + per-track validation).
 3. Optionally run the cross-source availability probe (search-only; never
    downloads) and report which Antra sources can serve a sample track.

Results are cached in SQLite (keyed on the exact request identity) with a TTL;
`refresh` bypasses the read. Failures are cached too, so a wrong album does
not hammer the APIs on every Lidarr search retry.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Optional

from . import engine_adapter
from .config import BridgeConfig
from .matching import DeezerClient, TrackExpectation, match_album
from .store import Store

logger = logging.getLogger("antra_bridge.resolve")

MAX_CANDIDATES_IN_PAYLOAD = 8
_DEZER_URL_RE = re.compile(r"deezer\.com/(?:[a-z]{2}/)?album/(\d+)", re.IGNORECASE)


def canonical_key(artist: str, album: str, year: Optional[int], tracks: Optional[list[dict]]) -> str:
    payload = {
        "artist": (artist or "").strip(),
        "album": (album or "").strip(),
        "year": year,
        "tracks": [
            {"title": (t.get("title") or "").strip(), "duration_s": t.get("duration_s")}
            for t in (tracks or [])
        ],
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def expected_from_tracks(tracks: Optional[list[dict]]) -> Optional[list[TrackExpectation]]:
    if not tracks:
        return None
    return [
        TrackExpectation(title=t.get("title") or "", duration_s=t.get("duration_s"))
        for t in tracks
    ]


def _match_payload(result) -> dict:
    return {
        "ok": result.ok,
        "deezer_id": result.deezer_id,
        "deezer_url": result.deezer_url,
        "deezer_nb_tracks": result.nb_tracks,
        "matched_title": result.matched_title,
        "matched_artist": result.matched_artist,
        "confidence": result.confidence,
        "reasons": list(result.reasons),
        "candidates": result.candidates_considered[:MAX_CANDIDATES_IN_PAYLOAD],
    }


def _payload_from_provided_url(req: dict) -> Optional[dict]:
    url = (req.get("deezer_url") or "").strip()
    if not url:
        return None
    match = _DEZER_URL_RE.search(url)
    if not match:
        return {
            "ok": False,
            "deezer_id": None,
            "deezer_url": url,
            "confidence": None,
            "reasons": [f"provided deezer_url is not a Deezer album link: {url[:120]}"],
            "candidates": [],
        }
    album_id = int(match.group(1))
    try:
        detail = DeezerClient().album_detail(album_id)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "deezer_id": album_id,
            "deezer_url": url,
            "confidence": None,
            "reasons": [f"provided deezer_url could not be verified: {type(exc).__name__}: {exc}"[:200]],
            "candidates": [],
        }
    reasons = [
        f"deezer_url supplied by caller; album verified ('{detail.get('title')}' by "
        f"{(detail.get('artist') or {}).get('name')}, {detail.get('nb_tracks')} tracks)"
    ]
    ok = True
    expected = len(req.get("tracks") or [])
    if expected and detail.get("nb_tracks") != expected:
        ok = False
        reasons.append(
            f"track count mismatch: deezer has {detail.get('nb_tracks')}, expected {expected}"
        )
    return {
        "ok": ok,
        "deezer_id": album_id,
        "deezer_url": url,
        "deezer_nb_tracks": detail.get("nb_tracks"),
        "matched_title": detail.get("title"),
        "matched_artist": (detail.get("artist") or {}).get("name"),
        "confidence": None,
        "reasons": reasons,
        "candidates": [],
    }


def _run_probe(cfg: BridgeConfig, deezer_url: str) -> dict:
    try:
        tracks = engine_adapter.fetch_album_tracks(cfg, deezer_url)
        return engine_adapter.probe_sources(cfg, tracks, cfg.probe_tracks)
    except Exception as exc:  # noqa: BLE001
        logger.warning("availability probe failed: %s", exc)
        return {"probe_error": f"{type(exc).__name__}: {exc}"[:300]}


def resolve_request(cfg: BridgeConfig, store: Store, req: dict, *, allow_probe: bool) -> dict:
    key = canonical_key(req.get("artist"), req.get("album"), req.get("year"), req.get("tracks"))
    query = {"artist": req.get("artist"), "album": req.get("album"), "year": req.get("year")}
    want_probe = allow_probe and cfg.probe_enabled and cfg.probe_tracks > 0 and cfg.probe_tracks is not None
    want_probe = bool(want_probe)
    cached = None if req.get("refresh") else store.cache_get(key)

    if cached is not None:
        needs_probe = cached.get("ok") and want_probe and "availability" not in cached
        if not needs_probe:
            cached.setdefault("query", query)
            return {**cached, "cached": True}
        cached["availability"] = _run_probe(cfg, cached["deezer_url"])
        cached.setdefault("query", query)
        store.cache_put(key, cached, cfg.cache_ttl_s)
        return {**cached, "cached": True}

    payload = _payload_from_provided_url(req)
    if payload is None or not payload.get("ok"):
        matched = match_album(
            req.get("artist") or "",
            req.get("album") or "",
            year=req.get("year"),
            expected_tracks=expected_from_tracks(req.get("tracks")),
        )
        matched_payload = _match_payload(matched)
        # A caller-provided URL failing verification is more informative than a
        # fresh search only when the search also fails; prefer the search result.
        if not matched_payload.get("ok") and payload is not None:
            payload = {**payload, "reasons": payload["reasons"] + matched_payload["reasons"]}
        else:
            payload = matched_payload

    payload["key"] = key
    payload.setdefault("query", query)
    if payload.get("ok") and want_probe:
        payload["availability"] = _run_probe(cfg, payload["deezer_url"])
    store.cache_put(key, payload, cfg.cache_ttl_s)
    return {**payload, "cached": False}
