"""Engagement lifecycle over the transport."""

import pytest

from fde_engine import engagement, model
from fde_engine.transport import TransportError
from fde_engine_test_helpers import FakeTransport

NOW = "2026-07-08T17:00:00Z"
LATER = "2026-07-08T18:00:00Z"


def test_init_writes_engagement_doc_at_canonical_path():
    t = FakeTransport()
    meta = engagement.init_engagement(t, "sourdough-coach", "Sourdough Coach", now=NOW)
    assert meta["phase"] == "intake"
    assert meta["phase_history"] == [f"intake {NOW}"]
    stored = t.read("fde/engagements/sourdough-coach/engagement.md")
    assert model.parse_engagement(stored) == meta


def test_init_refuses_to_clobber_an_existing_engagement():
    t = FakeTransport()
    engagement.init_engagement(t, "x", "X", now=NOW)
    with pytest.raises(engagement.EngagementError):
        engagement.init_engagement(t, "x", "X again", now=LATER)


def test_load_returns_none_for_missing_engagement():
    assert engagement.load_engagement(FakeTransport(), "ghost") is None


def test_set_phase_validates_and_records_history():
    t = FakeTransport()
    engagement.init_engagement(t, "x", "X", now=NOW)
    meta = engagement.set_phase(t, "x", "interview", now=LATER)
    assert meta["phase"] == "interview"
    assert meta["updated_at"] == LATER
    assert meta["phase_history"][-1] == f"interview {LATER}"
    # persisted, not just returned
    assert engagement.load_engagement(t, "x")["phase"] == "interview"


def test_set_phase_rejects_invalid_transition():
    t = FakeTransport()
    engagement.init_engagement(t, "x", "X", now=NOW)
    with pytest.raises(engagement.EngagementError):
        engagement.set_phase(t, "x", "build", now=LATER)


def test_set_phase_rejects_missing_engagement():
    with pytest.raises(engagement.EngagementError):
        engagement.set_phase(FakeTransport(), "ghost", "interview", now=NOW)


def test_status_folds_meta_artifacts_and_hint():
    t = FakeTransport()
    engagement.init_engagement(t, "x", "X", now=NOW)
    t.write("fde/engagements/x/intake/brief.md", "the brief")
    st = engagement.status(t, "x")
    assert st["slug"] == "x" and st["phase"] == "intake"
    assert st["artifacts"]["intake/brief.md"] is True
    assert st["artifacts"]["architecture.md"] is False
    assert "interview" in st["next"]  # hint points at the next move


# --- Bug #4: next-hint must track artifact completion, not just the phase ---


def test_next_hint_says_produce_while_phase_artifacts_incomplete():
    t = FakeTransport()
    engagement.init_engagement(t, "x", "X", now=NOW)
    engagement.set_phase(t, "x", "interview", now=LATER)
    # interview needs plan.md AND findings.md; write only one
    t.write("fde/engagements/x/interview/plan.md", "topics")
    st = engagement.status(t, "x")
    assert "write interview/findings.md" in st["next"] or "interview/plan.md" in st["next"]
    # still in "produce" mode — not telling the user to advance yet
    assert "phase x architecture" not in st["next"]


def test_next_hint_says_advance_once_phase_artifacts_complete():
    t = FakeTransport()
    engagement.init_engagement(t, "x", "X", now=NOW)
    engagement.set_phase(t, "x", "interview", now=LATER)
    t.write("fde/engagements/x/interview/plan.md", "topics")
    t.write("fde/engagements/x/interview/findings.md", "findings")
    st = engagement.status(t, "x")
    # both artifacts present -> advance hint (distinct from the produce hint):
    # leads with advancing, names the transition command, doesn't re-ask for
    # artifacts already written.
    assert "advance to architecture" in st["next"]
    assert "in place" in st["next"]
    assert "fde-engine phase <slug> architecture" in st["next"]


def test_prototype_complete_hint_surfaces_the_user_gate():
    t = FakeTransport()
    engagement.init_engagement(t, "x", "X", now=NOW)
    for i, ph in enumerate(["interview", "architecture", "plan", "prototype"], start=1):
        engagement.set_phase(t, "x", ph, now=f"2026-07-09T0{i}:00:00Z")
    t.write("fde/engagements/x/prototype/verification.md", "1. real-data: PASS")
    st = engagement.status(t, "x")
    nxt = st["next"].lower()
    assert "gate" in nxt or ("build" in nxt and "iterate" in nxt)


def test_status_raises_for_missing_engagement():
    with pytest.raises(engagement.EngagementError):
        engagement.status(FakeTransport(), "ghost")


def test_list_engagements_returns_slug_title_phase_sorted():
    t = FakeTransport()
    engagement.init_engagement(t, "beta", "Beta", now=NOW)
    engagement.init_engagement(t, "alpha", "Alpha", now=NOW)
    engagement.set_phase(t, "alpha", "interview", now=LATER)
    rows = engagement.list_engagements(t)
    assert [(r["slug"], r["phase"]) for r in rows] == [
        ("alpha", "interview"), ("beta", "intake"),
    ]


def test_list_skips_directories_without_a_valid_engagement_doc():
    t = FakeTransport()
    engagement.init_engagement(t, "real", "Real", now=NOW)
    t.write("fde/engagements/junk/notes.md", "not an engagement")
    assert [r["slug"] for r in engagement.list_engagements(t)] == ["real"]


# --- Fix C2: slug validation at the remote_path chokepoint ----------------


def test_init_rejects_path_traversal_slug():
    t = FakeTransport()
    with pytest.raises(engagement.EngagementError, match="invalid slug"):
        engagement.init_engagement(t, "../../etc/whatever", "X", now=NOW)
    # nothing was written anywhere -- the traversal never reached transport.write
    assert t.files == {}


def test_remote_path_rejects_invalid_slug_directly():
    with pytest.raises(engagement.EngagementError, match="invalid slug"):
        engagement.remote_path("../evil", "engagement.md")


def test_init_suggests_the_slugified_form_in_the_error():
    t = FakeTransport()
    with pytest.raises(engagement.EngagementError, match="evil"):
        engagement.init_engagement(t, "../evil", "X", now=NOW)


def test_init_with_a_valid_hyphenated_slug_still_works():
    t = FakeTransport()
    meta = engagement.init_engagement(t, "sourdough-coach-2", "X", now=NOW)
    assert meta["slug"] == "sourdough-coach-2"


# --- Fix I1: corrupt engagement.md must not read as "no engagement" -------


def _write_corrupt_doc(t, slug="x"):
    t.write(f"fde/engagements/{slug}/engagement.md", "not even frontmatter, just garbage")


def test_init_refuses_to_overwrite_a_corrupt_engagement_doc():
    t = FakeTransport()
    _write_corrupt_doc(t)
    with pytest.raises(engagement.EngagementError, match="does not parse"):
        engagement.init_engagement(t, "x", "X", now=NOW)
    # refused to overwrite -> content is untouched
    assert t.read("fde/engagements/x/engagement.md") == "not even frontmatter, just garbage"


def test_init_corrupt_doc_error_mentions_refusing_to_overwrite():
    t = FakeTransport()
    _write_corrupt_doc(t)
    with pytest.raises(engagement.EngagementError, match="refusing to overwrite"):
        engagement.init_engagement(t, "x", "X", now=NOW)


def test_status_raises_distinct_error_for_corrupt_engagement_doc():
    t = FakeTransport()
    _write_corrupt_doc(t)
    with pytest.raises(engagement.EngagementError, match="does not parse"):
        engagement.status(t, "x")


def test_set_phase_raises_distinct_error_for_corrupt_engagement_doc():
    t = FakeTransport()
    _write_corrupt_doc(t)
    with pytest.raises(engagement.EngagementError, match="does not parse"):
        engagement.set_phase(t, "x", "interview", now=NOW)


# --- Fix I4: unreachable store must not read as "no engagement" -----------


class UnreachableTransport(FakeTransport):
    """read() behaves like "not found" everywhere (as an expired-auth /
    offline backend would), but list_dir() raises -- the store itself is
    unreachable, not merely missing this one document."""

    def list_dir(self, prefix):
        raise TransportError("list failed: store unreachable")


def test_status_raises_transport_error_when_store_unreachable():
    t = UnreachableTransport()
    with pytest.raises(TransportError):
        engagement.status(t, "ghost")


def test_set_phase_raises_transport_error_when_store_unreachable():
    t = UnreachableTransport()
    with pytest.raises(TransportError):
        engagement.set_phase(t, "ghost", "interview", now=NOW)
