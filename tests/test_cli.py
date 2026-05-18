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
