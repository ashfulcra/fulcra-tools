"""The Gmail account registry + the B4 OAuth state/binding lifecycle.

Multi-account is a first-class dimension. Each authorized Gmail account is
keyed by an opaque, stable ``account_id`` (uuid4 minted at add-time); the
email address is metadata, never a keychain-key or path segment.

**Secrets** live in the OS keychain (via collect's ``credentials`` helpers,
namespaced under plugin id ``gmail``):

* the SHARED OAuth client (``client_id`` + ``client_secret``) — one Workspace
  app for all accounts;
* one refresh token per account at ``account:<account_id>:refresh_token``.

**Non-secret registry state** lives in a small JSON document (a
:class:`RegistryStore`) alongside collect's state db:

* ``accounts`` — rows ``{account_id, email, display_order, added_at,
  status}``;
* ``nonces`` — the short-lived add-account setup-session map
  (``nonce → {intent, created_at, code_verifier, redirect_uri, …}``, 10-min
  TTL).

**B4 — OAuth ``state`` + account binding.** ``state`` is an unguessable
single-use CSRF nonce, NOT an account label:

1. :meth:`AccountRegistry.begin_add_account` mints a nonce + PKCE pair and
   records the setup session.
2. The consent redirect comes back to the callback with ``code`` + that
   ``state``. :meth:`AccountRegistry.complete_add_account` **consumes the
   nonce exactly once, atomically** — missing / mismatched / replayed /
   expired all reject with NO token stored.
3. Only after a valid consume does it exchange the code, then DISCOVER the
   authorized address via ``users.getProfile`` and name the registry +
   keychain from THAT address — never from an operator-supplied hint. A
   re-auth of an existing address rotates the token in place (no duplicate
   row); a genuinely new address mints a fresh ``account_id``.

The design keeps a fake keychain + a fake store injectable so the whole
lifecycle is unit-testable with synthetic data and no real network/keychain.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import httpx

from . import client as _client

_log = logging.getLogger("fulcra_gmail.accounts")

#: collect keychain plugin-id namespace for all Gmail secrets.
_KEYCHAIN_PLUGIN_ID = "gmail"
#: keychain keys for the shared OAuth client.
_CLIENT_ID_KEY = "client:client_id"
_CLIENT_SECRET_KEY = "client:client_secret"
#: TTL for an add-account setup session (nonce). Plan: 10 minutes.
NONCE_TTL_SECONDS = 600

# Account status enum (strings kept flat for JSON/PluginState portability).
STATUS_ACTIVE = "active"
STATUS_AUTH_FAILED = "auth_failed"


def _refresh_token_key(account_id: str) -> str:
    return f"account:{account_id}:refresh_token"


def _now() -> float:
    return time.time()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Injectable ports (keychain + non-secret store)
# ---------------------------------------------------------------------------


class Keychain(Protocol):
    """The keychain surface the registry needs. Production is
    :class:`CollectKeychain`; tests inject a dict-backed fake."""

    def set(self, key: str, value: str) -> None: ...
    def get(self, key: str) -> str | None: ...
    def delete(self, key: str) -> None: ...


class CollectKeychain:
    """Default :class:`Keychain` backed by ``fulcra_collect.credentials``,
    namespaced under the ``gmail`` plugin id. Secrets never leave the OS
    keychain."""

    def __init__(self, plugin_id: str = _KEYCHAIN_PLUGIN_ID) -> None:
        self._plugin_id = plugin_id

    def set(self, key: str, value: str) -> None:
        from fulcra_collect import credentials as _creds

        _creds.set_secret(self._plugin_id, key, value)

    def get(self, key: str) -> str | None:
        from fulcra_collect import credentials as _creds

        return _creds.get_secret(self._plugin_id, key)

    def delete(self, key: str) -> None:
        from fulcra_collect import credentials as _creds

        _creds.delete_secret(self._plugin_id, key)


class RegistryStore(Protocol):
    """A tiny read/write port for the non-secret registry document.

    ``transaction()`` is a context manager that must provide MUTUAL EXCLUSION
    over the store for the whole ``read → modify → write`` critical section —
    across processes as well as threads for a durable backend — so the B4
    single-use-nonce consume can never race (two callers popping the same
    nonce) and concurrent mutations can never lose an update.
    """

    def read(self) -> dict: ...
    def write(self, doc: dict) -> None: ...
    def transaction(self) -> "contextlib.AbstractContextManager": ...


class JsonFileStore:
    """Default :class:`RegistryStore` — a single JSON document beside
    collect's state db (``<config>/gmail/registry.json``).

    Non-secret only: account rows + the nonce map. The whole doc is small
    (one entry per authorized account + transient nonces) so read-modify-write
    of the entire file is fine.

    **Cross-process atomicity.** The daemon can service concurrent OAuth
    callbacks, and each may construct its own :class:`AccountRegistry` /
    :class:`JsonFileStore` over the same file — so an in-process
    ``threading.Lock`` alone does NOT make a nonce consume single-use.
    :meth:`transaction` takes an exclusive ``fcntl.flock`` on a persistent
    sidecar ``.lock`` file; :class:`AccountRegistry` holds it around the ENTIRE
    read→validate→pop→write section, and :meth:`write` stays tmp+rename
    (atomic ``replace``) INSIDE that lock so a torn write can't corrupt state
    and no update is lost.

    Why ``flock`` and not ``fcntl.lockf`` / ``F_SETLK``: POSIX record locks are
    keyed by ``(process, inode)`` — two threads in one process would NOT block
    each other. BSD ``flock`` is keyed by the OPEN FILE DESCRIPTION, and each
    :meth:`transaction` call ``open()``s its own fd, so two *threads* contend
    exactly as two *processes* do. The lock is on a SIDECAR file (never
    renamed) rather than the data file, because :meth:`write`'s atomic rename
    swaps the data file's inode and would orphan a lock held on it.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or self._default_path()

    @staticmethod
    def _default_path() -> Path:
        from fulcra_collect.config import config_dir

        return config_dir() / "gmail" / "registry.json"

    @property
    def _lock_path(self) -> Path:
        return self._path.with_suffix(".lock")

    @contextlib.contextmanager
    def transaction(self) -> "Iterator[None]":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # O_CLOEXEC so the fd isn't inherited by worker subprocesses (which
        # would silently keep the lock held after a fork+exec).
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def read(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            _log.warning("gmail: registry doc at %s unreadable — treating as empty",
                         self._path)
            return {}

    def write(self, doc: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Account:
    account_id: str
    email: str
    display_order: int
    added_at: str
    status: str = STATUS_ACTIVE


@dataclass(frozen=True)
class AddAccountSession:
    """Everything the callback needs to finish an add-account flow. Returned
    by :meth:`AccountRegistry.begin_add_account`; the wizard uses
    ``authorize_url`` and the callback passes ``state`` back."""

    state: str
    code_verifier: str
    code_challenge: str
    redirect_uri: str
    authorize_url: str


@dataclass(frozen=True)
class AddAccountResult:
    ok: bool
    #: One of the reject reason codes when ``ok`` is False (opaque, no PII):
    #: ``invalid_nonce`` (missing/mismatched/replayed) | ``expired_nonce`` |
    #: ``no_client_credentials``.
    reason: str | None = None
    account_id: str | None = None
    email: str | None = None
    #: True when a new ``account_id`` was minted; False on an in-place re-auth.
    is_new: bool = False


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


class AccountRegistry:
    """Opaque ``account_id`` ↔ email registry + secrets custody + the B4
    add-account lifecycle.

    Every mutation runs inside :meth:`_locked` — the store's cross-process
    ``transaction()`` (outermost) plus this instance's ``threading.Lock`` —
    and reads the store fresh then writes it back, so the whole
    read→validate→pop→write section is atomic against other threads AND other
    processes/instances sharing the same durable store. A fake store + fake
    keychain fully exercise the semantics without any real backing service.
    """

    def __init__(
        self,
        *,
        store: RegistryStore | None = None,
        keychain: Keychain | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._store = store if store is not None else JsonFileStore()
        self._keychain = keychain if keychain is not None else CollectKeychain()
        self._transport = transport
        self._lock = threading.Lock()

    # -- locking ------------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self) -> "Iterator[None]":
        """Hold the cross-process store lock (outermost) and the in-process
        lock (innermost) for one read-modify-write section.

        Lock ORDER is fixed everywhere — store ``transaction()`` first, then
        ``self._lock`` — so two locks can never be acquired in opposite orders
        and deadlock. Every mutating method wraps its ``_load → mutate →
        _save`` in this; read-only methods don't lock (``write()`` is an atomic
        tmp+rename, so a concurrent reader always sees a whole old-or-new doc).
        """
        with self._store.transaction():
            with self._lock:
                yield

    # -- doc helpers --------------------------------------------------------

    def _load(self) -> dict:
        doc = self._store.read() or {}
        doc.setdefault("accounts", [])
        doc.setdefault("nonces", {})
        return doc

    def _save(self, doc: dict) -> None:
        self._store.write(doc)

    # -- shared client credentials -----------------------------------------

    def set_client_credentials(self, client_id: str, client_secret: str) -> None:
        """Store the shared Workspace OAuth client (one app, all accounts)."""
        self._keychain.set(_CLIENT_ID_KEY, client_id)
        self._keychain.set(_CLIENT_SECRET_KEY, client_secret)

    def client_credentials(self) -> tuple[str | None, str | None]:
        return (
            self._keychain.get(_CLIENT_ID_KEY),
            self._keychain.get(_CLIENT_SECRET_KEY),
        )

    # -- account queries ----------------------------------------------------

    def list_accounts(self) -> list[Account]:
        doc = self._load()
        rows = sorted(doc["accounts"], key=lambda r: r.get("display_order", 0))
        return [self._row_to_account(r) for r in rows]

    def get_account(self, account_id: str) -> Account | None:
        for row in self._load()["accounts"]:
            if row["account_id"] == account_id:
                return self._row_to_account(row)
        return None

    def find_by_email(self, email: str) -> Account | None:
        target = email.strip().lower()
        for row in self._load()["accounts"]:
            if row["email"].strip().lower() == target:
                return self._row_to_account(row)
        return None

    @staticmethod
    def _row_to_account(row: dict) -> Account:
        return Account(
            account_id=row["account_id"],
            email=row["email"],
            display_order=row.get("display_order", 0),
            added_at=row.get("added_at", ""),
            status=row.get("status", STATUS_ACTIVE),
        )

    def get_refresh_token(self, account_id: str) -> str | None:
        return self._keychain.get(_refresh_token_key(account_id))

    def set_status(self, account_id: str, status: str) -> None:
        with self._locked():
            doc = self._load()
            for row in doc["accounts"]:
                if row["account_id"] == account_id:
                    row["status"] = status
                    self._save(doc)
                    return

    def mark_auth_failed(self, account_id: str) -> None:
        """Fail-soft: flip the account to ``auth_failed`` (surfaced in health).
        Called by :class:`~fulcra_gmail.client.GmailClient` on an
        ``invalid_grant`` refusal. Never removes the token — a re-auth rotates
        it back to ``active``."""
        self.set_status(account_id, STATUS_AUTH_FAILED)

    def remove_account(self, account_id_or_email: str) -> bool:
        """Drop the keychain token + the registry row. Idempotent; returns
        True if a row was removed. Ledger/Files history is retained elsewhere
        (not this registry's concern)."""
        with self._locked():
            doc = self._load()
            match: dict | None = None
            for row in doc["accounts"]:
                if (row["account_id"] == account_id_or_email
                        or row["email"].strip().lower()
                        == account_id_or_email.strip().lower()):
                    match = row
                    break
            if match is None:
                return False
            self._keychain.delete(_refresh_token_key(match["account_id"]))
            doc["accounts"] = [
                r for r in doc["accounts"] if r["account_id"] != match["account_id"]
            ]
            self._save(doc)
            return True

    # -- B4: add-account nonce lifecycle -----------------------------------

    def begin_add_account(
        self, redirect_uri: str, *, expected_email: str | None = None
    ) -> AddAccountSession:
        """Mint a single-use ``state`` nonce + PKCE pair and record the setup
        session, returning everything the wizard needs (incl. the authorize
        URL).

        ``expected_email`` is an OPTIONAL operator hint recorded for audit
        only — the account is ALWAYS named from ``getProfile`` at completion,
        never from this hint (B4).
        """
        client_id, _secret = self.client_credentials()
        state = uuid.uuid4().hex + uuid.uuid4().hex  # 256-bit unguessable nonce
        code_verifier, code_challenge = _client.generate_pkce()
        with self._locked():
            doc = self._load()
            self._gc_nonces(doc)
            doc["nonces"][state] = {
                "intent": "add_account",
                "created_at": _now(),
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
                "expected_email": expected_email,
            }
            self._save(doc)
        authorize_url = _client.build_authorize_url(
            client_id or "", redirect_uri, state, code_challenge
        )
        return AddAccountSession(
            state=state,
            code_verifier=code_verifier,
            code_challenge=code_challenge,
            redirect_uri=redirect_uri,
            authorize_url=authorize_url,
        )

    def _gc_nonces(self, doc: dict) -> None:
        """Drop expired nonces (mutates ``doc`` in place)."""
        now = _now()
        doc["nonces"] = {
            n: s
            for n, s in doc["nonces"].items()
            if now - s.get("created_at", 0) <= NONCE_TTL_SECONDS
        }

    def _consume_nonce(self, state: str | None) -> dict | None:
        """Atomically consume ``state`` exactly once.

        Returns the setup-session dict on success, or ``None`` for every
        reject path — missing / mismatched (unknown) / replayed (already
        consumed → gone) / expired (present but stale). Expired sessions are
        dropped. NOTHING outside this method interprets the nonce, so
        one-time semantics are guaranteed by the pop under the lock.
        """
        if not state:
            return None
        with self._locked():
            doc = self._load()
            session = doc["nonces"].pop(state, None)
            if session is None:
                # missing / mismatched / already-replayed
                self._save(doc)
                return None
            if _now() - session.get("created_at", 0) > NONCE_TTL_SECONDS:
                # expired — already popped above, so it stays consumed
                self._save(doc)
                return None
            self._save(doc)
            return session

    def complete_add_account(self, state: str | None, code: str) -> AddAccountResult:
        """Finish an add-account flow after the OAuth callback (B4).

        Order matters for the "no token stored on reject" invariant: the nonce
        is consumed FIRST; any reject returns before a single token call, so a
        bad callback never writes a secret. Only a valid consume proceeds to
        the code exchange → ``getProfile`` binding → keychain/registry write.
        """
        session = self._consume_nonce(state)
        if session is None:
            _log.warning("gmail: add-account rejected — invalid/expired/replayed state")
            reason = "invalid_nonce"
            # Distinguish expired for the caller when we can still see it was
            # a known-but-stale nonce is not possible post-pop; keep it opaque.
            return AddAccountResult(ok=False, reason=reason)

        client_id, client_secret = self.client_credentials()
        if not (client_id and client_secret):
            _log.error("gmail: add-account cannot proceed — shared client creds absent")
            return AddAccountResult(ok=False, reason="no_client_credentials")

        tokens = _client.exchange_code(
            code,
            code_verifier=session["code_verifier"],
            redirect_uri=session["redirect_uri"],
            client_id=client_id,
            client_secret=client_secret,
            transport=self._transport,
        )
        access_token = tokens["access_token"]
        refresh_token = tokens["refresh_token"]

        # B4 binding: the authorized address is DISCOVERED from the granted
        # token, not from any operator hint.
        profile = _client.fetch_profile(access_token, transport=self._transport)
        email = profile["emailAddress"]

        with self._locked():
            doc = self._load()
            existing = self._find_row(doc, email)
            if existing is not None:
                # Re-auth of a known address: rotate token in place, no dup row,
                # and clear any prior auth_failed status.
                account_id = existing["account_id"]
                existing["status"] = STATUS_ACTIVE
                is_new = False
            else:
                account_id = uuid.uuid4().hex
                order = 1 + max((r.get("display_order", 0) for r in doc["accounts"]),
                                default=0)
                doc["accounts"].append({
                    "account_id": account_id,
                    "email": email,
                    "display_order": order,
                    "added_at": _iso_now(),
                    "status": STATUS_ACTIVE,
                })
                is_new = True
            self._save(doc)

        # Secret last: the row exists to point at it.
        self._keychain.set(_refresh_token_key(account_id), refresh_token)
        _log.info("gmail: account %s bound via getProfile (is_new=%s)",
                  account_id, is_new)
        return AddAccountResult(
            ok=True, account_id=account_id, email=email, is_new=is_new
        )

    @staticmethod
    def _find_row(doc: dict, email: str) -> dict | None:
        target = email.strip().lower()
        for row in doc["accounts"]:
            if row["email"].strip().lower() == target:
                return row
        return None

    # -- introspection (tests / health) ------------------------------------

    def _snapshot(self) -> dict:
        """A deep copy of the persisted doc — for tests/health only."""
        return copy.deepcopy(self._load())
