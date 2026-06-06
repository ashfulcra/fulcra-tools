"""Local agent/host configuration commands for fulcra-coord.

The per-host setup surface: declare/show/clear this host's agent identity
(``identity``, per-cwd) and the operator's human handle (``human``), toggle the
timeline-annotations writer (``annotations``), and the hidden ``session-task`` hook
helper that maps a session id to its current task. These mutate local config /
print state — they never touch the bus.

Extracted from cli.py behind stable re-exports; depends only on lower layers
(identity / session_link / annotations + the output leaf utils) and never imports
cli, so the split has no cycle.
"""

from __future__ import annotations

from typing import Any, Optional

from . import identity, session_link
from . import annotations as lifecycle_annotations
from .output import info as _info, print_json as _print_json


def cmd_session_task(args: Any, backend: Optional[list[str]] = None) -> int:
    """Print the task id for a session id (used by hooks). Hidden command."""
    ptr = session_link.read_pointer(args.session_id)
    if not ptr or not ptr.get("task_id"):
        return 1
    print(ptr["task_id"])
    return 0


def cmd_identity(args: Any, backend: Optional[list[str]] = None) -> int:
    """Show, set, or clear this host's declared agent identity (the handshake).

    - `identity`            → show the resolved id and its source (explicit/env/
                              config/derived) so an operator can see who they are
                              and *why*.
    - `identity set <id>`   → persist <id> for the CURRENT cwd; an existing
                              long-running session declares its stable id once and
                              every subsequent bus op in that repo reuses it.
                              Per-cwd, so a sibling session in another repo is
                              never clobbered.
    - `identity clear`      → remove the persisted id for the current cwd (fall
                              back to env/derived; the legacy global is NOT used).
    - `identity migrate`    → copy the legacy global identity (if any) into this
                              cwd's per-cwd entry, so a pre-split setup keeps its
                              declared id without the silent global fallback (I-1).
    """
    action = getattr(args, "identity_action", None)
    out_format = getattr(args, "format", "table")

    if action == "set":
        agent_id = args.agent_id
        identity.set_identity(agent_id)
        if out_format == "json":
            _print_json({"agent": agent_id, "source": "config", "action": "set"})
        else:
            _info(f"Identity set: {agent_id}")
            _info(f"  Persisted to: {identity.identity_path()}")
        return 0

    if action == "migrate":
        # I-1 migration helper: the legacy global is no longer resolved silently,
        # so an operator who relied on it copies it into this repo's per-cwd entry
        # once. No-op (with a note) when there's nothing to migrate.
        legacy = identity.read_legacy_identity()
        if legacy:
            identity.set_identity(legacy)
        agent, source = identity.resolve_agent_source()
        if out_format == "json":
            _print_json({"agent": agent, "source": source, "action": "migrate",
                         "migrated": bool(legacy)})
        else:
            if legacy:
                _info(f"Migrated legacy global identity '{legacy}' into this repo.")
                _info(f"  Persisted to: {identity.identity_path()}")
            else:
                _info("No legacy global identity to migrate.")
            _info(f"Now resolving as: {agent}  (source: {source})")
        return 0

    if action == "clear":
        removed = identity.clear_identity()
        agent, source = identity.resolve_agent_source()
        if out_format == "json":
            _print_json({"agent": agent, "source": source, "action": "clear",
                         "removed": removed})
        else:
            if removed:
                _info("Identity cleared.")
            else:
                _info("No persisted identity to clear.")
            _info(f"Now resolving as: {agent}  (source: {source})")
        return 0

    # show (default)
    agent, source = identity.resolve_agent_source()
    # I-1: surface a one-line hint when a legacy global exists AND this cwd has no
    # per-cwd entry, so an operator who set the old global learns it no longer
    # resolves automatically and how to re-declare it for this repo.
    legacy = identity.read_legacy_identity()
    show_legacy_hint = bool(legacy) and identity.read_identity() is None
    if out_format == "json":
        _print_json({"agent": agent, "source": source,
                     "identity_file": str(identity.identity_path()),
                     "legacy_global": legacy})
    else:
        _info(f"Agent:  {agent}")
        _info(f"Source: {source}")
        if show_legacy_hint:
            _info(f"  Note: legacy global identity '{legacy}' found; it is no longer "
                  f"used automatically —")
            _info(f"        run `fulcra-coord identity set <id>` to set this repo's "
                  f"identity (or `identity migrate`).")
        elif source != "config":
            _info(f"  (declare a stable id with: fulcra-coord identity set <agent-id>)")
    return 0


def cmd_human(args: Any, backend: Optional[list[str]] = None) -> int:
    """Show, set, or clear the human operator's handle (situational awareness).

    The human is an addressable identity on the bus — the one tasks are
    "blocked on ME" against. Defaults to the neutral ``human`` so the public repo
    carries no name; this operator runs ``fulcra-coord human set ash``.

    - `human`              → show the resolved handle + its source (env/config/
                             default).
    - `human set <handle>` → persist <handle> globally for this machine.
    - `human clear`        → remove the persisted handle (fall back to env/default).
    """
    action = getattr(args, "human_action", None)
    out_format = getattr(args, "format", "table")

    if action == "set":
        handle = args.handle
        identity.set_human(handle)
        if out_format == "json":
            _print_json({"human": handle, "source": "config", "action": "set"})
        else:
            _info(f"Human handle set: {handle}")
            _info(f"  Persisted to: {identity.human_path()}")
        return 0

    if action == "clear":
        removed = identity.clear_human()
        handle, source = identity.resolve_human_source()
        if out_format == "json":
            _print_json({"human": handle, "source": source, "action": "clear",
                         "removed": removed})
        else:
            _info("Human handle cleared." if removed
                  else "No persisted human handle to clear.")
            _info(f"Now resolving as: {handle}  (source: {source})")
        return 0

    # show (default)
    handle, source = identity.resolve_human_source()
    if out_format == "json":
        _print_json({"human": handle, "source": source,
                     "human_file": str(identity.human_path())})
    else:
        _info(f"Human:  {handle}")
        _info(f"Source: {source}")
        if source == "default":
            _info("  (personalize with: fulcra-coord human set <handle>)")
    return 0


def cmd_annotations(args: Any, backend: Optional[list[str]] = None) -> int:
    """Enable, disable, or inspect the Agent-Tasks timeline annotations writer.

    Annotations drop a durable breadcrumb on the operator's Fulcra timeline every
    time an agent creates/picks-up/updates/completes a task. Historically they
    only fired if ``FULCRA_COORD_ANNOTATIONS=http`` was exported in each shell, so
    the timeline rarely filled. This command PERSISTS the enablement once
    (machine-wide) so every agent emits without a per-session export.

    - ``annotations on``     → persist ``http`` to the config file.
    - ``annotations off``    → remove the config file (resolves to off unless the
                               env var is set — env always wins).
    - ``annotations`` / ``status`` → report the resolved mode, its SOURCE
                               (env/config/default), and whether a bearer token
                               resolves (the token VALUE is never printed).
    """
    action = getattr(args, "annotations_action", None)
    out_format = getattr(args, "format", "table")

    if action == "on":
        path = lifecycle_annotations.set_persisted_mode("http")
        if out_format == "json":
            _print_json({"mode": "http", "source": "config", "action": "on"})
        else:
            _info("Annotations enabled (mode: http).")
            _info(f"  Persisted to: {path}")
            _info("  Every agent on this machine will now emit Agent-Tasks "
                  "timeline annotations.")
        return 0

    if action == "off":
        removed = lifecycle_annotations.clear_persisted_mode()
        mode, source = lifecycle_annotations.resolve_mode_source()
        if out_format == "json":
            _print_json({"mode": mode, "source": source, "action": "off",
                         "removed": removed})
        else:
            _info("Annotations disabled." if removed
                  else "No persisted annotation mode to clear.")
            if source == "env":
                _info(f"  Note: FULCRA_COORD_ANNOTATIONS is set in this shell — "
                      f"still resolving as {mode} (env overrides config).")
            else:
                _info(f"  Now resolving as: {mode}  (source: {source})")
        return 0

    # status (default / bare)
    mode, source = lifecycle_annotations.resolve_mode_source()
    # Reuse the doctor's token check so `status` and `[Annotations]` agree on
    # whether a write could actually authenticate. NEVER print the token value.
    token_ok = bool(lifecycle_annotations._resolve_token())
    if out_format == "json":
        _print_json({"mode": mode, "source": source, "token_ok": token_ok,
                     "config_file": str(lifecycle_annotations._annotations_config_path())})
    else:
        _info(f"Annotations: {mode}")
        _info(f"Source:      {source}")
        _info(f"Token:       {'OK' if token_ok else 'not available'}")
        if mode == "off":
            _info("  (enable for every agent with: fulcra-coord annotations on)")
    return 0
