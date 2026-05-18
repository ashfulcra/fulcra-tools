"""CLI entry point — exercised via click's CliRunner."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from fulcra_attention import state as state_mod
from fulcra_attention.cli import cli


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(state_mod, "DEFAULT_PATH", state_path)
    monkeypatch.setenv("FULCRA_ATTENTION_STATE", str(state_path))
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    yield state_path


def test_bootstrap_creates_def_and_tags(_isolate_state, mocker):
    posted: list[dict] = []
    def responder(r: httpx.Request) -> httpx.Response:
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

    # State persisted
    s = state_mod.load(_isolate_state)
    assert s.attention_definition_id == "def-attention"


def test_bootstrap_idempotent(_isolate_state, mocker):
    # Pre-populate state with definition; transport should never be hit.
    state_mod.save(
        state_mod.State(
            attention_definition_id="def-existing",
            tag_ids={"attention": "a", "web": "w"},
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


def test_setup_generates_bearer_token_and_relay_json(_isolate_state, tmp_path, mocker, monkeypatch):
    relay_dir = tmp_path / "fulcra-attention-config"
    monkeypatch.setenv("FULCRA_ATTENTION_RELAY_JSON", str(relay_dir / "relay.json"))
    # Skip service install on the test box.
    fake_install = mocker.patch(
        "fulcra_attention.cli.service_manager.install",
        return_value=tmp_path / "fake-service-file",
    )
    res = CliRunner().invoke(cli, ["setup"])
    assert res.exit_code == 0, res.output
    relay_json = relay_dir / "relay.json"
    assert relay_json.exists()
    body = json.loads(relay_json.read_text())
    assert "bearer_token" in body and len(body["bearer_token"]) >= 40
    assert body["port"] == 8771
    # Token printed for paste-into-extension
    assert body["bearer_token"] in res.output
    fake_install.assert_called_once()


def test_setup_is_idempotent_preserves_existing_token(_isolate_state, tmp_path, mocker, monkeypatch):
    relay_json = tmp_path / "relay.json"
    relay_json.write_text(json.dumps({"bearer_token": "PRE-EXISTING", "port": 8771}))
    monkeypatch.setenv("FULCRA_ATTENTION_RELAY_JSON", str(relay_json))
    mocker.patch("fulcra_attention.cli.service_manager.install",
                 return_value=tmp_path / "fake")
    res = CliRunner().invoke(cli, ["setup"])
    assert res.exit_code == 0
    body = json.loads(relay_json.read_text())
    assert body["bearer_token"] == "PRE-EXISTING"
    assert "PRE-EXISTING" in res.output
