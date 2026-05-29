"""Tests for SP5 task 1: daemon-side Fulcra access-token refresh-on-401.

Covers three layers added by SP5 task 1:

1. ``credentials.refresh_fulcra_access_token`` — shells out to the
   ``fulcra`` CLI, parses stdout, updates the keychain, and sets/clears
   the ``_refresh_failed`` flag depending on success.
2. ``credentials.is_refresh_failed`` / ``clear_refresh_failed`` — the
   read+clear surface exposed for the routes module.
3. The ``_RetryingClient`` returned by ``web.build_app``'s
   ``fulcra_http_client`` — retries once on 401 after calling the
   refresh helper, otherwise transparent.
4. The ``/api/fulcra/auth/status`` reply now includes ``refresh_failed``.
5. Both interactive sign-in paths (paste-token + cli_login) clear the
   flag on success.

These complement the existing test_web.py auth-route coverage; we keep
them in a focused file so future refresh-related changes have an obvious
home.
"""
from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

import fulcra_collect.credentials as _creds_mod
from fulcra_collect.daemon import Config, Daemon
from fulcra_collect.registry import RegistryResult
from fulcra_collect.web import _ensure_token, build_app


# ---------------------------------------------------------------------------
# Helpers (mirror test_web.py so the test reads similarly)
# ---------------------------------------------------------------------------

def _build_daemon(collect_home):
    return Daemon(registry=RegistryResult(plugins={}), config=Config())


def _client(daemon) -> TestClient:
    token = _ensure_token()
    app = build_app(daemon)
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest.fixture(autouse=True)
def _reset_refresh_flag():
    """Reset the process-level ``_refresh_failed`` flag between tests.

    ``credentials`` lives in module scope, so tests would otherwise leak
    state into one another. Saves the prior value just in case it's
    ever set by an external caller during a test session.
    """
    prior = _creds_mod._refresh_failed
    _creds_mod._refresh_failed = False
    yield
    _creds_mod._refresh_failed = prior


# ---------------------------------------------------------------------------
# refresh_fulcra_access_token
# ---------------------------------------------------------------------------

def _fake_run_factory(stdout: str, returncode: int = 0):
    def _fake_run(args, capture_output, text, timeout):  # noqa: ARG001
        return subprocess.CompletedProcess(
            args=args, returncode=returncode, stdout=stdout, stderr="",
        )
    return _fake_run


def test_refresh_calls_print_access_token_and_updates_keychain(
    _in_memory_keyring, monkeypatch,
):
    """Happy path: CLI exits 0 with a token; helper stores it + returns it."""
    monkeypatch.setattr(
        _creds_mod.shutil, "which", lambda name: "/usr/local/bin/fulcra",
    )
    seen_args: list[list[str]] = []

    def _fake_run(args, capture_output, text, timeout):  # noqa: ARG001
        seen_args.append(args)
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="fresh-token-123\n", stderr="",
        )

    monkeypatch.setattr(_creds_mod.subprocess, "run", _fake_run)

    out = _creds_mod.refresh_fulcra_access_token()

    assert out == "fresh-token-123"
    assert seen_args == [["/usr/local/bin/fulcra", "auth", "print-access-token"]]
    # Keychain updated with the new token (via set_user_secret).
    assert _creds_mod.get_user_secret("bearer-token") == "fresh-token-123"
    # And success clears any prior refresh-failed state.
    assert _creds_mod.is_refresh_failed() is False


def test_refresh_returns_none_when_cli_not_on_path(_in_memory_keyring, monkeypatch):
    """If `fulcra` isn't installed, return None and set the failed flag."""
    monkeypatch.setattr(_creds_mod.shutil, "which", lambda name: None)
    # Also block the well-known-location fallback (~/.local/bin etc.) so the
    # test is hermetic on machines where the CLI is actually installed.
    monkeypatch.setattr(_creds_mod.os.path, "isfile", lambda p: False)
    # subprocess.run shouldn't even be reached — guard via a sentinel.
    monkeypatch.setattr(
        _creds_mod.subprocess, "run",
        lambda *a, **kw: pytest.fail("subprocess.run should not be called"),
    )

    assert _creds_mod.refresh_fulcra_access_token() is None
    assert _creds_mod.is_refresh_failed() is True


def test_refresh_returns_none_on_cli_nonzero_exit(_in_memory_keyring, monkeypatch):
    """CLI exits non-zero (e.g. refresh token expired) -> failure path."""
    monkeypatch.setattr(_creds_mod.shutil, "which", lambda name: "/bin/fulcra")
    monkeypatch.setattr(
        _creds_mod.subprocess, "run", _fake_run_factory("", returncode=1),
    )

    assert _creds_mod.refresh_fulcra_access_token() is None
    assert _creds_mod.is_refresh_failed() is True
    # Keychain must NOT be poisoned with an empty value.
    assert _creds_mod.get_user_secret("bearer-token") is None


def test_refresh_returns_none_on_empty_stdout(_in_memory_keyring, monkeypatch):
    """CLI exits 0 but stdout is whitespace -> treat as failure."""
    monkeypatch.setattr(_creds_mod.shutil, "which", lambda name: "/bin/fulcra")
    monkeypatch.setattr(
        _creds_mod.subprocess, "run", _fake_run_factory("   \n", returncode=0),
    )

    assert _creds_mod.refresh_fulcra_access_token() is None
    assert _creds_mod.is_refresh_failed() is True


def test_refresh_returns_none_on_cli_timeout(_in_memory_keyring, monkeypatch):
    """A hung CLI -> timeout caught and surfaces as failure."""
    monkeypatch.setattr(_creds_mod.shutil, "which", lambda name: "/bin/fulcra")

    def _raise(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="fulcra", timeout=10)

    monkeypatch.setattr(_creds_mod.subprocess, "run", _raise)

    assert _creds_mod.refresh_fulcra_access_token() is None
    assert _creds_mod.is_refresh_failed() is True


def test_is_refresh_failed_and_clear_round_trip(_in_memory_keyring, monkeypatch):
    """`clear_refresh_failed` flips the flag back to False."""
    monkeypatch.setattr(_creds_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(_creds_mod.os.path, "isfile", lambda p: False)
    _creds_mod.refresh_fulcra_access_token()
    assert _creds_mod.is_refresh_failed() is True

    _creds_mod.clear_refresh_failed()
    assert _creds_mod.is_refresh_failed() is False


# ---------------------------------------------------------------------------
# _RetryingClient (returned by fulcra_http_client)
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Minimal httpx.Response stand-in — just status_code is enough."""

    def __init__(self, status_code: int, marker: str = "") -> None:
        self.status_code = status_code
        self.marker = marker


class _FakeInnerClient:
    """Records calls + returns a queued sequence of responses per method.

    Used to verify the wrapper's 401-retry behaviour without spinning up
    real httpx machinery.
    """

    def __init__(self, responses: dict[str, list[_FakeResponse]]) -> None:
        # Per-method response queues; popped left-to-right.
        self._responses = {k: list(v) for k, v in responses.items()}
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, tuple, dict]] = []

    def _make_method(self, name: str):
        def _call(*args, **kwargs):
            self.calls.append((name, args, dict(kwargs)))
            queue = self._responses.get(name, [])
            if not queue:
                raise AssertionError(
                    f"_FakeInnerClient: no more responses queued for {name!r}"
                )
            return queue.pop(0)
        return _call

    def __getattr__(self, name: str):
        if name in ("get", "post", "put", "delete", "patch", "head"):
            return self._make_method(name)
        raise AttributeError(name)

    # context-manager protocol — pass-through
    def __exit__(self, *exc):
        return False

    def close(self) -> None:
        return None


def _patch_http_client_factory(monkeypatch, fake_inner: _FakeInnerClient):
    """Replace ``httpx.Client`` (via fulcra_collect.web) with a callable
    that returns the given fake.

    The wrapper's __init__ reads ``_self.httpx.Client`` so we override
    that attribute. The fake ignores all init kwargs.
    """
    import fulcra_collect.web as _web

    def _factory(*args, **kwargs):  # noqa: ARG001
        # The wrapper passes base_url, timeout, headers, follow_redirects;
        # we just stash the initial headers so the test can assert on
        # header mutation through the wrapper.
        fake_inner.headers = dict(kwargs.get("headers", {}))
        return fake_inner

    # Swap only `Client` on the httpx attribute; other httpx things
    # (exceptions, etc.) still resolve to the real module.
    class _StubHttpx:
        Client = staticmethod(_factory)
        # Expose other httpx names lazily so any code that touches them
        # still works (routes/fulcra_auth.py uses _web.httpx.TimeoutException
        # etc., but the wrapper itself only needs Client).
        def __getattr__(self, name):  # noqa: D401
            import httpx as _h
            return getattr(_h, name)

    monkeypatch.setattr(_web, "httpx", _StubHttpx())


def _capture_route_context(monkeypatch) -> dict[str, object]:
    """Wrap ``RouteContext`` so build_app's ctx leaks into a dict.

    Used by the retry-wrapper tests to grab the real
    ``fulcra_http_client`` closure (which is otherwise scoped inside
    ``build_app``) and exercise it directly — no need to spin up a route
    that happens to use it.
    """
    captured: dict[str, object] = {}
    from fulcra_collect.routes import _deps as _deps_mod
    import fulcra_collect.web as _web
    real_ctx = _deps_mod.RouteContext

    def _capture(**kw):
        ctx = real_ctx(**kw)
        captured["ctx"] = ctx
        return ctx

    monkeypatch.setattr(_deps_mod, "RouteContext", _capture)
    monkeypatch.setattr(_web, "RouteContext", _capture)
    return captured


def test_retrying_client_retries_get_on_401(
    collect_home, _in_memory_keyring, monkeypatch,
):
    """First GET returns 401; wrapper calls refresh; second GET returns 200.

    Asserts refresh was invoked, the inner client's Authorization header
    was rewritten with the new token, the wrapper made two GETs (initial
    + retry), and the second (200) response is what gets returned to
    the caller.
    """
    fake = _FakeInnerClient({
        "get": [_FakeResponse(401, "stale"), _FakeResponse(200, "fresh")],
    })
    _patch_http_client_factory(monkeypatch, fake)

    refresh_calls = {"n": 0}

    def _fake_refresh():
        refresh_calls["n"] += 1
        return "new-token"

    monkeypatch.setattr(_creds_mod, "refresh_fulcra_access_token", _fake_refresh)

    captured = _capture_route_context(monkeypatch)
    daemon = _build_daemon(collect_home)
    build_app(daemon)

    client = captured["ctx"].fulcra_http_client("stale-token")
    # Initial header reflects the seeded token (via _FakeInnerClient
    # which captures the kwargs passed to httpx.Client(...)).
    assert fake.headers["Authorization"] == "Bearer stale-token"

    resp = client.get("/some/path")

    assert refresh_calls["n"] == 1
    assert fake.headers["Authorization"] == "Bearer new-token"
    assert [c[0] for c in fake.calls] == ["get", "get"]
    assert resp.status_code == 200
    assert resp.marker == "fresh"


def test_retrying_client_retries_post_on_401(
    collect_home, _in_memory_keyring, monkeypatch,
):
    """POST is also wrapped — same retry semantics as GET."""
    fake = _FakeInnerClient({
        "post": [_FakeResponse(401), _FakeResponse(200, "after-refresh")],
    })
    _patch_http_client_factory(monkeypatch, fake)
    monkeypatch.setattr(
        _creds_mod, "refresh_fulcra_access_token", lambda: "fresh-post",
    )

    captured = _capture_route_context(monkeypatch)
    daemon = _build_daemon(collect_home)
    build_app(daemon)

    client = captured["ctx"].fulcra_http_client("stale-token")
    resp = client.post("/whatever", json={"x": 1})

    assert fake.headers["Authorization"] == "Bearer fresh-post"
    assert resp.status_code == 200
    assert resp.marker == "after-refresh"


def test_retrying_client_returns_original_401_when_refresh_fails(
    collect_home, _in_memory_keyring, monkeypatch,
):
    """If refresh helper returns None, the wrapper returns the original 401.

    No second request is made — the caller sees the 401, the
    process-level ``_refresh_failed`` flag is already set by the helper,
    and ``/api/fulcra/auth/status`` surfaces it.
    """
    fake = _FakeInnerClient({"get": [_FakeResponse(401, "stale-only")]})
    _patch_http_client_factory(monkeypatch, fake)
    monkeypatch.setattr(_creds_mod, "refresh_fulcra_access_token", lambda: None)

    captured = _capture_route_context(monkeypatch)
    daemon = _build_daemon(collect_home)
    build_app(daemon)

    client = captured["ctx"].fulcra_http_client("stale-token")
    resp = client.get("/whatever")

    # Only the original call happened — no retry.
    assert [c[0] for c in fake.calls] == ["get"]
    assert resp.status_code == 401
    assert resp.marker == "stale-only"


# ---------------------------------------------------------------------------
# /api/fulcra/auth/status — reply shape
# ---------------------------------------------------------------------------

def test_auth_status_reports_refresh_failed_false_initially(
    collect_home, _in_memory_keyring,
):
    daemon = _build_daemon(collect_home)
    client = _client(daemon)
    r = client.get("/api/fulcra/auth/status")
    assert r.status_code == 200
    body = r.json()
    assert body["refresh_failed"] is False


def test_auth_status_reports_refresh_failed_true_after_failed_refresh(
    collect_home, _in_memory_keyring, monkeypatch,
):
    # Drive `_refresh_failed=True` by running a failing refresh.
    monkeypatch.setattr(_creds_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(_creds_mod.os.path, "isfile", lambda p: False)
    assert _creds_mod.refresh_fulcra_access_token() is None
    assert _creds_mod.is_refresh_failed() is True

    daemon = _build_daemon(collect_home)
    client = _client(daemon)
    r = client.get("/api/fulcra/auth/status")
    assert r.status_code == 200
    assert r.json()["refresh_failed"] is True


def test_auth_status_reports_refresh_failed_false_after_clear(
    collect_home, _in_memory_keyring, monkeypatch,
):
    monkeypatch.setattr(_creds_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(_creds_mod.os.path, "isfile", lambda p: False)
    _creds_mod.refresh_fulcra_access_token()
    _creds_mod.clear_refresh_failed()

    daemon = _build_daemon(collect_home)
    client = _client(daemon)
    r = client.get("/api/fulcra/auth/status")
    assert r.json()["refresh_failed"] is False


# ---------------------------------------------------------------------------
# Both sign-in paths clear the flag on success
# ---------------------------------------------------------------------------

def _mock_httpx_success(mocker):
    """Mock httpx.Client whose GET returns 200 — copied from test_web.py."""
    mock_resp = mocker.Mock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = mocker.Mock()
    mock_client = mocker.MagicMock()
    mock_client.__enter__ = mocker.Mock(return_value=mock_client)
    mock_client.__exit__ = mocker.Mock(return_value=False)
    mock_client.get = mocker.Mock(return_value=mock_resp)
    return mock_client


def test_paste_token_signin_clears_refresh_failed_flag(
    collect_home, _in_memory_keyring, mocker, monkeypatch,
):
    """POST /api/fulcra/auth/token sets refresh_failed -> False."""
    # Seed the flag so we can observe it being cleared.
    monkeypatch.setattr(_creds_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(_creds_mod.os.path, "isfile", lambda p: False)
    _creds_mod.refresh_fulcra_access_token()
    assert _creds_mod.is_refresh_failed() is True

    mock_client = _mock_httpx_success(mocker)
    mocker.patch("httpx.Client", return_value=mock_client)

    daemon = _build_daemon(collect_home)
    client = _client(daemon)
    r = client.post("/api/fulcra/auth/token", json={"token": "new-token"})
    assert r.status_code == 200

    assert _creds_mod.is_refresh_failed() is False


def test_cli_login_signin_clears_refresh_failed_flag(
    collect_home, _in_memory_keyring, monkeypatch, mocker,
):
    """POST /api/fulcra/auth/cli_login sets refresh_failed -> False."""
    # Seed the flag.
    monkeypatch.setattr(_creds_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(_creds_mod.os.path, "isfile", lambda p: False)
    _creds_mod.refresh_fulcra_access_token()
    assert _creds_mod.is_refresh_failed() is True

    # Now restore shutil.which so cli_login can find the CLI, and mock
    # subprocess.run for both `auth login` and `auth print-access-token`.
    import shutil as _shutil_mod
    monkeypatch.setattr(_shutil_mod, "which", lambda name: "/usr/local/bin/fulcra")

    def _fake_run(args, capture_output, text, timeout):  # noqa: ARG001
        if "login" in args:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="", stderr="",
            )
        if "print-access-token" in args:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="cli-token\n", stderr="",
            )
        raise AssertionError(f"unexpected subprocess.run args: {args}")

    import subprocess as _subprocess_mod
    monkeypatch.setattr(_subprocess_mod, "run", _fake_run)

    # Mock the post-login validation httpx GET to return 200.
    mock_client = _mock_httpx_success(mocker)
    mocker.patch("httpx.Client", return_value=mock_client)

    daemon = _build_daemon(collect_home)
    client = _client(daemon)
    r = client.post("/api/fulcra/auth/cli_login")
    assert r.status_code == 200, r.text

    assert _creds_mod.is_refresh_failed() is False


def test_refresh_finds_fulcra_in_well_known_location_when_not_on_path(
    monkeypatch, tmp_path,
) -> None:
    """When shutil.which returns None (launchd-restricted PATH), the
    helper should find the CLI in well-known install locations like
    ~/.local/bin. Closes the bug discovered in SP5 Task 4 where the
    launchd-managed daemon's PATH excluded the CLI install dir."""
    from fulcra_collect import credentials as _creds

    # Create a fake fulcra binary in tmp_path
    fake_cli = tmp_path / "fulcra"
    fake_cli.write_text("#!/bin/sh\necho fake-token\n")
    fake_cli.chmod(0o755)

    # shutil.which returns None (CLI not on PATH)
    monkeypatch.setattr(_creds.shutil, "which", lambda name: None)
    # but _find_fulcra_cli should walk the candidates list — point
    # the first candidate at our fake binary
    monkeypatch.setattr(_creds.os.path, "expanduser",
                        lambda p: str(fake_cli) if p == "~/.local/bin/fulcra" else p)

    found = _creds._find_fulcra_cli()
    assert found == str(fake_cli), f"Expected {fake_cli}, got {found}"


def test_cli_status_uses_find_fulcra_cli_fallback_when_not_on_path(
    collect_home, _in_memory_keyring, monkeypatch,
) -> None:
    """The browser-sign-in probe must resolve the CLI via
    ``credentials._find_fulcra_cli`` (which checks ~/.local/bin etc.), not
    bare ``shutil.which``. The launchd-managed daemon runs with a restricted
    PATH that excludes ~/.local/bin, so a bare ``which`` reports the CLI
    missing — blocking sign-in with "The fulcra CLI is not on PATH" — even
    when it is installed. This mirrors the refresh-path fix and closes the
    same gap on the sign-in path.
    """
    import os
    FAKE = "/fake/.local/bin/fulcra"
    _real_isfile, _real_access, _real_expand = (
        os.path.isfile, os.access, os.path.expanduser,
    )

    # Simulate launchd's restricted PATH: which() can't see fulcra...
    monkeypatch.setattr(_creds_mod.shutil, "which", lambda name: None)
    # ...but it IS installed at ~/.local/bin, which _find_fulcra_cli checks.
    monkeypatch.setattr(
        _creds_mod.os.path, "expanduser",
        lambda p: FAKE if p == "~/.local/bin/fulcra" else _real_expand(p),
    )
    monkeypatch.setattr(
        _creds_mod.os.path, "isfile",
        lambda p: True if p == FAKE else _real_isfile(p),
    )
    monkeypatch.setattr(
        _creds_mod.os, "access",
        lambda p, mode: True if p == FAKE else _real_access(p, mode),
    )

    # The probe execs `<cli> auth print-access-token`; intercept so we don't
    # try to run the (nonexistent) fake path.
    def _fake_run(args, capture_output, text, timeout):  # noqa: ARG001
        assert args[0] == FAKE, f"probe should use the resolved CLI, got {args!r}"
        return subprocess.CompletedProcess(args, 0, stdout="tok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    daemon = _build_daemon(collect_home)
    client = _client(daemon)
    r = client.get("/api/fulcra/auth/cli_status")
    assert r.status_code == 200
    assert r.json()["available"] is True


def test_refresh_returns_none_when_cli_truly_missing(monkeypatch) -> None:
    """When shutil.which AND all well-known locations come up empty,
    _find_fulcra_cli returns None and refresh_fulcra_access_token
    sets _refresh_failed=True."""
    from fulcra_collect import credentials as _creds

    monkeypatch.setattr(_creds.shutil, "which", lambda name: None)
    monkeypatch.setattr(_creds.os.path, "isfile", lambda p: False)

    _creds.clear_refresh_failed()
    result = _creds.refresh_fulcra_access_token()
    assert result is None
    assert _creds.is_refresh_failed() is True
