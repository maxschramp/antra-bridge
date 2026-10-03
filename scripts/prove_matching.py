"""
Phase 0 proof: {artist, album} -> validated Deezer link for two test albums.

Ground truth comes from the MusicBrainz API (the same identity source Lidarr
uses), so this exercises exactly the cross-source matching problem the bridge
faces: MB tracklist in, Deezer edition out, per-track validation in between.

Usage:
    python scripts/prove_matching.py            # runs the two default albums
    python scripts/prove_matching.py "Artist" "Album" [year]
"""
from __future__ import annotations

import sys

import requests

sys.path.insert(0, ".")
from antra_bridge.matching import TrackExpectation, match_album  # noqa: E402

MB_API = "https://musicbrainz.org/ws/2"
MB_HEADERS = {"User-Agent": "antra-lidarr-dev/0.1 (maxschramp)"}

DEFAULT_ALBUMS = [
    ("Daft Punk", "Random Access Memories", 2013),          # mainstream
    ("Susumu Yokota", "Sakura", 1999),                      # obscure / electronic
]


def mb_release_tracklist(artist: str, album: str, year: int | None):
    """Fetch the best matching MusicBrainz release + its tracklist."""
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

    def key(rel):
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
    tracks: list[TrackExpectation] = []
    for medium in data.get("media", []):
        for t in medium.get("tracks", []):
            length_ms = t.get("length") or 0
            tracks.append(
                TrackExpectation(
                    title=t.get("title", ""),
                    duration_s=(length_ms / 1000.0) if length_ms else None,
                )
            )
    return {
        "mbid": pick["id"],
        "title": data.get("title"),
        "date": data.get("date"),
        "tracks": tracks,
    }


def run_one(artist: str, album: str, year: int | None):
    print("=" * 72)
    print(f"TEST ALBUM: {artist} - {album}" + (f" ({year})" if year else ""))
    print("=" * 72)

    mb = mb_release_tracklist(artist, album, year)
    print(f"MusicBrainz: {mb['title']} [{mb['date']}] mbid={mb['mbid']} "
          f"tracks={len(mb['tracks'])}")
    for t in mb["tracks"][:4]:
        dur = f"{t.duration_s:.0f}s" if t.duration_s else "?"
        print(f"   - {t.title} ({dur})")
    if len(mb["tracks"]) > 4:
        print(f"   ... +{len(mb['tracks']) - 4} more")

    result = match_album(artist, album, year=year, expected_tracks=mb["tracks"])

    print()
    print("Deezer candidates considered:")
    for c in result.candidates_considered:
        note = f" SKIPPED: {c['skipped']}" if "skipped" in c else ""
        print(f"   id={c.get('deezer_id')} '{c.get('title')}' by {c.get('artist')} "
              f"[{c.get('nb_tracks')} tracks, type={c.get('record_type')}, "
              f"year={c.get('release_year', c.get('release_date'))}] "
              f"(a={c.get('artist_sim')}, t={c.get('title_sim')}){note}")

    print()
    if result.ok:
        print(f"VERDICT: MATCH -> {result.deezer_url} "
              f"(confidence {result.confidence})")
        for r in result.reasons:
            print(f"   {r}")
        bad = [c for c in result.track_report if not c.ok]
        print(f"   per-track: {len(result.track_report)} compared, "
              f"{len(result.track_report) - len(bad)} ok, {len(bad)} weak")
        for c in bad[:6]:
            print(f"     #{c.index:02d} '{c.expected_title}' vs '{c.actual_title}' "
                  f"(sim={c.title_sim}, dur_delta={c.duration_delta_s})")
    else:
        print("VERDICT: NO VALID MATCH (this is a clean failure, not a crash)")
        for r in result.reasons:
            print(f"   {r}")
    print()
    return result


def main():
    args = sys.argv[1:]
    if len(args) >= 2:
        year = int(args[2]) if len(args) > 2 and args[2].isdigit() else None
        albums = [(args[0], args[1], year)]
    else:
        albums = DEFAULT_ALBUMS

    ok = 0
    for artist, album, year in albums:
        result = run_one(artist, album, year)
        ok += 1 if result.ok else 0
    print("=" * 72)
    print(f"SUMMARY: {ok}/{len(albums)} albums matched + validated")
    sys.exit(0 if ok == len(albums) else 1)


if __name__ == "__main__":
    main()
