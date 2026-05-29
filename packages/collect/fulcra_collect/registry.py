"""Plugin discovery. Plugins register under the `fulcra_collect.plugins`
entry-point group; each entry resolves to a `Plugin` (or a zero-arg
callable returning one). A plugin that fails to load, resolves to a
non-Plugin, or collides on id is excluded and recorded — never fatal.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from importlib.metadata import entry_points

from ._bundled_plugins import BUNDLED_PLUGINS
from .plugin import Plugin

ENTRY_POINT_GROUP = "fulcra_collect.plugins"


@dataclass
class RegistryResult:
    plugins: dict[str, Plugin] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)  # entry name -> message


@dataclass(frozen=True)
class _ManifestEntry:
    """Adapts a (id, "module:attr") manifest row to the .name/.load()
    shape ``load_plugins`` already consumes, so the frozen path reuses
    the exact same resolution + error handling as the entry-point path."""
    name: str
    target: str

    def load(self):
        module_path, _, attr = self.target.partition(":")
        return getattr(importlib.import_module(module_path), attr)


def load_plugins(entries) -> RegistryResult:
    """Resolve an iterable of entry-point-like objects (each with `.name`
    and `.load()`) into a RegistryResult."""
    result = RegistryResult()
    for entry in entries:
        try:
            obj = entry.load()
            if callable(obj) and not isinstance(obj, Plugin):
                obj = obj()
            if not isinstance(obj, Plugin):
                raise TypeError(f"entry {entry.name!r} resolved to {type(obj).__name__}, "
                                "expected a Plugin")
            if obj.id in result.plugins:
                result.errors[entry.name] = f"duplicate plugin id {obj.id!r}"
                continue
            result.plugins[obj.id] = obj
        except Exception as exc:  # noqa: BLE001 — a bad plugin must not crash the hub
            result.errors[entry.name] = f"{type(exc).__name__}: {exc}"
    return result


def discover() -> RegistryResult:
    """Discover plugins from the entry-point group, falling back to the
    static manifest when that's empty.

    Editable/dev installs expose the entry points normally. A py2app
    freeze drops the .dist-info metadata, so entry_points() comes back
    empty there — and we import the known plugins from BUNDLED_PLUGINS
    instead."""
    eps = list(entry_points(group=ENTRY_POINT_GROUP))
    if eps:
        return load_plugins(eps)
    return load_plugins(
        _ManifestEntry(name=pid, target=target)
        for pid, target in BUNDLED_PLUGINS
    )
