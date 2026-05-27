"""Tests for the Apple Podcasts health check used by the wizard's
test_connection step.

Companion to test_apple_podcasts_importer.py — the importer side proves
parse_db's WHERE clause is correct on real-shaped data; this side proves
the wizard surfaces the same count via a cheap COUNT(*) query before the
user reaches the run loop, and that DB-missing / FDA-denied failures
return actionable HealthResult.summary strings.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from fulcra_media.apple_podcasts_health import apple_podcasts_health_check


@dataclass
class _Ctx:
    """Minimal RunContext stand-in — the health check only reads
    ctx.config. credentials/plugin_id are accepted for shape parity."""
    config: dict = field(default_factory=dict)
    credentials: dict = field(default_factory=dict)
    plugin_id: str = "apple-podcasts"


# The CREATE TABLE statements here are deliberately a strict subset of the
# real Apple Podcasts schema (see tests/fixtures/_build_apple_podcasts_fixture.py)
# — just the columns the health check's two queries touch. Keeping the
# fixture minimal makes it obvious which columns the check depends on, and
# lets the test fail loudly if someone adds a column dependency without
# updating either the importer or this fixture.
_SCHEMA = """
CREATE TABLE ZMTPODCAST (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT);
CREATE TABLE ZMTEPISODE (
    Z_PK INTEGER PRIMARY KEY,
    ZTITLE TEXT, ZCLEANEDTITLE TEXT,
    ZPODCAST INTEGER REFERENCES ZMTPODCAST(Z_PK),
    ZPLAYCOUNT INTEGER,
    ZPLAYSTATEMANUALLYSET INTEGER,
    ZLASTDATEPLAYED REAL
);
"""


def _build_db(tmp_path: Path, rows: list[tuple]) -> Path:
    """Build a synthetic MTLibrary.sqlite with two podcasts and the
    supplied episode rows. Each row is
    (Z_PK, ZTITLE, ZPODCAST, ZPLAYCOUNT, ZPLAYSTATEMANUALLYSET, ZLASTDATEPLAYED).
    """
    db = tmp_path / "MTLibrary.sqlite"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO ZMTPODCAST VALUES (1, 'Reply All')")
    conn.execute("INSERT INTO ZMTPODCAST VALUES (2, 'Hard Fork')")
    for pk, title, podcast, play_count, manual, last_played in rows:
        conn.execute(
            "INSERT INTO ZMTEPISODE "
            "(Z_PK, ZTITLE, ZCLEANEDTITLE, ZPODCAST, ZPLAYCOUNT, "
            " ZPLAYSTATEMANUALLYSET, ZLASTDATEPLAYED) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pk, title, title, podcast, play_count, manual, last_played),
        )
    conn.commit()
    conn.close()
    return db


def test_health_check_db_present_with_played_episodes(tmp_path):
    """DB has played episodes -> ok=True, summary mentions count, preview
    lists up to 3 most-recent entries."""
    db = _build_db(tmp_path, rows=[
        # ZPLAYCOUNT > 0, manual=0, last_played set -> counted
        (10, "Crime Machine, Part I", 1, 1, 0, 769876200.0),
        (11, "AI Episode", 2, 3, 0, 769962600.0),  # newest
        (12, "Older Ep", 1, 1, 0, 769000000.0),
        # Filtered: ZPLAYCOUNT == 0
        (13, "In-Progress Ep", 1, 0, 0, 769900000.0),
        # Filtered: manually marked played
        (14, "Marked Played", 2, 1, 1, 769900000.0),
    ])
    ctx = _Ctx(config={"db_path": str(db)})

    result = apple_podcasts_health_check(ctx)

    assert result.ok is True
    assert "3" in result.summary  # only the 3 valid rows
    assert "played episode" in result.summary
    # Preview: 3 entries, ordered newest-first by ZLASTDATEPLAYED DESC
    assert len(result.preview) == 3
    assert result.preview[0]["title"].startswith("Hard Fork — AI Episode")
    assert result.preview[1]["title"].startswith("Reply All — Crime Machine")
    assert result.preview[2]["title"].startswith("Reply All — Older Ep")
    # watched_at is an ISO timestamp (Mac epoch -> Unix -> UTC)
    assert result.preview[0]["watched_at"].startswith("20")


def test_health_check_db_present_zero_played(tmp_path):
    """DB exists, schema is valid, but every episode is filtered out
    -> ok=True with a friendly nudge, not an error."""
    db = _build_db(tmp_path, rows=[
        (10, "In-Progress", 1, 0, 0, 769876200.0),  # ZPLAYCOUNT=0
        (11, "Manually Marked", 2, 1, 1, 769962600.0),  # manual=1
    ])
    ctx = _Ctx(config={"db_path": str(db)})

    result = apple_podcasts_health_check(ctx)

    assert result.ok is True
    assert "No played episodes" in result.summary
    assert result.preview == []


def test_health_check_db_missing(tmp_path, monkeypatch):
    """No DB file at the configured path and the auto-glob finds
    nothing either -> ok=False with an actionable hint mentioning
    Podcasts."""
    # Repoint the module-level glob at an empty tmp dir. (Monkeypatching
    # $HOME doesn't work — _DB_GLOB is computed at import time off of
    # Path.home(), so the constant is already baked in by the time the
    # test runs.)
    monkeypatch.setattr(
        "fulcra_media.apple_podcasts_health._DB_GLOB",
        str(tmp_path / "nope" / "MTLibrary.sqlite"),
    )
    ctx = _Ctx(config={})  # no db_path override

    result = apple_podcasts_health_check(ctx)

    assert result.ok is False
    assert "Podcasts" in result.summary
