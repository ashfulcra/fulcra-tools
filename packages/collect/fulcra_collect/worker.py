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
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from fulcra_common import BaseFulcraClient

from . import config, credentials, db, state
from .plugin import Plugin, RunContext
from .registry import RegistryResult, discover


def _claim_dedup_keys(keys: set[str]) -> bool:
    """Worker-side per-event write-dedup claim, backed by the daemon's
    ``state.db`` — the SAME store the attention extension route claims
    against. Opens (or reuses) this process's thread-local connection to
    the daemon home's ``state.db`` and atomically claims the key set.

    Fail-closed: if the dedup store is unavailable we return ``False``
    (skip the write) rather than risk a duplicate — mirroring the
    attention route's "never send a duplicate" preference over "never lose
    an event". The worker runs as a subprocess on the same machine as the
    daemon, so the db path resolves to the identical file via
    ``db.default_path()`` / ``config_dir()``."""
    try:
        conn = db.open()
        return db.claim_dedup_keys(conn, keys)
    except Exception:  # noqa: BLE001 — fail closed, never crash the run
        logging.getLogger("fulcra_collect.worker").exception(
            "dedup claim failed; skipping write to avoid a possible duplicate",
        )
        return False


def _unclaim_dedup_keys(keys: set[str]) -> None:
    """Release dedup keys in the daemon's ``state.db`` — the inverse of
    ``_claim_dedup_keys``. Called by the media import path after a batch POST
    FAILED, so the events are retried on the next run rather than lost. A
    failure here is logged and swallowed: the run is already unwinding on the
    POST error, and re-raising would only mask it."""
    try:
        conn = db.open()
        db.unclaim_dedup_keys(conn, keys)
    except Exception:  # noqa: BLE001 — never mask the original POST failure
        logging.getLogger("fulcra_collect.worker").exception(
            "dedup unclaim failed; %d key(s) may stay claimed and be skipped "
            "next run", len(set(keys)),
        )


class _FulcraDefinitionAdapter:
    """Thin adapter over BaseFulcraClient exposing the interface expected by
    ``fulcra_common.definitions.resolve_definition_id``:

    * ``list_definitions(name=...)`` — returns every **live** (non-deleted)
      annotation definition whose ``name`` matches exactly.
    * ``create_definition(name=..., **spec)`` — POSTs a new annotation
      definition and returns the JSON response dict (must have ``"id"``).

    ``BaseFulcraClient`` has no public ``list_definitions`` / ``create_definition``
    methods — it exposes the raw HTTP primitives. This adapter is the single
    place where the gap is bridged so the resolver stays HTTP-agnostic.
    """

    def __init__(self, base_client: "BaseFulcraClient") -> None:
        self._c = base_client  # BaseFulcraClient instance

    def list_definitions(self, *, name: str) -> list[dict]:
        """Return live (non-deleted) annotation definitions named ``name``."""
        r = self._c._client().get(
            "/user/v1alpha1/annotation",
            headers=self._c._authed_headers(),
        )
        r.raise_for_status()
        return [
            d for d in r.json()
            if d.get("name") == name and not d.get("deleted_at")
        ]

    def create_definition(self, *, name: str, **spec) -> dict:
        """POST a new annotation definition and return the response body.

        Fulcra's create endpoint rejects bodies missing either `tags` or
        `description` (HTTP 422 with `{detail: [..., loc: ["body",
        "duration", "description"], type: "missing"}]`) even though every
        plugin's SPEC dict treats both as optional. The wire-helper paths
        (duration_definition_payload, moment_definition_payload) always
        include them; the resolver's generic path didn't, so a stale-
        cache re-resolution that bottomed out in create_definition
        would 422 instead of recover. Default both when the SPEC
        doesn't supply them.
        """
        body = {"name": name, "tags": [], "description": "", **spec}
        r = self._c._client().post(
            "/user/v1alpha1/annotation",
            json=body,
            headers=self._c._authed_headers(),
        )
        r.raise_for_status()
        return r.json()

    def definition_exists(self, def_id: str) -> bool:
        """Pass-through to BaseFulcraClient.definition_exists. Exposed on
        the adapter so RunContext can validate cached def ids without
        importing HTTP machinery directly. Returns True on network
        failure (be conservative — don't churn re-resolutions on flakes)."""
        return self._c.definition_exists(def_id)

    def resolve_tag(self, name: str) -> str:
        """Look up / create a tag by name and return its id.

        Pass-through to ``BaseFulcraClient._resolve_tag`` so a plugin that
        needs resolved tag ids for a rich definition create (the Attention
        plugin's canonical attention/web tags) can resolve them through the
        SAME daemon-side client the resolver uses — no second client, no
        separate auth path. Exposed publicly on the adapter so plugins
        never reach into the base client's private helper."""
        return self._c._resolve_tag(name)


def _make_fulcra_definition_client() -> _FulcraDefinitionAdapter:
    """Zero-arg factory: return a definition adapter over a fresh
    ``BaseFulcraClient``.  The worker passes this factory into every
    ``RunContext`` so plugins that call ``ctx.resolved_definition_id`` have
    everything they need without importing HTTP machinery themselves."""
    from fulcra_common import BaseFulcraClient
    return _FulcraDefinitionAdapter(BaseFulcraClient())

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
        _fulcra_client_factory=_make_fulcra_definition_client,
        _claim_dedup_keys=_claim_dedup_keys,
        _unclaim_dedup_keys=_unclaim_dedup_keys,
        # Keychain write-back: lets a plugin persist rotated secrets (e.g.
        # Trakt's single-use OAuth refresh tokens) without importing
        # fulcra_collect.credentials itself.
        _set_credential=lambda key, value: credentials.set_secret(
            plugin.id, key, value),
    )
    missing = sorted(c.key for c in plugin.required_credentials
                     if not ctx.credentials.get(c.key))
    if missing:
        emit({"type": "result", "outcome": "error",
              "error": (f"missing required credential(s): {', '.join(missing)} — "
                        f"set with: fulcra-collect set-credential {plugin.id} <key>"),
              "watermark": getattr(ctx.state, "watermark", None),
              "definition_id": getattr(ctx.state, "definition_id", None),
              "definition_validated_at": getattr(
                  ctx.state, "definition_validated_at", None)})
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
              "watermark": getattr(ctx.state, "watermark", None),
              "definition_id": getattr(ctx.state, "definition_id", None),
              "definition_validated_at": getattr(
                  ctx.state, "definition_validated_at", None)})
        return "error"
    # The plugin advanced ctx.state.watermark and/or ctx.state.definition_id
    # in this (worker) process; the runner — the single state-writer in the
    # core process — persists them from here via the result event.
    emit({"type": "result", "outcome": "done", "error": None,
          "watermark": getattr(ctx.state, "watermark", None),
          "definition_id": getattr(ctx.state, "definition_id", None),
          "definition_validated_at": getattr(
              ctx.state, "definition_validated_at", None)})
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
