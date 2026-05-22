"""Plugin discovery. Plugins register under the `fulcra_collect.plugins`
entry-point group; each entry resolves to a `Plugin` (or a zero-arg
callable returning one). A plugin that fails to load, resolves to a
non-Plugin, or collides on id is excluded and recorded — never fatal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import entry_points

from .plugin import Plugin

ENTRY_POINT_GROUP = "fulcra_collect.plugins"


@dataclass
class RegistryResult:
    plugins: dict[str, Plugin] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)  # entry name -> message


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
    """Discover plugins from the real entry-point group."""
    return load_plugins(entry_points(group=ENTRY_POINT_GROUP))
