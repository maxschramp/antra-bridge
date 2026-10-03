"""
Antra Bridge: headless HTTP service that lets Lidarr drive Antra's
multi-source download pipeline.

Lidarr speaks MusicBrainz identities; Antra consumes service links. The bridge
maps one to the other (validated Deezer album links), runs the engine headless,
and exposes jobs with per-track progress. See NOTES.md for the design and
failure modes.
"""

__version__ = "0.1.0"
