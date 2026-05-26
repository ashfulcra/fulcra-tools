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
    # attention-relay is now `manual` — the daemon hosts the
    # extension endpoint directly, so the plugin no longer runs a
    # supervised HTTP server. It exists as a manual sanity-check.
    assert result.plugins["attention-relay"].kind == "manual"
    assert result.plugins["lastfm"].kind == "scheduled"
    # dayone is now scheduled (every 6 h) so live-app mode keeps a fresh
    # watermark without the user clicking Run Now.
    assert result.plugins["dayone"].kind == "scheduled"


def test_daemon_status_lists_the_discovered_plugins(collect_home: Path):
    d = Daemon(registry=discover(), config=Config(enabled={"lastfm"}))
    reply = d.handle_request({"cmd": "status"})
    ids = {p["id"] for p in reply["plugins"]}
    assert {"attention-relay", "lastfm", "dayone"} <= ids
