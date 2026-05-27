# SP5: Fulcra auth refresh + Reconnect button — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Fix the user-visible "Fulcra rejected the request — your sign-in may have expired" banner that appears in Settings → Annotation tracks even though dashboard events are still flowing. The daemon caches Fulcra access tokens in the keychain but never re-invokes the CLI's refresh-token machinery; access tokens silently expire and synchronous management calls fail.

**Architecture:** Three layers of change. (1) Daemon centralises Fulcra API calls through a helper that on 401, re-invokes `fulcra auth print-access-token` to get a fresh access token via the CLI's refresh-token store, updates the keychain, retries once. (2) Daemon tracks a process-level `refresh_failed` flag set when even the CLI's refresh fails; cleared on successful re-sign-in. (3) Web UI Settings page reads the new auth status field and surfaces a "Reconnect to Fulcra" banner + button that triggers `cli_login` without leaving Settings.

**Tech Stack:** Python 3.12+ + httpx + the `fulcra` CLI (subprocess); vanilla JS + Alpine (web UI).

**Source:** Session 2026-05-27 user report — Settings banner shows auth-expired while dashboard events still flow because plugins with their own OAuth tokens (Trakt etc.) are independent of the daemon's shared bearer token.

**HEAD at plan start:** `5c7ab42` (end of session before SP5).

**Reading list:**
- `packages/collect/fulcra_collect/web.py:146-172` — `fulcra_token_or_401` + `fulcra_http_client` (the choke point).
- `packages/collect/fulcra_collect/routes/fulcra_auth.py:25-101` — current auth-status routes + CLI flow.
- `packages/collect/fulcra_collect/daemon.py:_delete_definition` — the SP2-extracted method that hits Fulcra directly.
- `packages/web-ui/dist/static/settings.js` — current Settings page Alpine factory.

---

## File Structure

| File | Change |
|---|---|
| `packages/collect/fulcra_collect/credentials.py` | Modify: add `refresh_fulcra_access_token() -> str | None` that runs `fulcra auth print-access-token`, updates keychain, returns fresh token. |
| `packages/collect/fulcra_collect/web.py` | Modify: `fulcra_http_client` wraps httpx.Client requests to retry once on 401 via the refresh helper. New `_fulcra_refresh_state` module-level flag tracks "refresh-attempted-and-failed". |
| `packages/collect/fulcra_collect/routes/fulcra_auth.py` | Modify: `/api/fulcra/auth/status` reply includes `refresh_failed: bool`. Successful sign-in (paste-token or cli_login) clears the flag. |
| `packages/collect/tests/test_routes_fulcra_auth.py` (or wherever) | Modify: add tests for the new status-reply shape + the refresh-on-401 retry behaviour. |
| `packages/web-ui/dist/static/settings.js` | Modify: fetch auth status on mount; when `refresh_failed`, render banner + Reconnect button. |
| `packages/web-ui/dist/index.html` | Modify: Settings template adds the banner block. |

Total surface: ~150 lines added, ~40 modified.

---

## Task 1: Daemon-side refresh-on-401

**Files:**
- Modify: `packages/collect/fulcra_collect/credentials.py` (or wherever the keychain wrapper lives).
- Modify: `packages/collect/fulcra_collect/web.py` — `fulcra_http_client` becomes refresh-aware.
- Modify: `packages/collect/fulcra_collect/routes/fulcra_auth.py` — paste-token + cli_login success clears the refresh-failed flag.
- Modify (create if absent): `packages/collect/tests/test_fulcra_refresh.py`.

**Step 1: Add the `refresh_fulcra_access_token` helper.**

In `credentials.py` (or wherever the existing `get_user_secret("bearer-token")` lives), add:

```python
import subprocess
import shutil
import logging
from threading import Lock

_log = logging.getLogger("fulcra_collect.credentials")

# Process-level lock so concurrent 401s don't race to refresh.
_refresh_lock = Lock()

# Process-level state: True when the most recent refresh attempt
# exhausted (CLI returned non-zero or empty stdout). Cleared on
# successful sign-in (paste-token POST or cli_login POST) by the
# routes module — see clear_refresh_failed() below.
_refresh_failed = False


def refresh_fulcra_access_token() -> str | None:
    """Re-invoke ``fulcra auth print-access-token`` and store the result.

    Used by the daemon's Fulcra-API call path when a 401 comes back —
    the CLI has refresh tokens stored separately and can mint a fresh
    access token without user interaction (until the refresh token
    itself expires).

    Returns the new access token on success, None on failure. On
    failure sets the module-level _refresh_failed flag which the
    /api/fulcra/auth/status route reads so the web UI can show a
    Reconnect banner.

    Concurrency: serialised via _refresh_lock so multiple plugins
    hitting 401 simultaneously don't fork-bomb the CLI.

    Why a CLI subprocess rather than the OAuth refresh dance in-
    process: the ``fulcra`` CLI already implements the refresh dance
    correctly (including refresh-token rotation when Fulcra sends a
    new one) and owns the refresh-token storage on disk. Re-
    implementing that in the daemon would duplicate state + create
    refresh-token drift between the CLI and daemon when they're
    used in the same session.
    """
    global _refresh_failed
    with _refresh_lock:
        cli_path = shutil.which("fulcra")
        if not cli_path:
            _log.warning("refresh_fulcra_access_token: fulcra CLI not on PATH")
            _refresh_failed = True
            return None
        try:
            r = subprocess.run(
                [cli_path, "auth", "print-access-token"],
                capture_output=True, text=True, timeout=10,
            )
        except subprocess.TimeoutExpired:
            _log.warning("refresh_fulcra_access_token: CLI timed out")
            _refresh_failed = True
            return None
        if r.returncode != 0 or not r.stdout.strip():
            _log.warning(
                "refresh_fulcra_access_token: CLI returned %d (stderr=%s)",
                r.returncode, (r.stderr or "").strip()[:200],
            )
            _refresh_failed = True
            return None
        new_token = r.stdout.strip()
        set_user_secret("bearer-token", new_token)
        _refresh_failed = False
        _log.info("refresh_fulcra_access_token: succeeded; keychain updated")
        return new_token


def is_refresh_failed() -> bool:
    """Whether the most recent refresh attempt exhausted. Surfaced via
    /api/fulcra/auth/status so the web UI can show a Reconnect banner."""
    return _refresh_failed


def clear_refresh_failed() -> None:
    """Clear the refresh-failed flag. Called after a successful sign-in
    (paste-token POST or cli_login POST) so the Reconnect banner
    disappears once the user has re-authed."""
    global _refresh_failed
    _refresh_failed = False
```

**Step 2: Wrap `fulcra_http_client` so requests retry on 401.**

In `packages/collect/fulcra_collect/web.py`, around line 154 where `fulcra_http_client` is defined, the current implementation returns a raw `httpx.Client`. Replace with a wrapper that retries.

```python
    def fulcra_http_client(fulcra_token: str):
        """Return an httpx.Client pre-configured to talk to the Fulcra API.

        Wraps the standard httpx.Client so that on a 401 response, the
        client transparently invokes refresh_fulcra_access_token() to
        get a fresh access token from the fulcra CLI's refresh-token
        store, updates the Authorization header, and retries once.

        If the retry also returns 401/403 (CLI's refresh token also
        expired or the tenant revoked access), the response is returned
        as-is and the credentials module's _refresh_failed flag is
        already set — /api/fulcra/auth/status will surface it so the
        web UI shows a Reconnect banner.

        Goes through this module's ``httpx`` attribute so tests that
        monkeypatch ``fulcra_collect.web.httpx`` see their stub used.
        """
        from fulcra_common import DEFAULT_BASE_URL
        from . import credentials as _creds
        import fulcra_collect.web as _self

        class _RetryingClient:
            def __init__(self, token: str) -> None:
                self._token = token
                self._inner = _self.httpx.Client(
                    base_url=DEFAULT_BASE_URL,
                    timeout=15.0,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "User-Agent": "fulcra-collect/web-ui",
                    },
                    follow_redirects=True,
                )

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self._inner.__exit__(*exc)

            def close(self) -> None:
                self._inner.close()

            def _retry_with_fresh_token(self, method: str, *args, **kwargs):
                """Call refresh_fulcra_access_token, swap the auth header
                on the inner client, retry the same request once."""
                fresh = _creds.refresh_fulcra_access_token()
                if not fresh:
                    return None
                self._token = fresh
                self._inner.headers["Authorization"] = f"Bearer {fresh}"
                return getattr(self._inner, method)(*args, **kwargs)

            def _wrap(self, method: str):
                def wrapped(*args, **kwargs):
                    response = getattr(self._inner, method)(*args, **kwargs)
                    if response.status_code == 401:
                        retry_resp = self._retry_with_fresh_token(
                            method, *args, **kwargs,
                        )
                        if retry_resp is not None:
                            return retry_resp
                    return response
                return wrapped

            def __getattr__(self, name: str):
                # Forward GET/POST/PUT/DELETE/etc. — these are the call
                # sites that need 401-retry. Other attributes (e.g. .headers)
                # pass through to the inner client directly.
                if name in ("get", "post", "put", "delete", "patch", "head"):
                    return self._wrap(name)
                return getattr(self._inner, name)

        return _RetryingClient(fulcra_token)
```

**Step 3: Clear the refresh-failed flag on successful sign-in.**

In `routes/fulcra_auth.py`:

- In `fulcra_auth_set` (after the `_creds.set_user_secret("bearer-token", token)` line), add `_creds.clear_refresh_failed()`.
- In `fulcra_auth_cli_login` (after the equivalent `set_user_secret`), add the same.

**Step 4: `/api/fulcra/auth/status` returns the new flag.**

Update `fulcra_auth_status` in `routes/fulcra_auth.py`:

```python
    @app.get("/api/fulcra/auth/status", dependencies=[Depends(require_token)])
    def fulcra_auth_status():
        from .. import credentials as _creds
        return {
            "authenticated": _creds.has_user_secret("bearer-token"),
            "refresh_failed": _creds.is_refresh_failed(),
        }
```

**Step 5: Tests.**

Add `packages/collect/tests/test_fulcra_refresh.py` (or extend existing test_fulcra_auth.py):

- Test that `refresh_fulcra_access_token` calls the CLI subprocess and updates the keychain on success — mock subprocess.run.
- Test that it sets `_refresh_failed = True` on CLI non-zero exit AND that `is_refresh_failed()` returns True.
- Test that the wrapper client retries on 401 after calling refresh — fake httpx response with status 401, assert retry made + auth header updated.
- Test that `clear_refresh_failed()` clears the flag.
- Test that the status route reply includes the new field.

**Step 6: Run + commit.**

```bash
cd packages/collect && uv run pytest -q
```

```bash
git add packages/collect/
git commit -m "feat(collect): refresh Fulcra access token on 401 via CLI helper (SP5 task 1)

The daemon previously cached the Fulcra access token in the keychain
forever — when it expired (typically 1h), every synchronous
management call (list defs, soft-delete, etc.) returned 401 and the
user saw 'your sign-in may have expired' banners even though plugins
with their own OAuth refresh tokens kept ingesting events.

Fix: centralise Fulcra API calls through a wrapper httpx-style
client that on a 401 response, re-invokes \`fulcra auth print-
access-token\` (the CLI handles refresh-token rotation; we just
call it again). Stores the fresh token in keychain, swaps the
Authorization header, retries once.

If the retry also fails (CLI's refresh token also expired), a
process-level _refresh_failed flag stays set. /api/fulcra/auth/status
now reports it as 'refresh_failed: bool' so the web UI can show a
Reconnect banner — that surface lands in SP5 task 3.

Successful sign-in (paste-token or cli_login) clears the flag.

Refs SP5 D-auth, session feedback 2026-05-27."
```

---

## Task 2: Refresh-state propagation through the Daemon._delete_definition path

**Why:** `Daemon._delete_definition` builds its own `httpx.Client` directly (not via `fulcra_http_client`) so it bypasses the new retry logic. Make it use the wrapper, OR replicate the retry inside.

**Files:**
- Modify: `packages/collect/fulcra_collect/daemon.py:_delete_definition`.

**Step 1: Find the httpx.Client construction.**

```bash
grep -n "_web.httpx.Client\|httpx.Client" packages/collect/fulcra_collect/daemon.py | head -5
```

**Step 2: Replace with a call into the same refresh-aware wrapper.**

Either lift the `_RetryingClient` from web.py to a shared module (cleanest) or call `refresh_fulcra_access_token` inline on 401 in `_delete_definition`. Pick whichever produces less duplication.

**Step 3: Verify the existing 9 error paths still resolve to the right `code` field. The new refresh path is transparent — a successful refresh+retry produces the same final response shape; an exhausted refresh produces a 401 which maps to `code="unauthorized"` per the existing logic.**

**Step 4: Update test_daemon_delete_definition.py to cover the refresh-on-401 case.**

- Mock the first httpx call to return 401, the CLI subprocess to return a new token, the retry httpx call to succeed.
- Assert the result is `{"ok": True, ...}` not the 401 error.
- Assert `_refresh_failed` is False after.

**Step 5: Commit.**

```bash
git add packages/collect/fulcra_collect/daemon.py packages/collect/tests/test_daemon_delete_definition.py
git commit -m "feat(collect): _delete_definition uses refresh-aware Fulcra client (SP5 task 2)

The Daemon._delete_definition method (SP2 task 1) built its own
httpx.Client to talk to Fulcra and so bypassed the refresh-on-401
wrapper added in SP5 task 1. Route it through the same wrapper so
soft-delete also benefits from automatic access-token refresh.

Adds one test asserting refresh-on-401 happens transparently when
the user soft-deletes a definition after their access token has
expired but the CLI's refresh token is still valid.

Refs SP5 D-auth, session feedback 2026-05-27."
```

---

## Task 3: Web UI Settings page Reconnect banner

**Files:**
- Modify: `packages/web-ui/dist/static/settings.js`.
- Modify: `packages/web-ui/dist/index.html` — Settings template.

**Step 1: Settings.js fetches auth status on mount.**

In `settings.js`, the existing `boot()` (or `init()`) gains:

```javascript
    fulcraAuthStatus: { authenticated: true, refresh_failed: false },

    async _loadAuthStatus() {
      try {
        const status = await api("/api/fulcra/auth/status");
        this.fulcraAuthStatus = status;
      } catch (e) {
        // Network/daemon error — leave the existing state (assume OK so
        // we don't flash a misleading banner on a transient failure).
      }
    },
```

Call `_loadAuthStatus()` from the existing boot path. Re-call after `reconnectToFulcra()` succeeds.

**Step 2: Reconnect handler.**

```javascript
    reconnectInFlight: false,
    reconnectError: "",

    async reconnectToFulcra() {
      this.reconnectError = "";
      this.reconnectInFlight = true;
      try {
        const result = await api("/api/fulcra/auth/cli_login", {
          method: "POST",
          body: JSON.stringify({}),
        });
        if (!result.ok) {
          this.reconnectError = result.error || "Sign-in didn't complete.";
        } else {
          // Refresh the auth status + re-fetch any data the banner was hiding.
          await this._loadAuthStatus();
          await this._loadDefinitions();  // or whatever the existing reload is
        }
      } catch (e) {
        this.reconnectError = e.message || "Reconnect failed.";
      } finally {
        this.reconnectInFlight = false;
      }
    },
```

**Step 3: index.html — banner at top of Settings.**

Find the `route === 'settings'` template block. Add at the top (before the rest of the settings content):

```html
            <template x-if="fulcraAuthStatus.refresh_failed">
              <div class="rounded-lg border border-amber-200 bg-amber-50 p-4 space-y-3">
                <div class="text-sm text-amber-900">
                  <span class="font-semibold">Your Fulcra sign-in expired.</span>
                  Some management actions (listing annotation tracks,
                  soft-deleting) will fail until you reconnect. Plugins
                  with their own credentials (Trakt, Last.fm, etc.) will
                  keep ingesting events.
                </div>
                <div class="flex items-center gap-3">
                  <button @click="reconnectToFulcra()"
                          :disabled="reconnectInFlight"
                          class="px-4 py-2 rounded bg-amber-600 text-white font-medium hover:bg-amber-700 disabled:opacity-50 text-sm">
                    <span x-text="reconnectInFlight ? 'Reconnecting…' : 'Reconnect to Fulcra'"></span>
                  </button>
                  <span x-show="reconnectError" x-text="reconnectError"
                        class="text-sm text-red-700"></span>
                </div>
              </div>
            </template>
```

**Step 4: `node --check` + manual smoke (curl the served settings page; confirm the new template renders).**

```bash
node --check packages/web-ui/dist/static/settings.js
node --check packages/web-ui/dist/static/app.js  # if you touched it
curl -s http://127.0.0.1:9292/ | grep -c "reconnectToFulcra"
```

**Step 5: Commit.**

```bash
git add packages/web-ui/dist/static/settings.js packages/web-ui/dist/index.html
git commit -m "feat(web-ui): Settings page Reconnect-to-Fulcra banner (SP5 task 3)

When the daemon's Fulcra access-token refresh exhausts (CLI's refresh
token also expired or revoked), /api/fulcra/auth/status now returns
refresh_failed=true. The Settings page reads it on mount and shows a
prominent amber banner at the top:

  'Your Fulcra sign-in expired. Some management actions will fail
  until you reconnect.'

with a Reconnect button that triggers the same cli_login flow the
onboarding wizard uses, but without leaving Settings. On success
the banner disappears and the existing Annotation tracks fetch is
retried.

Refs SP5 D-auth, session feedback 2026-05-27."
```

---

## Task 4: Rebuild daemon + manual verification

- [ ] `launchctl kickstart -k gui/$(id -u)/com.fulcra.collect`
- [ ] Verify `/api/fulcra/auth/status` returns the new field shape via curl.
- [ ] Verify Settings page no longer shows the "your sign-in may have expired" banner (the refresh succeeded on the first management call after restart).
- [ ] Full pytest sweep.
- [ ] Update memory file.

## Final cross-cutting code review

After all 4 tasks land, dispatch `superpowers:code-reviewer` over `5c7ab42..HEAD`. Cover:
- Refresh-on-401 wrapper is transparent to existing callers (no test broke).
- `_RetryingClient` correctly forwards non-HTTP-method attributes (`.headers`, `.close()`, etc.) to the inner client.
- Concurrent 401s serialise on `_refresh_lock`.
- Settings banner appears only when `refresh_failed` is true — no false positives on cold-start.

## Acceptance

- [ ] When the daemon's access token expires, the next Fulcra call auto-refreshes via the CLI and succeeds (no user-visible failure).
- [ ] When the CLI's refresh token is ALSO expired, the Settings page shows the Reconnect banner.
- [ ] Clicking Reconnect runs `cli_login`, refreshes the status, removes the banner.
- [ ] Existing tests pass.
