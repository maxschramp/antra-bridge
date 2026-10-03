# antra-bridge

Headless HTTP bridge that lets Lidarr drive Antra's multi-source download
pipeline. Lidarr thinks in MusicBrainz identities; Antra consumes service
links. The bridge maps one to the other and runs the download.

Status: Phase 0 (matching prototype + proofs). The FastAPI service, job store
and download orchestration land in Phase 1.

Layout:
- `antra_bridge/matching.py` - {artist, album, year, tracklist} -> validated
  Deezer album link (search + local scoring + per-track validation).
- `scripts/prove_matching.py` - Phase 0 proof harness: pulls the expected
  tracklist from MusicBrainz and runs the matcher against the live Deezer API.

See `NOTES.md` for findings, gotchas and decisions.
