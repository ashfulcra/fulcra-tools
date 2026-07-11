"""Per-account Gmail REST v1 client (read-only).

``GmailClient(account_id)`` is a thin httpx wrapper that resolves the given
account's refresh token from the registry, mints a Google access token on
demand, and exposes exactly the three read calls the poller needs:

* :meth:`GmailClient.list_message_ids` — ``users.messages.list`` fully
  paginated (follows ``nextPageToken`` to exhaustion; makes NO assumption
  about page order, per the API docs).
* :meth:`GmailClient.get_message` — ``users.messages.get`` (``format=full``
  by default).
* :meth:`GmailClient.get_profile` — ``users.getProfile`` (returns
  ``emailAddress``; used for the B4 account-binding step in
  :mod:`fulcra_gmail.accounts`).

There is deliberately NO history-API support: v1 syncs by re-querying with a
contiguous-frontier cursor (see the plan's Sync model), not by
``historyId``.

Refresh-on-401 mirrors ``fulcra_collect.web._RetryingClient``: a 401 forces
a token refresh and retries the request once. A ``400 invalid_grant`` on the
refresh itself means the account's grant was revoked or expired — that is a
fail-soft condition: the account is marked ``auth_failed`` in the registry
and the calling read returns an empty/None result WITHOUT raising, so one
dead account never takes down a multi-account poll. Every other HTTP error
propagates.

No email content — subject/body/from/snippet — is ever logged, at any level
(privacy design B2). Only opaque ids reach a log sink.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import httpx

if TYPE_CHECKING:
    from .accounts import AccountRegistry

_log = logging.getLogger("fulcra_gmail.client")

#: Gmail REST v1 base. All message reads hang off ``users/me/...``.
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
#: Google OAuth 2.0 token endpoint (code exchange + refresh).
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
#: Google OAuth 2.0 authorization endpoint (consent screen).
GOOGLE_AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
#: The ONLY scope this relay ever requests. Never modify/send/delete.
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

_TIMEOUT = httpx.Timeout(30.0)


class AccountAuthFailedError(Exception):
    """Raised INTERNALLY when a refresh returns ``400 invalid_grant``.

    Never escapes a public :class:`GmailClient` read — those catch it, mark
    the account ``auth_failed``, and return a fail-soft empty result. It is a
    control-flow signal, not an error the caller is expected to handle.
    """


# ---------------------------------------------------------------------------
# PKCE + OAuth URL helpers (stateless; used by the registry's add-account flow)
# ---------------------------------------------------------------------------


def generate_pkce() -> tuple[str, str]:
    """Return a ``(code_verifier, code_challenge)`` PKCE pair (RFC 7636, S256).

    Same construction as ``fulcra_collect.oauth.start_flow`` — a high-entropy
    verifier and its URL-safe, unpadded SHA-256 challenge.
    """
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def build_authorize_url(
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    """Build the Google consent URL the wizard opens in a new tab.

    ``state`` is the single-use CSRF nonce minted by the registry (B4) — it
    is NOT an account label. ``access_type=offline`` + ``prompt=consent``
    guarantee a refresh token even on a re-authorization of the same address.
    """
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": GMAIL_READONLY_SCOPE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
    }
    return f"{GOOGLE_AUTHORIZE_ENDPOINT}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Token endpoint calls (module-level so the add-account flow can reuse them
# before any account_id exists)
# ---------------------------------------------------------------------------


def exchange_code(
    code: str,
    *,
    code_verifier: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """Exchange an authorization code for a token set.

    Returns the raw token JSON (``access_token``, ``refresh_token``, …).
    Raises ``httpx.HTTPStatusError`` if Google rejects the exchange.
    """
    payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }
    with httpx.Client(timeout=_TIMEOUT, transport=transport) as client:
        resp = client.post(GOOGLE_TOKEN_ENDPOINT, data=payload)
        resp.raise_for_status()
        return resp.json()


def refresh_access_token(
    refresh_token: str,
    *,
    client_id: str,
    client_secret: str,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Mint a fresh access token from a stored refresh token.

    Raises :class:`AccountAuthFailedError` on a ``400 invalid_grant`` (the
    grant was revoked or expired — the account needs re-authorization). Any
    other HTTP error propagates as ``httpx.HTTPStatusError``.
    """
    payload = {
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
    }
    with httpx.Client(timeout=_TIMEOUT, transport=transport) as client:
        resp = client.post(GOOGLE_TOKEN_ENDPOINT, data=payload)
    if resp.status_code == 400:
        error = ""
        try:
            error = (resp.json() or {}).get("error", "")
        except ValueError:
            error = ""
        if error == "invalid_grant":
            raise AccountAuthFailedError("refresh token rejected (invalid_grant)")
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_profile(
    access_token: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """Call ``users.getProfile`` with a bare access token.

    Used by the add-account flow to DISCOVER the authorized address from the
    granted token (B4) before any ``account_id`` exists, so it cannot depend
    on :class:`GmailClient` (which resolves a token by account). Returns the
    profile JSON (``emailAddress``, ``messagesTotal``, ``historyId``, …).
    """
    with httpx.Client(timeout=_TIMEOUT, transport=transport) as client:
        resp = client.get(
            f"{GMAIL_API_BASE}/users/me/profile",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Per-account client
# ---------------------------------------------------------------------------


class GmailClient:
    """Read-only Gmail client bound to one registry ``account_id``.

    The refresh token and shared client credentials are resolved from
    ``registry`` lazily. An access token is minted on first use and cached
    for the client's lifetime; a 401 forces a single refresh-and-retry.

    ``transport`` is an optional ``httpx.BaseTransport`` (a test seam — inject
    an ``httpx.MockTransport`` to serve both the token endpoint and the Gmail
    API without a network).
    """

    def __init__(
        self,
        account_id: str,
        *,
        registry: "AccountRegistry",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.account_id = account_id
        self._registry = registry
        self._transport = transport
        self._access_token: str | None = None

    # -- token plumbing -----------------------------------------------------

    def _mint_access_token(self) -> str | None:
        """Refresh this account's access token. Returns None (and marks the
        account ``auth_failed``) on ``invalid_grant`` — never raises for it."""
        refresh_token = self._registry.get_refresh_token(self.account_id)
        client_id, client_secret = self._registry.client_credentials()
        if not (refresh_token and client_id and client_secret):
            _log.warning(
                "gmail: account %s missing refresh token or client creds — "
                "marking auth_failed",
                self.account_id,
            )
            self._registry.mark_auth_failed(self.account_id)
            return None
        try:
            token = refresh_access_token(
                refresh_token,
                client_id=client_id,
                client_secret=client_secret,
                transport=self._transport,
            )
        except AccountAuthFailedError:
            _log.warning(
                "gmail: account %s refresh returned invalid_grant — "
                "marking auth_failed (fail-soft; other accounts proceed)",
                self.account_id,
            )
            self._registry.mark_auth_failed(self.account_id)
            return None
        self._access_token = token
        return token

    def _ensure_token(self, *, force: bool = False) -> str | None:
        if self._access_token is not None and not force:
            return self._access_token
        return self._mint_access_token()

    def _authed_get(self, path: str, params: dict | None = None):
        """GET ``{GMAIL_API_BASE}/{path}`` with refresh-on-401.

        Returns the ``httpx.Response`` on success, or ``None`` if the account
        is auth-failed (fail-soft). Non-401 HTTP errors raise.
        """
        token = self._ensure_token()
        if token is None:
            return None
        url = f"{GMAIL_API_BASE}/{path}"
        with httpx.Client(timeout=_TIMEOUT, transport=self._transport) as client:
            resp = client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params=params or {},
            )
            if resp.status_code == 401:
                token = self._ensure_token(force=True)
                if token is None:
                    return None
                resp = client.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    params=params or {},
                )
        resp.raise_for_status()
        return resp

    # -- public reads -------------------------------------------------------

    def list_message_ids(self, q: str) -> list[str]:
        """Return every message id matching ``q``, following ``nextPageToken``
        to exhaustion.

        The Gmail API documents no ordering guarantee across pages, so this
        makes none — it simply accumulates every id. Callers that need order
        must sort themselves (the poller sorts by ``(internalDate, id)``).
        Returns ``[]`` if the account is auth-failed (fail-soft).
        """
        ids: list[str] = []
        page_token: str | None = None
        while True:
            params: dict[str, str] = {"q": q}
            if page_token:
                params["pageToken"] = page_token
            resp = self._authed_get("users/me/messages", params)
            if resp is None:
                return []
            data = resp.json()
            for message in data.get("messages", []) or []:
                ids.append(message["id"])
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return ids

    def get_message(self, message_id: str, format: str = "full") -> dict | None:  # noqa: A002
        """Fetch one message. ``None`` if the account is auth-failed."""
        resp = self._authed_get(
            f"users/me/messages/{message_id}", {"format": format}
        )
        return resp.json() if resp is not None else None

    def get_profile(self) -> dict | None:
        """``users.getProfile`` for this account. ``None`` if auth-failed."""
        resp = self._authed_get("users/me/profile")
        return resp.json() if resp is not None else None
