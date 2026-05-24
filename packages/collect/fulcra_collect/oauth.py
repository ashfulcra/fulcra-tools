"""Daemon-side OAuth helper.

Plugins that authenticate via OAuth (Trakt, future Spotify, etc.)
declare an `oauth_handler` callable on Plugin. The web UI's wizard
calls /api/oauth/{plugin_id}/start to get an authorization URL +
state; opens that URL in a new browser tab; the third-party service
redirects back to /api/oauth/{plugin_id}/callback with a code +
state. This module stores in-flight state, validates the callback,
and invokes the plugin's oauth_handler with the code to complete
the token exchange.

PKCE (RFC 7636): code_verifier + code_challenge prevent code
interception. State (CSRF token) prevents cross-site request
forgery on the callback.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from dataclasses import dataclass


# In-flight state lives in-memory; lost on daemon restart. v1.5 may
# persist to disk if we hit cases where users start an OAuth flow,
# the daemon restarts mid-flow, and they can't recover. For v1, the
# state TTL is short (10 min) and a daemon restart means starting
# the flow over from the wizard.
_STATE_TTL_SECONDS = 600
_state_lock = threading.Lock()
_inflight: dict[str, "_PendingState"] = {}


@dataclass
class _PendingState:
    plugin_id: str
    code_verifier: str
    redirect_uri: str
    created_at: float


def _gc():
    """Drop expired pending states."""
    now = time.monotonic()
    with _state_lock:
        for k in list(_inflight):
            if now - _inflight[k].created_at > _STATE_TTL_SECONDS:
                _inflight.pop(k, None)


def start_flow(plugin_id: str, redirect_uri: str) -> tuple[str, str, str]:
    """Generate a state token + PKCE code verifier/challenge for a new
    OAuth flow. Returns (state, code_verifier, code_challenge).

    The caller (the wizard's oauth step renderer) is responsible for
    building the authorization URL using the plugin's authorize endpoint
    + the returned state + code_challenge. The web UI opens that URL
    in a new tab; the user authorizes; the third party redirects to
    /api/oauth/{plugin_id}/callback?code=...&state=... which is handled
    by `complete_flow`.
    """
    _gc()
    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    with _state_lock:
        _inflight[state] = _PendingState(
            plugin_id=plugin_id,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
            created_at=time.monotonic(),
        )
    return state, code_verifier, code_challenge


def complete_flow(state: str) -> _PendingState | None:
    """Pop and return the pending state for `state`. Returns None if
    state is unknown / expired. Caller (the callback route) invokes
    the plugin's oauth_handler with the returned PendingState's
    code_verifier and the auth code from the callback URL."""
    _gc()
    with _state_lock:
        return _inflight.pop(state, None)
