"""Registry + cursors. Cursors live in MY store and a bad write must not
silently replay a peer's whole outbox."""
import json
import os

import pytest

from coord_mesh import peers

UID = "a24a9667-c2c6-4bbf-9a0f-36ea0afcb521"


def test_missing_file_is_an_empty_registry(tmp_path):
    reg = peers.load(str(tmp_path / "nope.json"))
    assert reg["spaces"] == {}


def test_corrupt_file_raises_rather_than_resetting(tmp_path):
    """Starting from empty on a corrupt registry would reset every cursor and
    replay every peer's outbox — louder is safer."""
    p = tmp_path / "peers.json"
    p.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(ValueError):
        peers.load(str(p))


def test_non_registry_document_raises(tmp_path):
    p = tmp_path / "peers.json"
    p.write_text(json.dumps({"something": "else"}), encoding="utf-8")
    with pytest.raises(ValueError, match="not a registry"):
        peers.load(str(p))


def test_save_then_load_roundtrips(tmp_path):
    p = str(tmp_path / "d" / "peers.json")
    reg = peers._empty()
    peers.upsert_space(reg, "sp1", name="michael", members=[UID])
    peers.set_cursor(reg, "sp1", UID, "rec-1")
    peers.save(reg, p)
    assert peers.get_cursor(peers.load(p), "sp1", UID) == "rec-1"


def test_save_is_atomic_leaving_no_tmp_behind(tmp_path):
    p = str(tmp_path / "peers.json")
    peers.save(peers._empty(), p)
    assert os.path.exists(p)
    assert not os.path.exists(p + ".tmp")


def test_space_kind_defaults_to_pair_and_group_is_accepted():
    """The abstraction rule: groups slot in beside pairs without a rewrite."""
    reg = peers._empty()
    assert peers.upsert_space(reg, "a")["kind"] == peers.KIND_PAIR
    assert peers.upsert_space(reg, "b", kind=peers.KIND_GROUP)["kind"] == peers.KIND_GROUP


def test_unknown_space_kind_refused():
    with pytest.raises(ValueError):
        peers.upsert_space(peers._empty(), "a", kind="broadcast")


def test_cursor_is_none_for_an_unknown_space_or_peer():
    reg = peers._empty()
    assert peers.get_cursor(reg, "nope", UID) is None
    peers.upsert_space(reg, "sp1")
    assert peers.get_cursor(reg, "sp1", UID) is None


@pytest.mark.parametrize("bad", [None, ""])
def test_empty_cursor_write_is_refused(bad):
    """An unidentifiable record leaves the position UNKNOWN; writing it as the
    cursor would reset the peer to 'never read'."""
    reg = peers._empty()
    with pytest.raises(ValueError, match="empty cursor"):
        peers.set_cursor(reg, "sp1", UID, bad)


def test_cursors_are_per_peer_within_a_space():
    reg = peers._empty()
    other = "315c1b32-5399-40e1-b808-2346da7bf32e"
    peers.set_cursor(reg, "sp1", UID, "r1")
    peers.set_cursor(reg, "sp1", other, "r9")
    assert peers.get_cursor(reg, "sp1", UID) == "r1"
    assert peers.get_cursor(reg, "sp1", other) == "r9"


def test_registry_path_honors_the_env_override(monkeypatch):
    monkeypatch.setenv("COORD_MESH_PEERS", "/tmp/x/peers.json")
    assert peers.registry_path() == "/tmp/x/peers.json"
