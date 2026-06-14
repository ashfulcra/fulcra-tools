"""_load_meta must raise ValueError (-> rc 2) on a malformed meta.json.

It read data["spec"]/["created_at"]/["updated_at"] unguarded, so a meta.json
that is valid JSON but missing a key raised KeyError — which escapes run()'s
(StoreError, LockError, ValueError) handler as an uncaught traceback.
"""
import pytest

from fulcra_vault.cli import _load_meta


class _Store:
    def __init__(self, text):
        self._t = text

    def read_text(self, path):
        return self._t


def test_load_meta_missing_key_raises_valueerror():
    with pytest.raises(ValueError):
        _load_meta(_Store('{"created_at": "x", "updated_at": "y"}'))  # no "spec"


def test_load_meta_non_object_raises_valueerror():
    with pytest.raises(ValueError):
        _load_meta(_Store("[]"))
