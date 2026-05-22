"""End-to-end: the hub discovers the three reference plugins that the
sibling packages register, and the daemon reports them."""
from __future__ import annotations

from pathlib import Path

from fulcra_collect.config import Config
from fulcra_collect.daemon import Daemon
from fulcra_collect.registry import discover


def test_registry_discovers_the_three_reference_plugins():
    result = discover()
    # The three plan-1a adapters register real entry points in their
    # packages' pyproject.toml; with the workspace synced they are found.
    assert "attention-relay" in result.plugins
    assert "lastfm" in result.plugins
    assert "dayone" in result.plugins
    assert result.plugins["attention-relay"].kind == "service"
    assert result.plugins["lastfm"].kind == "scheduled"
    assert result.plugins["dayone"].kind == "manual"


def test_daemon_status_lists_the_discovered_plugins(collect_home: Path):
    d = Daemon(registry=discover(), config=Config(enabled={"lastfm"}))
    reply = d.handle_request({"cmd": "status"})
    ids = {p["id"] for p in reply["plugins"]}
    assert {"attention-relay", "lastfm", "dayone"} <= ids
