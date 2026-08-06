"""The five-section handoff gate.

The load-bearing test in this file is
`test_artifact_pointing_at_a_missing_anchor_is_refused`. That is the real
incident: coord-boss's 2026-08-06 takeover resumed a checkpoint whose artifact
list pointed at a "Cold start" section of an agent doc that did not exist on
main. Every section was PRESENT. A presence check passes that handoff and the
successor still wakes up with nothing — which is why the artifacts rule is a live
resolve and why a test suite that only checked section presence would certify the
exact bug it was written after.

The other theme is what the gate must NOT do: routine `snapshot` keeps today's
behavior (PR 535 doctrine), an agent with genuinely no open questions must be
able to say so without inventing one, and nothing is written when validation
fails.
"""
from __future__ import annotations

import pytest

from coord_engine import handoff


GOOD = {
    "objective": ("coord-opus-worker, build lane. Engine v1.11.0 matches fleet "
                  "pin f4261da5b33e; suite 1758 green at aff8138."),
    "decisions": [
        "dual-green merge authority granted, expires 2026-08-09 — one reviewer "
        "plus CI, because serial review stalled the queue for two days",
        "push in the same wake or treat as not done — standing law, the "
        "snapshot rollback eats local commits",
    ],
    "next_actions": [
        "land docs/coord/agents/coord-opus-worker.md Cold start (87c7eb8)",
        "triage the 68 needs-me items for live vs stale",
    ],
    "open_questions": [
        "does build-lane need a role doc — owner coord-boss",
    ],
    "artifacts": [
        "docs/coord/CHECKPOINT-HANDOFF.md",
        "docs/coord/agents/coord-opus-worker.md (Cold start section)",
    ],
}


def resolver_for(files):
    """A resolver over an in-memory {path: text} map."""
    def _resolve(art):
        text = files.get(art.path)
        if text is None:
            return False, "not found in the store or the working tree"
        if art.anchor and not handoff._anchor_present(text, art.anchor):
            return False, f"exists but has no section matching {art.anchor!r}"
        return True, f"fake:{art.path}"
    return _resolve


FILES = {
    "docs/coord/CHECKPOINT-HANDOFF.md": "# Checkpoint handoff standard\n",
    "docs/coord/agents/coord-opus-worker.md": "# Harness\n\n## Cold start\n\nread this\n",
}


# --- the incident ----------------------------------------------------------

def test_artifact_pointing_at_a_missing_anchor_is_refused():
    """THE regression. The file exists; the named section does not."""
    files = dict(FILES)
    files["docs/coord/agents/coord-opus-worker.md"] = "# Harness\n\n## Identity\n"
    findings = handoff.validate(GOOD, resolve=resolver_for(files))
    assert [f.section for f in findings] == ["artifacts"]
    assert "Cold start" in findings[0].detail


def test_a_present_but_unresolvable_artifact_is_refused():
    findings = handoff.validate(
        {**GOOD, "artifacts": ["docs/coord/DOES-NOT-EXIST.md"]},
        resolve=resolver_for(FILES))
    assert [f.section for f in findings] == ["artifacts"]
    assert "does not resolve" in findings[0].detail


def test_missing_resolver_is_UNKNOWN_not_a_pass():
    """A gate that skips its own live check when it cannot run it would ship
    the false-pass class this standard exists to close."""
    findings = handoff.validate(GOOD, resolve=None)
    assert [f.section for f in findings] == ["artifacts", "artifacts"]
    assert all("NOT VERIFIED" in f.detail for f in findings)


def test_a_fully_resolvable_handoff_passes():
    assert handoff.validate(GOOD, resolve=resolver_for(FILES)) == []


# --- anchors ---------------------------------------------------------------

@pytest.mark.parametrize("ref,path,anchor", [
    ("docs/a.md (Cold start section)", "docs/a.md", "Cold start section"),
    ("docs/a.md#cold-start", "docs/a.md", "cold-start"),
    ("docs/a.md", "docs/a.md", None),
    ("team/fulcra/_coord/x.json", "team/fulcra/_coord/x.json", None),
])
def test_artifact_reference_forms(ref, path, anchor):
    art = handoff.parse_artifact(ref)
    assert (art.path, art.anchor) == (path, anchor)


def test_anchor_matching_ignores_the_word_section_and_punctuation():
    """A reading list says "Cold start section"; the doc says "## Cold start".
    Failing a handoff over that difference is a gate nobody would trust."""
    doc = "# Doc\n\n## Cold start\n"
    assert handoff._anchor_present(doc, "Cold start section")
    assert handoff._anchor_present(doc, "cold-start")


def test_anchor_must_be_a_heading_not_just_body_text():
    doc = "# Doc\n\nSee the Cold start notes elsewhere.\n"
    assert not handoff._anchor_present(doc, "Cold start")


@pytest.mark.parametrize("bad", [None, "", "   ", 42, "()"])
def test_unusable_artifact_references_are_rejected(bad):
    assert handoff.parse_artifact(bad) is None


# --- decisions -------------------------------------------------------------

def test_decision_without_an_expiry_is_refused():
    findings = handoff.validate(
        {**GOOD, "decisions": ["dual-green merge authority is granted to me"]},
        resolve=resolver_for(FILES))
    assert [f.section for f in findings] == ["decisions"]
    assert "no expiry" in findings[0].detail


def test_standing_law_may_declare_permanence_instead_of_a_date():
    """Forcing a fake expiry onto standing doctrine would be worse than no
    gate — the field would be filled and the signal gone."""
    findings = handoff.validate(
        {**GOOD, "decisions": ["codex twins are pull-based and off the router "
                               "— standing law, they self-schedule"]},
        resolve=resolver_for(FILES))
    assert findings == []


def test_a_bare_labelled_decision_with_a_date_still_needs_rationale():
    findings = handoff.validate(
        {**GOOD, "decisions": ["ok 2026-08-09"]}, resolve=resolver_for(FILES))
    assert [f.section for f in findings] == ["decisions"]
    assert "no rationale" in findings[0].detail


def test_one_bad_decision_yields_one_finding_not_two():
    """A bare label with no expiry has one root problem; reporting it twice
    would teach the reader the gate is noisy."""
    findings = handoff.validate({**GOOD, "decisions": ["ok"]},
                                resolve=resolver_for(FILES))
    assert len(findings) == 1


# --- next actions ----------------------------------------------------------

def test_next_action_without_an_identifier_is_refused():
    findings = handoff.validate(
        {**GOOD, "next_actions": ["finish the thing we discussed"]},
        resolve=resolver_for(FILES))
    assert [f.section for f in findings] == ["next_actions"]
    assert "no identifier" in findings[0].detail


@pytest.mark.parametrize("action", [
    "land PR 532",
    "resume task build-handoff-validation-for-continuity-park",
    "read docs/coord/CHECKPOINT-HANDOFF.md",
    "verify head aec9a3a405523605fb9e404b7484650ecdc148fb",
])
def test_identifiers_that_make_an_action_startable(action):
    assert handoff.validate({**GOOD, "next_actions": [action]},
                            resolve=resolver_for(FILES)) == []


# --- open questions --------------------------------------------------------

def test_open_question_without_an_owner_is_refused():
    findings = handoff.validate(
        {**GOOD, "open_questions": ["not sure what to do about the cursor"]},
        resolve=resolver_for(FILES))
    assert [f.section for f in findings] == ["open_questions"]
    assert "no owner" in findings[0].detail


@pytest.mark.parametrize("none_form", ["none", "None.", "n/a", "nothing open"])
def test_an_agent_with_no_open_questions_may_say_so(none_form):
    """Requiring a non-empty list would buy a filled field and lose the signal:
    an agent with nothing undecided would invent something."""
    assert handoff.validate({**GOOD, "open_questions": [none_form]},
                            resolve=resolver_for(FILES)) == []


def test_an_empty_open_questions_list_is_still_refused():
    """Empty cannot be told apart from forgotten — that is why 'none' must be
    written out."""
    findings = handoff.validate({**GOOD, "open_questions": []},
                                resolve=resolver_for(FILES))
    assert [f.section for f in findings] == ["open_questions"]


# --- objective -------------------------------------------------------------

@pytest.mark.parametrize("objective", [None, "", "   ", "done"])
def test_thin_or_absent_objective_is_refused(objective):
    findings = handoff.validate({**GOOD, "objective": objective},
                                resolve=resolver_for(FILES))
    assert [f.section for f in findings] == ["objective"]


# --- reporting -------------------------------------------------------------

def test_all_findings_are_reported_not_just_the_first():
    """Naming one missing section at a time turns fixing a handoff into a
    guessing game at exactly the moment the author is out of context."""
    findings = handoff.validate({"objective": ""}, resolve=None)
    assert {f.section for f in findings} == set(handoff.REQUIRED_SECTIONS)


def test_findings_are_ordered_by_section_so_a_reader_fixes_top_down():
    msg = handoff.format_findings(handoff.validate({"objective": ""}, resolve=None))
    positions = [msg.index(f"[{s}]") for s in handoff.REQUIRED_SECTIONS]
    assert positions == sorted(positions)


def test_the_refusal_message_says_nothing_was_written():
    msg = handoff.format_findings(handoff.validate({"objective": ""}, resolve=None))
    assert "NOTHING WAS WRITTEN" in msg
    assert "continuity snapshot" in msg  # points at the routine tier


def test_a_passing_handoff_produces_no_message():
    assert handoff.format_findings([]) == ""


def test_a_non_document_snapshot_fails_every_section():
    assert {f.section for f in handoff.validate(None)} == set(
        handoff.REQUIRED_SECTIONS)


# --- the store resolver ----------------------------------------------------

class FakeTransport:
    def __init__(self, docs):
        self.docs = docs
        self.reads: list[str] = []

    def read(self, path):
        self.reads.append(path)
        return self.docs.get(path)


def test_store_resolver_tries_the_team_prefix_for_a_relative_path():
    t = FakeTransport({"team/fulcra/_coord/x.md": "# X\n"})
    ok, where = handoff.store_resolver(t, "fulcra")(
        handoff.parse_artifact("_coord/x.md"))
    assert ok and where == "store:team/fulcra/_coord/x.md"


def test_store_resolver_does_not_double_prefix_an_absolute_store_path():
    t = FakeTransport({"team/fulcra/_coord/x.md": "# X\n"})
    handoff.store_resolver(t, "fulcra")(
        handoff.parse_artifact("team/fulcra/_coord/x.md"))
    assert t.reads == ["team/fulcra/_coord/x.md"]


def test_store_resolver_checks_the_anchor_in_the_resolved_document():
    t = FakeTransport({"team/fulcra/a.md": "# A\n\n## Identity\n"})
    ok, reason = handoff.store_resolver(t, "fulcra")(
        handoff.parse_artifact("a.md (Cold start section)"))
    assert not ok and "no section matching" in reason


def test_store_resolver_falls_back_to_the_working_tree(tmp_path, monkeypatch):
    doc = tmp_path / "handoff.md"
    doc.write_text("# Doc\n\n## Cold start\n")
    monkeypatch.chdir(tmp_path)
    ok, where = handoff.store_resolver(FakeTransport({}), "fulcra")(
        handoff.parse_artifact("handoff.md (Cold start)"))
    assert ok and where == "repo:handoff.md"


def test_store_resolver_treats_a_raising_transport_as_unresolved():
    class Boom:
        def read(self, path):
            raise RuntimeError("store down")

    ok, reason = handoff.store_resolver(Boom(), "fulcra")(
        handoff.parse_artifact("_coord/x.md"))
    assert not ok and "not found" in reason
