"""Tests for fulcra_common.cross_source_fingerprint.

These cover the category-level dedup fingerprints that two importers
producing the same listen/watch must emit identically. Per-source
source_ids carry on alongside; these tests don't touch that path.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fulcra_common.cross_source_fingerprint import (
    bucket_5min,
    listened_fingerprint,
    normalize_title,
    podcast_fingerprint,
    watched_movie_fingerprint,
    watched_tv_fingerprint,
)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

class TestNormalizeTitle:
    def test_empty_inputs(self) -> None:
        assert normalize_title("") == ""
        assert normalize_title("   ") == ""

    def test_basic_lowercase_strip(self) -> None:
        assert normalize_title("  Yellow  ") == "yellow"

    def test_strip_parenthetical_suffix(self) -> None:
        assert normalize_title("Yellow (Remastered 2011)") == "yellow"

    def test_strip_bracket_suffix(self) -> None:
        assert normalize_title("Yellow [Deluxe Edition]") == "yellow"

    def test_strip_feat_inline(self) -> None:
        assert normalize_title("Track Name feat. Some Artist") == "track name"
        assert normalize_title("Track Name ft. Other") == "track name"
        assert normalize_title("Track Name featuring Whoever") == "track name"

    def test_strip_trailing_dash_remaster(self) -> None:
        assert normalize_title("Song - Remastered") == "song"
        assert normalize_title("Song - 2011 Remaster") == "song"
        assert normalize_title("Song - Radio Edit") == "song"

    def test_iterates_nested_suffixes(self) -> None:
        # "Foo (Live) [2024]" should collapse both
        assert normalize_title("Foo (Live) [2024]") == "foo"

    def test_three_forms_of_same_track_collapse(self) -> None:
        """The core normalization promise: differently-decorated versions
        of "Yellow" from different services normalize identically so the
        downstream fingerprint matches."""
        plain = normalize_title("Yellow")
        remaster = normalize_title("Yellow (Remastered 2011)")
        feat = normalize_title("Yellow (feat. Joe)")
        assert plain == remaster == feat == "yellow"


# ---------------------------------------------------------------------------
# Time bucketing
# ---------------------------------------------------------------------------

class TestBucket5Min:
    def test_rounds_down_to_5min(self) -> None:
        dt = datetime(2026, 5, 26, 14, 37, 42, tzinfo=timezone.utc)
        assert bucket_5min(dt) == "2026-05-26T14:35:00Z"

    def test_exact_bucket_boundary_stays(self) -> None:
        dt = datetime(2026, 5, 26, 14, 35, 0, tzinfo=timezone.utc)
        assert bucket_5min(dt) == "2026-05-26T14:35:00Z"

    def test_other_tz_normalised_to_utc(self) -> None:
        tz_plus2 = timezone(timedelta(hours=2))
        dt = datetime(2026, 5, 26, 16, 37, 42, tzinfo=tz_plus2)  # 14:37:42 UTC
        assert bucket_5min(dt) == "2026-05-26T14:35:00Z"


# ---------------------------------------------------------------------------
# listened_fingerprint
# ---------------------------------------------------------------------------

class TestListenedFingerprint:
    def test_returns_namespaced_id(self) -> None:
        ts = datetime(2026, 5, 26, 14, 35, tzinfo=timezone.utc)
        fp = listened_fingerprint(timestamp=ts, artist="Coldplay", track="Yellow")
        assert fp is not None
        assert fp.startswith("com.fulcra.content.listened.v1.")
        # 16 hex chars after the prefix
        assert len(fp.split(".")[-1]) == 16

    def test_same_listen_two_sources_same_bucket(self) -> None:
        """Two-source same-event: timestamps within the same 5-min bucket
        produce identical fingerprints."""
        ts1 = datetime(2026, 5, 26, 14, 35, 12, tzinfo=timezone.utc)
        ts2 = datetime(2026, 5, 26, 14, 37, 5, tzinfo=timezone.utc)
        fp1 = listened_fingerprint(timestamp=ts1, artist="Coldplay", track="Yellow")
        fp2 = listened_fingerprint(timestamp=ts2, artist="Coldplay", track="Yellow")
        assert fp1 == fp2

    def test_bucket_boundary_differs(self) -> None:
        """Boundary case: 14:34:00 and 14:35:00 are in different 5-minute
        buckets (14:30-14:35 vs 14:35-14:40) so they produce different
        fingerprints. This is the unavoidable cost of any time-bucketed
        scheme — flagged as acceptable in the module docstring."""
        ts1 = datetime(2026, 5, 26, 14, 34, 0, tzinfo=timezone.utc)
        ts2 = datetime(2026, 5, 26, 14, 35, 0, tzinfo=timezone.utc)
        fp1 = listened_fingerprint(timestamp=ts1, artist="Coldplay", track="Yellow")
        fp2 = listened_fingerprint(timestamp=ts2, artist="Coldplay", track="Yellow")
        assert fp1 != fp2

    def test_remaster_variants_collide(self) -> None:
        """The normalization promise end-to-end: same time, same artist,
        differently-decorated track titles still match."""
        ts = datetime(2026, 5, 26, 14, 35, tzinfo=timezone.utc)
        fp_plain = listened_fingerprint(timestamp=ts, artist="Coldplay", track="Yellow")
        fp_remaster = listened_fingerprint(
            timestamp=ts, artist="Coldplay", track="Yellow (Remastered 2011)"
        )
        fp_feat = listened_fingerprint(
            timestamp=ts, artist="Coldplay", track="Yellow (feat. Whoever)"
        )
        assert fp_plain == fp_remaster == fp_feat

    def test_missing_track_returns_none(self) -> None:
        ts = datetime(2026, 5, 26, 14, 35, tzinfo=timezone.utc)
        assert listened_fingerprint(timestamp=ts, artist="X", track="") is None
        assert listened_fingerprint(timestamp=ts, artist="X", track="   ") is None

    def test_missing_artist_still_emits(self) -> None:
        """Apple Music drops artist on ~32% of rows — we still emit a
        fingerprint on (time, track) so two artist-less sources can
        dedupe against each other."""
        ts = datetime(2026, 5, 26, 14, 35, tzinfo=timezone.utc)
        fp = listened_fingerprint(timestamp=ts, artist="", track="Yellow")
        assert fp is not None and fp.startswith("com.fulcra.content.listened.v1.")


# ---------------------------------------------------------------------------
# watched_tv_fingerprint
# ---------------------------------------------------------------------------

class TestWatchedTvFingerprint:
    def test_basic_emit(self) -> None:
        ts = datetime(2026, 5, 26, 20, 0, tzinfo=timezone.utc)
        fp = watched_tv_fingerprint(timestamp=ts, show="Severance", season=2, episode=5)
        assert fp is not None
        assert fp.startswith("com.fulcra.content.watched.v1.")

    def test_int_vs_string_season_episode_match(self) -> None:
        ts = datetime(2026, 5, 26, 20, 0, tzinfo=timezone.utc)
        fp_int = watched_tv_fingerprint(timestamp=ts, show="Severance", season=2, episode=5)
        fp_str = watched_tv_fingerprint(timestamp=ts, show="Severance", season="2", episode="5")
        assert fp_int == fp_str

    def test_missing_show_returns_none(self) -> None:
        ts = datetime(2026, 5, 26, 20, 0, tzinfo=timezone.utc)
        assert watched_tv_fingerprint(timestamp=ts, show="", season=1, episode=1) is None

    def test_missing_season_or_episode_returns_none(self) -> None:
        ts = datetime(2026, 5, 26, 20, 0, tzinfo=timezone.utc)
        assert watched_tv_fingerprint(timestamp=ts, show="X", season=None, episode=1) is None
        assert watched_tv_fingerprint(timestamp=ts, show="X", season=1, episode=None) is None
        assert watched_tv_fingerprint(timestamp=ts, show="X", season="", episode=1) is None


# ---------------------------------------------------------------------------
# watched_movie_fingerprint
# ---------------------------------------------------------------------------

class TestWatchedMovieFingerprint:
    def test_basic_emit(self) -> None:
        ts = datetime(2026, 5, 26, 20, 0, tzinfo=timezone.utc)
        fp = watched_movie_fingerprint(timestamp=ts, title="Dune Part Two")
        assert fp is not None
        assert fp.startswith("com.fulcra.content.watched.v1.")

    def test_missing_title_returns_none(self) -> None:
        ts = datetime(2026, 5, 26, 20, 0, tzinfo=timezone.utc)
        assert watched_movie_fingerprint(timestamp=ts, title="") is None


class TestTvVsMovieDistinction:
    """A TV episode and a movie with the same title-at-bucket must NOT
    collide. The payload prefix (``watched_tv|`` vs ``watched_movie|``)
    keeps the hash inputs distinct even though both surface under
    ``com.fulcra.content.watched.v1.<hash>``."""

    def test_tv_and_movie_differ(self) -> None:
        ts = datetime(2026, 5, 26, 20, 0, tzinfo=timezone.utc)
        tv = watched_tv_fingerprint(
            timestamp=ts, show="Foundation", season=2, episode=5
        )
        movie = watched_movie_fingerprint(timestamp=ts, title="Foundation")
        assert tv is not None and movie is not None
        assert tv != movie


# ---------------------------------------------------------------------------
# podcast_fingerprint
# ---------------------------------------------------------------------------

class TestPodcastFingerprint:
    def test_basic_emit(self) -> None:
        ts = datetime(2026, 5, 26, 7, 0, tzinfo=timezone.utc)
        fp = podcast_fingerprint(
            timestamp=ts, show="The Daily", episode="The Iowa Caucus"
        )
        assert fp is not None
        assert fp.startswith("com.fulcra.content.podcast.v1.")

    def test_missing_inputs_return_none(self) -> None:
        ts = datetime(2026, 5, 26, 7, 0, tzinfo=timezone.utc)
        assert podcast_fingerprint(timestamp=ts, show="", episode="X") is None
        assert podcast_fingerprint(timestamp=ts, show="X", episode="") is None


# ---------------------------------------------------------------------------
# None timestamp guard (parametrised because every fingerprint must agree)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "call",
    [
        lambda: listened_fingerprint(timestamp=None, artist="A", track="T"),  # type: ignore[arg-type]
        lambda: watched_tv_fingerprint(timestamp=None, show="S", season=1, episode=1),  # type: ignore[arg-type]
        lambda: watched_movie_fingerprint(timestamp=None, title="M"),  # type: ignore[arg-type]
        lambda: podcast_fingerprint(timestamp=None, show="S", episode="E"),  # type: ignore[arg-type]
    ],
)
def test_none_timestamp_returns_none(call) -> None:
    assert call() is None
