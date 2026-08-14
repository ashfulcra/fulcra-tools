"""Every continuity SAVE casts a shadow on the timeline; no save may depend on it.

Two invariants, and they pull in opposite directions on purpose:

1. **Visibility.** ``continuity snapshot`` and ``continuity park`` each emit one
   moment to the checkpoint channel, carrying the same four-dimension identity
   tags a bus event does, so a person watching the Fulcra timeline sees agents
   checkpointing. Reads (``resume``) emit nothing — a read is not a save.
2. **Fail-open.** The checkpoint FILE is the source of truth and the moment is
   its shadow. Absent config, malformed config, unreadable store, refused
   record write — every one of them still writes the checkpoint and still
   returns the same exit code. This is deliberately the inverse of park's
   loud ``CHECKPOINT NOT WRITTEN`` rule, which governs the file itself.
"""
from __future__ import annotations

import json

import pytest

from coord_engine import bus_tags, checkpoint_channel, cli, records
from coord_engine_test_helpers import FakeTransport

TEAM = "r"

BASE = "cb951ecb-f21c-4aee-826e-2cb0b12517d6"
AGENT = "0913d5df-830c-458e-b40a-0a04eafaa5cd"
PLATFORM = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f"
HARNESS = "2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f60"
MODEL = "3e4f5a6b-7c8d-4e9f-a0b1-2c3d4e5f6071"
FULL = {"agent": AGENT, "platform": PLATFORM, "harness": HARNESS,
        "model": MODEL}

#: The live channel (operator-provisioned 2026-08-04), spec set, separate from
#: the events channel.
CHECKPOINT_TYPE = "MomentAnnotation/a09350b2-e245-4348-ae63-bfb35c712c49"


class RecordingTransport(FakeTransport):
    """FakeTransport that captures ``record_write`` calls."""

    def __init__(self, record_ok=True, record_raises=False):
        super().__init__()
        self.record_ok = record_ok
        self.record_raises = record_raises
        self.records_written: list[dict] = []

    def record_write(self, data_type, api_version, note, source,
                     recorded_at=None, tags=None):
        if self.record_raises:
            raise RuntimeError("transport exploded")
        self.records_written.append({
            "data_type": data_type, "api_version": api_version, "note": note,
            "source": source, "tags": tags})
        return self.record_ok


def _config(data_type=CHECKPOINT_TYPE, schema=checkpoint_channel.SCHEMA,
            api_version="v1alpha1") -> str:
    doc = {"schema": schema, "data_type": data_type,
           "api_version": api_version}
    return json.dumps(doc)


def _seed(t, *, config=None, tags=None):
    if config is not None:
        t.put(checkpoint_channel.config_path(TEAM), config)
    if tags is not None:
        t.put(bus_tags.tags_path(TEAM),
              json.dumps({"schema": bus_tags.SCHEMA, "base": BASE,
                          "agents": tags}))


def _seed_held_role(t, role="reviewer", agent="amy"):
    t.put(f"team/{TEAM}/roles/{role}.md",
          "---\ntype: Role\nsla_hours: 24\n---\n")
    cli.main(["roles", "claim", TEAM, role, "-a", agent], transport=t)


def _moments(t):
    return [r for r in t.records_written
            if r["data_type"] == CHECKPOINT_TYPE]


def _note(t, index=0):
    return json.loads(_moments(t)[index]["note"])


# --- the emission points -----------------------------------------------------

def test_park_emits_one_fully_tagged_moment(capsys):
    """The headline: parking a role is visible on the timeline, filterable by
    every identity dimension the agent registered."""
    t = RecordingTransport()
    _seed(t, config=_config(), tags={"amy": FULL})
    _seed_held_role(t)
    capsys.readouterr()

    assert cli.main(["continuity", "park", TEAM, "-a", "amy",
                     "--objective", "eod"], transport=t) == 0

    (moment,) = _moments(t)
    assert moment["data_type"] == CHECKPOINT_TYPE
    assert moment["api_version"] == "v1alpha1"
    assert moment["source"] == "amy"
    assert moment["tags"] == [AGENT, PLATFORM, HARNESS, MODEL, BASE]
    note = json.loads(moment["note"])
    assert note == {
        "v": 1, "kind": "checkpoint", "agent": "amy", "task": "role-reviewer",
        "objective": "eod",
        "path": "team/r/member/amy/continuity/role-reviewer/latest.json"}
    # the note points at bytes that actually exist
    assert note["path"] in t.store
    assert json.loads(t.store[note["path"]])["objective"] == "eod"
    assert capsys.readouterr().err == ""


def test_park_emits_one_moment_per_parked_role():
    t = RecordingTransport()
    _seed(t, config=_config(), tags={"amy": FULL})
    _seed_held_role(t, "reviewer")
    _seed_held_role(t, "oncall")

    assert cli.main(["continuity", "park", TEAM, "-a", "amy"], transport=t) == 0

    tasks_seen = sorted(json.loads(m["note"])["task"] for m in _moments(t))
    assert tasks_seen == ["role-oncall", "role-reviewer"]


def test_snapshot_emits_a_moment(capsys):
    t = RecordingTransport()
    _seed(t, config=_config(), tags={"ash": FULL})

    assert cli.main(["continuity", "snapshot", TEAM, "ash", "build-l6",
                     "--objective", "ship the layer"], transport=t) == 0

    (moment,) = _moments(t)
    assert moment["source"] == "ash"
    assert moment["tags"] == [AGENT, PLATFORM, HARNESS, MODEL, BASE]
    assert json.loads(moment["note"]) == {
        "v": 1, "kind": "checkpoint", "agent": "ash", "task": "build-l6",
        "objective": "ship the layer",
        "path": "team/r/member/ash/continuity/build-l6/latest.json"}
    assert capsys.readouterr().err == ""


def test_snapshot_moment_carries_the_slugified_task():
    """The note's task/path must name the bytes on disk, not the raw argument."""
    t = RecordingTransport()
    _seed(t, config=_config(), tags={"ash": FULL})
    cli.main(["continuity", "snapshot", TEAM, "ash", "feat/Sub Task",
              "--objective", "x"], transport=t)
    note = _note(t)
    assert note["task"] == "feat-sub-task"
    assert note["path"].endswith("/continuity/feat-sub-task/latest.json")
    assert note["path"] in t.store


def test_resume_emits_nothing_reads_are_not_saves(capsys):
    """A resume is a pure read. Emitting on it would make the timeline claim
    state was saved when nothing was, and would double every checkpoint's
    apparent frequency."""
    t = RecordingTransport()
    _seed(t, config=_config(), tags={"ash": FULL})
    cli.main(["continuity", "snapshot", TEAM, "ash", "w", "--objective", "x"],
             transport=t)
    assert len(_moments(t)) == 1

    assert cli.main(["continuity", "resume", TEAM, "ash", "w"],
                    transport=t) == 0
    assert cli.main(["continuity", "resume", TEAM, "ash"], transport=t) == 0
    assert len(_moments(t)) == 1        # unchanged by two reads


def test_briefing_and_checkpoint_ref_reads_emit_nothing():
    """The other continuity verbs that touch a snapshot are reads too."""
    t = RecordingTransport()
    _seed(t, config=_config(), tags={"amy": FULL})
    _seed_held_role(t)
    cli.main(["continuity", "park", TEAM, "-a", "amy"], transport=t)
    before = len(_moments(t))

    cli.main(["continuity", "checkpoint", TEAM, "--role", "reviewer"],
             transport=t)
    cli.main(["briefing", TEAM, "-a", "amy"], transport=t)
    assert len(_moments(t)) == before


# --- the fail-open rule (the inverse of park's loud rule) --------------------

def test_absent_config_is_silent_and_park_still_succeeds(capsys):
    """Pre-adoption teams are the majority during a rollout: no config means no
    moment and NO NOISE. A warning on every park would train agents to ignore
    warnings."""
    t = RecordingTransport()
    _seed(t, tags={"amy": FULL})            # tags provisioned, channel not
    _seed_held_role(t)
    capsys.readouterr()

    assert cli.main(["continuity", "park", TEAM, "-a", "amy"], transport=t) == 0

    captured = capsys.readouterr()
    assert _moments(t) == []
    assert captured.err == ""
    assert "parked reviewer" in captured.out
    # the load-bearing act happened
    assert "team/r/member/amy/continuity/role-reviewer/latest.json" in t.store


def test_absent_config_snapshot_still_writes_and_exits_zero(capsys):
    t = RecordingTransport()
    assert cli.main(["continuity", "snapshot", TEAM, "ash", "w",
                     "--objective", "x"], transport=t) == 0
    assert _moments(t) == []
    assert capsys.readouterr().err == ""
    assert "team/r/member/ash/continuity/w/latest.json" in t.store


def test_malformed_config_is_loud_and_park_still_succeeds(capsys):
    """Bytes exist and do not parse: LOUD every time, no emission, and the file
    is never rewritten — a human fixes durable bytes. The park is untouched."""
    t = RecordingTransport()
    _seed(t, config='{"schema": "coord.checkpoints-channel.v1", "data_type": ""}',
          tags={"amy": FULL})
    before = t.store[checkpoint_channel.config_path(TEAM)]
    _seed_held_role(t)
    capsys.readouterr()

    assert cli.main(["continuity", "park", TEAM, "-a", "amy"], transport=t) == 0

    captured = capsys.readouterr()
    assert _moments(t) == []
    assert "INVALID" in captured.err
    assert "checkpoints.json" in captured.err
    assert "parked reviewer" in captured.out
    assert t.store[checkpoint_channel.config_path(TEAM)] == before
    assert "team/r/member/amy/continuity/role-reviewer/latest.json" in t.store


def test_malformed_config_never_auto_creates_the_document(capsys):
    t = RecordingTransport()
    _seed(t, config="not json at all")
    cli.main(["continuity", "snapshot", TEAM, "ash", "w", "--objective", "x"],
             transport=t)
    assert t.store[checkpoint_channel.config_path(TEAM)] == "not json at all"
    assert "INVALID" in capsys.readouterr().err


def test_a_records_config_copied_into_place_is_invalid_not_interpreted(capsys):
    """Emitting checkpoints into the EVENTS channel is worse than emitting
    none: it puts unroutable records in front of every queue reader."""
    t = RecordingTransport()
    _seed(t, config='{"data_type": "MomentAnnotation/events", '
                    '"api_version": "v1alpha1"}')
    cli.main(["continuity", "snapshot", TEAM, "ash", "w", "--objective", "x"],
             transport=t)
    assert _moments(t) == []
    assert t.records_written == []
    assert "INVALID" in capsys.readouterr().err


def test_record_write_refused_warns_and_park_rc_is_unchanged(capsys):
    """The moment did not land. The checkpoint did, so the exit code says so."""
    t = RecordingTransport(record_ok=False)
    _seed(t, config=_config(), tags={"amy": FULL})
    _seed_held_role(t)
    capsys.readouterr()

    assert cli.main(["continuity", "park", TEAM, "-a", "amy"], transport=t) == 0

    captured = capsys.readouterr()
    assert "checkpoint moment" in captured.err and "FAILED" in captured.err
    assert captured.err.count("\n") == 1            # ONE line, not a stack
    assert "parked reviewer" in captured.out
    assert "team/r/member/amy/continuity/role-reviewer/latest.json" in t.store


def test_record_write_raising_cannot_fail_the_park(capsys):
    t = RecordingTransport(record_raises=True)
    _seed(t, config=_config(), tags={"amy": FULL})
    _seed_held_role(t)
    capsys.readouterr()

    assert cli.main(["continuity", "park", TEAM, "-a", "amy"], transport=t) == 0

    captured = capsys.readouterr()
    assert "checkpoint moment" in captured.err
    assert "parked reviewer" in captured.out


def test_record_write_raising_cannot_fail_the_snapshot(capsys):
    t = RecordingTransport(record_raises=True)
    _seed(t, config=_config())
    assert cli.main(["continuity", "snapshot", TEAM, "ash", "w",
                     "--objective", "x"], transport=t) == 0
    assert "checkpoint moment" in capsys.readouterr().err
    assert "team/r/member/ash/continuity/w/latest.json" in t.store


def test_unreadable_config_is_unknown_not_absent_and_never_cached(capsys):
    """A degraded store must not masquerade as a team that declined the
    channel — and because the verdict is UNKNOWN it is re-read next time."""
    class Erroring(RecordingTransport):
        def __init__(self):
            super().__init__()
            self.fail_config = True

        def read_classified(self, path):
            if path == checkpoint_channel.config_path(TEAM) and self.fail_config:
                return None, "error"
            raw = self.store.get(path)
            return (raw, "ok") if raw is not None else (None, "absent")

    t = Erroring()
    _seed(t, config=_config())
    assert cli.main(["continuity", "snapshot", TEAM, "ash", "w",
                     "--objective", "x"], transport=t) == 0
    assert _moments(t) == []
    assert "unreadable" in capsys.readouterr().err

    t.fail_config = False                     # store recovers
    assert cli.main(["continuity", "snapshot", TEAM, "ash", "w",
                     "--objective", "x"], transport=t) == 0
    assert len(_moments(t)) == 1              # the "error" verdict was not memoized


def test_a_transport_that_cannot_write_records_still_parks(capsys):
    """Old/limited transports have no record_write at all."""
    t = FakeTransport()
    t.put(checkpoint_channel.config_path(TEAM), _config())
    t.put(f"team/{TEAM}/roles/reviewer.md", "---\ntype: Role\nsla_hours: 24\n---\n")
    cli.main(["roles", "claim", TEAM, "reviewer", "-a", "amy"], transport=t)
    capsys.readouterr()

    assert cli.main(["continuity", "park", TEAM, "-a", "amy"], transport=t) == 0
    assert "parked reviewer" in capsys.readouterr().out


# --- tag resolution is shared with the bus ----------------------------------

def test_moment_tags_use_the_same_registry_states_as_bus_events(capsys):
    """Sender missing from the registry: base tag only, one warning, and the
    moment still lands — identical to the bus contract."""
    t = RecordingTransport()
    _seed(t, config=_config(), tags={"someone-else": {"agent": AGENT}})
    cli.main(["continuity", "snapshot", TEAM, "ash", "w", "--objective", "x"],
             transport=t)
    assert _moments(t)[0]["tags"] == [BASE]
    assert "no identity tag for 'ash'" in capsys.readouterr().err


def test_absent_tag_registry_emits_untagged_and_silently(capsys):
    t = RecordingTransport()
    _seed(t, config=_config())
    cli.main(["continuity", "snapshot", TEAM, "ash", "w", "--objective", "x"],
             transport=t)
    assert _moments(t)[0]["tags"] is None
    assert capsys.readouterr().err == ""


def test_a_partial_registry_entry_tags_what_it_has():
    t = RecordingTransport()
    _seed(t, config=_config(), tags={"ash": {"agent": AGENT, "model": MODEL}})
    cli.main(["continuity", "snapshot", TEAM, "ash", "w", "--objective", "x"],
             transport=t)
    assert _moments(t)[0]["tags"] == [AGENT, MODEL, BASE]


# --- the note ----------------------------------------------------------------

def test_objective_is_truncated_to_140_characters():
    """A moment note is a timeline LABEL; the snapshot file holds the full text
    and the note points at it."""
    long_objective = "x" * 139 + "CUT" + "y" * 100
    t = RecordingTransport()
    _seed(t, config=_config())
    cli.main(["continuity", "snapshot", TEAM, "ash", "w",
              "--objective", long_objective], transport=t)

    note = _note(t)
    assert len(note["objective"]) == 140
    assert note["objective"] == long_objective[:140]
    assert note["objective"].endswith("xC")       # a hard slice, no ellipsis
    # the untruncated objective is still durable in the checkpoint itself
    assert json.loads(t.store[note["path"]])["objective"] == long_objective


def test_an_objective_at_the_boundary_is_untouched():
    assert checkpoint_channel.truncate_objective("a" * 140) == "a" * 140
    assert checkpoint_channel.truncate_objective("a" * 141) == "a" * 140
    assert checkpoint_channel.truncate_objective(None) == ""


def test_the_note_is_compact_json_in_declared_key_order():
    note = checkpoint_channel.build_note(
        agent="ash", task="t", objective="o", path="p")
    assert note == '{"v":1,"kind":"checkpoint","agent":"ash","task":"t",' \
                   '"objective":"o","path":"p"}'
    assert " " not in note                         # compact separators


def test_park_default_objective_rides_the_moment():
    t = RecordingTransport()
    _seed(t, config=_config())
    _seed_held_role(t)
    cli.main(["continuity", "park", TEAM, "-a", "amy"], transport=t)
    assert _note(t)["objective"] == "parked role reviewer at session exit"


# --- the config document -----------------------------------------------------

def test_config_lives_beside_records_json_not_inside_it():
    """A SEPARATE document on purpose: old engines classify an authority
    carrying fields they do not know as malformed, which fails their queue
    closed. A new stream gets a new document."""
    assert checkpoint_channel.CONFIG_NAME != records.CONFIG_NAME
    assert checkpoint_channel.config_path(TEAM) != records.config_path(TEAM)
    assert checkpoint_channel.CONFIG_NAME == "_coord/bus-v3/checkpoints.json"


def test_emission_never_reads_or_writes_the_records_authority():
    t = RecordingTransport()
    _seed(t, config=_config(), tags={"ash": FULL})
    cli.main(["continuity", "snapshot", TEAM, "ash", "w", "--objective", "x"],
             transport=t)
    assert records.config_path(TEAM) not in t.store
    assert len(_moments(t)) == 1


@pytest.mark.parametrize("raw", [
    "",
    "[]",
    "null",
    '{"data_type": "MomentAnnotation/x"}',                       # no schema
    '{"schema": "coord.checkpoints-channel.v2", "data_type": "x"}',
    '{"schema": "coord.checkpoints-channel.v1"}',                # no data_type
    '{"schema": "coord.checkpoints-channel.v1", "data_type": 7}',
    '{"schema": "coord.checkpoints-channel.v1", "data_type": "x",'
    ' "api_version": ""}',
])
def test_parse_config_refuses_everything_it_cannot_trust(raw):
    assert checkpoint_channel.parse_config(raw) is None


def test_parse_config_defaults_the_api_version():
    cfg = checkpoint_channel.parse_config(
        '{"schema": "coord.checkpoints-channel.v1", "data_type": " T "}')
    assert cfg == {"data_type": "T",
                   "api_version": checkpoint_channel.DEFAULT_API_VERSION}


def test_config_is_memoized_per_process_like_the_tag_registry():
    t = RecordingTransport()
    _seed(t, config=_config())
    assert checkpoint_channel.load_config(t, TEAM)[1] == "ok"
    del t.store[checkpoint_channel.config_path(TEAM)]
    assert checkpoint_channel.load_config(t, TEAM)[1] == "ok"      # cached
    checkpoint_channel.cache_clear()
    assert checkpoint_channel.load_config(t, TEAM)[1] == "absent"


def test_a_snapshot_that_did_not_persist_does_not_report_success(capsys):
    """Found live during a bus outage: `snapshot <id>` printed and rc was 0
    while the store was unreachable and nothing was written.

    The failure was ALREADY KNOWN at that point — `transport.write` returns
    False on a transport failure, and the code captured it into a local and
    spent it on the cosmetic "should this cast a shadow" decision while the
    exit code and the success line went out unchanged.

    Continuity is the durability mechanism. A park that reports success without
    reaching the store leaves a successor resuming from the PREVIOUS checkpoint
    believing it is current, and that happens exactly when the host is in
    trouble — which is when parking matters most."""
    class Dark(FakeTransport):
        def write(self, path, content):
            return False  # transport failure, not a rejected write

    t = Dark()
    capsys.readouterr()
    rc = cli.main(["continuity", "snapshot", "r", "agent-x", "slug",
                   "--objective", "o", "--next", "n"], transport=t)
    out, err = capsys.readouterr()
    assert rc == 3, "an unpersisted snapshot is a DEGRADED answer, not success"
    assert "snapshot CHK" not in out, "must not print the success line"
    assert "FAILED to persist" in err and "NOTHING was saved" in err
    assert "believe it is current" in err, "say what the successor will do"


def test_a_persisted_snapshot_still_reports_success(capsys):
    """The control: without it, returning 3 unconditionally would pass the test
    above and break every healthy park in the fleet."""
    t = FakeTransport()
    capsys.readouterr()
    rc = cli.main(["continuity", "snapshot", "r", "agent-y", "slug",
                   "--objective", "o", "--next", "n"], transport=t)
    out = capsys.readouterr().out
    assert rc == 0 and "snapshot CHK" in out
