"""Four-dimension identity tags on bus v3 writes.

The invariant under test is narrow and load-bearing: an event written by agent
X carries X's registered dimension tags — agent, platform, harness, model — plus
the channel base tag, so the Fulcra timeline can group by any of them. And
NOTHING about tag resolution may ever cost a write: the registry states each get
a case (provisioned, partial, sender missing, malformed, absent) and every one
of them still lands the record.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from coord_engine import bus_tags, cli, records
from coord_engine_test_helpers import FakeTransport

BASE = "cb951ecb-f21c-4aee-826e-2cb0b12517d6"
AGENT = "0913d5df-830c-458e-b40a-0a04eafaa5cd"
PLATFORM = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f"
HARNESS = "2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f60"
MODEL = "3e4f5a6b-7c8d-4e9f-a0b1-2c3d4e5f6071"
CODER = "7a1f9c2e-4b6d-4c1a-9f0e-2d3b5a6c7e8f"

TEAM = "r"
FULL = {"agent": AGENT, "platform": PLATFORM, "harness": HARNESS,
        "model": MODEL}


class TaggingTransport(FakeTransport):
    """FakeTransport that records what ``record_write`` was handed."""

    def __init__(self, record_ok=True):
        super().__init__()
        self.record_ok = record_ok
        self.records_written: list[dict] = []

    def record_write(self, data_type, api_version, note, source,
                     recorded_at=None, tags=None):
        self.records_written.append({
            "data_type": data_type, "note": note, "source": source,
            "recorded_at": recorded_at, "tags": tags})
        return self.record_ok


def _registry(agents: dict) -> str:
    return json.dumps({"schema": bus_tags.SCHEMA, "base": BASE,
                       "agents": agents})


def _bus(t, *, tags=None):
    t.put(records.config_path(TEAM), '{"data_type": "MomentAnnotation/x"}')
    if tags is not None:
        t.put(bus_tags.tags_path(TEAM), tags)


def _emit(t, sender="coord-boss"):
    return records.emit_event(
        t, records.load_config(t, TEAM), sender=sender, to="codex-coder",
        kind="directive", priority="P0", slug="s", team=TEAM)


# --- the write path ----------------------------------------------------------

def test_tagged_write_carries_every_dimension_then_base(capsys):
    t = TaggingTransport()
    _bus(t, tags=_registry({"coord-boss": FULL}))
    assert _emit(t) is True
    (rec,) = t.records_written
    assert rec["tags"] == [AGENT, PLATFORM, HARNESS, MODEL, BASE]
    assert capsys.readouterr().err == ""


def test_partial_entry_attaches_what_exists_without_warning(capsys):
    """Not every agent has a meaningful harness; nagging on every write would
    bury the warning that matters."""
    t = TaggingTransport()
    _bus(t, tags=_registry({"coord-boss": {"agent": AGENT, "model": MODEL}}))
    assert _emit(t) is True
    assert t.records_written[0]["tags"] == [AGENT, MODEL, BASE]
    assert capsys.readouterr().err == ""


def test_each_sender_gets_its_own_dimensions():
    t = TaggingTransport()
    _bus(t, tags=_registry({"coord-boss": FULL,
                            "codex-coder": {"agent": CODER}}))
    _emit(t, sender="coord-boss")
    _emit(t, sender="codex-coder")
    assert [r["tags"] for r in t.records_written] == [
        [AGENT, PLATFORM, HARNESS, MODEL, BASE], [CODER, BASE]]


def test_missing_sender_entry_warns_and_writes_base_tag_only(capsys):
    """Not silent, and not a failed write: the event lands under the channel
    tag and the agent is told exactly which verb fixes it."""
    t = TaggingTransport()
    _bus(t, tags=_registry({"codex-coder": {"agent": CODER}}))
    assert _emit(t, sender="coord-boss") is True
    (rec,) = t.records_written
    assert rec["tags"] == [BASE]
    err = capsys.readouterr().err
    assert "no identity tag for 'coord-boss'" in err
    assert "bus-v3 tag-provision" in err and "--model" in err


def test_malformed_registry_is_loud_untagged_and_never_recreated(capsys):
    t = TaggingTransport()
    _bus(t, tags='{"schema": "coord.bus-tags.v2", "base": "not-a-uuid"}')
    before = t.store[bus_tags.tags_path(TEAM)]
    assert _emit(t) is True
    (rec,) = t.records_written
    assert rec["tags"] is None                       # untagged, not guessed
    assert "INVALID" in capsys.readouterr().err
    assert t.store[bus_tags.tags_path(TEAM)] == before   # bytes untouched


def test_a_v1_registry_is_invalid_not_silently_upgraded(capsys):
    """The identity-only predecessor schema. A human migrates it; an engine
    that guessed the new shape would invent dimensions nobody declared."""
    t = TaggingTransport()
    _bus(t, tags=json.dumps({"schema": "coord.bus-tags.v1", "base": BASE,
                             "agents": {"coord-boss": AGENT}}))
    assert _emit(t) is True
    assert t.records_written[0]["tags"] is None
    assert "INVALID" in capsys.readouterr().err


def test_absent_registry_writes_untagged_and_quietly(capsys):
    """A team that never provisioned tags has not asked for them."""
    t = TaggingTransport()
    _bus(t)
    assert _emit(t) is True
    assert t.records_written[0]["tags"] is None
    assert capsys.readouterr().err == ""


def test_no_team_context_writes_untagged():
    t = TaggingTransport()
    _bus(t, tags=_registry({"coord-boss": FULL}))
    records.emit_event(t, records.load_config(t, TEAM), sender="coord-boss",
                       to="x", kind="claim", priority="P3", slug="s")
    assert t.records_written[0]["tags"] is None


def test_registry_is_read_once_per_process():
    t = TaggingTransport()
    _bus(t, tags=_registry({"coord-boss": FULL}))
    reads = []
    inner = t.read
    t.read = lambda p: (reads.append(p), inner(p))[1]
    _emit(t)
    _emit(t)
    assert reads.count(bus_tags.tags_path(TEAM)) == 1


def test_remind_tags_the_future_dated_record(monkeypatch):
    """End to end through a real write verb, not just the helper."""
    monkeypatch.setattr(
        cli, "_now", lambda: datetime(2026, 7, 27, 18, 0, tzinfo=timezone.utc))
    t = TaggingTransport()
    _bus(t, tags=_registry({"boss": FULL}))
    assert cli.main(["remind", TEAM, "amy", "2h", "Check the oven",
                     "--from", "boss"], transport=t) == 0
    (rec,) = t.records_written
    assert rec["tags"] == [AGENT, PLATFORM, HARNESS, MODEL, BASE]
    assert rec["recorded_at"]


# --- registry parsing --------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "not json",
    "[]",
    json.dumps({"base": BASE, "agents": {}}),                    # no schema
    json.dumps({"schema": "other", "base": BASE, "agents": {}}),  # wrong schema
    json.dumps({"schema": bus_tags.SCHEMA, "agents": {}}),        # no base
    json.dumps({"schema": bus_tags.SCHEMA, "base": "x", "agents": {}}),
    json.dumps({"schema": bus_tags.SCHEMA, "base": BASE, "agents": []}),
    json.dumps({"schema": bus_tags.SCHEMA, "base": BASE,
                "agents": {"a": AGENT}}),          # v1 flat entry
    json.dumps({"schema": bus_tags.SCHEMA, "base": BASE,
                "agents": {"a": {"agent": "coord-boss"}}}),  # NAME, not uuid
    json.dumps({"schema": bus_tags.SCHEMA, "base": BASE,
                "agents": {"a": {"model": MODEL}}}),   # no agent dimension
    json.dumps({"schema": bus_tags.SCHEMA, "base": BASE,
                "agents": {"a": {"agent": AGENT, "fleet": MODEL}}}),  # unknown
])
def test_parse_registry_refuses_anything_it_cannot_trust(raw):
    assert bus_tags.parse_registry(raw) is None


def test_parse_registry_accepts_a_freshly_seeded_empty_roster():
    assert bus_tags.parse_registry(_registry({})) == {"base": BASE,
                                                      "agents": {}}


def test_parse_registry_accepts_a_partial_entry():
    parsed = bus_tags.parse_registry(
        _registry({"coord-boss": {"agent": AGENT, "platform": PLATFORM}}))
    assert parsed["agents"]["coord-boss"] == {"agent": AGENT,
                                              "platform": PLATFORM}


def test_transport_error_is_unknown_not_absent_and_is_not_cached():
    class Flaky:
        def __init__(self):
            self.calls = 0

        def read_classified(self, path):
            self.calls += 1
            return (None, "error") if self.calls == 1 else (
                _registry({"coord-boss": FULL}), "ok")

    t = Flaky()
    assert bus_tags.load_registry(t, TEAM) == (None, "error")
    registry, status = bus_tags.load_registry(t, TEAM)
    assert status == "ok" and registry["agents"]["coord-boss"] == FULL


def test_record_write_puts_tag_uuids_on_the_stdin_document(monkeypatch):
    from coord_engine import transport as transport_mod
    calls = []
    monkeypatch.setattr(
        transport_mod, "run_bounded",
        lambda argv, timeout, *, stdin_data=None, **kw: (
            calls.append(stdin_data), (0, "", ""))[1])
    t = transport_mod.FulcraFileTransport(command=["fulcra-api"], timeout=5)
    assert t.record_write("T", "v1alpha1", "{}", "boss",
                          tags=[AGENT, MODEL, BASE])
    assert json.loads(calls[0])["tags"] == [AGENT, MODEL, BASE]
    t.record_write("T", "v1alpha1", "{}", "boss", tags=[])
    assert "tags" not in json.loads(calls[1])       # empty == byte-identical


# --- the provisioning verb ---------------------------------------------------

def _provision(t, *extra, agent="coord-boss"):
    return cli.main(["bus-v3", "tag-provision", TEAM, "--agent", agent,
                     *extra], transport=t)


class ProvisioningTransport(TaggingTransport):
    """Transport whose raw tag capability hands back a uuid per tag NAME."""

    def __init__(self, tags=None):
        super().__init__()
        self.tags = tags or {}
        self.ensured: list[str] = []

    def tag_ensure(self, name):
        self.ensured.append(name)
        return self.tags.get(name)


def _entry(t, agent="coord-boss"):
    return json.loads(t.store[bus_tags.tags_path(TEAM)])["agents"].get(agent)


def test_tag_provision_creates_all_four_dimensions(capsys):
    t = ProvisioningTransport({
        "agent:coord-boss": AGENT, "platform:claude-code": PLATFORM,
        "harness:ccr": HARNESS, "model:opus-5": MODEL})
    t.put(bus_tags.tags_path(TEAM), _registry({}))
    assert _provision(t, "--platform", "claude-code", "--harness", "ccr",
                      "--model", "opus-5") == 0
    assert t.ensured == ["agent:coord-boss", "platform:claude-code",
                         "harness:ccr", "model:opus-5"]
    assert _entry(t) == FULL
    assert BASE in capsys.readouterr().out


def test_tag_provision_records_externally_created_uuids():
    t = ProvisioningTransport()             # raw capability resolves nothing
    t.put(bus_tags.tags_path(TEAM), _registry({}))
    assert _provision(t, "--tag-id-agent", AGENT,
                      "--tag-id-model", MODEL) == 0
    assert t.ensured == []                  # --tag-id-* short-circuits creation
    assert _entry(t) == {"agent": AGENT, "model": MODEL}


def test_tag_provision_rejects_a_tag_id_that_is_not_a_uuid(capsys):
    t = ProvisioningTransport()
    t.put(bus_tags.tags_path(TEAM), _registry({}))
    assert _provision(t, "--tag-id-agent", "coord-boss") == 2
    assert "not a uuid" in capsys.readouterr().err


def test_tag_provision_prints_a_recipe_per_unresolvable_dimension(capsys):
    t = ProvisioningTransport({"agent:coord-boss": AGENT})   # only agent works
    t.put(bus_tags.tags_path(TEAM), _registry({}))
    assert _provision(t, "--platform", "claude-code", "--model", "opus-5") == 2
    err = capsys.readouterr().err
    assert "/user/v1alpha1/tag" in err
    assert "--tag-id-platform" in err and "--tag-id-model" in err
    assert _entry(t) == {"agent": AGENT}    # partial progress still recorded


def test_tag_provision_is_idempotent_for_an_already_registered_identity(capsys):
    t = ProvisioningTransport({"agent:coord-boss": CODER})
    t.put(bus_tags.tags_path(TEAM), _registry({"coord-boss": FULL}))
    assert _provision(t) == 0
    assert t.ensured == []                  # no re-resolution, no API call
    assert "already registered" in capsys.readouterr().out
    assert _entry(t) == FULL


def test_re_provisioning_a_model_rewrites_only_that_dimension(capsys):
    """A model switch is one cheap command; the other three tags stand."""
    new_model = "4f5a6b7c-8d9e-4f0a-b1c2-3d4e5f607182"
    t = ProvisioningTransport({"model:sonnet-5": new_model})
    t.put(bus_tags.tags_path(TEAM), _registry({"coord-boss": FULL}))
    assert _provision(t, "--model", "sonnet-5") == 0
    assert t.ensured == ["model:sonnet-5"]
    assert _entry(t) == {**FULL, "model": new_model}


def test_tag_provision_names_the_dimensions_still_missing(capsys):
    t = ProvisioningTransport({"agent:coord-boss": AGENT})
    t.put(bus_tags.tags_path(TEAM), _registry({}))
    assert _provision(t) == 0
    out = capsys.readouterr().out
    assert "platform/harness/model" in out
    assert _entry(t) == {"agent": AGENT}


def test_tag_provision_refuses_an_absent_registry(capsys):
    t = ProvisioningTransport({"agent:coord-boss": AGENT})
    assert _provision(t) == 2
    assert "ABSENT" in capsys.readouterr().err
    assert bus_tags.tags_path(TEAM) not in t.store   # never auto-created


def test_tag_provision_refuses_a_malformed_registry(capsys):
    t = ProvisioningTransport({"agent:coord-boss": AGENT})
    t.put(bus_tags.tags_path(TEAM), "{oops")
    assert _provision(t) == 2
    assert "INVALID" in capsys.readouterr().err
    assert t.store[bus_tags.tags_path(TEAM)] == "{oops"


def test_tag_provision_names_the_uuids_when_the_registry_write_fails(capsys):
    t = ProvisioningTransport({"agent:coord-boss": AGENT})
    t.put(bus_tags.tags_path(TEAM), _registry({}))
    t.write = lambda path, content: False
    assert _provision(t) == 2
    # The tag EXISTS now; the retry must reuse it, not create a second one.
    assert f"--tag-id-agent {AGENT}" in capsys.readouterr().err


def test_tag_provision_requires_an_identity(monkeypatch, capsys):
    monkeypatch.delenv("FULCRA_COORD_AGENT", raising=False)
    t = ProvisioningTransport()
    assert cli.main(["bus-v3", "tag-provision", TEAM], transport=t) == 2
    assert "no agent identity" in capsys.readouterr().err


def test_provisioning_makes_the_next_write_tagged():
    """The point of the verb: provision, then the very next event is tagged."""
    t = ProvisioningTransport({
        "agent:coord-boss": AGENT, "platform:claude-code": PLATFORM,
        "harness:ccr": HARNESS, "model:opus-5": MODEL})
    _bus(t, tags=_registry({}))
    assert _emit(t) is True and t.records_written[0]["tags"] == [BASE]
    assert _provision(t, "--platform", "claude-code", "--harness", "ccr",
                      "--model", "opus-5") == 0
    assert _emit(t) is True
    assert t.records_written[1]["tags"] == [AGENT, PLATFORM, HARNESS, MODEL,
                                           BASE]
