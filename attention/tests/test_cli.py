"""CLI entry point — exercised via click's CliRunner.

The standalone HTTP relay is gone: the fulcra-collect daemon owns the
ingest endpoint. The CLI surface is the set of headless/multi-machine
management commands (bootstrap / setup / status / defs / adopt / reset)
and runs no listener of its own. `setup` just registers the
`machine:<host>` tag — it does not generate a token or install a
service unit.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from fulcra_attention import state as state_mod
from fulcra_attention.cli import cli
from fulcra_attention.fulcra import CATEGORY_VOCAB


def _pre_cached_tags() -> dict[str, str]:
    """Cache attention/web + all vocab tags so ensure_definitions makes no calls."""
    out = {"attention": "a", "web": "w"}
    for slug in CATEGORY_VOCAB:
        out[f"category:{slug}"] = f"t-{slug}"
    return out


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(state_mod, "DEFAULT_PATH", state_path)
    monkeypatch.setenv("FULCRA_ATTENTION_STATE", str(state_path))
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    yield state_path


def test_cli_command_surface_is_the_intended_set():
    """The relay-era listener command is gone; the surface is exactly the
    headless/multi-machine management commands. Pinned so a stray re-add of
    a relay subcommand (or accidental removal of a kept one) is caught."""
    assert set(cli.commands) == {
        "bootstrap", "setup", "status", "defs", "adopt", "reset",
    }
    assert "relay" not in cli.commands


def test_bootstrap_creates_def_and_tags(_isolate_state, mocker):
    posted: list[dict] = []
    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "GET" and r.url.path == "/user/v1alpha1/annotation":
            return httpx.Response(200, json=[])  # no existing defs
        if r.method == "GET" and "/tag/name/" in r.url.path:
            return httpx.Response(404)
        if r.method == "POST" and r.url.path == "/user/v1alpha1/tag":
            body = json.loads(r.content)
            return httpx.Response(200, json={"id": f"tag-{body['name']}"})
        if r.method == "POST" and r.url.path == "/user/v1alpha1/annotation":
            posted.append(json.loads(r.content))
            return httpx.Response(200, json={"id": "def-attention"})
        raise AssertionError(f"unexpected {r.method} {r.url}")
    transport = httpx.MockTransport(responder)
    mocker.patch(
        "fulcra_attention.cli.FulcraClient",
        lambda **kw: __import__("fulcra_attention.fulcra", fromlist=["FulcraClient"]).FulcraClient(transport=transport, **kw),
    )

    res = CliRunner().invoke(cli, ["bootstrap"])
    assert res.exit_code == 0, res.output
    assert "def-attention" in res.output

    s = state_mod.load(_isolate_state)
    assert s.attention_definition_id == "def-attention"


def test_bootstrap_idempotent(_isolate_state, mocker):
    state_mod.save(
        state_mod.State(
            attention_definition_id="def-existing",
            tag_ids=_pre_cached_tags(),
        ),
        _isolate_state,
    )

    def responder(r: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no requests expected, got {r.method} {r.url}")
    transport = httpx.MockTransport(responder)
    mocker.patch(
        "fulcra_attention.cli.FulcraClient",
        lambda **kw: __import__("fulcra_attention.fulcra", fromlist=["FulcraClient"]).FulcraClient(transport=transport, **kw),
    )
    res = CliRunner().invoke(cli, ["bootstrap"])
    assert res.exit_code == 0
    assert "def-existing" in res.output


def _patch_setup_client(mocker, posted_tags: list[dict] | None = None):
    """Patch FulcraClient in cli with a transport that handles ensure_tag for
    the machine:<hostname> lookup setup does. Returns the recorded list."""
    posted_tags = posted_tags if posted_tags is not None else []

    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "GET" and "/tag/name/" in r.url.path:
            return httpx.Response(404)
        if r.method == "POST" and r.url.path == "/user/v1alpha1/tag":
            body = json.loads(r.content)
            posted_tags.append(body)
            return httpx.Response(200, json={"id": f"tag-{body['name']}"})
        raise AssertionError(f"unexpected {r.method} {r.url}")
    transport = httpx.MockTransport(responder)
    mocker.patch(
        "fulcra_attention.cli.FulcraClient",
        lambda **kw: __import__("fulcra_attention.fulcra", fromlist=["FulcraClient"]).FulcraClient(transport=transport, **kw),
    )
    return posted_tags


def test_setup_registers_machine_tag(_isolate_state, tmp_path, mocker):
    """setup() now just registers a machine:<host> tag — no bearer
    token, no launchd unit (the daemon owns the HTTP listener)."""
    state_mod.save(
        state_mod.State(
            attention_definition_id="def-setup-test", tag_ids=_pre_cached_tags(),
        ),
        _isolate_state,
    )
    posted_tags = _patch_setup_client(mocker)
    res = CliRunner().invoke(cli, ["setup", "--hostname", "testbox"])
    assert res.exit_code == 0, res.output
    # machine:<hostname> tag was created and persisted
    assert {"name": "machine:testbox"} in posted_tags
    s = state_mod.load(_isolate_state)
    assert s.hostname == "testbox"
    assert s.tag_ids["machine:testbox"] == "tag-machine:testbox"
    # User is told to set the extension-token in the web UI
    assert "extension-token" in res.output.lower() or "extension token" in res.output.lower()


def test_setup_strips_local_suffix_from_hostname(_isolate_state, tmp_path, mocker):
    """`.local` mDNS suffix is stripped so the tag stays portable across networks."""
    state_mod.save(
        state_mod.State(attention_definition_id="def-x", tag_ids=_pre_cached_tags()),
        _isolate_state,
    )
    _patch_setup_client(mocker)
    res = CliRunner().invoke(cli, ["setup", "--hostname", "DeskBookPro.local"])
    assert res.exit_code == 0, res.output
    s = state_mod.load(_isolate_state)
    assert s.hostname == "deskbookpro"


def test_setup_requires_bootstrap_first(_isolate_state, tmp_path, mocker):
    # No bootstrap has run — state is empty
    res = CliRunner().invoke(cli, ["setup"])
    assert res.exit_code != 0
    assert "bootstrap" in res.output.lower()


def test_status_prints_state_json(_isolate_state):
    state_mod.save(
        state_mod.State(
            attention_definition_id="def-x",
            tag_ids={"attention": "a", "web": "w"},
            watermarks={"curl/0.1": "2026-05-18T14:00:00Z"},
        ),
        _isolate_state,
    )
    res = CliRunner().invoke(cli, ["status"])
    assert res.exit_code == 0
    parsed = json.loads(res.output)
    assert parsed["attention_definition_id"] == "def-x"
    assert parsed["tag_ids"]["attention"] == "a"
    assert parsed["watermarks"]["curl/0.1"] == "2026-05-18T14:00:00Z"


def test_reset_requires_confirm(_isolate_state):
    state_mod.save(
        state_mod.State(attention_definition_id="def-x"),
        _isolate_state,
    )
    res = CliRunner().invoke(cli, ["reset"])
    assert res.exit_code != 0
    assert "--confirm" in res.output


def test_reset_with_confirm_soft_deletes_and_clears(_isolate_state, mocker):
    state_mod.save(
        state_mod.State(
            attention_definition_id="def-to-delete",
            tag_ids={"attention": "a", "web": "w"},
            watermarks={"curl": "2026-05-18T14:00:00Z"},
        ),
        _isolate_state,
    )
    calls = []
    def responder(r: httpx.Request) -> httpx.Response:
        calls.append((r.method, r.url.path))
        if r.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError(f"unexpected {r.method} {r.url}")
    transport = httpx.MockTransport(responder)
    mocker.patch(
        "fulcra_attention.cli.FulcraClient",
        lambda **kw: __import__("fulcra_attention.fulcra", fromlist=["FulcraClient"]).FulcraClient(transport=transport, **kw),
    )
    res = CliRunner().invoke(cli, ["reset", "--confirm"])
    assert res.exit_code == 0
    assert ("DELETE", "/user/v1alpha1/annotation/def-to-delete") in calls
    s = state_mod.load(_isolate_state)
    assert s.attention_definition_id is None
    assert s.watermarks == {}


def test_defs_lists_all_attention_definitions(_isolate_state, mocker):
    """`defs` lists every Attention definition so the user can spot the
    duplicates an older create-only bootstrap left behind, and see which
    one this machine currently points at."""
    state_mod.save(
        state_mod.State(attention_definition_id="def-mine"),
        _isolate_state,
    )

    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "GET" and r.url.path == "/user/v1alpha1/annotation":
            return httpx.Response(200, json=[
                {"name": "Attention", "annotation_type": "duration",
                 "id": "def-old", "created_at": "2026-05-18T00:00:00Z",
                 "deleted_at": None},
                {"name": "Attention", "annotation_type": "duration",
                 "id": "def-mine", "created_at": "2026-05-19T00:00:00Z",
                 "deleted_at": None},
                {"name": "Energy", "annotation_type": "scale",
                 "id": "def-unrelated", "created_at": "2026-01-01T00:00:00Z"},
            ])
        raise AssertionError(f"unexpected {r.method} {r.url}")

    transport = httpx.MockTransport(responder)
    mocker.patch(
        "fulcra_attention.cli.FulcraClient",
        lambda **kw: __import__("fulcra_attention.fulcra", fromlist=["FulcraClient"]).FulcraClient(transport=transport, **kw),
    )
    res = CliRunner().invoke(cli, ["defs"])
    assert res.exit_code == 0, res.output
    assert "def-old" in res.output
    assert "def-mine" in res.output
    assert "THIS MACHINE" in res.output
    assert "def-unrelated" not in res.output


def test_adopt_points_local_state_at_given_definition(_isolate_state):
    """`adopt` rewrites state.json so this machine merges onto another
    machine's definition. Local-only — no Fulcra call."""
    state_mod.save(
        state_mod.State(attention_definition_id="def-old-local"),
        _isolate_state,
    )
    res = CliRunner().invoke(cli, ["adopt", "def-from-other-machine"])
    assert res.exit_code == 0, res.output
    assert "adopted: def-from-other-machine" in res.output
    s = state_mod.load(_isolate_state)
    assert s.attention_definition_id == "def-from-other-machine"
