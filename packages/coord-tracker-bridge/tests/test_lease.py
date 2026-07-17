import json

import pytest

from coord_tracker_bridge import FileLease, LeaseHeld


def test_overlapping_identical_bridge_runs_are_rejected(tmp_path):
    first = FileLease(tmp_path, "coord:fulcra", "linear:team", "hash", owner="one")
    second = FileLease(tmp_path, "coord:fulcra", "linear:team", "hash", owner="two")

    first.acquire()
    with pytest.raises(LeaseHeld, match="one"):
        second.acquire()
    first.release()
    second.acquire()
    second.release()


def test_expired_lease_is_reclaimed(tmp_path):
    lease = FileLease(tmp_path, "source", "tracker", "hash", clock=lambda: 100.0, owner="new")
    tmp_path.mkdir(exist_ok=True)
    lease.path.write_text(json.dumps({"owner": "old", "expires_at": 99.0}))

    lease.acquire()

    assert json.loads(lease.path.read_text())["owner"] == "new"
    lease.release()


def test_unreadable_lease_fails_closed(tmp_path):
    lease = FileLease(tmp_path, "source", "tracker", "hash")
    tmp_path.mkdir(exist_ok=True)
    lease.path.write_text("not json")

    with pytest.raises(LeaseHeld, match="unreadable"):
        lease.acquire()


def test_held_lease_can_refresh_without_changing_owner(tmp_path):
    now = [100.0]
    lease = FileLease(tmp_path, "source", "tracker", "hash", ttl_seconds=10, clock=lambda: now[0], owner="one")
    lease.acquire()
    now[0] = 105.0

    lease.refresh()

    assert json.loads(lease.path.read_text()) == {"owner": "one", "expires_at": 115.0}
    lease.release()
