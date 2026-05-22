"""Plugin discovery + validation."""
from __future__ import annotations

from datetime import timedelta

from fulcra_collect.plugin import Plugin
from fulcra_collect.registry import RegistryResult, load_plugins


class FakeEntry:
    """Stands in for an importlib.metadata EntryPoint."""
    def __init__(self, name, loader):
        self.name = name
        self._loader = loader

    def load(self):
        return self._loader()


def _plugin(pid):
    return Plugin(id=pid, name=pid, kind="manual", run=lambda ctx: None)


def test_load_plugins_collects_valid_plugins():
    entries = [
        FakeEntry("a", lambda: _plugin("a")),
        FakeEntry("b", lambda: _plugin("b")),
    ]
    result = load_plugins(entries)
    assert set(result.plugins) == {"a", "b"}
    assert result.errors == {}


def test_a_plugin_factory_callable_is_also_accepted():
    # An entry point may resolve to a Plugin OR a zero-arg callable -> Plugin.
    entries = [FakeEntry("a", lambda: (lambda: _plugin("a")))]
    result = load_plugins(entries)
    assert "a" in result.plugins


def test_an_entry_that_raises_on_load_is_recorded_not_fatal():
    def boom():
        raise RuntimeError("bad import")
    entries = [
        FakeEntry("good", lambda: _plugin("good")),
        FakeEntry("bad", boom),
    ]
    result = load_plugins(entries)
    assert set(result.plugins) == {"good"}
    assert "bad" in result.errors
    assert "bad import" in result.errors["bad"]


def test_an_entry_resolving_to_a_non_plugin_is_recorded():
    entries = [FakeEntry("notaplugin", lambda: "just a string")]
    result = load_plugins(entries)
    assert result.plugins == {}
    assert "notaplugin" in result.errors


def test_duplicate_plugin_ids_keep_the_first_and_record_the_clash():
    entries = [
        FakeEntry("x", lambda: _plugin("dup")),
        FakeEntry("y", lambda: _plugin("dup")),
    ]
    result = load_plugins(entries)
    assert set(result.plugins) == {"dup"}
    assert any("dup" in e for e in result.errors.values())
