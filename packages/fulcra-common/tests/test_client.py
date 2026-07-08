"""Tests for the shared BaseFulcraClient."""
from __future__ import annotations

import subprocess
import urllib.error
from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
import pytest

from fulcra_common import BaseFulcraClient, ImportResult

UTC = timezone.utc


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
    """When no `fulcra` sits next to sys.executable, fall back to a PATH
    lookup so the CLI is still found. The resolver now execs the RESOLVED
    absolute path (not the bare name), so exec-time PATH no longer matters."""
    import fulcra_common.client as client_mod

    monkeypatch.delenv("FULCRA_ACCESS_TOKEN", raising=False)
    fake_python = tmp_path / "python"
    fake_python.write_text("")
    monkeypatch.setattr("fulcra_common.client.sys.executable", str(fake_python))
    monkeypatch.setattr(client_mod.shutil, "which",
                        lambda name: "/resolved/from/path/fulcra")
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"path-tok\n")

    monkeypatch.setattr("fulcra_common.client.subprocess.run", fake_run)
    assert BaseFulcraClient().get_token() == "path-tok"
    assert captured["cmd"][0] == "/resolved/from/path/fulcra"


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


def test_records_visible_returns_found_subset(recording_transport):
    """Typed ingest is async with NO server dedup and silent line drops, so
    callers verify landings by re-querying. This helper answers 'which of
    my source_ids are visible yet?'"""
    rows = [
        {"sources": ["com.fulcra.test.a", "com.fulcradynamics.annotation.d1"]},
        {"sources": ["com.fulcra.test.b"]},
    ]
    def handler(request):
        assert "/data/v1alpha1/event/MomentAnnotation" in str(request.url)
        return httpx.Response(200, json=rows)
    client = BaseFulcraClient(transport=recording_transport(handler))
    got = client.records_visible(
        "MomentAnnotation", {"com.fulcra.test.a", "com.fulcra.test.c"},
        datetime(2026, 7, 8, tzinfo=UTC), datetime(2026, 7, 9, tzinfo=UTC))
    assert got == {"com.fulcra.test.a"}


def test_import_result_is_a_plain_record():
    r = ImportResult(total=10, skipped_existing=3, posted=7, verified=7)
    assert (r.total, r.skipped_existing, r.posted, r.verified) == (10, 3, 7, 7)


# ---------------------------------------------------------------------------
# definition_exists (A2)
# ---------------------------------------------------------------------------

def _exists_client(recording_transport, handler):
    return BaseFulcraClient(transport=recording_transport(handler))


def test_definition_exists_true_when_live(recording_transport):
    """Per-id GET returns the def with deleted_at null -> True. Uses
    GET /user/v1alpha1/annotation/{id}, NOT the whole-catalog fetch —
    O(1) instead of O(catalog) per validation."""
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/user/v1alpha1/annotation/def-a"
        return httpx.Response(200, json={"id": "def-a", "deleted_at": None})

    client = _exists_client(recording_transport, handler)
    assert client.definition_exists("def-a") is True


def test_definition_exists_false_when_soft_deleted(recording_transport):
    """Verified live 2026-07-07: a soft-deleted def returns 200 WITH
    deleted_at set (not 404) — must check the field."""
    def handler(request):
        return httpx.Response(200, json={
            "id": "def-gone", "deleted_at": "2026-01-01T00:00:00Z"})

    client = _exists_client(recording_transport, handler)
    assert client.definition_exists("def-gone") is False


def test_definition_exists_false_on_403_wrong_account(recording_transport):
    """Verified live 2026-07-07: an id that isn't yours (nonexistent OR
    another account's — the exact account-switch hazard this method guards)
    returns 403, not 404. Both mean 'not valid for this account' -> False."""
    def handler(request):
        return httpx.Response(403, json={"detail": "forbidden"})

    client = _exists_client(recording_transport, handler)
    assert client.definition_exists("def-foreign") is False


def test_definition_exists_false_on_404(recording_transport):
    def handler(request):
        return httpx.Response(404, json={"detail": "not found"})

    client = _exists_client(recording_transport, handler)
    assert client.definition_exists("def-missing") is False


def test_definition_exists_true_on_auth_flake_401(recording_transport):
    """401 is an auth hiccup (expired token mid-refresh), not a verdict on
    the def — conservative True so a flake never triggers re-resolution."""
    def handler(request):
        return httpx.Response(401, json={"detail": "unauthorized"})

    client = _exists_client(recording_transport, handler)
    assert client.definition_exists("def-any") is True


def test_definition_exists_true_on_server_error(recording_transport):
    def handler(request):
        return httpx.Response(500)

    client = _exists_client(recording_transport, handler)
    assert client.definition_exists("def-any") is True


def test_definition_exists_true_on_network_exception(recording_transport):
    """Conservative: returns True on any network failure (assume-exists).
    A flaky API must never trigger spurious re-resolution."""
    def handler(request):
        raise httpx.ConnectError("boom")

    client = _exists_client(recording_transport, handler)
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


# ---------------------------------------------------------------------------
# restore_definition — undo for soft_delete_definition
# ---------------------------------------------------------------------------

def test_restore_definition_true_on_success(monkeypatch):
    """Returns True when restore_annotation succeeds (no exception)."""
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    client = BaseFulcraClient()

    fake_lib = MagicMock()
    # The lib returns the restored annotation's JSON body on success.
    fake_lib.restore_annotation.return_value = {"id": "def-1", "deleted_at": None}
    monkeypatch.setattr(BaseFulcraClient, "_lib", lambda self: fake_lib)

    assert client.restore_definition("def-1") is True
    fake_lib.restore_annotation.assert_called_once_with("def-1")


def test_restore_definition_false_on_not_found(monkeypatch):
    """Returns False when restore_annotation raises HTTPError 404 (not found)."""
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    client = BaseFulcraClient()

    fake_lib = MagicMock()
    fake_lib.restore_annotation.side_effect = urllib.error.HTTPError(
        url="http://x", code=404, msg="Not Found", hdrs=None, fp=None
    )
    monkeypatch.setattr(BaseFulcraClient, "_lib", lambda self: fake_lib)

    assert client.restore_definition("def-missing") is False


def test_restore_definition_propagates_other_errors(monkeypatch):
    """Non-404 HTTPErrors (e.g. 500) are re-raised, not swallowed."""
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    client = BaseFulcraClient()

    fake_lib = MagicMock()
    fake_lib.restore_annotation.side_effect = urllib.error.HTTPError(
        url="http://x", code=500, msg="Server Error", hdrs=None, fp=None
    )
    monkeypatch.setattr(BaseFulcraClient, "_lib", lambda self: fake_lib)

    with pytest.raises(urllib.error.HTTPError):
        client.restore_definition("def-err")


# ---------------------------------------------------------------------------
# data_updates_summary (P2 items 5+10 — data-updates gating)
# ---------------------------------------------------------------------------

def test_data_updates_summary_returns_only_data_types(recording_transport, monkeypatch):
    """Hits GET /data/v1/updates with ISO-Z start/end params and returns the
    data_types dict alone — file_changes (megabytes on busy accounts) never
    escapes to the caller."""
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")

    def responder(r: httpx.Request) -> httpx.Response:
        assert r.url.path == "/data/v1/updates"
        params = dict(r.url.params)
        assert params["start_time"] == "2026-07-05T00:00:00Z"
        assert params["end_time"] == "2026-07-06T00:00:00Z"
        return httpx.Response(200, json={
            "data_types": {"DurationAnnotation": 3, "StepCount": 412},
            "file_changes": [{"path": "huge"}] * 50,
        })

    transport = recording_transport(responder)
    client = BaseFulcraClient(transport=transport)
    out = client.data_updates_summary(
        datetime(2026, 7, 5, tzinfo=timezone.utc),
        datetime(2026, 7, 6, tzinfo=timezone.utc),
    )
    assert out == {"DurationAnnotation": 3, "StepCount": 412}


def test_data_updates_summary_empty_and_missing_data_types(recording_transport, monkeypatch):
    """A window with no activity ({} or missing data_types) yields {}."""
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    client = BaseFulcraClient(
        transport=recording_transport(
            lambda r: httpx.Response(200, json={"data_types": {}, "file_changes": []}),
        ),
    )
    t0 = datetime(2020, 6, 1, tzinfo=timezone.utc)
    t1 = datetime(2020, 7, 1, tzinfo=timezone.utc)
    assert client.data_updates_summary(t0, t1) == {}
    client2 = BaseFulcraClient(
        transport=recording_transport(lambda r: httpx.Response(200, json={})),
    )
    assert client2.data_updates_summary(t0, t1) == {}


def test_data_updates_summary_raises_on_http_error(recording_transport, monkeypatch):
    """HTTP errors raise (the live server 500s on large windows). Callers
    fail open — 'can't gate; proceed without the optimization'."""
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    client = BaseFulcraClient(
        transport=recording_transport(
            lambda r: httpx.Response(500, text="Internal Server Error"),
        ),
    )
    with pytest.raises(httpx.HTTPStatusError):
        client.data_updates_summary(
            datetime(2026, 6, 29, tzinfo=timezone.utc),
            datetime(2026, 7, 6, tzinfo=timezone.utc),
        )


def test_data_updates_summary_never_logs_file_changes(recording_transport, monkeypatch, caplog):
    """file_changes must never be materialised into a log record — it can be
    megabytes of coordination-bus churn."""
    import logging as _logging

    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    marker = "SENTINEL-FILE-CHANGE-PATH"
    client = BaseFulcraClient(
        transport=recording_transport(
            lambda r: httpx.Response(200, json={
                "data_types": {"MomentAnnotation": 1},
                "file_changes": [{"path": marker}],
            }),
        ),
    )
    with caplog.at_level(_logging.DEBUG):
        out = client.data_updates_summary(
            datetime(2026, 7, 5, tzinfo=timezone.utc),
            datetime(2026, 7, 6, tzinfo=timezone.utc),
        )
    assert out == {"MomentAnnotation": 1}
    assert marker not in caplog.text
    # And the returned dict carries no reference to file_changes content.
    assert marker not in repr(out)
# update_definition — rename/update WITHOUT orphaning history (collect P2 #7)
#
# The Fulcra PUT /user/v1alpha1/annotation/{id} is a FULL-REPLACE over a
# discriminated union (moment/duration/boolean/numeric/people/scale): every
# union member requires name+description+tags, and boolean/duration/numeric/
# scale additionally require measurement_spec (scale also requires spec).
# A partial body would null-out measurement_spec/spec and corrupt scale /
# numeric definitions — so update_definition must GET the current record,
# merge only the changed fields, and PUT the complete body back.
# ---------------------------------------------------------------------------

def _current_def(annotation_type: str) -> dict:
    """A representative GET body for each PUT-union annotation_type.

    measurement_spec / spec values are deliberately non-trivial so the
    merge tests can assert they ride through the PUT byte-identical.
    """
    base = {
        "id": "def-1",
        "annotation_type": annotation_type,
        "name": "Old Name",
        "description": "old description",
        "tags": ["tag-a", "tag-b"],
        # Record-only fields the GET returns but the PUT union does not
        # accept — the merge must NOT copy these into the PUT body.
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-06-01T00:00:00Z",
        "deleted_at": None,
        "fulcra_userid": "00000000-0000-0000-0000-000000000001",
        "fulcra_source_id": "com.fulcradynamics.annotation.def-1",
    }
    if annotation_type in ("moment", "people"):
        base["measurement_spec"] = None
        base["spec"] = None
    elif annotation_type == "scale":
        base["measurement_spec"] = {
            "measurement_type": "scale", "min": 1, "max": 10,
        }
        base["spec"] = {
            "scale": {"labels": {"1": "awful", "10": "great"}},
        }
    else:  # boolean / duration / numeric
        base["measurement_spec"] = {
            "measurement_type": annotation_type
            if annotation_type in ("boolean", "duration") else "count",
            "unit": "s" if annotation_type == "duration" else None,
        }
        base["spec"] = None
    return base


def _update_transport(recording_transport, current: dict, *,
                      get_status: int = 200, put_status: int = 200):
    """Transport answering GET with `current` and echoing the PUT body."""
    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "GET":
            return httpx.Response(get_status, json=current)
        assert r.method == "PUT"
        import json as _json
        return httpx.Response(put_status, json=_json.loads(r.content or b"{}"))
    return recording_transport(responder)


@pytest.mark.parametrize(
    "annotation_type",
    ["moment", "duration", "boolean", "numeric", "people", "scale"],
)
def test_update_definition_full_replace_preserves_specs(
    recording_transport, annotation_type,
):
    """The PUT body is the GET body with only the requested fields changed.

    For every union member: measurement_spec and spec must ride through
    IDENTICAL to what the GET returned (None stays None for moment/people;
    the scale/numeric specs are preserved exactly), annotation_type is
    unchanged, and record-only fields (created_at etc.) are not sent.
    """
    current = _current_def(annotation_type)
    transport = _update_transport(recording_transport, current)
    client = BaseFulcraClient(transport=transport)

    assert client.update_definition("def-1", name="New Name") is True

    assert [r.method for r in transport.requests] == ["GET", "PUT"]
    get_req, put_req = transport.requests
    assert get_req.url.path == "/user/v1alpha1/annotation/def-1"
    assert put_req.url.path == "/user/v1alpha1/annotation/def-1"
    # Both legs are authed.
    assert get_req.headers["Authorization"].startswith("Bearer ")
    assert put_req.headers["Authorization"].startswith("Bearer ")

    import json as _json
    body = _json.loads(put_req.content)
    assert body["name"] == "New Name"
    # Unchanged fields are the GET values, verbatim.
    assert body["annotation_type"] == annotation_type
    assert body["description"] == current["description"]
    assert body["tags"] == current["tags"]
    assert body["measurement_spec"] == current["measurement_spec"]
    assert body["spec"] == current["spec"]
    # Record-only fields must not leak into the PUT union body.
    for record_only in ("created_at", "updated_at", "deleted_at",
                        "fulcra_userid", "fulcra_source_id"):
        assert record_only not in body


def test_update_definition_merges_description_and_tags(recording_transport):
    current = _current_def("scale")
    transport = _update_transport(recording_transport, current)
    client = BaseFulcraClient(transport=transport)

    assert client.update_definition(
        "def-1", description="new desc", tags=["tag-z"],
    ) is True

    import json as _json
    body = _json.loads(transport.requests[1].content)
    assert body["description"] == "new desc"
    assert body["tags"] == ["tag-z"]
    # Name untouched; specs preserved exactly.
    assert body["name"] == "Old Name"
    assert body["measurement_spec"] == current["measurement_spec"]
    assert body["spec"] == current["spec"]


def test_update_definition_false_on_get_404(recording_transport):
    """Unknown id on the GET leg → False, and NO PUT is attempted."""
    transport = _update_transport(
        recording_transport, {"detail": "not found"}, get_status=404,
    )
    client = BaseFulcraClient(transport=transport)
    assert client.update_definition("def-missing", name="X") is False
    assert [r.method for r in transport.requests] == ["GET"]


def test_update_definition_false_on_put_404(recording_transport):
    """A 404 on the PUT leg (deleted between GET and PUT) → False."""
    transport = _update_transport(
        recording_transport, _current_def("moment"), put_status=404,
    )
    client = BaseFulcraClient(transport=transport)
    assert client.update_definition("def-1", name="X") is False


def test_update_definition_propagates_non_404_errors(recording_transport):
    """A 500 from Fulcra raises — never silently swallowed."""
    transport = _update_transport(
        recording_transport, _current_def("moment"), put_status=500,
    )
    client = BaseFulcraClient(transport=transport)
    with pytest.raises(httpx.HTTPStatusError):
        client.update_definition("def-1", name="X")

    transport2 = _update_transport(
        recording_transport, {"detail": "boom"}, get_status=503,
    )
    client2 = BaseFulcraClient(transport=transport2)
    with pytest.raises(httpx.HTTPStatusError):
        client2.update_definition("def-1", name="X")


def test_update_definition_rejects_forbidden_fields(recording_transport):
    """annotation_type / measurement_spec / spec changes are a different,
    dangerous operation — reject with ValueError BEFORE any HTTP request."""
    transport = _update_transport(recording_transport, _current_def("scale"))
    client = BaseFulcraClient(transport=transport)
    for forbidden in (
        {"annotation_type": "numeric"},
        {"measurement_spec": {"measurement_type": "count"}},
        {"spec": {"scale": {}}},
    ):
        with pytest.raises(ValueError, match="name.*description.*tags"):
            client.update_definition("def-1", name="X", **forbidden)
    assert transport.requests == []


def test_update_definition_rejects_empty_update(recording_transport):
    """No fields (or all-None fields) → ValueError, no HTTP traffic."""
    transport = _update_transport(recording_transport, _current_def("moment"))
    client = BaseFulcraClient(transport=transport)
    with pytest.raises(ValueError, match="at least one"):
        client.update_definition("def-1")
    with pytest.raises(ValueError, match="at least one"):
        client.update_definition("def-1", name=None, description=None, tags=None)
    assert transport.requests == []


def test_get_token_finds_cli_in_well_known_locations(monkeypatch, tmp_path):
    """Under launchd the daemon gets PATH=/usr/bin:/bin:/usr/sbin:/sbin and
    the CLI lives at ~/.local/bin/fulcra (uv tool install) — neither the venv
    sibling nor bare PATH resolution finds it, and every worker-side client
    call died with 'fulcra CLI not found' (live failure 2026-06-10; the
    collect daemon needed a manual venv symlink). get_token must fall back to
    the same well-known locations collect's credentials._find_fulcra_cli uses."""
    import fulcra_common.client as client_mod

    monkeypatch.delenv("FULCRA_ACCESS_TOKEN", raising=False)
    # No venv sibling, nothing on PATH.
    monkeypatch.setattr(client_mod.sys, "executable", str(tmp_path / "venv" / "python"))
    monkeypatch.setattr(client_mod.shutil, "which", lambda name: None)
    # A fake HOME with the CLI at ~/.local/bin/fulcra.
    home = tmp_path / "home"
    cli = home / ".local" / "bin" / "fulcra"
    cli.parent.mkdir(parents=True)
    cli.write_text("#!/bin/sh\necho well-known-tok\n")
    cli.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))

    assert BaseFulcraClient().get_token() == "well-known-tok"


def test_get_token_error_when_cli_nowhere(monkeypatch, tmp_path):
    """When the CLI is genuinely absent everywhere, the error must stay the
    clear actionable RuntimeError (not a FileNotFoundError traceback)."""
    import pytest
    import fulcra_common.client as client_mod

    monkeypatch.delenv("FULCRA_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(client_mod.sys, "executable", str(tmp_path / "venv" / "python"))
    monkeypatch.setattr(client_mod.shutil, "which", lambda name: None)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))

    with pytest.raises(RuntimeError, match="fulcra CLI not found"):
        BaseFulcraClient().get_token()
