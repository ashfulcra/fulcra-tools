"""Tests for the non-interactive wizard sign-in flow (collect review P2 #11).

fulcra-api 0.1.35 adds ``fulcra auth login --get-auth-url``: it prints the
web-auth URL + verification code + device code and exits immediately, and
the flow is completed later with ``fulcra auth login --device-code <CODE>``
(which polls, like the classic interactive login). The daemon exposes this
as two endpoints so the web UI can render a *clickable* sign-in link
instead of relying on the daemon subprocess opening a browser tab — which
never works under launchd / SSH / headless sessions:

- ``POST /api/fulcra/auth/cli_login_start`` — runs ``--get-auth-url``,
  parses stdout, returns ``{auth_url, web_auth_code, device_code, ...}``.
- ``POST /api/fulcra/auth/cli_login_poll`` — completes via
  ``--device-code``, then stores the token exactly like the classic
  ``cli_login`` path (same keychain slot, same validation).

Mocking patterns mirror test_fulcra_refresh.py.
"""
from __future__ import annotations

import logging
import subprocess

import pytest
from fastapi.testclient import TestClient

import fulcra_collect.credentials as _creds_mod
from fulcra_collect.daemon import Config, Daemon
from fulcra_collect.registry import RegistryResult
from fulcra_collect.routes.fulcra_auth import _parse_get_auth_url_output
from fulcra_collect.web import _ensure_token, build_app


# ---------------------------------------------------------------------------
# Helpers (mirror test_fulcra_refresh.py so the tests read similarly)
# ---------------------------------------------------------------------------

def _build_daemon(collect_home):
    return Daemon(registry=RegistryResult(plugins={}), config=Config())


def _client(daemon) -> TestClient:
    token = _ensure_token()
    app = build_app(daemon)
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# Captured verbatim from `fulcra auth login --get-auth-url` (fulcra-api
# 0.1.35, live run 2026-07-06). Codes below are from an abandoned flow.
GET_AUTH_URL_STDOUT = """\
Open the web auth URL in a browser, verify the web auth code, and complete the web auth flow.

Web auth URL: https://fulcra.us.auth0.com/activate?user_code=NQSW-TZHN
- Web auth code: NQSW-TZHN
- Device code: GkI_65iHqAeTVNxsPtpJ1KgH

After finishing the web auth flow, complete authentication with the device code by running:

fulcra-api auth login --device-code GkI_65iHqAeTVNxsPtpJ1KgH
"""


def _fake_run_factory(routes):
    """Build a subprocess.run stand-in routed by argv contents.

    ``routes`` maps a distinguishing argv token -> (returncode, stdout,
    stderr) or an exception instance to raise.
    """
    seen: list[list[str]] = []

    def _fake_run(args, capture_output, text, timeout):  # noqa: ARG001
        seen.append(list(args))
        for token, result in routes.items():
            if token in args:
                if isinstance(result, BaseException):
                    raise result
                rc, out, err = result
                return subprocess.CompletedProcess(
                    args=args, returncode=rc, stdout=out, stderr=err,
                )
        raise AssertionError(f"unexpected subprocess.run args: {args}")

    _fake_run.seen = seen
    return _fake_run


@pytest.fixture
def cli_on_path(monkeypatch):
    monkeypatch.setattr(
        _creds_mod, "_find_fulcra_cli", lambda: "/usr/local/bin/fulcra",
    )


def _mock_httpx_success(mocker):
    """Mock httpx.Client whose GET returns 200 — same as test_fulcra_refresh."""
    mock_resp = mocker.Mock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = mocker.Mock()
    mock_client = mocker.MagicMock()
    mock_client.__enter__ = mocker.Mock(return_value=mock_client)
    mock_client.__exit__ = mocker.Mock(return_value=False)
    mock_client.get = mocker.Mock(return_value=mock_resp)
    return mock_client


# ---------------------------------------------------------------------------
# Output parser
# ---------------------------------------------------------------------------

def test_parse_get_auth_url_output_live_shape():
    parsed = _parse_get_auth_url_output(GET_AUTH_URL_STDOUT)
    assert parsed == {
        "auth_url": "https://fulcra.us.auth0.com/activate?user_code=NQSW-TZHN",
        "web_auth_code": "NQSW-TZHN",
        "device_code": "GkI_65iHqAeTVNxsPtpJ1KgH",
    }


def test_parse_get_auth_url_output_rejects_garbage():
    assert _parse_get_auth_url_output("Signed in!\n") is None
    assert _parse_get_auth_url_output("") is None
    # URL without a device code is unusable — we can't complete the flow.
    assert _parse_get_auth_url_output("Web auth URL: https://x.example\n") is None


# ---------------------------------------------------------------------------
# POST /api/fulcra/auth/cli_login_start
# ---------------------------------------------------------------------------

def test_cli_login_start_returns_url_and_codes(
    collect_home, _in_memory_keyring, cli_on_path, monkeypatch,
):
    fake_run = _fake_run_factory({
        "--get-auth-url": (0, GET_AUTH_URL_STDOUT, ""),
    })
    monkeypatch.setattr(subprocess, "run", fake_run)

    client = _client(_build_daemon(collect_home))
    r = client.post("/api/fulcra/auth/cli_login_start")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["auth_url"] == (
        "https://fulcra.us.auth0.com/activate?user_code=NQSW-TZHN"
    )
    assert body["web_auth_code"] == "NQSW-TZHN"
    assert body["device_code"] == "GkI_65iHqAeTVNxsPtpJ1KgH"
    assert "expires_hint" in body
    # And the CLI was invoked with the non-interactive flag.
    assert fake_run.seen == [
        ["/usr/local/bin/fulcra", "auth", "login", "--get-auth-url"],
    ]


def test_cli_login_start_never_logs_device_code(
    collect_home, _in_memory_keyring, cli_on_path, monkeypatch, caplog,
):
    """The device code is a bearer-equivalent secret pre-approval; the
    daemon log must never contain it verbatim."""
    monkeypatch.setattr(
        subprocess, "run",
        _fake_run_factory({"--get-auth-url": (0, GET_AUTH_URL_STDOUT, "")}),
    )
    client = _client(_build_daemon(collect_home))
    with caplog.at_level(logging.DEBUG):
        r = client.post("/api/fulcra/auth/cli_login_start")
    assert r.status_code == 200
    assert "GkI_65iHqAeTVNxsPtpJ1KgH" not in caplog.text


def test_cli_login_start_old_cli_without_flag_is_409_fallback(
    collect_home, _in_memory_keyring, cli_on_path, monkeypatch,
):
    """A pre-0.1.35 CLI rejects the flag (click: 'No such option'). The
    daemon signals 409 so the web UI falls back to the classic blocking
    cli_login flow."""
    monkeypatch.setattr(
        subprocess, "run",
        _fake_run_factory({
            "--get-auth-url": (
                2, "", "Error: No such option: --get-auth-url\n",
            ),
        }),
    )
    client = _client(_build_daemon(collect_home))
    r = client.post("/api/fulcra/auth/cli_login_start")
    assert r.status_code == 409
    assert "fallback" in r.json()["detail"].lower()


def test_cli_login_start_unparseable_stdout_is_409_fallback(
    collect_home, _in_memory_keyring, cli_on_path, monkeypatch,
):
    """Exit 0 but output we can't parse (future CLI reshaping the text):
    degrade to the classic flow rather than 500."""
    monkeypatch.setattr(
        subprocess, "run",
        _fake_run_factory({"--get-auth-url": (0, "something new\n", "")}),
    )
    client = _client(_build_daemon(collect_home))
    r = client.post("/api/fulcra/auth/cli_login_start")
    assert r.status_code == 409
    assert "fallback" in r.json()["detail"].lower()


def test_cli_login_start_cli_missing_is_424(
    collect_home, _in_memory_keyring, monkeypatch,
):
    """Same error shape as the classic cli_login when the CLI is absent."""
    monkeypatch.setattr(_creds_mod, "_find_fulcra_cli", lambda: None)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: pytest.fail("subprocess.run should not be called"),
    )
    client = _client(_build_daemon(collect_home))
    r = client.post("/api/fulcra/auth/cli_login_start")
    assert r.status_code == 424
    assert "not on PATH" in r.json()["detail"]


def test_cli_login_start_other_cli_error_is_400_with_stderr_tail(
    collect_home, _in_memory_keyring, cli_on_path, monkeypatch,
):
    monkeypatch.setattr(
        subprocess, "run",
        _fake_run_factory({
            "--get-auth-url": (1, "", "Network error: could not reach Auth0\n"),
        }),
    )
    client = _client(_build_daemon(collect_home))
    r = client.post("/api/fulcra/auth/cli_login_start")
    assert r.status_code == 400
    assert "Network error: could not reach Auth0" in r.json()["detail"]


# ---------------------------------------------------------------------------
# POST /api/fulcra/auth/cli_login_poll
# ---------------------------------------------------------------------------

def test_cli_login_poll_success_stores_token_like_cli_login(
    collect_home, _in_memory_keyring, cli_on_path, monkeypatch, mocker,
):
    """Completion stores the token via the exact same keychain write path
    as the classic cli_login: credentials slot 'bearer-token', and the
    refresh-failed flag is cleared."""
    _creds_mod._refresh_failed = True  # seed so we observe the clear

    fake_run = _fake_run_factory({
        "--device-code": (0, "Authenticated.\n", ""),
        "print-access-token": (0, "poll-token-xyz\n", ""),
    })
    monkeypatch.setattr(subprocess, "run", fake_run)
    mocker.patch("httpx.Client", return_value=_mock_httpx_success(mocker))

    client = _client(_build_daemon(collect_home))
    r = client.post(
        "/api/fulcra/auth/cli_login_poll",
        json={"device_code": "GkI_65iHqAeTVNxsPtpJ1KgH"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    # Same keychain slot the paste-token and cli_login paths use.
    assert _creds_mod.get_user_secret("bearer-token") == "poll-token-xyz"
    assert _creds_mod.is_refresh_failed() is False
    # The CLI was driven with the device code, then print-access-token.
    assert fake_run.seen == [
        ["/usr/local/bin/fulcra", "auth", "login",
         "--device-code", "GkI_65iHqAeTVNxsPtpJ1KgH"],
        ["/usr/local/bin/fulcra", "auth", "print-access-token"],
    ]

    _creds_mod._refresh_failed = False  # restore module state


def test_cli_login_poll_timeout_is_504(
    collect_home, _in_memory_keyring, cli_on_path, monkeypatch,
):
    monkeypatch.setattr(
        subprocess, "run",
        _fake_run_factory({
            "--device-code": subprocess.TimeoutExpired(cmd="fulcra", timeout=150),
        }),
    )
    client = _client(_build_daemon(collect_home))
    r = client.post(
        "/api/fulcra/auth/cli_login_poll", json={"device_code": "abc123"},
    )
    assert r.status_code == 504
    assert "didn't complete" in r.json()["detail"]


def test_cli_login_poll_wrong_code_surfaces_stderr_tail(
    collect_home, _in_memory_keyring, cli_on_path, monkeypatch,
):
    monkeypatch.setattr(
        subprocess, "run",
        _fake_run_factory({
            "--device-code": (1, "", "Error: expired_token: the device code is invalid or expired\n"),
        }),
    )
    client = _client(_build_daemon(collect_home))
    r = client.post(
        "/api/fulcra/auth/cli_login_poll", json={"device_code": "stale"},
    )
    assert r.status_code == 400
    assert "expired_token" in r.json()["detail"]


def test_cli_login_poll_redacts_device_code_from_cli_error(
    collect_home, _in_memory_keyring, cli_on_path, monkeypatch, caplog,
):
    """If a future CLI echoes the device code in an error, keep it out of
    both daemon logs and the browser-visible error text."""
    secret = "GkI_65iHqAeTVNxsPtpJ1KgH"
    monkeypatch.setattr(
        subprocess, "run",
        _fake_run_factory({
            "--device-code": (
                1,
                "",
                f"Error: device code {secret} is invalid or expired\n",
            ),
        }),
    )
    client = _client(_build_daemon(collect_home))
    with caplog.at_level(logging.WARNING):
        r = client.post(
            "/api/fulcra/auth/cli_login_poll",
            json={"device_code": secret},
        )
    assert r.status_code == 400
    assert secret not in r.json()["detail"]
    assert secret not in caplog.text
    assert "GkI_…(24 chars)" in r.json()["detail"]


def test_cli_login_poll_empty_device_code_is_400(
    collect_home, _in_memory_keyring, cli_on_path, monkeypatch,
):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: pytest.fail("subprocess.run should not be called"),
    )
    client = _client(_build_daemon(collect_home))
    r = client.post(
        "/api/fulcra/auth/cli_login_poll", json={"device_code": "   "},
    )
    assert r.status_code == 400


def test_cli_login_poll_cli_missing_is_424(
    collect_home, _in_memory_keyring, monkeypatch,
):
    monkeypatch.setattr(_creds_mod, "_find_fulcra_cli", lambda: None)
    client = _client(_build_daemon(collect_home))
    r = client.post(
        "/api/fulcra/auth/cli_login_poll", json={"device_code": "abc"},
    )
    assert r.status_code == 424


# ---------------------------------------------------------------------------
# Regression: the classic cli_login endpoint is unchanged
# ---------------------------------------------------------------------------

def test_classic_cli_login_still_works(
    collect_home, _in_memory_keyring, cli_on_path, monkeypatch, mocker,
):
    # Order matters in the factory: "login" appears in both argvs, so
    # route print-access-token first.
    fake_run = _fake_run_factory({
        "print-access-token": (0, "classic-token\n", ""),
        "login": (0, "", ""),
    })
    monkeypatch.setattr(subprocess, "run", fake_run)
    mocker.patch("httpx.Client", return_value=_mock_httpx_success(mocker))

    client = _client(_build_daemon(collect_home))
    r = client.post("/api/fulcra/auth/cli_login")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    assert _creds_mod.get_user_secret("bearer-token") == "classic-token"
    # Classic path still runs the plain blocking login (no --get-auth-url,
    # no --device-code).
    assert ["/usr/local/bin/fulcra", "auth", "login"] in fake_run.seen


# ---------------------------------------------------------------------------
# The daemon serves the edited web-ui source (no build step — dist/ IS
# the source; see packages/web-ui/README.md).
# ---------------------------------------------------------------------------

def test_daemon_serves_wizard_with_auth_url_flow(collect_home):
    _ensure_token()
    daemon = _build_daemon(collect_home)
    client = TestClient(build_app(daemon))
    r = client.get("/static/onboarding.js")
    assert r.status_code == 200
    assert "cli_login_start" in r.text
    assert "cli_login_poll" in r.text
