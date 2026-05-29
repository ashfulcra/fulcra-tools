"""Plugin secrets, stored in the OS keychain via `keyring`.

Each secret is keyed by (plugin_id, credential key). The keyring service
name namespaces every entry under this app so it is distinct from any
other keychain item.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from threading import Lock

import keyring
import keyring.errors

_SERVICE_PREFIX = "fulcra-collect"

_log = logging.getLogger("fulcra_collect.credentials")

# How long to wait on a single keychain read before treating the item as
# absent. The happy-path read returns in well under a millisecond; this only
# fires when the read is blocked (see _keyring_get).
_KEYCHAIN_READ_TIMEOUT_S = 3.0


def _keyring_get(service: str, account: str,
                 *, timeout: float = _KEYCHAIN_READ_TIMEOUT_S) -> str | None:
    """Read a keychain item without ever blocking the caller indefinitely.

    macOS Security.framework reads block on an ACL-confirmation dialog
    ("<app> wants to use a key in your keychain") whenever the accessing
    binary's identity no longer matches the item's ACL — which happens after
    a reinstall or a re-signed bundle. The daemon's control server is
    single-threaded, so ONE blocked read wedges every request (status,
    quick-record, credential checks) and the menubar shows "daemon not
    reachable". Run the read on a worker thread and give up after ``timeout``,
    returning None so the daemon degrades to "not authenticated" (the SP5
    Reconnect banner already covers that state) instead of hanging.

    The abandoned worker stays parked on the OS call until the dialog is
    dismissed — a bounded one-thread-per-timeout leak, far better than a dead
    daemon. A keychain that isn't prompting returns near-instantly, so the
    timeout never fires on the happy path.
    """
    result: dict = {}

    def _run() -> None:
        try:
            result["value"] = keyring.get_password(service, account)
        except Exception as exc:  # noqa: BLE001 — re-raised on the caller thread
            result["error"] = exc

    t = threading.Thread(target=_run, daemon=True, name="keyring-read")
    t.start()
    t.join(timeout)
    if t.is_alive():
        _log.warning(
            "keyring read for %s/%s timed out after %.1fs — treating the item "
            "as absent. The keychain is most likely waiting on an unanswered "
            "access-confirmation prompt; re-sign-in (or click 'Always Allow' "
            "on the prompt) to restore access.",
            service, account, timeout,
        )
        return None
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _service(plugin_id: str) -> str:
    return f"{_SERVICE_PREFIX}:{plugin_id}"


def set_secret(plugin_id: str, key: str, value: str) -> None:
    keyring.set_password(_service(plugin_id), key, value)


def get_secret(plugin_id: str, key: str) -> str | None:
    return _keyring_get(_service(plugin_id), key)


def delete_secret(plugin_id: str, key: str) -> None:
    """Idempotent: silently no-ops if the entry is already absent. The
    UI calls this on every 'Disconnect' click; absence is success."""
    try:
        keyring.delete_password(_service(plugin_id), key)
    except keyring.errors.PasswordDeleteError:
        pass


def has_secret(plugin_id: str, key: str) -> bool:
    """Return True iff a non-empty secret is present in the keychain for
    (plugin_id, key). Intended for the menubar's `credential_status`
    handler, which reports credential presence without revealing values."""
    return bool(get_secret(plugin_id, key))


# ---------------------------------------------------------------------------
# User-level (account-scoped) secrets
# ---------------------------------------------------------------------------

_USER_SERVICE = "fulcra-collect:user"


def set_user_secret(key: str, value: str) -> None:
    """Store a user-level (not plugin-specific) secret in the OS keychain.
    Used for the shared Fulcra bearer token and any other user-account-
    level credential."""
    keyring.set_password(_USER_SERVICE, key, value)


def get_user_secret(key: str) -> str | None:
    return _keyring_get(_USER_SERVICE, key)


def has_user_secret(key: str) -> bool:
    """True iff a non-empty user-level secret is present for `key`."""
    return bool(get_user_secret(key))


def delete_user_secret(key: str) -> None:
    """Idempotent: silently no-ops if the entry is already absent."""
    try:
        keyring.delete_password(_USER_SERVICE, key)
    except keyring.errors.PasswordDeleteError:
        pass


# ---------------------------------------------------------------------------
# Fulcra access-token refresh (SP5 task 1)
#
# The daemon caches the Fulcra access token in the keychain. Access tokens
# typically expire after ~1 hour; the `fulcra` CLI separately stores a
# refresh token and knows how to rotate it. Rather than re-implement the
# OAuth refresh dance in-process (which would duplicate refresh-token state
# between daemon and CLI and risk drift), we shell out to
# `fulcra auth print-access-token` whenever a 401 indicates the cached
# access token has gone stale. That command mints a fresh access token via
# the CLI's refresh-token store, prints it to stdout, and exits 0.
#
# On hard failure (CLI missing, timed out, returned non-zero, empty stdout),
# we set a process-level `_refresh_failed` flag. The
# `/api/fulcra/auth/status` route reads it via `is_refresh_failed()` so the
# web UI can render a Reconnect banner in Settings. Successful interactive
# sign-in (paste-token or cli_login) calls `clear_refresh_failed()` to
# dismiss the banner.
# ---------------------------------------------------------------------------

# Serialises concurrent 401-driven refreshes so multiple plugins/handlers
# hitting an expired token at once don't fork-bomb the CLI. The CLI itself
# is fast (sub-second on the happy path) so a Lock is fine.
_refresh_lock = Lock()

# Process-level state: True once a refresh attempt has exhausted (CLI not
# installed, non-zero exit, empty stdout, or timed out). Cleared on
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
    """
    found = shutil.which("fulcra")
    if found:
        return found

    # Well-known locations the launchd-default PATH misses. Order
    # matters: ~/.local/bin first (uv tool install), then
    # /opt/homebrew/bin (Apple Silicon brew), then /usr/local/bin
    # (Intel brew + general). All paths are static; we don't try to
    # introspect $HOME for portability across machines that might
    # have unusual home-dir layouts.
    candidates = [
        os.path.expanduser("~/.local/bin/fulcra"),
        "/opt/homebrew/bin/fulcra",
        "/usr/local/bin/fulcra",
    ]
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def refresh_fulcra_access_token() -> str | None:
    """Re-invoke ``fulcra auth print-access-token`` and store the result.

    Used by the daemon's Fulcra-API call path when a 401 comes back — the
    CLI has refresh tokens stored separately and can mint a fresh access
    token without user interaction (until the refresh token itself
    expires).

    Returns the new access token on success, None on failure. On failure
    sets the module-level ``_refresh_failed`` flag which the
    ``/api/fulcra/auth/status`` route reads so the web UI can show a
    Reconnect banner.

    Concurrency: serialised via ``_refresh_lock`` so multiple plugins hitting
    401 simultaneously share a refresh window rather than each forking
    their own CLI subprocess in parallel. Note that the lock serialises
    the refreshes but does NOT deduplicate them — N concurrent 401s
    produce N sequential CLI invocations rather than 1 shared. That's
    fine for the single-user daemon (refresh failure surfaces as a banner
    + user re-signs-in), but worth noting if a future use case ever
    batches many simultaneous Fulcra calls.

    Why a CLI subprocess rather than the OAuth refresh dance in-process:
    the ``fulcra`` CLI already implements the refresh dance correctly
    (including refresh-token rotation when Fulcra sends a new one) and
    owns the refresh-token storage on disk. Re-implementing that in the
    daemon would duplicate state + create refresh-token drift between the
    CLI and daemon when they're used in the same session.
    """
    global _refresh_failed
    with _refresh_lock:
        cli_path = _find_fulcra_cli()
        if not cli_path:
            _log.warning(
                "refresh_fulcra_access_token: fulcra CLI not found on PATH or "
                "in any well-known location (~/.local/bin, /opt/homebrew/bin, "
                "/usr/local/bin)."
            )
            _refresh_failed = True
            return None
        try:
            r = subprocess.run(
                [cli_path, "auth", "print-access-token"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            _log.warning("refresh_fulcra_access_token: CLI timed out")
            _refresh_failed = True
            return None
        if r.returncode != 0 or not r.stdout.strip():
            _log.warning(
                "refresh_fulcra_access_token: CLI returned %d (stderr=%s)",
                r.returncode,
                (r.stderr or "").strip()[:200],
            )
            _refresh_failed = True
            return None
        new_token = r.stdout.strip()
        set_user_secret("bearer-token", new_token)
        _refresh_failed = False
        _log.info("refresh_fulcra_access_token: succeeded; keychain updated")
        return new_token


def is_refresh_failed() -> bool:
    """Whether the most recent refresh attempt exhausted.

    Surfaced via ``/api/fulcra/auth/status`` so the web UI can show a
    Reconnect banner when the CLI's refresh token has also expired (or the
    CLI isn't installed). Read-only; flip via ``clear_refresh_failed`` or
    via a successful ``refresh_fulcra_access_token`` call.
    """
    return _refresh_failed


def clear_refresh_failed() -> None:
    """Clear the refresh-failed flag.

    Called after a successful interactive sign-in (paste-token POST or
    cli_login POST) so the Reconnect banner disappears once the user has
    re-authed. Idempotent — calling when already False is a no-op.
    """
    global _refresh_failed
    _refresh_failed = False
