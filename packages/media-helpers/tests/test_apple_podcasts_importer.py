from pathlib import Path


from fulcra_media.importers.apple_podcasts import parse_db

FIXTURE = Path(__file__).parent / "fixtures" / "apple_podcasts_mtlibrary.sqlite"


def test_parse_db_returns_only_completed_unmanual_high_playhead():
    events = list(parse_db(FIXTURE))
    # ep 10 (Reply All) + ep 11 (Hard Fork) — 2 of 4 rows
    assert len(events) == 2
    uuids = sorted(e.external_ids["zuuid"] for e in events)
    assert uuids == ["ep-uuid-10", "ep-uuid-11"]


def test_parse_db_episode_shape():
    events = list(parse_db(FIXTURE))
    e = next(e for e in events if e.external_ids["zuuid"] == "ep-uuid-10")
    assert e.importer == "apple-podcasts"
    assert e.service == "apple-podcasts"
    assert e.category == "listened"
    assert e.note == "Reply All – The Crime Machine, Part I"
    assert e.title == "Reply All"
    # 2700s duration -> start = end - 2700s
    assert (e.end_time - e.start_time).total_seconds() == 2700
    assert e.timestamp_confidence == "medium"
    assert e.external_ids["content_fingerprint"].startswith("podcast:reply-all:")


def test_parse_db_deterministic_id_per_play_snapshot():
    """sha256(ZUUID|ZLASTDATEPLAYED) so a new last-played stamp = new event."""
    events = list(parse_db(FIXTURE))
    ids = [e.deterministic_id for e in events]
    assert len(ids) == len(set(ids))
    assert all(i.startswith("com.fulcra.media.apple-podcasts.v1.") for i in ids)


def test_parse_db_includes_play_count():
    events = list(parse_db(FIXTURE))
    e = next(e for e in events if e.external_ids["zuuid"] == "ep-uuid-10")
    # ZPLAYCOUNT defaults to NULL in the fixture (we didn't set it).
    # Should surface as 0 (NULL coerced) or the column value.
    assert "play_count" in e.external_ids


import subprocess


def test_find_timemachine_snapshots_constructs_paths(tmp_path, mocker):
    """tmutil listbackups output → list of MTLibrary.sqlite paths."""
    # Create two fake backup roots with the podcasts DB nested inside
    user_home = tmp_path / "Users" / "alice"
    podcasts_rel = "Library/Group Containers/243LU875E5.groups.com.apple.podcasts/Documents/MTLibrary.sqlite"

    backup1 = tmp_path / "Backups" / "2026-05-01"
    backup1_db = backup1 / "Users/alice" / podcasts_rel
    backup1_db.parent.mkdir(parents=True)
    backup1_db.write_text("fake1")

    backup2 = tmp_path / "Backups" / "2026-05-15"
    backup2_db = backup2 / "Macintosh HD/Users/alice" / podcasts_rel
    backup2_db.parent.mkdir(parents=True)
    backup2_db.write_text("fake2")

    backup3 = tmp_path / "Backups" / "2026-05-16"
    # backup3 has no podcasts DB — should be skipped silently

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=f"{backup1}\n{backup2}\n{backup3}\n", stderr=""
        )

    mocker.patch("subprocess.run", side_effect=fake_run)
    from fulcra_media.importers.apple_podcasts import find_timemachine_snapshots
    paths = find_timemachine_snapshots(user_home=user_home)
    assert len(paths) == 2
    assert all(p.name == "MTLibrary.sqlite" for p in paths)


def test_find_timemachine_snapshots_tmutil_missing(mocker):
    """If tmutil isn't installed, return empty list rather than crash."""
    mocker.patch("subprocess.run", side_effect=FileNotFoundError())
    from fulcra_media.importers.apple_podcasts import find_timemachine_snapshots
    assert find_timemachine_snapshots() == []
