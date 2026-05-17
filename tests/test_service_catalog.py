"""Sanity tests for the service catalog data."""
from fulcra_media.service_catalog import (
    SERVICES,
    ServiceEntry,
    categories,
    get,
    services_for_category,
)


def test_catalog_has_entries():
    assert len(SERVICES) > 0


def test_every_entry_is_serviceentry():
    assert all(isinstance(s, ServiceEntry) for s in SERVICES)


def test_keys_are_unique():
    keys = [s.key for s in SERVICES]
    assert len(keys) == len(set(keys))


def test_categories_returns_distinct_ordered():
    cats = categories()
    assert len(cats) == len(set(cats))
    # Music and video should both be present
    assert "music" in cats
    assert "video" in cats


def test_services_for_category_sorted_by_rank():
    music = services_for_category("music")
    assert len(music) > 1
    assert music[0].rank <= music[-1].rank


def test_lastfm_is_top_music_pick():
    music = services_for_category("music")
    assert music[0].key == "lastfm"
    assert music[0].pathway == "api"


def test_trakt_is_top_video_pick():
    video = services_for_category("video")
    assert video[0].key == "trakt"


def test_get_by_key():
    assert get("lastfm").label == "Last.fm"
    assert get("nonexistent") is None


def test_available_flag_present_on_every_entry():
    for s in SERVICES:
        assert isinstance(s.available, bool)


def test_deezer_is_second_music_pick():
    """Last.fm rank=1, Deezer rank=2 (also a direct API, single-account)."""
    music = services_for_category("music")
    assert music[0].key == "lastfm"
    assert music[1].key == "deezer"
    assert music[1].pathway == "api"
    assert music[1].import_cmd == "deezer"
    assert music[1].wizard == "deezer"


def test_music_ranks_are_unique_after_deezer_bump():
    """When inserting deezer at rank=2, all later music entries should bump."""
    music = services_for_category("music")
    ranks = [s.rank for s in music]
    assert ranks == sorted(ranks)
    assert len(ranks) == len(set(ranks))  # no duplicates


def test_letterboxd_is_available_with_importer_and_wizard():
    """Letterboxd flipped to available now that the RSS importer is wired up."""
    lb = get("letterboxd")
    assert lb is not None
    assert lb.available is True
    assert lb.import_cmd == "letterboxd"
    assert lb.wizard == "letterboxd"
    assert lb.pathway == "rss"
    assert lb.category == "video"


def test_video_ranks_are_unique_after_letterboxd_promotion():
    """Letterboxd takes rank=4; generic-csv-video bumped to 5."""
    video = services_for_category("video")
    ranks = [s.rank for s in video]
    assert ranks == sorted(ranks)
    assert len(ranks) == len(set(ranks))
