"""Category-level content fingerprint for cross-source deduplication.

Two importers that capture the same listen / watch should emit the same
fingerprint so Fulcra's ingest dedupes them, even though their per-plugin
source_ids differ. Returned as a string in the same shape as the existing
plugin-specific source_ids: ``com.fulcra.content.<category>.v1.<sha256_16>``.

The fingerprint is added ALONGSIDE the existing plugin source_id (not
instead of it). Both end up in the event's ``source`` array. Different
importers produce different plugin source_ids but the same content
fingerprint, and Fulcra dedupes on any source-id match.

Categories:
  - ``listened`` — music tracks. Inputs: timestamp, artist, track.
    Timestamp bucketed to nearest 5 minutes to absorb scrobble-vs-actual-
    play skew between sources (Last.fm fires on track start, Apple Music
    on track end, etc.).
  - ``watched``  — TV episode. Inputs: timestamp, show, season, episode.
  - ``watched_movie`` — separate kind because a movie has no S/E and
    dedup purely on title-at-time-bucket is risky for repeat viewings.
  - ``podcast``  — podcast episodes. Inputs: timestamp, show, episode title.

Normalization:
  - Track / artist / title / show: lowercase, strip whitespace, drop
    parenthetical / bracket suffixes like ``" (Remastered 2011)"`` /
    ``" (feat. X)"`` / ``" [Deluxe Edition]"`` since different sources
    include or omit them inconsistently. Also strip trailing
    ``" - Remastered"`` / ``" - 2011 Remaster"`` / ``" - Radio Edit"`` etc.
  - Timestamps: rounded DOWN to the start of a 5-minute window (UTC).

Boundary note: two listens at 14:34:59 and 14:35:00 land in different
buckets (14:30 vs 14:35). The 5-minute window absorbs typical
source-to-source skew (seconds to ~minute) but won't help at the rare
exact boundary. Acceptable — the per-plugin source_id still uniquely
identifies each importer's row, so worst case is a missed cross-source
dedup, never a false merge.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

_PAREN_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")
_BRACKET_SUFFIX = re.compile(r"\s*\[[^\]]*\]\s*$")
_FEAT_INLINE = re.compile(r"\s+(feat\.|ft\.|featuring)\s+.*$", re.IGNORECASE)
# Strip "- Remastered", "- 2011 Remaster", "- Deluxe Edition", etc.
_TRAIL_DASH = re.compile(
    # Optional leading year ("2011 Remaster"), then the qualifier word.
    r"\s+-\s+(?:\d{4}\s+)?"
    r"(remastered|remaster|deluxe|extended|mono|stereo|live"
    r"|radio edit|single version).*$",
    re.IGNORECASE,
)
_WS = re.compile(r"\s+")


def normalize_title(s: str) -> str:
    """Strip Apple/Spotify/Last.fm-style suffix noise so the same track
    reads the same regardless of which source recorded it."""
    if not s:
        return ""
    out = s.strip()
    # Iterate paren/bracket strip until stable (handles "Foo (Live) [2024]").
    for _ in range(3):
        prev = out
        out = _PAREN_SUFFIX.sub("", out)
        out = _BRACKET_SUFFIX.sub("", out)
        if out == prev:
            break
    out = _FEAT_INLINE.sub("", out)
    out = _TRAIL_DASH.sub("", out)
    out = _WS.sub(" ", out).strip().lower()
    return out


def bucket_5min(dt: datetime) -> str:
    """Round ``dt`` DOWN to the start of a 5-minute window. Returns a UTC
    string ``YYYY-MM-DDTHH:MM:00Z`` suitable for hashing."""
    if dt.tzinfo is None:
        # Assume UTC for naive datetimes — caller bug, but don't crash here.
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    minute = (dt.minute // 5) * 5
    bucketed = dt.replace(minute=minute, second=0, microsecond=0)
    return bucketed.isoformat().replace("+00:00", "Z")


def _hash16(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def listened_fingerprint(
    *, timestamp: datetime, artist: str, track: str
) -> str | None:
    """Cross-source fingerprint for a music listen, or ``None`` if any
    required input is empty.

    Artist is treated as optional (Apple Music drops it on ~32% of rows
    in real takeouts); if missing, the fingerprint dedupes on
    (time, track) which is still useful for sources that also lack
    artist. Track and timestamp are required.
    """
    if not timestamp:
        return None
    nt = normalize_title(track)
    if not nt:
        return None
    na = normalize_title(artist)
    bucket = bucket_5min(timestamp)
    payload = f"listened|{bucket}|{na}|{nt}"
    return f"com.fulcra.content.listened.v1.{_hash16(payload)}"


def watched_tv_fingerprint(
    *,
    timestamp: datetime,
    show: str,
    season: int | str | None,
    episode: int | str | None,
) -> str | None:
    """Cross-source fingerprint for a TV episode watch.

    Returns ``None`` unless we have show + a (season, episode) pair.
    Season / episode can be ints or strings; both are coerced to ``str``
    so ``S2E5`` from one source matches ``S2E5`` from another regardless
    of int-vs-string typing.
    """
    if not timestamp:
        return None
    ns = normalize_title(show)
    if not ns:
        return None
    if season is None or episode is None or season == "" or episode == "":
        return None
    bucket = bucket_5min(timestamp)
    payload = f"watched_tv|{bucket}|{ns}|S{season}E{episode}"
    return f"com.fulcra.content.watched.v1.{_hash16(payload)}"


def watched_movie_fingerprint(
    *, timestamp: datetime, title: str
) -> str | None:
    """Cross-source fingerprint for a movie watch.

    Note the distinct ``watched_movie|`` payload prefix vs.
    ``watched_tv|`` — a movie and a TV episode that happen to share the
    same title-at-bucket would otherwise collide. The visible
    ``com.fulcra.content.watched.v1.`` namespace is shared, but the hash
    keys diverge.
    """
    if not timestamp:
        return None
    nt = normalize_title(title)
    if not nt:
        return None
    bucket = bucket_5min(timestamp)
    payload = f"watched_movie|{bucket}|{nt}"
    return f"com.fulcra.content.watched.v1.{_hash16(payload)}"


def podcast_fingerprint(
    *, timestamp: datetime, show: str, episode: str
) -> str | None:
    """Cross-source fingerprint for a podcast episode listen.

    Requires show + episode + timestamp; returns ``None`` if any are
    missing. Episode title is normalised the same way as music tracks
    so ``"#142: Foo Bar"`` vs ``"#142: Foo Bar (rebroadcast)"`` collapse.
    """
    if not timestamp:
        return None
    ns = normalize_title(show)
    ne = normalize_title(episode)
    if not ns or not ne:
        return None
    bucket = bucket_5min(timestamp)
    payload = f"podcast|{bucket}|{ns}|{ne}"
    return f"com.fulcra.content.podcast.v1.{_hash16(payload)}"
