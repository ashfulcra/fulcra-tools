import pytest

from fulcra_media.importers.base import content_fingerprint, _slugify


def test_slugify_basic():
    assert _slugify("Stranger Things") == "stranger-things"
    assert _slugify("Dune: Part Two") == "dune-part-two"
    assert _slugify("  Multiple    Spaces  ") == "multiple-spaces"


def test_slugify_strips_special_chars():
    assert _slugify("Should I Marry A Murderer?!") == "should-i-marry-a-murderer"
    assert _slugify("Sci-Fi & Fantasy") == "sci-fi-fantasy"


def test_fingerprint_tv_episode():
    fp = content_fingerprint("tv", show="Severance", season=2, episode=1)
    assert fp == "tv:severance:s02e01"


def test_fingerprint_movie_with_year():
    fp = content_fingerprint("movie", title="Dune: Part Two", year=2024)
    assert fp == "movie:dune-part-two:y2024"


def test_fingerprint_movie_no_year():
    fp = content_fingerprint("movie", title="Dune: Part Two")
    assert fp == "movie:dune-part-two"


def test_fingerprint_music_track():
    fp = content_fingerprint("music", artist="Daft Punk", track="Get Lucky")
    assert fp == "music:daft-punk:get-lucky"


def test_fingerprint_podcast_episode_by_guid():
    fp = content_fingerprint("podcast", show="Reply All", guid="abc-123")
    assert fp == "podcast:reply-all:abc-123"


def test_fingerprint_podcast_episode_by_title():
    fp = content_fingerprint("podcast", show="Reply All", title="The Crime Machine, Part I")
    assert fp == "podcast:reply-all:the-crime-machine-part-i"


def test_fingerprint_unknown_kind_raises():
    with pytest.raises(ValueError):
        content_fingerprint("nope", title="x")
