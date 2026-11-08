"""Private v1 twin hints are scoped IDs, never unverified delivery claims."""
import stat

import pytest


DESTINATION = "Event/11111111-2222-4333-8444-555555555555"
IDENTIFIER = "11111111-2222-4333-8444-666666666666"


def test_missing_twin_index_read_does_not_create_files(tmp_path):
    from fulcra_media.collect_twins import MediaTwinIndex
    path = tmp_path / "twins.sqlite"
    assert MediaTwinIndex(path).lookup("account", DESTINATION, ["synthetic"]) == {}
    assert not path.exists()


def test_verified_twin_hints_are_private_and_scoped(tmp_path):
    from fulcra_media.collect_twins import MediaTwinIndex
    path = tmp_path / "twins.sqlite"
    index = MediaTwinIndex(path)
    index.record("account", DESTINATION, [{"id": IDENTIFIER, "timestamp_confidence": "high",
        "external_ids": {"content_fingerprint": "synthetic"}}])
    assert index.lookup("account", DESTINATION, ["synthetic"]) == {"synthetic": IDENTIFIER}
    assert index.lookup("other", DESTINATION, ["synthetic"]) == {}
    assert index.lookup("account", "Event/11111111-2222-4333-8444-777777777777", ["synthetic"]) == {}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_unknown_twin_index_is_not_a_clean_empty(tmp_path):
    from fulcra_media.collect_twins import MediaTwinIndex
    path = tmp_path / "twins.sqlite"
    path.write_bytes(b"not a database")
    path.chmod(0o600)
    with pytest.raises(Exception):
        MediaTwinIndex(path).lookup("account", DESTINATION, ["synthetic"])
