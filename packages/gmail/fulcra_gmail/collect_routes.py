"""Gmail-specific add-account OAuth routes for the collect daemon.

collect's generic ``routes/oauth.py`` manages its own ``state`` (via
``start_flow``/``complete_flow``) and stores the returned tokens under one
plugin-credential namespace — a model that is fundamentally incompatible with
the Gmail relay's **B4** flow, where ``state`` IS a single-use registry nonce and
the account is discovered from ``users.getProfile`` and written per-account. So
the Gmail relay ships its OWN two endpoints:

* ``GET /api/oauth/gmail/add-account/start`` — mints a nonce + PKCE via the
  registry (:meth:`AccountRegistry.begin_add_account`) and 302-redirects the
  operator to Google's consent screen with ``state=<nonce>``.
* ``GET /api/oauth/callback`` — the redirect Google is configured with
  (``http://127.0.0.1:9292/api/oauth/callback``). Validates + consumes the nonce
  exactly once, exchanges the code, binds the account discovered via
  ``getProfile``, and writes the registry row + keychain token
  (:meth:`AccountRegistry.complete_add_account`). Missing / replayed / expired
  nonces are rejected with **no token stored**; an already-known address rotates
  its token in place (no duplicate row).

The wizard's "Add a Gmail account" step links to the start endpoint; running it
again adds another account. Only live-credential verification (needs the real
OAuth client) is deferred to Task 4.

The endpoints delegate to ``registry_factory()`` so tests can inject a
fake-backed registry (fake store + keychain + ``httpx.MockTransport``) and drive
the whole flow without a real network or keychain.
"""
from __future__ import annotations

import logging

from .accounts import AccountRegistry
from .collect_plugin import (
    ADD_ACCOUNT_START_PATH as START_PATH,
    OAUTH_CALLBACK_PATH as CALLBACK_PATH,
    REDIRECT_URI,
    _registry,
)

_log = logging.getLogger("fulcra_gmail.collect_routes")


# ---------------------------------------------------------------------------
# Thin, framework-free handlers (delegating to the T1 registry lifecycle)
# ---------------------------------------------------------------------------


def start_add_account(registry: AccountRegistry, *, redirect_uri: str = REDIRECT_URI):
    """Begin an add-account flow; return the :class:`AddAccountSession` (whose
    ``authorize_url`` carries the single-use nonce as ``state``)."""
    return registry.begin_add_account(redirect_uri)


def complete_callback(registry: AccountRegistry, state: str | None, code: str | None):
    """Finish an add-account callback; return the :class:`AddAccountResult`.

    A missing ``code`` short-circuits to a rejected result with no token stored
    (the nonce is only consumed once a code is present to exchange)."""
    if not code:
        from .accounts import AddAccountResult
        return AddAccountResult(ok=False, reason="missing_code")
    return registry.complete_add_account(state, code)


# ---------------------------------------------------------------------------
# Small result pages (opaque — never any PII)
# ---------------------------------------------------------------------------


def _success_html(result) -> str:
    action = "added" if result.is_new else "re-authorized"
    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<title>Gmail account connected</title></head><body>"
        f"<h1>Gmail account {action}</h1>"
        "<p>You can close this tab and return to Fulcra Collect. "
        "Run the add-account step again to connect another account.</p>"
        "</body></html>"
    )


def _failure_html(reason: str) -> str:
    import html as _html
    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<title>Gmail sign-in failed</title></head><body>"
        "<h1>Sign-in failed</h1>"
        f"<p><code>{_html.escape(reason)}</code></p>"
        "<p>Close this tab and start the add-account step again.</p>"
        "</body></html>"
    )


# ---------------------------------------------------------------------------
# FastAPI registration (lazy import so the pure handlers need no web deps)
# ---------------------------------------------------------------------------


def register(app, ctx, *, registry_factory=None) -> None:
    """Register the Gmail add-account start + callback endpoints on ``app``.

    ``registry_factory`` builds the :class:`AccountRegistry` per request
    (default: the production keychain + JSON store). Tests inject a
    fake-backed factory.
    """
    from fastapi.responses import HTMLResponse, RedirectResponse

    make_registry = registry_factory or (lambda: _registry())

    @app.get(START_PATH)
    def gmail_add_account_start():  # pragma: no cover - exercised via TestClient
        session = start_add_account(make_registry())
        # 302 to Google's consent screen; the nonce rides in ``state``.
        return RedirectResponse(session.authorize_url, status_code=302)

    @app.get(CALLBACK_PATH)
    def gmail_oauth_callback(  # pragma: no cover - exercised via TestClient
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ):
        if error:
            return HTMLResponse(_failure_html(error), status_code=400)
        result = complete_callback(make_registry(), state, code)
        if result.ok:
            _log.info("gmail: add-account callback bound account %s (is_new=%s)",
                      result.account_id, result.is_new)
            return HTMLResponse(_success_html(result), status_code=200)
        _log.warning("gmail: add-account callback rejected (%s)", result.reason)
        return HTMLResponse(_failure_html(result.reason or "rejected"), status_code=400)
