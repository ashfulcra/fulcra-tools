"""The worker subprocess: run one plugin, stream JSON-line events.

Invoked as `python -m fulcra_collect _worker <plugin-id>`. Writes zero or
more {"type":"progress",...} lines then exactly one
{"type":"result","outcome":"done"|"error",...} line to stdout. Runs in
its own process so a plugin's crash, hang, or dependencies are isolated.
"""
from __future__ import annotations

import contextlib
import json
import logging
import re
import sys
import traceback
from typing import TextIO

from . import config, credentials, state
from .plugin import Plugin, RunContext
from .registry import RegistryResult, discover

# Query-parameter names (case-insensitive) whose values are secrets.
_SECRET_PARAM_NAMES = (
    "token", "key", "secret", "password", "passwd", "pwd", "auth",
    "access_token", "refresh_token", "api_key", "apikey", "bearer",
    "sig", "signature",
)
# `name=value` where name is secret-bearing — capture the value to redact.
_SECRET_PARAM_RE = re.compile(
    r"(?i)\b(" + "|".join(_SECRET_PARAM_NAMES) + r")=([^&\s\"']+)"
)
# `Bearer <token>` (optionally prefixed by `Authorization:`).
_BEARER_RE = re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._\-+/=]+)")
_MAX_ERROR_LEN = 4000


def _scrub_secrets(text: str) -> str:
    """Redact secrets that a plugin's exception/traceback might embed —
    a token leaked here would land in `state/<id>.json` and every
    `status` reply. Redacts secret-named URL query values and `Bearer`
    tokens, then truncates to a bounded length."""
    text = _SECRET_PARAM_RE.sub(r"\1=<redacted>", text)
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    if len(text) > _MAX_ERROR_LEN:
        text = text[:_MAX_ERROR_LEN] + "… (truncated)"
    return text


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
    missing = sorted(c.key for c in plugin.required_credentials
                     if not ctx.credentials.get(c.key))
    if missing:
        emit({"type": "result", "outcome": "error",
              "error": (f"missing required credential(s): {', '.join(missing)} — "
                        f"set with: fulcra-collect set-credential {plugin.id} <key>"),
              "watermark": getattr(ctx.state, "watermark", None)})
        return "error"
    try:
        # Redirect sys.stdout → stderr for the duration of plugin.run only.
        # A stray print() inside a plugin (or any library it imports) would
        # otherwise land in the middle of the JSON event stream — the runner
        # silently skips lines that fail json.loads, so a print() that broke
        # the `result` line would lose the result entirely and a watermark
        # advance with it. The `emit` closure above writes to the saved `out`
        # reference (the real stdout), so JSON events still get through; only
        # accidental writes from inside plugin.run are quarantined to stderr.
        with contextlib.redirect_stdout(sys.stderr):
            plugin.run(ctx)
    except Exception as exc:  # noqa: BLE001 — report, never propagate
        # The watermark is reported even on error: a plugin may advance it
        # partway through a run, and a partial advance must still persist.
        emit({"type": "result", "outcome": "error",
              "error": _scrub_secrets(
                  f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"),
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
