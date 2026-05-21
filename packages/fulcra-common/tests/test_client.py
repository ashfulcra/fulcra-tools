"""Tests for the shared BaseFulcraClient."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

import httpx
import pytest

from fulcra_common import BaseFulcraClient, ImportResult


def test_get_token_prefers_env_var(monkeypatch):
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "tok-from-env")
    # With the env var set, get_token must not shell out to the CLI.
    assert BaseFulcraClient().get_token() == "tok-from-env"


def test_authed_headers_carry_the_bearer_token(monkeypatch):
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "abc123")
    assert BaseFulcraClient()._authed_headers() == {"Authorization": "Bearer abc123"}


def test_get_token_shells_out_when_env_unset(monkeypatch):
    """With FULCRA_ACCESS_TOKEN unset, get_token runs
    `fulcra auth print-access-token` with a 30s timeout."""
    monkeypatch.delenv("FULCRA_ACCESS_TOKEN", raising=False)
    calls: list[tuple] = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"shell-tok\n")

    monkeypatch.setattr("fulcra_common.client.subprocess.run", fake_run)
    assert BaseFulcraClient().get_token() == "shell-tok"
    cmd, kwargs = calls[0]
    assert cmd[0].endswith("fulcra")
    assert cmd[1:] == ["auth", "print-access-token"]
    assert kwargs == {"check": True, "capture_output": True, "timeout": 30}


def test_get_token_falls_back_to_path_when_sibling_missing(monkeypatch, tmp_path):
    """When no `fulcra` sits next to sys.executable, fall back to a bare
    PATH lookup so the CLI is still found."""
    monkeypatch.delenv("FULCRA_ACCESS_TOKEN", raising=False)
    fake_python = tmp_path / "python"
    fake_python.write_text("")
    monkeypatch.setattr("fulcra_common.client.sys.executable", str(fake_python))
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"path-tok\n")

    monkeypatch.setattr("fulcra_common.client.subprocess.run", fake_run)
    assert BaseFulcraClient().get_token() == "path-tok"
    assert captured["cmd"][0] == "fulcra"  # bare PATH lookup


def test_get_token_raises_runtimeerror_on_cli_failure(monkeypatch):
    monkeypatch.delenv("FULCRA_ACCESS_TOKEN", raising=False)

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(returncode=1, cmd=cmd, stderr=b"not logged in")

    monkeypatch.setattr("fulcra_common.client.subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="fulcra auth print-access-token failed"):
        BaseFulcraClient().get_token()


def test_resolve_tag_returns_existing_tag(recording_transport):
    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "GET" and r.url.path == "/user/v1alpha1/tag/name/web":
            return httpx.Response(200, json={"id": "tag-web"})
        raise AssertionError(f"unexpected {r.method} {r.url}")

    client = BaseFulcraClient(transport=recording_transport(responder))
    assert client._resolve_tag("web") == "tag-web"


def test_resolve_tag_creates_when_missing(recording_transport):
    posted: list[dict] = []

    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "GET" and "/tag/name/" in r.url.path:
            return httpx.Response(404)
        if r.method == "POST" and r.url.path == "/user/v1alpha1/tag":
            posted.append(json.loads(r.content))
            return httpx.Response(200, json={"id": "tag-new"})
        raise AssertionError(f"unexpected {r.method} {r.url}")

    client = BaseFulcraClient(transport=recording_transport(responder))
    assert client._resolve_tag("brand-new") == "tag-new"
    assert posted == [{"name": "brand-new"}]


def test_resolve_tag_quote_name_encodes_the_lookup_path(recording_transport):
    seen: list[bytes] = []

    def responder(r: httpx.Request) -> httpx.Response:
        seen.append(r.url.raw_path)
        return httpx.Response(200, json={"id": "x"})

    client = BaseFulcraClient(transport=recording_transport(responder))
    # A '/' in the tag name must be percent-encoded so it stays one path
    # segment instead of splitting the lookup path.
    client._resolve_tag("a/b", quote_name=True)
    assert seen[0].endswith(b"/user/v1alpha1/tag/name/a%2Fb")


def test_soft_delete_definition_true_on_204(recording_transport):
    client = BaseFulcraClient(
        transport=recording_transport(lambda r: httpx.Response(204)),
    )
    assert client.soft_delete_definition("def-1") is True


def test_soft_delete_definition_false_on_404(recording_transport):
    client = BaseFulcraClient(
        transport=recording_transport(lambda r: httpx.Response(404)),
    )
    assert client.soft_delete_definition("missing") is False


def test_fetch_records_normalises_list_and_data_envelope(recording_transport):
    def responder(r: httpx.Request) -> httpx.Response:
        # Bare-list response shape.
        return httpx.Response(200, json=[{"source_id": "a"}])

    client = BaseFulcraClient(transport=recording_transport(responder))
    t0 = datetime(2026, 5, 21, tzinfo=timezone.utc)
    t1 = datetime(2026, 5, 22, tzinfo=timezone.utc)
    assert client.fetch_records(t0, t1) == [{"source_id": "a"}]

    client2 = BaseFulcraClient(
        transport=recording_transport(
            lambda r: httpx.Response(200, json={"data": [{"source_id": "b"}]}),
        ),
    )
    assert client2.fetch_records(t0, t1) == [{"source_id": "b"}]


def test_fetch_existing_source_ids_collects_and_filters_by_def(recording_transport):
    records = [
        {"source_id": "com.fulcradynamics.annotation.def-keep",
         "metadata": {"source": ["src-1", "com.fulcradynamics.annotation.def-keep"]}},
        {"source_id": "com.fulcradynamics.annotation.def-orphan",
         "metadata": {"source": ["src-orphan"]}},
    ]
    client = BaseFulcraClient(
        transport=recording_transport(lambda r: httpx.Response(200, json=records)),
    )
    t0 = datetime(2026, 5, 21, tzinfo=timezone.utc)
    t1 = datetime(2026, 5, 22, tzinfo=timezone.utc)
    # No filter: every source string is collected.
    assert client.fetch_existing_source_ids(t0, t1) == {
        "src-1", "com.fulcradynamics.annotation.def-keep", "src-orphan",
    }
    # Filtered: the orphan record (wrong def) is dropped.
    assert client.fetch_existing_source_ids(
        t0, t1, only_for_defs={"com.fulcradynamics.annotation.def-keep"},
    ) == {"src-1", "com.fulcradynamics.annotation.def-keep"}


def test_import_result_is_a_plain_record():
    r = ImportResult(total=10, skipped_existing=3, posted=7, verified=7)
    assert (r.total, r.skipped_existing, r.posted, r.verified) == (10, 3, 7, 7)
