"""The worker subprocess: run one plugin, stream JSON-line events.

Invoked as `python -m fulcra_collect _worker <plugin-id>`. Writes zero or
more {"type":"progress",...} lines then exactly one
{"type":"result","outcome":"done"|"error",...} line to stdout. Runs in
its own process so a plugin's crash, hang, or dependencies are isolated.
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
from typing import TextIO

from . import config, credentials, state
from .plugin import Plugin, RunContext
from .registry import RegistryResult, discover


def run_plugin(plugin: Plugin, *, out: TextIO) -> str:
    """Run one plugin, emitting JSON-line events to `out`. Returns the
    outcome ("done" | "error")."""
    def emit(event: dict) -> None:
        out.write(json.dumps(event) + "\n")
        out.flush()

    cfg = config.load()
    ctx = RunContext(
        plugin_id=plugin.id,
        config=cfg.plugin_settings.get(plugin.id, {}),
        credentials={
            c.key: credentials.get_secret(plugin.id, c.key)
            for c in plugin.required_credentials
        },
        state=state.load(plugin.id),
        log=logging.getLogger(f"fulcra_collect.plugin.{plugin.id}"),
        _emit=emit,
    )
    try:
        plugin.run(ctx)
    except Exception as exc:  # noqa: BLE001 — report, never propagate
        # The watermark is reported even on error: a plugin may advance it
        # partway through a run, and a partial advance must still persist.
        emit({"type": "result", "outcome": "error",
              "error": f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
              "watermark": getattr(ctx.state, "watermark", None)})
        return "error"
    # The plugin advanced ctx.state.watermark in this (worker) process; the
    # runner — the single state-writer in the core — persists it from here.
    emit({"type": "result", "outcome": "done", "error": None,
          "watermark": getattr(ctx.state, "watermark", None)})
    return "done"


def main(argv: list[str], *, registry: RegistryResult | None = None) -> int:
    """CLI entry for `_worker <plugin-id>`. Returns a process exit code."""
    reg = registry if registry is not None else discover()
    plugin_id = argv[0] if argv else ""
    plugin = reg.plugins.get(plugin_id)
    if plugin is None:
        sys.stdout.write(json.dumps({
            "type": "result", "outcome": "error",
            "error": f"unknown plugin id {plugin_id!r}",
        }) + "\n")
        return 1
    outcome = run_plugin(plugin, out=sys.stdout)
    return 0 if outcome == "done" else 1
