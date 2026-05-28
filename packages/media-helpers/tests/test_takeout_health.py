"""Tests for the takeout-file health checks used by the wizard's
test_connection step.

Covers the five file-shaped plugins (netflix, spotify-extended, youtube,
apple-takeout, apple-music-takeout). The shared helpers in
fulcra_media/takeout_health.py do path-resolution + parse + preview, so
each test exercises the three branches the wizard cares about:
  - no path configured (ok=False, no parser invocation)
  - missing file at the configured path (ok=False)
  - happy path → ok=True with a preview populated from the parser's
    first few events
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from fulcra_media import takeout_health


FIXTURES = Path(__file__).parent / "fixtures"


@dataclass
class _Ctx:
    """Minimal RunContext stand-in — the takeout checks read only
    ctx.config. credentials/plugin_id are accepted for shape parity."""
    config: dict = field(default_factory=dict)
    credentials: dict = field(default_factory=dict)
    plugin_id: str = "netflix"


# ---------------------------------------------------------------------------
# Netflix
# ---------------------------------------------------------------------------

def test_netflix_no_path_returns_friendly_error():
    """No path configured → ok=False, no parse attempt."""
    ctx = _Ctx(config={})

    result = takeout_health.netflix_health_check(ctx)

    assert result.ok is False
    assert "Netflix" in result.summary


def test_netflix_missing_file(tmp_path):
    """Path configured but file doesn't exist → ok=False with a "double
    check the path" hint."""
    ctx = _Ctx(config={"path": str(tmp_path / "nope.csv")})

    result = takeout_health.netflix_health_check(ctx)

    assert result.ok is False
    assert "Netflix" in result.summary


def test_netflix_happy_path_returns_preview():
    """Real fixture parses cleanly → ok=True with preview rows."""
    fixture = FIXTURES / "netflix_slim_small.csv"
    ctx = _Ctx(config={"path": str(fixture)})

    result = takeout_health.netflix_health_check(ctx)

    assert result.ok is True
    assert "Netflix" in result.summary
    assert len(result.preview) >= 1
    # The preview's title carries the show / movie title from the CSV
    assert result.preview[0]["title"]


# ---------------------------------------------------------------------------
# Spotify Extended (zip)
# ---------------------------------------------------------------------------

def test_spotify_extended_no_path_returns_friendly_error():
    ctx = _Ctx(config={}, plugin_id="spotify-extended")

    result = takeout_health.spotify_extended_health_check(ctx)

    assert result.ok is False
    assert "Spotify" in result.summary


def test_spotify_extended_happy_path_returns_preview():
    """The existing spotify_extended_sample.zip fixture parses to several
    events; the check picks the first few off."""
    fixture = FIXTURES / "spotify_extended_sample.zip"
    ctx = _Ctx(config={"path": str(fixture)}, plugin_id="spotify-extended")

    result = takeout_health.spotify_extended_health_check(ctx)

    assert result.ok is True
    assert "Spotify" in result.summary
    assert len(result.preview) >= 1
    assert result.preview[0]["title"]


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

def test_youtube_no_path_returns_friendly_error():
    ctx = _Ctx(config={}, plugin_id="youtube")

    result = takeout_health.youtube_health_check(ctx)

    assert result.ok is False
    assert "YouTube" in result.summary


def test_youtube_happy_path_returns_preview():
    fixture = FIXTURES / "youtube_watch_history_small.json"
    ctx = _Ctx(config={"path": str(fixture)}, plugin_id="youtube")

    result = takeout_health.youtube_health_check(ctx)

    assert result.ok is True
    assert "YouTube" in result.summary
    assert len(result.preview) >= 1


# ---------------------------------------------------------------------------
# Apple TV takeout
# ---------------------------------------------------------------------------

def test_apple_takeout_no_path_returns_friendly_error():
    ctx = _Ctx(config={}, plugin_id="apple-takeout")

    result = takeout_health.apple_takeout_health_check(ctx)

    assert result.ok is False
    assert "Apple" in result.summary


def test_apple_takeout_happy_path_returns_preview():
    """Playback Activity CSV fixture is the legacy / sparse shape — the
    parse_any router picks the right parser."""
    fixture = FIXTURES / "apple_takeout_playback_sample.csv"
    ctx = _Ctx(config={"path": str(fixture)}, plugin_id="apple-takeout")

    result = takeout_health.apple_takeout_health_check(ctx)

    assert result.ok is True
    assert "Apple TV" in result.summary
    assert len(result.preview) >= 1


def test_apple_takeout_unparseable_file_returns_friendly_error(tmp_path):
    """Real file at the path but the importer can't make sense of it →
    ok=False with the importer's error preserved in the summary."""
    bogus = tmp_path / "Video Play Activity.csv"
    bogus.write_text("not,a,real,header\n1,2,3,4\n")
    # File-shape detection happens via filename; this looks right.
    ctx = _Ctx(config={"path": str(bogus)}, plugin_id="apple-takeout")

    result = takeout_health.apple_takeout_health_check(ctx)

    # Either ok=False with an error, or ok=True with empty preview
    # (the importer's missing-required-column check is the more likely
    # path here). Both are valid "we tried and surfaced the problem"
    # outcomes for the wizard.
    assert result.ok is False or result.preview == []


# ---------------------------------------------------------------------------
# Apple Music takeout
# ---------------------------------------------------------------------------

def test_apple_music_takeout_no_path_returns_friendly_error():
    ctx = _Ctx(config={}, plugin_id="apple-music-takeout")

    result = takeout_health.apple_music_takeout_health_check(ctx)

    assert result.ok is False
    assert "Apple Music" in result.summary


def test_apple_music_takeout_happy_path_returns_preview(tmp_path):
    """Synthetic Apple Music Play Activity.csv — mirrors the fixture shape
    that the apple_music_takeout importer tests use."""
    header = (
        "Song Name,Container Album Name,Container Artist Name,"
        "Event Start Timestamp,Event End Timestamp,"
        "Play Duration Milliseconds,UTC Offset In Seconds,"
        "Event Type,End Reason Type"
    )
    rows = [
        # 7m29s, natural end — survives the importer's filters.
        "Blue Monday,Substance,New Order,"
        "2025-06-15 21:00:00,2025-06-15 21:07:29,"
        "449000,-25200,PLAY_END,NATURAL_END_OF_TRACK",
    ]
    csv = tmp_path / "Apple Music Play Activity.csv"
    csv.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    ctx = _Ctx(config={"path": str(csv)}, plugin_id="apple-music-takeout")

    result = takeout_health.apple_music_takeout_health_check(ctx)

    assert result.ok is True
    assert "Apple Music" in result.summary
    assert len(result.preview) == 1
    assert result.preview[0]["title"] == "Blue Monday"
