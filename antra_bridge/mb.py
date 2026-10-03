"""
MusicBrainz helper: fetch the expected tracklist for an album.

This is the ground-truth shape Lidarr works with (artist + album + tracklist
from MusicBrainz). Both the Phase 0 matching proof and the bridge proof use it
to supply realistic resolve requests.
"""
from __future__ import annotations

from typing import Optional

import requests

MB_API = "https://musicbrainz.org/ws/2"
MB_HEADERS = {"User-Agent": "antra-lidarr-dev/0.1 (maxschramp)"}


def mb_release_tracklist(artist: str, album: str, year: Optional[int] = None) -> dict:
    """Return {"mbid", "title", "date", "tracks": [{"title", "duration_s"}]}."""
    query = f'artist:"{artist}" AND release:"{album}"'
    resp = requests.get(
        f"{MB_API}/release",
        params={"query": query, "fmt": "json", "limit": 25},
        headers=MB_HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    releases = resp.json().get("releases", [])
    if not releases:
        raise RuntimeError(f"MusicBrainz found no releases for {artist} - {album}")

    def key(rel: dict) -> tuple:
        title_exact = 1 if rel.get("title", "").strip().lower() == album.lower() else 0
        year_match = 1 if year and (rel.get("date") or "").startswith(str(year)) else 0
        return (-title_exact, -year_match, rel.get("id", ""))

    pick = sorted(releases, key=key)[0]
    detail = requests.get(
        f"{MB_API}/release/{pick['id']}",
        params={"inc": "recordings", "fmt": "json"},
        headers=MB_HEADERS,
        timeout=20,
    )
    detail.raise_for_status()
    data = detail.json()
    tracks = []
    for medium in data.get("media", []):
        for t in medium.get("tracks", []):
            length_ms = t.get("length") or 0
            tracks.append(
                {
                    "title": t.get("title", ""),
                    "duration_s": (length_ms / 1000.0) if length_ms else None,
                }
            )
    return {
        "mbid": pick["id"],
        "title": data.get("title"),
        "date": data.get("date"),
        "tracks": tracks,
    }
