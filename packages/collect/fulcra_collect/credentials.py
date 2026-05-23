"""Plugin secrets, stored in the OS keychain via `keyring`.

Each secret is keyed by (plugin_id, credential key). The keyring service
name namespaces every entry under this app so it is distinct from any
other keychain item.
"""
from __future__ import annotations

import keyring

_SERVICE_PREFIX = "fulcra-collect"


def _service(plugin_id: str) -> str:
    return f"{_SERVICE_PREFIX}:{plugin_id}"


def set_secret(plugin_id: str, key: str, value: str) -> None:
    keyring.set_password(_service(plugin_id), key, value)


def get_secret(plugin_id: str, key: str) -> str | None:
    return keyring.get_password(_service(plugin_id), key)


def delete_secret(plugin_id: str, key: str) -> None:
    keyring.delete_password(_service(plugin_id), key)


def has_secret(plugin_id: str, key: str) -> bool:
    """Return True iff a non-empty secret is present in the keychain for
    (plugin_id, key). The menubar UI's `credential_status` handler is the
    only caller — it reports presence without ever reading the value."""
    return bool(get_secret(plugin_id, key))
