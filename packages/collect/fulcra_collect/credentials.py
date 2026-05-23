"""Plugin secrets, stored in the OS keychain via `keyring`.

Each secret is keyed by (plugin_id, credential key). The keyring service
name namespaces every entry under this app so it is distinct from any
other keychain item.
"""
from __future__ import annotations

import keyring
import keyring.errors

_SERVICE_PREFIX = "fulcra-collect"


def _service(plugin_id: str) -> str:
    return f"{_SERVICE_PREFIX}:{plugin_id}"


def set_secret(plugin_id: str, key: str, value: str) -> None:
    keyring.set_password(_service(plugin_id), key, value)


def get_secret(plugin_id: str, key: str) -> str | None:
    return keyring.get_password(_service(plugin_id), key)


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
