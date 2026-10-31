"""Plugin secrets, stored in the OS keychain via `keyring`.

Each secret is keyed by (plugin_id, credential key). The keyring service
name namespaces every entry under this app so it is distinct from any
other keychain item.
"""
from __future__ import annotations

import logging
import base64
import json
import math
import subprocess
import threading
import sys
from threading import Lock
from contextlib import contextmanager
from uuid import UUID

import keyring
import keyring.errors

from .macos_keychain import KeychainAccessRequired

_SERVICE_PREFIX = "fulcra-collect"

_log = logging.getLogger("fulcra_collect.credentials")

# Background reads never request macOS authentication UI. Explicit access
# requests use the same pending-call fence, so repeated checks/clicks cannot
# stack another OS call while an earlier one is still outstanding.
_KEYCHAIN_READ_TIMEOUT_S = 3.0
_MAX_PENDING_READS = 16
_pending_reads: set[tuple[str, str]] = set()
_pending_reads_lock = Lock()
_credential_revision = 0
_active_credential_writes = 0


@contextmanager
def _credential_write():
    global _credential_revision, _active_credential_writes
    with _pending_reads_lock:
        _credential_revision += 1
        _active_credential_writes += 1
    _invalidate_user_cache()
    try:
        yield
    finally:
        _invalidate_user_cache()
        with _pending_reads_lock:
            _credential_revision += 1
            _active_credential_writes -= 1


def _uses_macos_keychain() -> bool:
    if sys.platform != "darwin":
        return False
    from keyring.backends.macOS import Keyring
    return isinstance(keyring.get_keyring(), Keyring)


class KeychainReadPending(KeychainAccessRequired):
    """An owned read is still pending; another request must not prompt."""


class CredentialStateChanged(KeychainAccessRequired):
    """An overlapping credential transition invalidated this observation."""


def read_availability_error(exc: Exception) -> str:
    if isinstance(exc, (TimeoutError, KeychainReadPending)):
        return "pending"
    if isinstance(exc, CredentialStateChanged):
        return "changed"
    if isinstance(exc, KeychainAccessRequired):
        return "access_required"
    return "unavailable"


class CredentialReadError(KeychainAccessRequired):
    """Nonsecret origin from an actual declared-credential read, not traceback parsing."""
    def __init__(self, exc, *, stage, scope, key):
        super().__init__("Saved credential access is unverified; no credential was changed.")
        self.detail = {"stage": stage, "scope": scope, "key": key,
                       "availability": read_availability_error(exc)}


def credential_revision() -> int:
    with _pending_reads_lock:
        if _active_credential_writes:
            raise CredentialStateChanged("Credential state is changing; retry.")
        return _credential_revision


def _keyring_get(service: str, account: str, *, timeout: float | None = None,
                 allow_ui: bool = False) -> str | None:
    """Bounded read; no late result is reused by a subsequent fresh caller."""
    if timeout is None:
        timeout = _KEYCHAIN_READ_TIMEOUT_S
    item = (service, account)
    with _pending_reads_lock:
        if _active_credential_writes:
            raise CredentialStateChanged("Credential state is changing; retry.")
        if item in _pending_reads or len(_pending_reads) >= _MAX_PENDING_READS:
            raise KeychainReadPending("A Keychain read is still pending; retry after it completes.")
        revision = _credential_revision
        _pending_reads.add(item)
    result: dict = {}

    def _run() -> None:
        try:
            if _uses_macos_keychain():
                from .macos_keychain import get_password
                result["value"] = get_password(service, account, allow_ui=allow_ui)
            else:
                result["value"] = keyring.get_password(service, account)
        except Exception as exc:  # re-raised without logging credential values
            result["error"] = exc
        finally:
            with _pending_reads_lock:
                _pending_reads.discard(item)

    t = threading.Thread(target=_run, daemon=True, name="keyring-read")
    try:
        t.start()
    except BaseException:
        with _pending_reads_lock:
            _pending_reads.discard(item)
        raise
    t.join(timeout)
    if t.is_alive():
        # The pending slot remains occupied until the actual OS call ends.
        raise TimeoutError("Keychain read is still pending; availability is unknown.")
    with _pending_reads_lock:
        if revision != _credential_revision or _active_credential_writes:
            raise CredentialStateChanged("Credential state changed during the read; retry.")
    if "error" in result:
        raise result["error"]
    return result.get("value")


def get_plugin_credentials(plugin) -> dict[str, str | None]:
    """Hydrate declared scopes quietly; unused optional credentials stay optional."""
    values = {}
    for cred in plugin.required_credentials:
        try:
            values[cred.key] = (get_user_secret_fresh(cred.key) if getattr(cred, "user_level", False)
                                else get_secret(plugin.id, cred.key))
        except (keyring.errors.KeyringError, TimeoutError) as exc:
            if plugin.id == "trakt" or getattr(cred, "required", True):
                raise CredentialReadError(exc, stage="credential_hydration",
                    scope="user" if getattr(cred, "user_level", False) else "plugin",
                    key=cred.key) from exc
            # A LAN mode can run without its optional cloud API key. Keep
            # availability UNKNOWN in credential_status; never claim missing.
            values[cred.key] = None
    return values


def authorize_secret(plugin_id: str, key: str) -> bool:
    """Explicit user action only: request access without exposing the value."""
    return _authorize_item(_service(plugin_id), key)


def authorize_user_secret(key: str) -> bool:
    return _authorize_item(_USER_SERVICE, key)


def _authorize_item(service: str, key: str) -> bool:
    revision = credential_revision()
    _keyring_get(service, key, allow_ui=True)
    if credential_revision() != revision:
        raise CredentialStateChanged("Credential state changed during authorization; retry.")
    present = bool(_keyring_get(service, key))
    if credential_revision() != revision:
        raise CredentialStateChanged("Credential state changed during authorization; retry.")
    return present


def _service(plugin_id: str) -> str:
    return f"{_SERVICE_PREFIX}:{plugin_id}"


def set_secret(plugin_id: str, key: str, value: str) -> None:
    with _credential_write():
        keyring.set_password(_service(plugin_id), key, value)


def _mutate_trakt_secret_quiet(key: str, value: str | None) -> None:
    """Background Trakt receipt/mirror only; no UI-capable backend fallback."""
    if not _uses_macos_keychain():
        raise KeychainAccessRequired('Quiet Trakt credential writes unavailable')
    from .macos_keychain import mutate_trakt_password
    with _credential_write():
        mutate_trakt_password(key, value)


def get_secret(plugin_id: str, key: str) -> str | None:
    return _keyring_get(_service(plugin_id), key)


def delete_secret(plugin_id: str, key: str) -> None:
    """Idempotent: silently no-ops if the entry is already absent. The
    UI calls this on every 'Disconnect' click; absence is success."""
    try:
        with _credential_write():
            keyring.delete_password(_service(plugin_id), key)
    except keyring.errors.PasswordDeleteError:
        pass


def has_secret(plugin_id: str, key: str) -> bool:
    """Return True iff a non-empty secret is present in the keychain for
    (plugin_id, key). Intended for the menubar's `credential_status`
    handler, which reports credential presence without revealing values."""
    return bool(_keyring_get(_service(plugin_id), key))


# ---------------------------------------------------------------------------
# User-level (account-scoped) secrets
# ---------------------------------------------------------------------------

_USER_SERVICE = "fulcra-collect:user"

# UI cache only. Background macOS reads cannot request UI, and account-bound
# guards use get_user_secret_fresh to observe other processes. A generation
# fence prevents an old in-flight read overwriting a newer set/delete/clear.
_user_secret_cache: dict[str, str | None] = {}
_user_secret_cache_lock = Lock()
_user_secret_cache_generation = 0


def _invalidate_user_cache() -> None:
    """Drop all cached user secrets. For tests (so a token cached by one test
    can't leak into the next) and for a hard re-read after external changes."""
    global _user_secret_cache_generation
    with _user_secret_cache_lock:
        _user_secret_cache_generation += 1
        _user_secret_cache.clear()


def _clear_caches() -> None:
    """Invalidate old completions without releasing pending OS-call slots."""
    with _credential_write():
        pass


def set_user_secret(key: str, value: str) -> None:
    """Store a user-level (not plugin-specific) secret in the OS keychain.
    Used for the shared Fulcra bearer token and any other user-account-
    level credential."""
    with _credential_write():
        keyring.set_password(_USER_SERVICE, key, value)


def _mutate_user_secret_quiet(key: str, value: str) -> None:
    """No UI-capable backend fallback for automatic credential publication."""
    if not _uses_macos_keychain():
        raise KeychainAccessRequired('Quiet account credential writes unavailable; reconnect.')
    from .macos_keychain import mutate_user_password
    mutate_user_password(key, value)


def set_user_secret_quiet(key: str, value: str) -> None:
    """Bounded, quiet renewal write; ambiguous acknowledgement is failure.

    Interactive sign-in uses set_user_secret. A missing quiet backend requires
    reconnecting rather than a background fallback that might prompt.
    """
    if key not in {'bearer-token', 'oauth-credentials'}:
        raise KeychainAccessRequired('Automatic credential scope unavailable.')
    with _credential_write():
        _mutate_user_secret_quiet(key, value)


def get_user_secret(key: str) -> str | None:
    with _user_secret_cache_lock:
        with _pending_reads_lock:
            if _active_credential_writes:
                raise KeychainAccessRequired("Credential state is changing; retry.")
        if key in _user_secret_cache:
            return _user_secret_cache[key]
        generation = _user_secret_cache_generation
    value = _keyring_get(_USER_SERVICE, key)
    with _user_secret_cache_lock:
        with _pending_reads_lock:
            active_write = _active_credential_writes
        if active_write or generation != _user_secret_cache_generation:
            raise CredentialStateChanged("Credential state changed during the read; retry.")
        _user_secret_cache[key] = value
    return value


def get_user_secret_fresh(key: str) -> str | None:
    """Bounded keychain read for account-bound workers, bypassing process cache."""
    return _keyring_get(_USER_SERVICE, key)


def user_secret_availability(key: str) -> dict:
    """One bounded fresh QUIET observation, with separately labelled prior presence."""
    from datetime import datetime, timezone
    with _user_secret_cache_lock:
        previous = _user_secret_cache.get(key)
        saved = bool(previous) if key in _user_secret_cache else None
    attempted = False
    try:
        revision = credential_revision()
        attempted = True
        present = bool(get_user_secret_fresh(key))
        if credential_revision() != revision:
            raise CredentialStateChanged("Credential state changed during observation; retry.")
        availability = "present" if present else "missing"
        saved = present
    except Exception as exc:
        availability = read_availability_error(exc)
    return {"availability": availability, "saved_presence": saved,
            "checked_at": datetime.now(timezone.utc).isoformat() if attempted else None,
            "provenance": "fresh_quiet_keychain" if attempted else "not_checked"}


def has_user_secret(key: str) -> bool:
    """True iff a non-empty user-level secret is present for `key`."""
    return bool(get_user_secret(key))


def delete_user_secret(key: str) -> None:
    """Idempotent: silently no-ops if the entry is already absent."""
    try:
        with _credential_write():
            keyring.delete_password(_USER_SERVICE, key)
    except keyring.errors.PasswordDeleteError:
        pass


# ---------------------------------------------------------------------------
# Fulcra access-token refresh (SP5 task 1)
#
# New browser sign-ins use Collect-owned SDK credentials and a durable grant
# journal (owned_auth.py). Pre-upgrade tokens retain principal-checked CLI
# renewal only until the first committed owned-auth revision. Manual tokens do
# not inherit terminal CLI authority. A failed renewal sets the reconnect flag.
# ---------------------------------------------------------------------------

# Serializes refresh requests in this process. Owned refresh adds its own
# cross-process lease and persists a no-replay claim before spending a grant.
_refresh_lock = Lock()

# Process-level state: True after a current-authority renewal failure. Stale
# results cannot overwrite a newer interactive sign-in's status. Cleared on
# successful refresh OR on successful interactive sign-in from the routes
# module via clear_refresh_failed().
_refresh_failed = False


def _find_fulcra_cli() -> str | None:
    """Find the fulcra CLI binary.

    The launchd-managed daemon runs with a restricted PATH
    (`/usr/bin:/bin:/usr/sbin:/sbin`) that excludes the most common
    install locations for user-installed Python CLIs. Check PATH first
    (the common case for terminal-launched daemons + manual installs),
    then fall back to well-known locations.

    Discovered 2026-05-27: the launchd plist has no PATH override, so
    `shutil.which('fulcra')` returns None and auth-refresh-on-401
    silently fails even when the CLI is installed and working. The
    user sees the Reconnect banner immediately on a 401 with no way
    to know the daemon just can't find the CLI binary.

    Returns the absolute path to the CLI if found, None if not.

    Now a thin delegate to ``fulcra_common.client.find_fulcra_cli`` — the
    resolver moved there (2026-07-06) so worker-side clients created via
    ``BaseFulcraClient.get_token`` survive launchd too, instead of only the
    refresh path. The shared resolver additionally checks the venv-sibling
    location first (a venv-local install wins).
    """
    from fulcra_common.client import find_fulcra_cli

    return find_fulcra_cli()


def _token_principal(token: str | None, *, fresh=False) -> str | None:
    """Read trusted token metadata; the API still verifies its signature.

    Only an identifiable existing Collect account can authorize silent
    renewal. Fresh candidates need an unambiguous finite future expiry.
    """
    try:
        if not isinstance(token, str) or len(token.split(".")) != 3:
            return None
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        account = claims["fulcradynamics.com/userid"]
        if not isinstance(account, str):
            return None
        account = str(UUID(account))
        if fresh:
            from .plugin import _jwt_expired
            expiry = claims.get("exp")
            if (type(expiry) not in {int, float} or not math.isfinite(expiry)
                    or _jwt_expired(token)):
                return None
        return account
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        return None


def refresh_fulcra_access_token(*, expected_token: str | None = None) -> str | None:
    """Renew only the established Collect account; never adopt a CLI sign-in.

    Owned SDK credentials use one bounded rotating-grant request with durable
    intent, cross-process serialization and final revision/account checks.
    Manual tokens require a fresh explicit sign-in. Pre-upgrade credentials may
    use the older principal-checked CLI path until an owned-auth revision exists.
    Interactive changes, including a same-token paste, win over a late refresh.
    ``expected_token`` binds retries to the failed request's original account.
    Returns a fresh token, or None and the reconnect flag on renewal failure.
    """
    from . import config
    from .config_leases import account_refresh_scope
    from .owned_auth import attempts
    global _refresh_failed
    with _refresh_lock:
        try:
            with account_refresh_scope():
                revision = attempts().read_committed()
                original = get_user_secret_fresh('bearer-token')
                if (expected_token is not None
                        and _token_principal(expected_token) != _token_principal(original)):
                    return None
                if config.load().account_transition:
                    return None
                if (expected_token is not None and original != expected_token
                        and _token_principal(original, fresh=True) is not None):
                    return original
        except Exception:
            _log.warning('refresh_fulcra_access_token: credential state unavailable')
            return None
        result = _refresh_access_token_locked(expected_token=expected_token)
        # A stale failure cannot mark a newer successful sign-in as expired.
        # Account SH fences this last compare/flag write against interactive
        # account EX, including a same-token sign-in with a new revision.
        try:
            with account_refresh_scope():
                current = get_user_secret_fresh('bearer-token')
                if (attempts().read_committed() == revision
                        and current == (result if result is not None else original)):
                    _refresh_failed = result is None
        except Exception:
            pass  # Unknown authority cannot relabel the current account.
        return result


def _refresh_access_token_locked(*, expected_token=None):
    """Return the renewal result; caller serializes and owns the status flag."""
    from . import config
    from .config_leases import account_refresh_scope
    from .owned_auth import attempts, refresh_owned
    handled, owned_token = refresh_owned(expected_token=expected_token)
    if handled:
        return owned_token
    with account_refresh_scope():
        original = get_user_secret_fresh("bearer-token")
        principal = _token_principal(original)
        if config.load().account_transition or principal is None:
            _log.warning("refresh_fulcra_access_token: Collect principal unavailable; reconnect")
            return None
        if expected_token is not None:
            if _token_principal(expected_token) != principal:
                _log.warning("refresh_fulcra_access_token: request principal changed; retry cancelled")
                return None
            if original != expected_token and _token_principal(original, fresh=True) == principal:
                return original
    cli_path = _find_fulcra_cli()
    if not cli_path:
        _log.warning(
            "refresh_fulcra_access_token: fulcra CLI not found on PATH or "
            "in any well-known location (~/.local/bin, /opt/homebrew/bin, "
            "/usr/local/bin)."
        )
        return None
    try:
        r = subprocess.run(
            [cli_path, "auth", "print-access-token"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        _log.warning("refresh_fulcra_access_token: CLI unavailable or timed out")
        return None
    if r.returncode != 0 or not r.stdout.strip():
        _log.warning(
            "refresh_fulcra_access_token: CLI returned %d without a usable token",
            r.returncode,
        )
        return None
    new_token = r.stdout.strip()
    if _token_principal(new_token, fresh=True) != principal:
        _log.warning("refresh_fulcra_access_token: CLI principal or expiry mismatch; reconnect")
        return None
    # Even a same-token interactive sign-in revokes legacy CLI authority.
    try:
        with account_refresh_scope():
            if attempts().read_committed() != (None, None):
                return None
            current = get_user_secret_fresh("bearer-token")
            if config.load().account_transition or _token_principal(current) != principal:
                _log.warning("refresh_fulcra_access_token: sign-in changed during refresh; write cancelled")
                return None
            if current != original:
                # A newer interactive sign-in or another worker's refresh wins.
                if _token_principal(current, fresh=True) == principal:
                    return current
                return None
            set_user_secret_quiet("bearer-token", new_token)
    except Exception:
        return None
    _log.info("refresh_fulcra_access_token: succeeded; keychain updated")
    return new_token


def is_refresh_failed() -> bool:
    """Whether the most recent refresh attempt exhausted.

    Surfaced via ``/api/fulcra/auth/status`` so the web UI can show a
    Reconnect banner after renewal of the current account fails. Read-only;
    flip via ``clear_refresh_failed`` or
    via a successful ``refresh_fulcra_access_token`` call.
    """
    return _refresh_failed


def clear_refresh_failed() -> None:
    """Clear the refresh-failed flag.

    Called inside a successful interactive credential transition, so stale
    refresh results cannot restore an older account's reconnect banner. Idempotent — calling when already False is a no-op.
    """
    global _refresh_failed
    _refresh_failed = False
