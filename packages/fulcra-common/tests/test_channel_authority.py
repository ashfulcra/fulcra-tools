"""The coordination channel resolves from its AUTHORITY, never by name.

Regression cover for the 2026-08-03..08-06 fleet-wide delivery defect: the
superseded ``Agent Tasks`` definition is still named ``Agent Tasks``, still
``deprecated: false``, still ``deleted_at: null``. Every liveness check says
LIVE — truthfully — so a by-name resolve selects it forever and the records it
produces are durable, accepted, and invisible to every reader.

The load-bearing assertion in this file is the NEGATIVE one: no resolution path
may fall back to a name lookup. A test that only checks the happy path would
pass against the defective code.
"""
from __future__ import annotations

import json
import subprocess

import fulcra_common.annotations as annotations
import pytest

LIVE = "d04f357e-b556-4298-ad1e-4ce307d54041"
SUPERSEDED = "ea49d0d3-acb7-49c6-93b6-bee81d126c92"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Every test gets its own cache dir and an empty authority memo."""
    monkeypatch.setattr(annotations, "annotations_dir", lambda: tmp_path)
    monkeypatch.setattr(annotations, "_AUTHORITY_MEMO", {})
    monkeypatch.delenv("FULCRA_COORD_TEAM", raising=False)
    return tmp_path


def _stub_authority(monkeypatch, payload, *, rc=0):
    """Stub the CLI shell-out that downloads records.json.

    Writes ``payload`` to the destination path the caller passed, mimicking
    ``fulcra-api file download``. ``payload=None`` writes nothing.
    """
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        if rc == 0 and payload is not None:
            with open(cmd[-1], "w", encoding="utf-8") as fh:
                fh.write(payload)
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

    monkeypatch.setattr(annotations.subprocess, "run", fake_run)
    return seen


def _forbid_name_lookup(monkeypatch):
    """Make any by-name resolve an immediate, loud test failure."""

    def boom(*a, **k):
        raise AssertionError(
            "resolved the channel BY NAME — this is the defect under test")

    monkeypatch.setattr(annotations, "_resolve_def_via_cli", boom)


# --- _authority_definition_id -------------------------------------------


def test_authority_returns_bare_uuid_stripping_the_type_prefix(monkeypatch):
    seen = _stub_authority(
        monkeypatch, json.dumps({"data_type": f"MomentAnnotation/{LIVE}"}))
    assert annotations._authority_definition_id() == LIVE
    # THE LAST HARDCODED TEAM NAME IN THIS REPO, and it is here on purpose:
    # this asserts real current behaviour. `_coord_team()` still ends in
    # `or "fulcra"`, and the fixture above deletes FULCRA_COORD_TEAM, so the
    # path built here IS the shipped default. Do not "tidy" this literal —
    # removing the default is a fleet migration (every agent that writes
    # annotations must have FULCRA_COORD_TEAM set first, and today none
    # does), not a rename. Change the source and this line together or
    # neither.
    assert f"team/fulcra/{annotations._AUTHORITY_PATH}" in seen["cmd"]


def test_authority_honors_the_team_env(monkeypatch):
    monkeypatch.setenv("FULCRA_COORD_TEAM", "otherteam")
    seen = _stub_authority(
        monkeypatch, json.dumps({"data_type": f"MomentAnnotation/{LIVE}"}))
    annotations._authority_definition_id()
    assert f"team/otherteam/{annotations._AUTHORITY_PATH}" in seen["cmd"]


@pytest.mark.parametrize(
    "payload, rc",
    [
        (None, 1),                                   # download failed
        ("not json at all", 0),                      # unparseable
        (json.dumps({"data_type": ""}), 0),          # empty
        (json.dumps({}), 0),                         # key absent
        (json.dumps({"data_type": "MomentAnnotation/not-a-uuid"}), 0),
    ],
)
def test_authority_unreadable_is_none_never_a_guess(monkeypatch, payload, rc):
    """None means UNKNOWN. It must never resolve to a plausible default."""
    _stub_authority(monkeypatch, payload, rc=rc)
    assert annotations._authority_definition_id() is None


def test_a_cutover_is_picked_up_within_one_long_lived_process(monkeypatch):
    """The memo must EXPIRE. A heartbeat daemon that resolved the old channel
    once would otherwise keep writing to it until restart — this change's own
    defect, one layer down. Two emissions, authority moves between them."""
    current = {"id": SUPERSEDED}

    def fake_run(cmd, **kwargs):
        with open(cmd[-1], "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"data_type": f"MomentAnnotation/{current['id']}"}))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(annotations.subprocess, "run", fake_run)
    monkeypatch.setattr(annotations, "AUTHORITY_MEMO_TTL_SECONDS", 0.0)

    assert annotations._authority_definition_id() == SUPERSEDED
    current["id"] = LIVE
    assert annotations._authority_definition_id() == LIVE, (
        "the memo outlived the cutover — a resident writer would keep "
        "publishing to the retired channel")


def test_memo_is_used_inside_its_ttl(monkeypatch):
    """The TTL must not degrade into a shell-out per emit."""
    calls = []

    def counting_run(cmd, **kwargs):
        calls.append(cmd)
        with open(cmd[-1], "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"data_type": f"MomentAnnotation/{LIVE}"}))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(annotations.subprocess, "run", counting_run)
    monkeypatch.setattr(annotations, "AUTHORITY_MEMO_TTL_SECONDS", 300.0)

    assert annotations._authority_definition_id() == LIVE
    assert annotations._authority_definition_id() == LIVE
    assert len(calls) == 1, "a fresh memo must not re-read the authority"


def test_authority_memoizes_only_on_success(monkeypatch):
    calls = []

    def counting_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr(annotations.subprocess, "run", counting_run)
    assert annotations._authority_definition_id() is None
    assert annotations._authority_definition_id() is None
    assert len(calls) == 2, "a failed read must retry, not pin 'unknown'"


# --- _resolve_definition_id ---------------------------------------------


def test_authority_wins_over_a_stale_cache_and_heals_it(monkeypatch, tmp_path):
    """The exact incident: a warm cache pinning the superseded definition."""
    _forbid_name_lookup(monkeypatch)
    annotations._store_definition_id(SUPERSEDED)
    _stub_authority(
        monkeypatch, json.dumps({"data_type": f"MomentAnnotation/{LIVE}"}))

    assert annotations._resolve_definition_id([]) == LIVE
    written = json.loads((tmp_path / "definition.json").read_text())
    assert written["id"] == LIVE, "cache must self-heal, not wait for a TTL"


def test_operator_pin_outranks_the_authority(monkeypatch, caplog):
    """The pin is the escape hatch; this resolution must not disarm it."""
    _forbid_name_lookup(monkeypatch)
    monkeypatch.setattr(annotations, "_definition_live", lambda *a, **k: True)
    annotations.pin_definition_id(SUPERSEDED)
    _stub_authority(
        monkeypatch, json.dumps({"data_type": f"MomentAnnotation/{LIVE}"}))

    with caplog.at_level("WARNING"):
        assert annotations._resolve_definition_id([]) == SUPERSEDED
    assert "disagrees" in caplog.text, "a pin fighting the authority must be loud"


def test_unreadable_authority_falls_back_to_cache_loudly(monkeypatch, caplog):
    _forbid_name_lookup(monkeypatch)
    annotations._store_definition_id(LIVE)
    _stub_authority(monkeypatch, None, rc=1)

    with caplog.at_level("WARNING"):
        assert annotations._resolve_definition_id([]) == LIVE
    assert "UNVERIFIED" in caplog.text


def test_no_authority_and_no_cache_refuses_rather_than_resolving_by_name(
        monkeypatch, caplog):
    """THE regression guard. Returning "" makes the write a best-effort skip;
    a name lookup here would silently rebuild the original defect."""
    _forbid_name_lookup(monkeypatch)
    _stub_authority(monkeypatch, None, rc=1)

    with caplog.at_level("ERROR"):
        assert annotations._resolve_definition_id([]) == ""
    assert "REFUSING" in caplog.text


def test_write_http_skips_cleanly_when_resolution_refuses(monkeypatch):
    """A refusal must be a no-op write, not a raise — the projection is
    best-effort and the transition stays free to retry."""
    monkeypatch.setattr(annotations, "_resolve_token", lambda: "tok")
    monkeypatch.setattr(annotations, "_resolve_tag_id", lambda *a, **k: "t")
    monkeypatch.setattr(annotations, "_resolve_definition_id", lambda *a, **k: "")

    def boom(*a, **k):
        raise AssertionError("posted a record with no resolved definition")

    monkeypatch.setattr(annotations, "_request", boom)
    assert annotations._write_http({"cli_tags": ["agent-tasks"], "name": "n"}) is False


# --- pin_definition_id: the four branches -------------------------------


def test_pin_rejects_an_empty_id():
    with pytest.raises(ValueError, match="empty definition id"):
        annotations.pin_definition_id("")


def test_pin_rejects_a_non_uuid():
    with pytest.raises(ValueError, match="not a definition uuid"):
        annotations.pin_definition_id("Agent Tasks")


def test_pin_refuses_a_verified_dead_definition(monkeypatch):
    """A pin never expires, so pinning a dead id makes the incident permanent."""
    monkeypatch.setattr(annotations, "_definition_live", lambda *a, **k: False)
    monkeypatch.setattr(annotations, "_resolve_token", lambda: "tok")
    with pytest.raises(ValueError, match="refusing to pin"):
        annotations.pin_definition_id(LIVE)


def test_pin_proceeds_when_liveness_is_undeterminable(monkeypatch, caplog, tmp_path):
    """The hatch has to work in the degraded conditions it exists for."""
    monkeypatch.setattr(annotations, "_definition_live", lambda *a, **k: None)
    monkeypatch.setattr(annotations, "_resolve_token", lambda: None)
    with caplog.at_level("WARNING"):
        annotations.pin_definition_id(LIVE)
    assert "could not verify" in caplog.text
    assert json.loads((tmp_path / "definition.json").read_text())["pinned"] is True


def test_pin_force_skips_the_check_and_records_that_it_did(monkeypatch, caplog):
    def boom(*a, **k):
        raise AssertionError("force=True must not consult the catalog")

    monkeypatch.setattr(annotations, "_definition_live", boom)
    with caplog.at_level("WARNING"):
        annotations.pin_definition_id(LIVE, force=True)
    assert "force=True" in caplog.text, "an unchecked pin must say so in the log"
