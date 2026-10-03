"""
Album matching for the Antra bridge.

Maps {artist, album, year, tracklist} (Lidarr's view of the world) to a Deezer
album link (Antra's input) and validates the edition before accepting it.

Design notes (Phase 0 findings, tested against the live API):

- Deezer's public search API needs no auth. Field-qualified queries such as
  artist:"daft punk" album:"random access memories" returned zero results for
  albums we know exist, while a plain keyword query returned the right album as
  the first hit. So: search plain, score locally.
- Editions are the primary hazard: the same title exists as original (13
  tracks), 10th Anniversary (22), Drumless (13), plus tribute albums by other
  artists. Scoring on artist + title + year alone is not enough. If the caller
  supplies the expected tracklist, the winner must match its track count and
  pass per-track title/duration validation. No match -> fail with a report,
  never guess.
- Rate limits: Deezer allows ~50 requests / 5 s per IP. This module makes at
  most a handful of requests per match and backs off on HTTP 429 / API error
  code 4.
"""
from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Optional

import requests

DEEZER_API = "https://api.deezer.com"
HTTP_TIMEOUT = 20
HEADERS = {"User-Agent": "antra-bridge/0.1"}

# Scoring thresholds. Deliberately conservative: a wrong edition is worse than
# a clean "not found".
ARTIST_SIM_MIN = 0.75          # normalized artist similarity for a candidate to count
TITLE_SIM_MIN = 0.70           # normalized album-title similarity
TRACK_TITLE_SIM_MIN = 0.65    # per-track normalized title similarity
TRACK_MATCH_RATIO_MIN = 0.90   # share of expected tracks that must match
DURATION_TOLERANCE_S = 8       # cross-edition fade-pad drift allowance

_FEAT_RE = re.compile(r"\s*[\(\[]\s*(feat|ft|with|prod)\.?\s[^)\]]*[\)\]]", re.I)
_EDITION_RE = re.compile(
    r"\s*[\(\[]\s*(deluxe|remaster|anniversary|expanded|special|bonus|edition|version)[^)\]]*[\)\]]",
    re.I,
)


def normalize_text(value: str) -> str:
    v = unicodedata.normalize("NFKD", value or "")
    v = "".join(c for c in v if not unicodedata.combining(c))
    v = v.lower().replace("&", " and ")
    v = re.sub(r"[^a-z0-9]+", " ", v)
    return re.sub(r"\s+", " ", v).strip()


def normalize_artist(value: str) -> str:
    v = normalize_text(value)
    for prefix in ("the ", "a ", "an "):
        if v.startswith(prefix) and len(v) > len(prefix):
            return v[len(prefix):]
    return v


def normalize_track_title(value: str) -> str:
    v = _FEAT_RE.sub("", value or "")
    return normalize_text(v)


def normalize_album_title(value: str) -> str:
    """Strip edition markers so 'Random Access Memories' matches its deluxe."""
    v = _EDITION_RE.sub("", value or "")
    return normalize_text(v)


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


@dataclass
class TrackExpectation:
    title: str
    duration_s: Optional[float] = None


@dataclass
class TrackComparison:
    index: int
    expected_title: str
    actual_title: str
    title_sim: float
    duration_delta_s: Optional[float]
    ok: bool


@dataclass
class MatchResult:
    ok: bool
    deezer_id: Optional[int] = None
    deezer_url: Optional[str] = None
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)
    track_report: list[TrackComparison] = field(default_factory=list)
    candidates_considered: list[dict] = field(default_factory=list)


class DeezerClient:
    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers.update(HEADERS)

    def _get(self, path: str, params: Optional[dict] = None, retries: int = 3) -> dict:
        url = f"{DEEZER_API}{path}"
        last_exc: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=HTTP_TIMEOUT)
                if resp.status_code == 429:
                    raise RuntimeError("deezer rate limited (HTTP 429)")
                data = resp.json()
                if isinstance(data, dict) and data.get("error"):
                    err = data["error"]
                    if err.get("code") == 4:  # quota exceeded
                        raise RuntimeError(f"deezer quota: {err.get('message')}")
                    raise RuntimeError(f"deezer api error: {err}")
                return data
            except Exception as exc:  # noqa: BLE001 - retry loop
                last_exc = exc
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"Deezer request failed for {path}: {last_exc}")

    def search_albums(self, artist: str, album: str, limit: int = 25) -> list[dict]:
        data = self._get(
            "/search/album",
            {"q": f"{artist} {album}".strip(), "limit": limit},
        )
        return data.get("data", [])

    def album_detail(self, album_id: int) -> dict:
        return self._get(f"/album/{album_id}")

    def album_tracks(self, album_id: int) -> list[dict]:
        tracks: list[dict] = []
        data = self._get(f"/album/{album_id}/tracks", {"limit": 100})
        while True:
            tracks.extend(data.get("data", []))
            nxt = data.get("next")
            if not nxt:
                break
            data = self._get(nxt.replace(DEEZER_API, ""))
        return tracks


def _candidate_year(detail: dict) -> Optional[int]:
    date = (detail.get("release_date") or "").strip()
    return int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None


def match_album(
    artist: str,
    album: str,
    year: Optional[int] = None,
    expected_tracks: Optional[list[TrackExpectation]] = None,
    client: Optional[DeezerClient] = None,
) -> MatchResult:
    """Find and validate a Deezer album for the given identity.

    Returns MatchResult; ok=False with reasons + candidates_considered when the
    edition cannot be validated. Never raises for ordinary mismatches.
    """
    client = client or DeezerClient()
    result = MatchResult(ok=False)

    want_artist = normalize_artist(artist)
    want_title = normalize_album_title(album)
    expected_count = len(expected_tracks) if expected_tracks else None

    try:
        raw_candidates = client.search_albums(artist, album)
    except Exception as exc:  # noqa: BLE001
        result.reasons.append(f"deezer search failed: {exc}")
        return result

    if not raw_candidates:
        result.reasons.append(f"deezer search returned no candidates for '{artist} - {album}'")
        return result

    scored: list[dict[str, Any]] = []
    for cand in raw_candidates:
        cand_artist = (cand.get("artist") or {}).get("name", "")
        cand_title = cand.get("title", "")
        a_sim = similarity(want_artist, normalize_artist(cand_artist))
        t_sim = similarity(want_title, normalize_album_title(cand_title))
        entry = {
            "deezer_id": cand.get("id"),
            "title": cand_title,
            "artist": cand_artist,
            "nb_tracks": cand.get("nb_tracks"),
            "record_type": cand.get("record_type"),
            "artist_sim": round(a_sim, 3),
            "title_sim": round(t_sim, 3),
        }
        if a_sim < ARTIST_SIM_MIN or t_sim < TITLE_SIM_MIN:
            entry["skipped"] = "artist/title similarity below threshold"
            scored.append(entry)
            continue
        scored.append(entry)

    eligible = [e for e in scored if "skipped" not in e]
    result.candidates_considered = [dict(e) for e in scored]
    if not eligible:
        result.reasons.append("no candidate passed artist/title similarity thresholds")
        return result

    # Rank eligible candidates: prefer exact track count (when known), then
    # title similarity, then year proximity, then non-tribute record types.
    def rank_key(entry: dict) -> tuple:
        detail_year = None
        count_match = 0
        if expected_count is not None and entry.get("nb_tracks") == expected_count:
            count_match = 1
        year_pen = 0
        if year and entry.get("release_year"):
            year_pen = abs(entry["release_year"] - year)
        return (
            -count_match,
            -entry["title_sim"],
            year_pen,
            0 if entry.get("record_type") == "album" else 1,
        )

    # Fetch details (release_date) for eligible candidates to rank by year.
    for entry in eligible:
        try:
            detail = client.album_detail(entry["deezer_id"])
            entry["release_year"] = _candidate_year(detail)
            entry["release_date"] = detail.get("release_date")
            entry["record_type"] = entry.get("record_type") or detail.get("record_type")
            entry["nb_tracks"] = detail.get("nb_tracks", entry.get("nb_tracks"))
        except Exception as exc:  # noqa: BLE001
            entry["detail_error"] = str(exc)

    eligible.sort(key=rank_key)

    attempts: list[str] = []
    for entry in eligible:
        count = entry.get("nb_tracks")
        if expected_count is not None and count != expected_count:
            attempts.append(
                f"{entry['title']} ({entry['deezer_id']}, {count} tracks): "
                f"track count {count} != expected {expected_count}"
            )
            continue

        # Deep validation when we have expectations; otherwise accept the score.
        if expected_tracks is None:
            result.ok = True
            result.deezer_id = entry["deezer_id"]
            result.deezer_url = f"https://www.deezer.com/album/{entry['deezer_id']}"
            result.confidence = round(
                0.55 * entry["artist_sim"] + 0.45 * entry["title_sim"], 3
            )
            result.reasons.append(
                "no expected tracklist supplied; accepted on artist/title/year/count heuristics"
            )
            return result

        try:
            actual_tracks = client.album_tracks(entry["deezer_id"])
        except Exception as exc:  # noqa: BLE001
            attempts.append(f"{entry['title']} ({entry['deezer_id']}): tracklist fetch failed: {exc}")
            continue

        if len(actual_tracks) != len(expected_tracks):
            attempts.append(
                f"{entry['title']} ({entry['deezer_id']}): tracklist length "
                f"{len(actual_tracks)} != expected {len(expected_tracks)}"
            )
            continue

        comparisons: list[TrackComparison] = []
        matched = 0
        for i, (exp, act) in enumerate(zip(expected_tracks, actual_tracks), start=1):
            t_sim = similarity(
                normalize_track_title(exp.title), normalize_track_title(act.get("title", ""))
            )
            dur_delta = None
            if exp.duration_s and act.get("duration"):
                dur_delta = abs(exp.duration_s - float(act["duration"]))
            ok = t_sim >= TRACK_TITLE_SIM_MIN and (
                dur_delta is None or dur_delta <= DURATION_TOLERANCE_S
            )
            matched += 1 if ok else 0
            comparisons.append(
                TrackComparison(
                    index=i,
                    expected_title=exp.title,
                    actual_title=act.get("title", ""),
                    title_sim=round(t_sim, 3),
                    duration_delta_s=round(dur_delta, 1) if dur_delta is not None else None,
                    ok=ok,
                )
            )

        ratio = matched / len(expected_tracks)
        if ratio >= TRACK_MATCH_RATIO_MIN:
            result.ok = True
            result.deezer_id = entry["deezer_id"]
            result.deezer_url = f"https://www.deezer.com/album/{entry['deezer_id']}"
            result.confidence = round(
                0.45 * entry["artist_sim"]
                + 0.35 * entry["title_sim"]
                + 0.20 * ratio,
                3,
            )
            result.track_report = comparisons
            result.reasons.append(
                f"validated {matched}/{len(expected_tracks)} tracks "
                f"(ratio {ratio:.2f}) against '{entry['title']}' ({count} tracks)"
            )
            return result

        attempts.append(
            f"{entry['title']} ({entry['deezer_id']}): only {matched}/{len(expected_tracks)} "
            f"tracks matched (ratio {ratio:.2f} < {TRACK_MATCH_RATIO_MIN})"
        )

    result.reasons.append("no Deezer edition passed validation")
    result.reasons.extend(attempts[:6])
    if expected_count is not None:
        counts = sorted({e.get("nb_tracks") for e in eligible if e.get("nb_tracks")})
        result.reasons.append(
            f"expected {expected_count} tracks; candidate counts on Deezer: {counts}"
        )
    return result
