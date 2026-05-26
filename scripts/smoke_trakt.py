#!/usr/bin/env python3
"""Synthetic Trakt-onboarding smoke for fulcra-collect.

Walks the full Trakt onboarding flow via the daemon's HTTP API with Trakt
and Fulcra both mocked. If this script exits 0, the wizard's HTTP contract
works end-to-end for the happy path — manual user smoke will hit the same
routes and probably succeed.

Run from the repo root:
    uv run python scripts/smoke_trakt.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Track pass / fail across all steps
# ---------------------------------------------------------------------------

results: list[tuple[str, bool, str]] = []
_STEP_NUM = 0


def step(label: str):
    """Decorator: runs the function, records pass/fail, prints colored output."""
    def deco(fn):
        def wrapped(*args, **kwargs):
            global _STEP_NUM
            _STEP_NUM += 1
            num = _STEP_NUM
            try:
                fn(*args, **kwargs)
                results.append((label, True, ""))
                print(f"  \033[32m✓\033[0m  [{num:02d}] {label}")
            except Exception as exc:
                results.append((label, False, str(exc)))
                print(f"  \033[31m✗\033[0m  [{num:02d}] {label}: {exc}")
                raise
        return wrapped
    return deco


# ---------------------------------------------------------------------------
# Canned mock data
# ---------------------------------------------------------------------------

MOCKED_FULCRA_DEFS = [
    {
        "id": "def-watched-001",
        "name": "Watched",
        "annotation_type": "duration",
        "deleted_at": None,
    },
    {
        "id": "def-listened-002",
        "name": "Listened",
        "annotation_type": "duration",
        "deleted_at": None,
    },
    {
        "id": "def-old-deleted",
        "name": "Old Watched",
        "annotation_type": "duration",
        "deleted_at": "2025-01-01T00:00:00Z",   # should be filtered out
    },
]

MOCKED_TRAKT_TOKEN_RESPONSE = {
    "access_token": "mocked-trakt-access-token",
    "refresh_token": "mocked-trakt-refresh-token",
    "expires_in": 7776000,
    "created_at": 1700000000,
}

MOCKED_TRAKT_ME_RESPONSE = {
    "username": "smoke-test-user",
    "name": "Smoke Test",
    "vip": False,
}

MOCKED_TRAKT_HISTORY_RESPONSE = [
    {
        "id": 1,
        "watched_at": "2026-05-20T20:00:00.000Z",
        "action": "watch",
        "type": "movie",
        "movie": {"title": "Oppenheimer", "year": 2023,
                  "ids": {"trakt": 1, "slug": "oppenheimer-2023"}},
    },
    {
        "id": 2,
        "watched_at": "2026-05-19T21:30:00.000Z",
        "action": "watch",
        "type": "episode",
        "show": {"title": "Severance", "ids": {"trakt": 2}},
        "episode": {"season": 2, "number": 5, "title": "Homecoming"},
    },
]


# ---------------------------------------------------------------------------
# httpx mock: route by URL hostname
# ---------------------------------------------------------------------------

def _make_fake_response(status_code: int, json_body) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    # raise_for_status is a no-op for 2xx; raises for others
    if status_code >= 400:
        import httpx
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


class _FakeSyncClient:
    """Sync httpx.Client replacement that routes by URL substring."""

    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url: str, **kwargs):
        url_str = str(url)
        if "api.trakt.tv" in url_str:
            if "/users/me/history" in url_str:
                return _make_fake_response(200, MOCKED_TRAKT_HISTORY_RESPONSE)
            if "/users/me" in url_str:
                return _make_fake_response(200, MOCKED_TRAKT_ME_RESPONSE)
        # Fulcra annotation definitions
        if "fulcradynamics" in url_str or "/user/v1alpha1/annotation" in url_str:
            return _make_fake_response(200, MOCKED_FULCRA_DEFS)
        # Default: 200 empty list
        return _make_fake_response(200, [])

    def post(self, url: str, **kwargs):
        url_str = str(url)
        if "api.trakt.tv/oauth/token" in url_str:
            return _make_fake_response(200, MOCKED_TRAKT_TOKEN_RESPONSE)
        # Fulcra annotations write
        if "fulcradynamics" in url_str:
            return _make_fake_response(200, {"ok": True})
        return _make_fake_response(200, {})


class _FakeAsyncClient:
    """Async httpx.AsyncClient replacement (same routing as sync)."""

    def __init__(self, **kwargs):
        self._sync = _FakeSyncClient()

    async def __aenter__(self):
        return self._sync

    async def __aexit__(self, *args):
        pass

    def get(self, url: str, **kwargs):
        return self._sync.get(url, **kwargs)

    def post(self, url: str, **kwargs):
        return self._sync.post(url, **kwargs)


@contextmanager
def mock_outbound_httpx():
    """Patch httpx.Client and httpx.AsyncClient across all modules that might
    use them — web.py, daemon.py, trakt_health.py, trakt_oauth.py."""
    targets = [
        "httpx.Client",
        "httpx.AsyncClient",
        # Module-level patches for modules that import httpx directly
        "fulcra_collect.web.httpx.Client",
        "fulcra_collect.daemon.httpx.Client",
        "fulcra_media.trakt_health.httpx.Client",
        "fulcra_media.trakt_oauth.httpx.Client",
    ]
    patches = []
    for target in targets:
        try:
            p = patch(target, _FakeSyncClient)
            p.start()
            patches.append(p)
        except (AttributeError, ModuleNotFoundError):
            pass
    try:
        yield
    finally:
        for p in patches:
            try:
                p.stop()
            except RuntimeError:
                pass


# ---------------------------------------------------------------------------
# In-memory keyring so we never touch the OS keychain
# ---------------------------------------------------------------------------

@contextmanager
def in_memory_keyring():
    """Replace keyring backend with a dict for hermetic test isolation."""
    store: dict[tuple[str, str], str] = {}

    def _set(service, key, value):
        store[(service, key)] = value

    def _get(service, key):
        return store.get((service, key))

    def _delete(service, key):
        import keyring.errors
        if (service, key) not in store:
            raise keyring.errors.PasswordDeleteError("not found")
        del store[(service, key)]

    import fulcra_collect.credentials as _creds_mod
    orig_set = _creds_mod.keyring.set_password
    orig_get = _creds_mod.keyring.get_password
    orig_del = _creds_mod.keyring.delete_password
    _creds_mod.keyring.set_password = _set
    _creds_mod.keyring.get_password = _get
    _creds_mod.keyring.delete_password = _delete
    try:
        yield store
    finally:
        _creds_mod.keyring.set_password = orig_set
        _creds_mod.keyring.get_password = orig_get
        _creds_mod.keyring.delete_password = orig_del


# ---------------------------------------------------------------------------
# Daemon context manager
# ---------------------------------------------------------------------------

@contextmanager
def synthetic_daemon():
    """Spin up a real Daemon in an isolated tmp config home."""
    tmp = tempfile.mkdtemp(prefix="fulcra-smoke-")
    os.environ["FULCRA_COLLECT_HOME"] = tmp
    try:
        # Importing here ensures the env var is picked up by config_dir()
        from fulcra_collect.daemon import Daemon
        from fulcra_collect.config import Config
        from fulcra_collect.registry import discover

        registry = discover()   # real entry-point discovery
        daemon = Daemon(registry=registry, config=Config())
        # Set _web_url so the OAuth start route doesn't 503
        daemon._web_url = "http://127.0.0.1:7777"
        yield daemon
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        # Remove the env var so subsequent imports don't inherit the stale tmp path
        os.environ.pop("FULCRA_COLLECT_HOME", None)


# ---------------------------------------------------------------------------
# Main smoke runner
# ---------------------------------------------------------------------------

def main() -> int:
    from fastapi.testclient import TestClient
    from fulcra_collect.web import build_app, _ensure_token

    print()
    print("Fulcra Collect — Trakt onboarding smoke")
    print("=" * 55)
    print()

    had_failure = False

    with synthetic_daemon() as daemon:
        with in_memory_keyring():
            with mock_outbound_httpx():
                token = _ensure_token()
                app = build_app(daemon)
                client = TestClient(app, raise_server_exceptions=False)
                h = {"Authorization": f"Bearer {token}"}

                # --------------------------------------------------------
                # Step 1 — Fulcra auth status initially unauthenticated
                # --------------------------------------------------------
                @step("GET /api/fulcra/auth/status → authenticated:false")
                def step_01():
                    r = client.get("/api/fulcra/auth/status", headers=h)
                    assert r.status_code == 200, f"status={r.status_code}"
                    body = r.json()
                    assert body.get("authenticated") is False, f"body={body}"

                # --------------------------------------------------------
                # Steps 2–5 — Browser-based sign-in via the `fulcra` CLI.
                #
                # The daemon shells out to `fulcra auth login` (opens the
                # user's browser, polls for device-auth completion) and then
                # `fulcra auth print-access-token` to capture the token. We
                # mock both subprocess.run AND shutil.which so the smoke runs
                # without the real CLI being installed.
                # --------------------------------------------------------
                from unittest.mock import patch as _patch

                @step("GET /api/fulcra/auth/cli_status (no CLI) → available:false")
                def step_cli_status_absent():
                    with _patch("shutil.which", return_value=None):
                        r = client.get("/api/fulcra/auth/cli_status", headers=h)
                    assert r.status_code == 200, f"status={r.status_code}"
                    body = r.json()
                    assert body.get("available") is False, f"body={body}"
                    assert body.get("signed_in") is False, f"body={body}"

                @step("POST /api/fulcra/auth/cli_login (no CLI) → 424")
                def step_cli_login_absent():
                    with _patch("shutil.which", return_value=None):
                        r = client.post("/api/fulcra/auth/cli_login", headers=h)
                    assert r.status_code == 424, (
                        f"expected 424 when CLI absent; got {r.status_code} {r.text}"
                    )

                @step("GET /api/fulcra/auth/cli_status (CLI present, signed out) "
                      "→ available:true, signed_in:false")
                def step_cli_status_present():
                    fake = MagicMock()
                    fake.returncode = 1
                    fake.stdout = ""
                    with _patch("shutil.which", return_value="/fake/path/fulcra"), \
                         _patch("subprocess.run", return_value=fake):
                        r = client.get("/api/fulcra/auth/cli_status", headers=h)
                    assert r.status_code == 200
                    body = r.json()
                    assert body.get("available") is True, f"body={body}"
                    assert body.get("signed_in") is False, f"body={body}"

                @step("POST /api/fulcra/auth/cli_login (CLI sign-in succeeds) → ok:true")
                def step_cli_login_success():
                    login_ok = MagicMock()
                    login_ok.returncode = 0
                    login_ok.stdout = "Signed in.\n"
                    login_ok.stderr = ""

                    token_ok = MagicMock()
                    token_ok.returncode = 0
                    token_ok.stdout = "mocked-fulcra-cli-token\n"
                    token_ok.stderr = ""

                    with _patch("shutil.which", return_value="/fake/path/fulcra"), \
                         _patch("subprocess.run", side_effect=[login_ok, token_ok]):
                        r = client.post("/api/fulcra/auth/cli_login", headers=h)
                    assert r.status_code == 200, f"status={r.status_code} body={r.text}"
                    assert r.json().get("ok") is True

                # --------------------------------------------------------
                # Step 6 — Clear the CLI-set token so the fallback paste-token
                # path also gets exercised end-to-end below.
                # --------------------------------------------------------
                @step("DELETE /api/fulcra/auth/token → ok (resets for paste-token test)")
                def step_clear_auth():
                    r = client.delete("/api/fulcra/auth/token", headers=h)
                    assert r.status_code == 200
                    assert r.json().get("ok") is True

                # --------------------------------------------------------
                # Step 7 — Paste Fulcra token (fallback path)
                # --------------------------------------------------------
                @step("POST /api/fulcra/auth/token → ok:true (paste-token fallback)")
                def step_02():
                    r = client.post("/api/fulcra/auth/token",
                                    json={"token": "mocked-fulcra-token"},
                                    headers=h)
                    assert r.status_code == 200, f"status={r.status_code} body={r.text}"
                    assert r.json().get("ok") is True

                # --------------------------------------------------------
                # Step 8 — Fulcra auth status now authenticated
                # --------------------------------------------------------
                @step("GET /api/fulcra/auth/status → authenticated:true")
                def step_03():
                    r = client.get("/api/fulcra/auth/status", headers=h)
                    assert r.status_code == 200
                    assert r.json().get("authenticated") is True

                # --------------------------------------------------------
                # Step 4 — Status: confirm 16 plugins registered
                # --------------------------------------------------------
                @step("GET /api/status → 16 plugins registered")
                def step_04():
                    r = client.get("/api/status", headers=h)
                    assert r.status_code == 200, f"status={r.status_code}"
                    body = r.json()
                    assert body.get("ok") is True
                    plugins = body.get("plugins", [])
                    assert len(plugins) == 16, (
                        f"expected 16 plugins, got {len(plugins)}: "
                        f"{[p['id'] for p in plugins]}"
                    )

                # --------------------------------------------------------
                # Step 5 — Plugin contract: 7 steps, correct shape
                # --------------------------------------------------------
                @step("GET /api/plugin/trakt/contract → 7 setup_steps")
                def step_05():
                    r = client.get("/api/plugin/trakt/contract", headers=h)
                    assert r.status_code == 200, f"status={r.status_code}"
                    body = r.json()
                    assert body["id"] == "trakt"
                    assert body["kind"] == "scheduled"
                    steps = body.get("setup_steps", [])
                    assert len(steps) == 7, (
                        f"expected 7 steps, got {len(steps)}: "
                        f"{[s['kind'] for s in steps]}"
                    )
                    kinds = [s["kind"] for s in steps]
                    assert "oauth" in kinds, f"no oauth step in {kinds}"
                    assert "test_connection" in kinds
                    assert "definition_picker" in kinds
                    assert body["health_check_available"] is True

                # --------------------------------------------------------
                # Step 6 — Store client_id credential
                # --------------------------------------------------------
                @step("PUT /api/plugin/trakt/credential/client_id → ok")
                def step_06():
                    r = client.put("/api/plugin/trakt/credential/client_id",
                                   json={"secret": "smoke-client-id-abc123"},
                                   headers=h)
                    assert r.status_code == 200, f"status={r.status_code} body={r.text}"
                    assert r.json().get("ok") is True

                # --------------------------------------------------------
                # Step 7 — Store client_secret credential
                # --------------------------------------------------------
                @step("PUT /api/plugin/trakt/credential/client_secret → ok")
                def step_07():
                    r = client.put("/api/plugin/trakt/credential/client_secret",
                                   json={"secret": "smoke-client-secret-xyz789"},
                                   headers=h)
                    assert r.status_code == 200, f"status={r.status_code} body={r.text}"
                    assert r.json().get("ok") is True

                # --------------------------------------------------------
                # Step 8 — OAuth start: get state + challenge + redirect_uri
                # --------------------------------------------------------
                oauth_state: str = ""

                @step("POST /api/oauth/trakt/start → state + code_challenge + redirect_uri")
                def step_08():
                    nonlocal oauth_state
                    r = client.post("/api/oauth/trakt/start", headers=h)
                    assert r.status_code == 200, f"status={r.status_code} body={r.text}"
                    body = r.json()
                    assert "state" in body, f"missing state in {body}"
                    assert "code_challenge" in body, f"missing code_challenge in {body}"
                    assert "redirect_uri" in body, f"missing redirect_uri in {body}"
                    assert body["redirect_uri"].endswith("/api/oauth/trakt/callback"), (
                        f"unexpected redirect_uri: {body['redirect_uri']}"
                    )
                    oauth_state = body["state"]

                # Run steps so far; bail on critical failure before OAuth callback
                for fn in [step_01,
                           step_cli_status_absent, step_cli_login_absent,
                           step_cli_status_present, step_cli_login_success,
                           step_clear_auth,
                           step_02, step_03, step_04, step_05,
                           step_06, step_07, step_08]:
                    try:
                        fn()
                    except Exception:
                        had_failure = True
                        break

                # --------------------------------------------------------
                # Step 9 — OAuth callback: exchanges code, returns HTML
                # --------------------------------------------------------
                @step("GET /api/oauth/trakt/callback?code=…&state=… → HTML success page")
                def step_09():
                    r = client.get(
                        f"/api/oauth/trakt/callback"
                        f"?code=fake-auth-code&state={oauth_state}",
                        headers={},   # no Bearer — callback comes from Trakt browser
                    )
                    assert r.status_code == 200, (
                        f"status={r.status_code} body={r.text[:200]}"
                    )
                    assert "text/html" in r.headers.get("content-type", ""), (
                        f"expected HTML, got {r.headers.get('content-type')}"
                    )
                    # The success page contains "Signed in to trakt"
                    assert "Signed in" in r.text or "trakt" in r.text.lower(), (
                        f"unexpected success page content: {r.text[:200]}"
                    )

                # --------------------------------------------------------
                # Step 10 — After callback: access_token shows "set"
                # --------------------------------------------------------
                @step("GET /api/plugin/trakt/credentials → access_token:set")
                def step_10():
                    r = client.get("/api/plugin/trakt/credentials", headers=h)
                    assert r.status_code == 200, f"status={r.status_code}"
                    body = r.json()
                    assert body.get("ok") is True, f"body={body}"
                    creds = body.get("credentials", {})
                    assert creds.get("access_token") == "set", (
                        f"access_token expected 'set', got {creds.get('access_token')!r}"
                    )
                    assert creds.get("refresh_token") == "set", (
                        f"refresh_token expected 'set', got {creds.get('refresh_token')!r}"
                    )

                # --------------------------------------------------------
                # Step 11 — Health check: ok:true with preview items
                # --------------------------------------------------------
                @step("POST /api/plugin/trakt/health_check → ok:true + preview")
                def step_11():
                    r = client.post("/api/plugin/trakt/health_check", headers=h)
                    assert r.status_code == 200, f"status={r.status_code}"
                    body = r.json()
                    assert body.get("available") is True, f"body={body}"
                    assert body.get("ok") is True, (
                        f"health_check failed: {body.get('summary')}"
                    )
                    assert len(body.get("preview", [])) > 0, (
                        "expected non-empty preview list"
                    )

                # --------------------------------------------------------
                # Step 12 — List Fulcra definitions (annotation_type=duration)
                # --------------------------------------------------------
                @step("GET /api/definitions?annotation_type=duration → mocked defs")
                def step_12():
                    r = client.get("/api/definitions?annotation_type=duration", headers=h)
                    assert r.status_code == 200, f"status={r.status_code} body={r.text}"
                    body = r.json()
                    defs = body.get("definitions", [])
                    # Should be 2 non-deleted duration defs from the mock
                    assert len(defs) >= 1, f"expected at least 1 definition, got {defs}"
                    # Soft-deleted def must not appear
                    ids = {d["id"] for d in defs}
                    assert "def-old-deleted" not in ids, (
                        "soft-deleted definition leaked into results"
                    )

                # --------------------------------------------------------
                # Step 13 — Bind definition to trakt plugin
                # --------------------------------------------------------
                @step("POST /api/plugin/trakt/definition {definition_id} → ok:true")
                def step_13():
                    r = client.post("/api/plugin/trakt/definition",
                                    json={"definition_id": "def-watched-001"},
                                    headers=h)
                    assert r.status_code == 200, f"status={r.status_code} body={r.text}"
                    assert r.json().get("ok") is True

                # --------------------------------------------------------
                # Step 14 — Enable the trakt plugin
                # --------------------------------------------------------
                @step("POST /api/plugin/trakt/enable → ok:true")
                def step_14():
                    r = client.post("/api/plugin/trakt/enable", headers=h)
                    assert r.status_code == 200, f"status={r.status_code} body={r.text}"
                    assert r.json().get("ok") is True

                # --------------------------------------------------------
                # Step 15 — Status: trakt now enabled
                # --------------------------------------------------------
                @step("GET /api/status → trakt plugin is enabled")
                def step_15():
                    r = client.get("/api/status", headers=h)
                    assert r.status_code == 200
                    body = r.json()
                    plugins = {p["id"]: p for p in body.get("plugins", [])}
                    assert "trakt" in plugins, f"trakt missing from plugins: {list(plugins)}"
                    assert plugins["trakt"]["enabled"] is True, (
                        f"trakt.enabled={plugins['trakt']['enabled']}"
                    )

                # --------------------------------------------------------
                # Step 16 — Activity feed is accessible (may be empty)
                # --------------------------------------------------------
                @step("GET /api/activity → returns entries list")
                def step_16():
                    r = client.get("/api/activity", headers=h)
                    assert r.status_code == 200, f"status={r.status_code}"
                    body = r.json()
                    assert "entries" in body, f"missing 'entries' key: {body}"
                    # entries can be empty — that's fine; we just verify the shape

                # Run remaining steps sequentially, continuing after any failure
                for fn in [step_09, step_10, step_11, step_12, step_13,
                           step_14, step_15, step_16]:
                    try:
                        fn()
                    except Exception:
                        had_failure = True

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    total = len(results)

    print()
    print("=" * 55)
    if failed == 0:
        print(f"\033[32mAll {total} steps passed.\033[0m")
        return 0
    else:
        print(f"\033[31m{failed} of {total} steps FAILED.\033[0m")
        print()
        print("Failed steps:")
        for label, ok, err in results:
            if not ok:
                print(f"  \033[31m✗\033[0m  {label}")
                if err:
                    print(f"       {err}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
