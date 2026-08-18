"""Mesh addressing: `to_user` rides alongside `to`, and a bus-v3 reader that
has never heard of the mesh still sees a well-formed v1 note."""
import json

import pytest

from coord_mesh import envelope, safety

UID = "a24a9667-c2c6-4bbf-9a0f-36ea0afcb521"
MINE = "d64bbe9b-4902-42e9-a607-7db51ebc6379"


def test_build_carries_both_to_and_to_user():
    n = envelope.build(to_user=UID, kind="response", slug="mesh-m2")
    assert n["v"] == 1 and n["to_user"] == UID
    assert n["to"] == envelope.BROADCAST   # agent-plane address still present


def test_envelope_stays_readable_to_a_bus_v3_reader():
    """The v1 keys a non-mesh reader needs are all present and unchanged."""
    n = envelope.build(to_user=UID, kind="claim", slug="s", to="coord-boss")
    for k in ("v", "to", "kind", "pri", "slug"):
        assert k in n
    assert n["to"] == "coord-boss"


def test_ptr_included_only_when_given():
    assert "ptr" not in envelope.build(to_user=UID, kind="claim", slug="s")
    n = envelope.build(to_user=UID, kind="response", slug="s", ptr="reports/x.md")
    assert n["ptr"] == "reports/x.md"


def test_to_user_goes_through_the_named_uid_rail():
    """An event addressed at a name is delivered to nobody and looks sent."""
    with pytest.raises(safety.SafetyViolation):
        envelope.build(to_user="michael", kind="claim", slug="s")


@pytest.mark.parametrize("kind", ["chat", "", "Directive"])
def test_bad_kind_refused(kind):
    with pytest.raises(ValueError):
        envelope.build(to_user=UID, kind=kind, slug="s")


def test_missing_slug_refused():
    with pytest.raises(ValueError, match="slug"):
        envelope.build(to_user=UID, kind="claim", slug="  ")


def test_encode_is_a_string_matching_the_wire_contract():
    s = envelope.encode(envelope.build(to_user=UID, kind="claim", slug="s"))
    assert isinstance(s, str)
    assert json.loads(s)["to_user"] == UID


def test_parse_roundtrips():
    n = envelope.build(to_user=UID, kind="claim", slug="s")
    assert envelope.parse(envelope.encode(n)) == n


@pytest.mark.parametrize("junk", [None, "", "not json", "[]", '{"v":2}', '"str"'])
def test_parse_returns_none_for_non_v1_traffic(junk):
    """Legacy prose notes share the channel; skipping them must not raise."""
    assert envelope.parse(junk) is None


def test_addressed_to_matches_my_uid_and_broadcast():
    assert envelope.addressed_to({"to_user": MINE}, MINE)
    assert envelope.addressed_to({"to_user": envelope.BROADCAST}, MINE)
    assert not envelope.addressed_to({"to_user": UID}, MINE)


def test_note_without_to_user_is_not_mine_to_consume():
    """A peer's same-account bus traffic is NOT mesh traffic. Treating it as
    addressed would make every peer poll swallow that peer's internal bus."""
    assert not envelope.addressed_to({"to": "all", "kind": "claim"}, MINE)
    assert not envelope.addressed_to({"to_user": ""}, MINE)
