"""Tests for the shared BaseFulcraClient."""
from __future__ import annotations

import json
import subprocess
import urllib.error
from datetime import datetime, timezone
from unittest.mock import MagicMock

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


def test_lib_builds_non_expiring_client(monkeypatch):
    """_lib() must build a FulcraAPI whose credentials are NOT expired.

    Regression guard: `FulcraAPI(access_token=...)` leaves the expiration
    None, so `is_expired()` is True on the first call and the lib then
    attempts an impossible token refresh ("No refresh token available"),
    breaking every real network call. The other _lib() tests mock _lib()
    wholesale and so never exercised the real client construction — this
    test does NOT mock _lib(); it only patches get_token() and inspects the
    real client the method built.
    """
    monkeypatch.setattr(BaseFulcraClient, "get_token", lambda self: "dummy-tok")
    client = BaseFulcraClient()

    lib = client._lib()
    creds = lib.fulcra_credentials
    # The whole point: never expired, so the lib never tries to refresh.
    assert creds.is_expired() is False
    assert creds.access_token == "dummy-tok"
    # Expiration must be naive — is_expired() compares against a naive
    # datetime.now() and a tz-aware value would raise TypeError.
    assert creds.access_token_expiration.tzinfo is None
    # Cached: a second call returns the same client.
    assert client._lib() is lib


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


def test_resolve_tag_uses_lib_get_when_tag_exists(monkeypatch):
    """_resolve_tag returns the existing tag id via the lib without creating."""
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    client = BaseFulcraClient()

    fake_lib = MagicMock()
    fake_lib.get_tag_by_name.return_value = {"id": "eid", "name": "existing"}
    monkeypatch.setattr(BaseFulcraClient, "_lib", lambda self: fake_lib, raising=False)

    result = client._resolve_tag("existing")
    assert result == "eid"
    fake_lib.get_tag_by_name.assert_called_once_with("existing")
    fake_lib.create_tag.assert_not_called()


def test_resolve_tag_uses_lib_get_then_create_on_not_found(monkeypatch):
    """_resolve_tag creates via the lib when get_tag_by_name signals not-found."""
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    client = BaseFulcraClient()

    fake_lib = MagicMock()
    not_found = urllib.error.HTTPError(
        url="http://x", code=404, msg="Not Found", hdrs=None, fp=None
    )
    fake_lib.get_tag_by_name.side_effect = not_found
    # create_tag returns a list per the lib spec; new tag is [0]
    fake_lib.create_tag.return_value = [{"id": "tid", "name": "brand-new"}]
    monkeypatch.setattr(BaseFulcraClient, "_lib", lambda self: fake_lib, raising=False)

    result = client._resolve_tag("brand-new")
    assert result == "tid"
    fake_lib.get_tag_by_name.assert_called_once_with("brand-new")
    fake_lib.create_tag.assert_called_once_with("brand-new")


def test_resolve_tag_handles_colon_in_name(monkeypatch):
    """Real Agent-Tasks tag names contain colons (agent:claude, session:Mac).

    The lib does not percent-encode the name it puts in the lookup path, so
    _resolve_tag must encode it. Verify a colon name resolves: the LOOKUP
    receives the percent-encoded name (agent%3Aclaude) and the returned id is
    passed through intact.
    """
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    client = BaseFulcraClient()

    fake_lib = MagicMock()
    fake_lib.get_tag_by_name.return_value = {"id": "tag-agent-claude", "name": "agent:claude"}
    monkeypatch.setattr(BaseFulcraClient, "_lib", lambda self: fake_lib, raising=False)

    result = client._resolve_tag("agent:claude")
    assert result == "tag-agent-claude"
    # The colon must be percent-encoded for the GET lookup path.
    fake_lib.get_tag_by_name.assert_called_once_with("agent%3Aclaude")
    fake_lib.create_tag.assert_not_called()


def test_resolve_tag_creates_colon_name_with_raw_body(monkeypatch):
    """When a colon tag is missing, it is created with the RAW (un-encoded)
    name in the create_tag JSON body — only the lookup PATH gets encoded."""
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    client = BaseFulcraClient()

    fake_lib = MagicMock()
    not_found = urllib.error.HTTPError(
        url="http://x", code=404, msg="Not Found", hdrs=None, fp=None
    )
    fake_lib.get_tag_by_name.side_effect = not_found
    fake_lib.create_tag.return_value = [{"id": "tag-sess", "name": "session:Mac"}]
    monkeypatch.setattr(BaseFulcraClient, "_lib", lambda self: fake_lib, raising=False)

    result = client._resolve_tag("session:Mac")
    assert result == "tag-sess"
    # Lookup path is encoded; create body uses the raw name.
    fake_lib.get_tag_by_name.assert_called_once_with("session%3AMac")
    fake_lib.create_tag.assert_called_once_with("session:Mac")


def test_resolve_tag_returns_existing_tag(monkeypatch):
    # Kept for historical compat; superseded by test_resolve_tag_uses_lib_get_when_tag_exists.
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    client = BaseFulcraClient()
    fake_lib = MagicMock()
    fake_lib.get_tag_by_name.return_value = {"id": "tag-web", "name": "web"}
    monkeypatch.setattr(BaseFulcraClient, "_lib", lambda self: fake_lib, raising=False)
    assert client._resolve_tag("web") == "tag-web"


def test_resolve_tag_creates_when_missing(monkeypatch):
    # Kept for historical compat; superseded by test_resolve_tag_uses_lib_get_then_create_on_not_found.
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    client = BaseFulcraClient()
    fake_lib = MagicMock()
    not_found = urllib.error.HTTPError(
        url="http://x", code=404, msg="Not Found", hdrs=None, fp=None
    )
    fake_lib.get_tag_by_name.side_effect = not_found
    fake_lib.create_tag.return_value = [{"id": "tag-new", "name": "brand-new"}]
    monkeypatch.setattr(BaseFulcraClient, "_lib", lambda self: fake_lib, raising=False)
    assert client._resolve_tag("brand-new") == "tag-new"
    fake_lib.create_tag.assert_called_once_with("brand-new")


def test_resolve_tag_quote_name_encodes_the_lookup_path(monkeypatch):
    # The lib does NOT URL-encode the name, so _resolve_tag percent-encodes
    # it for the lookup path itself. A '/' must reach get_tag_by_name already
    # encoded as %2F so it stays one path segment. quote_name is compat-only
    # (encoding is unconditional now); we pass it to prove it's still accepted.
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    client = BaseFulcraClient()
    fake_lib = MagicMock()
    fake_lib.get_tag_by_name.return_value = {"id": "x", "name": "a/b"}
    monkeypatch.setattr(BaseFulcraClient, "_lib", lambda self: fake_lib, raising=False)
    result = client._resolve_tag("a/b", quote_name=True)
    assert result == "x"
    # The lookup path name is percent-encoded; the slash becomes %2F.
    fake_lib.get_tag_by_name.assert_called_once_with("a%2Fb")


def test_soft_delete_definition_true_on_204(monkeypatch):
    # Legacy name kept for history; now exercises the lib path.
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    client = BaseFulcraClient()
    fake_lib = MagicMock()
    fake_lib.delete_annotation.return_value = None
    monkeypatch.setattr(BaseFulcraClient, "_lib", lambda self: fake_lib)
    assert client.soft_delete_definition("def-1") is True


def test_soft_delete_definition_false_on_404(monkeypatch):
    # Legacy name kept for history; now exercises the lib path.
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    client = BaseFulcraClient()
    fake_lib = MagicMock()
    fake_lib.delete_annotation.side_effect = urllib.error.HTTPError(
        url="http://x", code=404, msg="Not Found", hdrs=None, fp=None
    )
    monkeypatch.setattr(BaseFulcraClient, "_lib", lambda self: fake_lib)
    assert client.soft_delete_definition("missing") is False


def test_fetch_records_normalises_list_and_data_envelope(monkeypatch):
    """fetch_records routes through the lib's fulcra_v1_api (raw bytes) and
    normalises both the bare-list and `{"data": [...]}` envelope to a list.

    The lib returns bytes; fetch_records json.loads them. We also assert the
    generic event call is made with the right data_type and that the window
    params keep the `...Z` ISO formatting.
    """
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    t0 = datetime(2026, 5, 21, tzinfo=timezone.utc)
    t1 = datetime(2026, 5, 22, tzinfo=timezone.utc)

    # Bare-list response shape.
    fake_lib = MagicMock()
    fake_lib.fulcra_v1_api.return_value = json.dumps([{"source_id": "a"}]).encode()
    monkeypatch.setattr(BaseFulcraClient, "_lib", lambda self: fake_lib)
    client = BaseFulcraClient()
    assert client.fetch_records(t0, t1) == [{"source_id": "a"}]
    # Generic event call: data_class "event", default data_type, Z-formatted window.
    fake_lib.fulcra_v1_api.assert_called_once_with(
        "event",
        "DurationAnnotation",
        {
            "start_time": "2026-05-21T00:00:00Z",
            "end_time": "2026-05-22T00:00:00Z",
        },
    )

    # `{"data": [...]}` envelope shape.
    fake_lib2 = MagicMock()
    fake_lib2.fulcra_v1_api.return_value = json.dumps(
        {"data": [{"source_id": "b"}]}
    ).encode()
    monkeypatch.setattr(BaseFulcraClient, "_lib", lambda self: fake_lib2)
    client2 = BaseFulcraClient()
    assert client2.fetch_records(t0, t1) == [{"source_id": "b"}]


def test_fetch_existing_source_ids_collects_and_filters_by_def(monkeypatch):
    records = [
        {"source_id": "com.fulcradynamics.annotation.def-keep",
         "metadata": {"source": ["src-1", "com.fulcradynamics.annotation.def-keep"]}},
        {"source_id": "com.fulcradynamics.annotation.def-orphan",
         "metadata": {"source": ["src-orphan"]}},
    ]
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    fake_lib = MagicMock()
    fake_lib.fulcra_v1_api.return_value = json.dumps(records).encode()
    monkeypatch.setattr(BaseFulcraClient, "_lib", lambda self: fake_lib)
    client = BaseFulcraClient()
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


# ---------------------------------------------------------------------------
# definition_exists (A2)
# ---------------------------------------------------------------------------

def test_definition_exists_true_when_present_and_not_deleted(monkeypatch):
    """Returns True when the id is in the catalog and has no deleted_at."""
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    client = BaseFulcraClient()

    fake_lib = MagicMock()
    fake_lib.annotations_catalog.return_value = [
        {"id": "def-a", "deleted_at": None},
        {"id": "def-b"},
    ]
    monkeypatch.setattr(BaseFulcraClient, "_lib", lambda self: fake_lib)

    assert client.definition_exists("def-a") is True
    fake_lib.annotations_catalog.assert_called_once()


def test_definition_exists_false_when_absent(monkeypatch):
    """Returns False when the id is not in the catalog at all."""
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    client = BaseFulcraClient()

    fake_lib = MagicMock()
    fake_lib.annotations_catalog.return_value = [
        {"id": "def-other"},
    ]
    monkeypatch.setattr(BaseFulcraClient, "_lib", lambda self: fake_lib)

    assert client.definition_exists("def-missing") is False


def test_definition_exists_false_when_soft_deleted(monkeypatch):
    """Returns False when the id is present but has a non-None deleted_at."""
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    client = BaseFulcraClient()

    fake_lib = MagicMock()
    fake_lib.annotations_catalog.return_value = [
        {"id": "def-gone", "deleted_at": "2026-01-01T00:00:00Z"},
    ]
    monkeypatch.setattr(BaseFulcraClient, "_lib", lambda self: fake_lib)

    assert client.definition_exists("def-gone") is False


def test_definition_exists_true_on_lib_exception(monkeypatch):
    """Conservative: returns True on any network/lib failure (assume-exists).

    A flaky API must never trigger spurious re-resolution.
    """
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    client = BaseFulcraClient()

    fake_lib = MagicMock()
    fake_lib.annotations_catalog.side_effect = Exception("network error")
    monkeypatch.setattr(BaseFulcraClient, "_lib", lambda self: fake_lib)

    assert client.definition_exists("def-any") is True


# ---------------------------------------------------------------------------
# soft_delete_definition (A2)
# ---------------------------------------------------------------------------

def test_soft_delete_definition_true_on_success(monkeypatch):
    """Returns True when delete_annotation succeeds (no exception)."""
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    client = BaseFulcraClient()

    fake_lib = MagicMock()
    fake_lib.delete_annotation.return_value = None  # lib returns nothing on success
    monkeypatch.setattr(BaseFulcraClient, "_lib", lambda self: fake_lib)

    assert client.soft_delete_definition("def-1") is True
    fake_lib.delete_annotation.assert_called_once_with("def-1")


def test_soft_delete_definition_false_on_not_found(monkeypatch):
    """Returns False when delete_annotation raises HTTPError 404 (not found)."""
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    client = BaseFulcraClient()

    fake_lib = MagicMock()
    fake_lib.delete_annotation.side_effect = urllib.error.HTTPError(
        url="http://x", code=404, msg="Not Found", hdrs=None, fp=None
    )
    monkeypatch.setattr(BaseFulcraClient, "_lib", lambda self: fake_lib)

    assert client.soft_delete_definition("def-missing") is False


def test_soft_delete_definition_propagates_other_errors(monkeypatch):
    """Non-404 HTTPErrors (e.g. 500) are re-raised, not swallowed."""
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    client = BaseFulcraClient()

    fake_lib = MagicMock()
    fake_lib.delete_annotation.side_effect = urllib.error.HTTPError(
        url="http://x", code=500, msg="Server Error", hdrs=None, fp=None
    )
    monkeypatch.setattr(BaseFulcraClient, "_lib", lambda self: fake_lib)

    with pytest.raises(urllib.error.HTTPError):
        client.soft_delete_definition("def-err")
