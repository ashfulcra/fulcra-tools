"""CLI command implementations for fulcra-coord.

Each command accepts parsed argparse namespace and an optional backend=
override for testing without live Fulcra access.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import cache, remote, schema, views, log as ops_log, session_link, claude_code, openclaw, heartbeat, codex, listener, identity
from . import env_int
# Leaf-utility modules extracted from this file. Re-exported under the historical
# underscore-prefixed names so every internal call site AND the test patch targets
# (fulcra_coord.cli._info / ._now_iso / ...) keep resolving unchanged — output.py /
# timeutil.py do not import cli, so there is no import cycle.
from .output import print_json as _print_json, err as _err, warn as _warn, info as _info
from .timeutil import iso_z as _iso_z, now_iso as _now_iso
from .textfmt import age_str as _age_str, until_str as _until_str, due_str as _due_str
# Retention / archival subsystem extracted from this file. Re-exported under the
# historical underscore-prefixed names so every remaining caller here
# (cmd_reconcile -> _run_retention; cmd_search / cmd_restore -> the cold-index
# readers) AND the test patch targets (fulcra_coord.cli._archive_task / ...)
# keep resolving. retention.py depends only on lower layers and never imports
# cli, so there is no import cycle.
from .retention import (
    _archive_month, _archive_index_shard, _archive_task, _read_index_shard,
    _list_index_shards, _retention_max_per_run, _claim_retention_marker,
    _prune_markers, _prune_dead_presence, _prune_dead_health, _run_retention,
    _RETENTION_DEADLINE_HEADROOM_SECONDS,
)
# Shared remote-task load/cache layer extracted from this file. Re-exported under
# the historical underscore-prefixed names so every cli-resident caller
# (cmd_status / cmd_reconcile / cmd_digest / _try_merge / _write_task_and_views /
# ...) AND the unmigrated test patch targets (fulcra_coord.cli._load_all_tasks /
# ...) keep resolving. io.py depends only on lower layers and never imports cli,
# so there is no import cycle.
from .io import (
    _cache_remote_task, _load_all_tasks, _load_task_summaries,
    _load_summaries_for_rebuild, _load_task, _updated_at_key,
)
# Presence subsystem extracted from this file. Re-exported under the historical
# names so the command dispatch (cmd_connect/cmd_workstream/cmd_presence),
# cmd_reconcile's _reconcile_presence call, cmd_start's _maybe_warn_legacy_identity
# call, and the test patch targets keep resolving. presence.py never imports cli.
from .presence import (
    _maybe_warn_legacy_identity, _derive_workstreams_from_open_tasks,
    _upsert_presence_aggregate, _write_presence, _load_own_presence, cmd_connect,
    _split_workstreams, cmd_workstream, cmd_presence, _reconcile_presence,
)
# Read-only situational-awareness commands extracted from this file. Re-exported so
# the command dispatch (entry.py) and the test imports of these commands keep
# resolving. query.py never imports cli.
from .query import cmd_status, cmd_agents, cmd_needs_me, cmd_resume
# Task write pipeline extracted from this file. Re-exported under the historical
# names so every write command (cmd_start/update/block/pause/done/abandon/tell/
# broadcast/assign/inbox/request-review) that calls _write_task_and_views, plus the
# test patch targets, keep resolving. writepipe.py never imports cli.
from .writepipe import (
    _stamp_session_pointer, _write_task_and_views, _emit_lifecycle, _lifecycle_for,
    _view_name_to_remote, _try_merge, _carry_fields, _union_events_and_acked,
    _repair_merged_tags,
)
# Liveness-aware reviewer routing extracted from this file. Re-exported so
# cmd_reconcile's _sweep_review_routes call, the request-review dispatch, and the
# test patch targets keep resolving. routing_ops.py never imports cli.
from .routing_ops import (
    _canonical_reviewer, _review_pool, _append_route_event_and_assignee,
    _force_block_for_human, _escalate_review_to_human, cmd_request_review,
    _reroute_minutes, _reroute_max, _accepted_stall_hours,
    _review_accepted_by_assignee, _classify_review, _sweep_review_routes,
)
# Operator situational-awareness output (digest push + health pull) extracted from
# this file. Re-exported so the digest/health/install-digest dispatch, cmd_doctor's
# _assess_fleet fold, and the test patch targets keep resolving. digest.py never
# imports cli.
from .digest import (
    _load_health_records, _freshest_digest_emit, _assess_fleet, cmd_health,
    _digest_lines, _render_digest, _digest_window_since, _digest_marker_path,
    _claim_digest_marker, cmd_digest, cmd_install_digest,
)
# Task lifecycle + directive commands extracted from this file. Re-exported so the
# command dispatch (entry.py) and the test imports keep resolving. lifecycle.py
# never imports cli.
from .lifecycle import (
    cmd_tell, cmd_broadcast, cmd_assign, cmd_start, cmd_update, cmd_block,
    cmd_pause, cmd_done, cmd_abandon,
)
# Imported under an alias because ``from __future__ import annotations`` above
# binds the bare name ``annotations`` to the __future__ feature, which would
# otherwise shadow this module on the cli namespace.
from . import annotations as lifecycle_annotations


# ---------------------------------------------------------------------------
# Session pointer
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

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


def _report_resolved_cli(plan: dict[str, Any]) -> None:
    """Print the CLI invocation baked into the just-installed hooks, and warn if
    it had to fall back to `python -m` (Gap 1) — that works, but signals the
    `fulcra-coord` entry point is not on PATH, which the operator may want to fix
    (e.g. with `fulcra-coord install-shim`)."""
    from . import cli_invocation
    resolved = plan.get("resolved_cli")
    if resolved:
        _info(f"  Hooks will call: {resolved}")
    if cli_invocation.used_python_m_fallback():
        _warn("fulcra-coord is not on PATH; hooks use the `python -m fulcra_coord` "
              "fallback. To put it on PATH, run: fulcra-coord install-shim")


def cmd_install_claude_code(args: Any, backend: Optional[list[str]] = None) -> int:
    """Install/uninstall Claude Code lifecycle hooks for coordination."""
    scope = "project" if getattr(args, "scope", "global") == "project" else "global"
    plan = claude_code.install_claude_code(
        scope=scope, uninstall=args.uninstall, dry_run=args.dry_run)
    if args.dry_run:
        _info("[dry-run] Would write to: " + plan["settings"])
        _info("[dry-run] Hook scripts: " + plan["hooks_dir"])
        for e in plan.get("events", []):
            _info(f"  + {e}")
        import json as _json
        if plan.get("would_write") is not None:
            _info("[dry-run] Resulting settings.json:")
            _info(_json.dumps(plan["would_write"], indent=2))
        return 0
    if args.uninstall:
        _info(f"Removed fulcra-coord hooks from {plan['settings']}")
        return 0
    _info(f"Installed Claude Code hooks ({scope}) -> {plan['settings']}")
    for e in plan["events"]:
        _info(f"  + {e}")
    _report_resolved_cli(plan)
    _info("New Claude Code sessions will now surface in-flight work and checkpoint automatically.")
    _info("Verify auth/connectivity with: fulcra-coord doctor")
    return 0


def cmd_install_openclaw(args: Any, backend: Optional[list[str]] = None) -> int:
    """Install/uninstall OpenClaw Track A coordination artifacts."""
    hooks_root = getattr(args, "hooks_root", None)
    plan = openclaw.install_openclaw(
        hooks_root=hooks_root, uninstall=args.uninstall, dry_run=args.dry_run)
    if args.dry_run:
        _info("[dry-run] OpenClaw hooks root: " + plan["hooks_root"])
        for w in plan.get("writes", []):
            _info(f"  + would write {w}")
        for r in plan.get("removes", []):
            _info(f"  - would remove {r}")
        return 0
    if args.uninstall:
        _info(f"Removed fulcra-coord OpenClaw artifacts from {plan['hooks_root']}")
        return 0
    _info(f"Installed OpenClaw Track A artifacts -> {plan['hooks_root']}")
    for d in plan.get("hook_dirs", []):
        _info(f"  + hook {d}")
    for f in plan.get("prompt_files", []):
        _info(f"  + prompt {f}")
    _report_resolved_cli(plan)
    _info("New OpenClaw sessions will surface in-flight work at boot and park "
          "active tasks on gateway shutdown.")
    _info("The handler.ts templates are written to the real OpenClaw "
          "automation-hook API (verified against the SDK source); they still "
          "can't be run in this repo.")

    # Track B add-on: materialize the Plugin-SDK plugin if requested. This is a
    # source drop only — building + registering needs npm/tsc, which the CLI
    # can't do, so we print the manual finish-the-install steps.
    if getattr(args, "with_plugin", False):
        from . import openclaw_plugin
        pplan = openclaw_plugin.install_openclaw_plugin(
            plugin_dir=getattr(args, "plugin_dir", None),
            uninstall=args.uninstall, dry_run=args.dry_run)
        if args.dry_run:
            _info("[dry-run] Track B plugin dir: " + pplan["plugin_dir"])
            for w in pplan.get("writes", []):
                _info(f"  + would write {w}")
            for r in pplan.get("removes", []):
                _info(f"  - would remove {r}")
        elif args.uninstall:
            _info(f"Removed Track B plugin sources from {pplan['plugin_dir']}")
        else:
            _info(f"Materialized Track B plugin sources -> {pplan['plugin_dir']}")
            _info("Build and register the plugin (needs npm; the CLI can't):")
            for step in pplan["build_steps"]:
                _info(f"    {step}")

    _info("Verify auth/connectivity with: fulcra-coord doctor")
    return 0


def cmd_install_codex(args: Any, backend: Optional[list[str]] = None) -> int:
    """Install/uninstall Codex lifecycle hooks for coordination (Gap 4)."""
    plan = codex.install_codex(
        uninstall=args.uninstall, dry_run=args.dry_run,
        target_dir=getattr(args, "target_dir", None))
    if args.dry_run:
        _info("[dry-run] Would write to: " + plan["hooks_file"])
        _info("[dry-run] Hook scripts: " + plan["hooks_dir"])
        for e in plan.get("events", []):
            _info(f"  + {e}")
        if plan.get("would_write") is not None:
            import json as _json
            _info("[dry-run] Resulting hooks.json:")
            _info(_json.dumps(plan["would_write"], indent=2))
        return 0
    if args.uninstall:
        _info(f"Removed fulcra-coord hooks from {plan['hooks_file']}")
        return 0
    _info(f"Installed Codex hooks -> {plan['hooks_file']}")
    for e in plan["events"]:
        _info(f"  + {e}")
    _report_resolved_cli(plan)
    _info("Codex SessionStart surfaces in-flight work; PreCompact checkpoints "
          "before context loss.")
    _info("No Stop hook by design — Codex Stop fires every turn; end-parking is "
          "delegated to the heartbeat. Install it with: fulcra-coord install-heartbeat")
    _info("Verify auth/connectivity with: fulcra-coord doctor")
    return 0


def cmd_install_heartbeat(args: Any, backend: Optional[list[str]] = None) -> int:
    """Install/uninstall a scheduled `fulcra-coord reconcile` heartbeat (Gap 2).

    The heartbeat is the safety net for crashed agents and end-hook-less surfaces
    (ChatGPT, and Codex whose Stop fires every turn): it re-runs reconcile on a
    cadence to sweep stale `active` tasks and rebuild needs-attention.json.
    """
    plan = heartbeat.install_heartbeat(
        interval_min=getattr(args, "interval_min", heartbeat.INTERVAL_MIN_DEFAULT),
        uninstall=args.uninstall,
        dry_run=args.dry_run,
        target_dir=getattr(args, "target_dir", None),
        logs_dir=getattr(args, "logs_dir", None),
    )
    if args.dry_run:
        _info(f"[dry-run] Heartbeat mechanism: {plan['mechanism']}")
        _info(f"[dry-run] Scheduled command: {plan['cli_command']} reconcile "
              f"(every {plan['interval_min']} min)")
        for w in plan.get("writes", []):
            _info(f"  + would write {w}")
        for r in plan.get("removes", []):
            _info(f"  - would remove {r}")
        return 0
    if args.uninstall:
        _info(f"Removed fulcra-coord heartbeat ({plan['mechanism']}).")
        return 0
    _info(f"Installed fulcra-coord heartbeat ({plan['mechanism']}) — "
          f"reconcile every {plan['interval_min']} min.")
    for w in plan.get("writes", []):
        _info(f"  + {w}")
    if plan["mechanism"] == "launchd":
        _info("Load it now (or it loads at next login): "
              f"launchctl load -w {plan['writes'][0]}")
    else:
        _info("Apply it now: crontab " + plan["writes"][0])
    _info(f"Scheduled command: {plan['cli_command']} reconcile")
    return 0


def cmd_install_listener(args: Any, backend: Optional[list[str]] = None) -> int:
    """Install/uninstall a scheduled `fulcra-coord notify-inbox` listener (Part 3).

    The durable, per-agent inbox listener: it polls for directives addressed to
    this agent on a cadence (default 10 min) and surfaces + notifies — so an
    idle agent notices directed work without a session open. launchd on macOS,
    crontab elsewhere. The Claude Code "scheduled remote agent" is the preferred
    mechanism (see adapters/claude-code/LISTENER.md); this is the harness-free
    fallback.
    """
    agent = getattr(args, "agent", None) or _derive_agent()
    plan = listener.install_listener(
        agent=agent,
        interval_min=getattr(args, "interval_min", listener.INTERVAL_MIN_DEFAULT),
        uninstall=args.uninstall,
        dry_run=args.dry_run,
        target_dir=getattr(args, "target_dir", None),
        logs_dir=getattr(args, "logs_dir", None),
    )
    if args.dry_run:
        _info(f"[dry-run] Listener mechanism: {plan['mechanism']}")
        _info(f"[dry-run] Scheduled command: {plan['cli_command']} "
              f"notify-inbox --agent {agent} (every {plan['interval_min']} min)")
        if plan.get("supersedes_legacy"):
            _info("[dry-run] Would supersede the legacy machine-global listener "
                  f"job watching {agent} (it migrates to a per-agent job).")
        for w in plan.get("writes", []):
            _info(f"  + would write {w}")
        for r in plan.get("removes", []):
            _info(f"  - would remove {r}")
        return 0
    if args.uninstall:
        _info(f"Removed fulcra-coord listener ({plan['mechanism']}).")
        return 0
    _info(f"Installed fulcra-coord listener ({plan['mechanism']}) for {agent} — "
          f"notify-inbox every {plan['interval_min']} min.")
    for w in plan.get("writes", []):
        _info(f"  + {w}")
    if plan["mechanism"] == "launchd":
        _info("Load it now (or it loads at next login): "
              f"launchctl load -w {plan['writes'][0]}")
    else:
        _info("Apply it now: crontab " + plan["writes"][0])
    _info(f"Scheduled command: {plan['cli_command']} notify-inbox --agent {agent}")
    return 0


def _derive_agent() -> str:
    """Resolve the caller's agent id when not given explicitly.

    Thin wrapper over identity.resolve_agent() — the single "who am I" entry
    point. Kept as a local alias so the (many) callsites read naturally; the
    resolution order (explicit > env > persisted identity > derived) now lives in
    fulcra_coord.identity so the CLI, listener, and `identity` command agree.
    """
    return identity.resolve_agent()


def cmd_inbox(args: Any, backend: Optional[list[str]] = None) -> int:
    """List (or ack) open directives addressed to the calling agent.

    Read path recomputes authoritatively from the full task set (see
    _load_inbox) — mirroring cmd_agents — rather than trusting a materialized
    inbox view, which can go stale once an inbox empties. `--ack <id>` records an
    inbox_ack event so the listener stops re-notifying, without claiming the task.
    """
    me = getattr(args, "agent", None) or _derive_agent()
    out_format = getattr(args, "format", "table")
    ack_id = getattr(args, "ack", None)
    show_all = bool(getattr(args, "all", False))

    if ack_id:
        task = _load_task(ack_id, backend=backend)
        if task is None:
            _err(f"Task not found: {ack_id}")
            return 1
        task = schema.apply_event(task, "inbox_ack", by=me,
                                  summary=f"Inbox acknowledged by {me}.")
        cache.write_cached_task(task)
        ok = False
        try:
            ok = _write_task_and_views(task, backend=backend, command="inbox-ack")
        except schema.ConflictError as e:
            _err(str(e))
            return 2
        except schema.NeedsReconcile as e:
            _warn(str(e))
            ok = True
        if not ok:
            _warn(f"Ack cached locally but remote upload failed: {ack_id}.")
            return 1
        _info(f"Acknowledged: {ack_id}")
        return 0

    # Load the task set ONCE, then derive both the shown items and the aged-out
    # count from it — no second backend round-trip. With --all the age-out filter
    # is bypassed and aged-out broadcasts are included; otherwise stale
    # informational broadcasts are hidden and only counted for the note below.
    all_tasks = _load_task_summaries(backend=backend)
    # BUG 14: pin a single `now` for the whole command. inbox_for and
    # aged_out_inbox_count each resolve _now() independently (3+ reads per
    # cmd_inbox), so at the age-out boundary the same broadcast could be SHOWN by
    # one read and COUNTED HIDDEN by a later one. One timestamp keeps them
    # consistent — an id is either shown or counted hidden, never both.
    now = views._now()
    items = views.inbox_for(me, all_tasks, now=now, include_aged=show_all)
    hidden = 0 if show_all else views.aged_out_inbox_count(me, all_tasks, now=now)

    if out_format == "json":
        _print_json({"agent": me, "count": len(items), "hidden_aged": hidden,
                     "inbox": items})
        return 0

    if not items:
        if hidden:
            _info(f"Inbox empty for {me} "
                  f"({hidden} older broadcast{'s' if hidden != 1 else ''} "
                  f"hidden — --all to show).")
        else:
            _info(f"Inbox empty for {me}.")
        return 0

    print(f"\n{'='*60}")
    print(f"  Inbox — directives for {me}")
    print(f"{'='*60}")
    for s in items:
        frm = s.get("owner_agent", "?")
        print(f"  [{s.get('priority','??')}] {s.get('id','')}  {s.get('title','')[:50]}")
        print(f"        from: {frm}")
        if s.get("next_action"):
            print(f"        next: {s['next_action'][:70]}")
    if hidden:
        print(f"\n  ({hidden} older broadcast{'s' if hidden != 1 else ''} "
              f"hidden — --all to show)")
    print()
    return 0


def _load_inbox(me: str, backend: Optional[list[str]] = None,
                include_aged: bool = False) -> list[dict[str, Any]]:
    """Open directives for `me`, recomputed authoritatively from the full task set.

    Mirrors cmd_agents: inbox_for over the live tasks is the single source of
    truth. We deliberately do NOT prefer a materialized inbox/<slug> view here.

    Membership uses prefix-aware matching (views.inbox_for / agent_matches): a
    directive addressed to a short id like `claude-code` reaches the full-id
    agent `claude-code:<host>:<repo>` it prefixes. This is the correctness fix
    for the original bug — strict slug equality silently dropped short-id
    directives.

    Why recompute (not read a materialized view): build_all_views only emits an
    inbox/<slug> view for assignees who still have at least one open directive.
    When an inbox empties — the last directive is acked or claimed — the stale
    inbox/<slug>.json (local cache AND remote) is never overwritten, so preferring
    it returned a phantom directive forever (`inbox` re-listed handled work, the
    listener re-notified, SessionStart re-injected). Recomputing from the task set
    always reflects the current truth, at the cost of one task-set load — the same
    cost cmd_agents pays.
    """
    # Summaries fast-path: inbox_for reads assignee/status/owner_agent and the
    # ack set, which the summary now carries (acked_by) — no event log / body
    # fetch needed. Falls back to a full load on an older bus.
    all_tasks = _load_task_summaries(backend=backend)
    # include_aged bypasses the broadcast age-out filter (the `inbox --all` path);
    # the default read hides stale informational broadcasts so they stop
    # cluttering the inbox / SessionStart, without touching any task.
    return views.inbox_for(me, all_tasks, include_aged=include_aged)


# ---------------------------------------------------------------------------
# Listener inbox surface + per-host health record assembly
# ---------------------------------------------------------------------------

def _inbox_surface_path(agent: str):
    """Where the listener drops pending directives for the next SessionStart to
    read. Root-scoped via cache_root() and suffixed by the agent slug so two
    agents on one machine don't clobber each other's surface file."""
    return cache.cache_root() / f"inbox-pending-{listener.agent_slug(agent)}.json"


def _build_health_record(*, now, duration_s, tasks_loaded, views_refreshed,
                         repair_backlog, retention_last_run, listener_last_fire,
                         bus_task_count) -> dict:
    """Assemble the per-host health record from a SUCCESSFUL reconcile's locals
    plus cheap reads. Pure given its args; identity/version read here so the
    caller stays a one-liner. host = short hostname (matches identity.derived_agent);
    agent = resolve_agent(). reconcile_at is the success instant."""
    import socket
    from . import __version__
    try:
        host = socket.gethostname().split(".")[0]
    except Exception:
        host = "host"
    return {
        "schema": "fulcra.coordination.health.v1",
        "host": host,
        "agent": identity.resolve_agent(),
        "version": __version__,
        "reconcile_at": _iso_z(now),
        "duration_s": duration_s,
        "tasks_loaded": tasks_loaded,
        "views_refreshed": views_refreshed,
        "repair_backlog": repair_backlog,
        "retention_last_run": retention_last_run,
        "listener_last_fire": listener_last_fire,
        "bus_task_count": bus_task_count,
    }


def _needs_me_seen_path(human: str):
    """Seen-set surface for blocked-on-you notifications, keyed by the HUMAN
    handle (not the polling agent): the "has the operator already been alerted
    about this item" marker. Like the inbox-pending surface but a set of task
    ids, so the listener notifies ONCE per new needs-me item and never re-fires
    for one it already announced. Slugged via the same agent_slug so a handle
    with odd characters maps to a safe filename."""
    return cache.cache_root() / f"needs-me-seen-{listener.agent_slug(human)}.json"


def _notify_new_needs_me(backend: Optional[list[str]] = None) -> None:
    """Fire a desktop notification for each NEW item blocked on the human.

    Polled alongside the inbox by the listener (Part 5). Resolves the human via
    resolve_human(), loads what's blocked on them, and for every item not yet in
    the per-human seen-set emits "⛔ <agent> needs you: <ask>" once. Idempotent:
    the seen-set (a task-id list persisted next to the inbox surface) means a
    repeat tick over the same item does not re-notify, while a genuinely new
    blocked-on-you item alerts. Best-effort — wrapped by the caller's try/except
    so it can never crash a polling tick. No-op when nothing is blocked."""
    human = identity.resolve_human()
    # needs_human reads status/assignee/tags — all on a summary; no body fetch.
    items = views.needs_human(_load_task_summaries(backend=backend), human)
    seen_path = _needs_me_seen_path(human)
    seen: set[str] = set()
    if seen_path.exists():
        try:
            seen = set(json.loads(seen_path.read_text()))
        except (json.JSONDecodeError, OSError, TypeError):
            seen = set()

    current_ids = {i["id"] for i in items}
    for it in items:
        if it["id"] in seen:
            continue
        ask = (it.get("blocked_on") or it.get("next_action") or "").strip()
        frm = it.get("owner_agent", "?")
        listener.emit_message(f"⛔ {frm} needs you: {ask}" if ask
                              else f"⛔ {frm} needs you: {it.get('title','')}")

    # Persist the seen-set as the CURRENT item ids: newly-notified items are now
    # seen, and items that have since cleared (resolved) drop out so that if the
    # SAME task is blocked-on-you again later it re-notifies (a fresh ask).
    cache.cache_root().mkdir(parents=True, exist_ok=True)
    seen_path.write_text(json.dumps(sorted(current_ids)))


def cmd_notify_inbox(args: Any, backend: Optional[list[str]] = None) -> int:
    """Poll the inbox for an agent; on non-empty, surface + notify (Part 3).

    The single call the scheduled listener (launchd/cron/heartbeat/scheduled
    remote agent) runs each tick. Notify-only: it writes the open directives to
    a local surface file the next SessionStart injects AND emits a best-effort
    desktop notification. No-op (no notification; surface file cleared to an
    empty inbox so a stale one doesn't linger) when the inbox is empty.
    Fail-safe — never raises out; a polling tick must not crash the scheduler.
    """
    me = getattr(args, "agent", None) or _derive_agent()
    try:
        items = _load_inbox(me, backend=backend)
        surface = _inbox_surface_path(me)
        cache.cache_root().mkdir(parents=True, exist_ok=True)
        payload = {"agent": me, "count": len(items), "inbox": items}
        surface.write_text(json.dumps(payload, indent=2))
        if items:
            listener.emit_notification(me, len(items))
        # ALSO notice anything newly blocked on the human (Part 5). Independent
        # of the agent's own inbox: a tick with an empty inbox can still alert on
        # a new blocked-on-you item. Best-effort within the same fail-safe guard.
        _notify_new_needs_me(backend=backend)
    except Exception as e:
        # A polling tick that fails must not bring down the scheduler; report to
        # stderr and exit clean (fail-safe contract).
        _warn(f"notify-inbox failed (non-fatal): {e}")
        return 0
    return 0


#: Max items rendered per digest block before collapsing the tail into "+N more".
#: Keeps the timeline note bounded (a 284-event-in-two-days bus could otherwise
#: produce a wall of text) while always showing the most-salient head of each list.
# Headroom for the review-route sweep's deadline gate (B1). The sweep runs
# BEFORE retention in cmd_reconcile and does per-directive network fetches +
# potential full view-rebuild writes, so it must leave enough of the reconcile
# budget for retention (which gates on the same deadline) to still make
# progress. Mirrors _RETENTION_DEADLINE_HEADROOM_SECONDS' role.
def _detect_stale_claims(all_tasks: list[dict[str, Any]],
                         now: datetime) -> list[str]:
    """Collect the ids of active tasks holding an EXPIRED claim.

    Tolerant of imperfect bus data by construction (A1): a body missing ``id``
    contributes nothing instead of raising ``KeyError``, and an unparseable
    ``claim_expires_at`` is skipped instead of raising ``ValueError``. This runs
    early in cmd_reconcile, BEFORE build_all_views/upload — an uncaught raise
    here would abort the whole reconcile and fail every heartbeat tick (the
    heartbeat-outage class of bug). So it must never raise on a real-world body
    that merely lacks a field."""
    stale_claims: list[str] = []
    for t in all_tasks:
        tid = t.get("id")
        if not tid:
            continue  # an id-less body can't be named as a stale claim
        claim = t.get("claim", {})
        expires = claim.get("claim_expires_at")
        if not expires:
            continue
        exp_dt = views._parse_dt(expires)
        if exp_dt is None:
            continue  # unparseable expiry — skip, never raise
        if now > exp_dt and t.get("status") == "active":
            stale_claims.append(tid)
    return stale_claims


def cmd_reconcile(args: Any, backend: Optional[list[str]] = None) -> int:
    """Repair views and resolve pending operation markers."""
    import time
    _info("Reconciling coordination views...")
    t0 = time.monotonic()
    timeout = env_int("FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS", 90)
    deadline = t0 + timeout

    markers = cache.list_op_markers()
    needs_repair = [m for m in markers if m.get("needs_reconcile")]
    if needs_repair:
        _info(f"  {len(needs_repair)} operation(s) need view repair.")

    try:
        all_tasks = _load_all_tasks(backend=backend)
    except Exception as e:
        _warn(f"Could not load remote index: {e}")
        all_tasks = cache.list_cached_tasks()

    _info(f"  {len(all_tasks)} task(s) loaded.")

    now = datetime.now(timezone.utc)
    stale_claims = _detect_stale_claims(all_tasks, now)

    if stale_claims:
        _warn(f"  Stale claims detected: {stale_claims}")

    if time.monotonic() - t0 > timeout:
        _err("Reconcile timeout exceeded.")
        return 1

    all_views = views.build_all_views(all_tasks)
    view_items = list(all_views.items())

    # Cache every view locally regardless of upload outcome — matches the prior
    # sequential loop, which wrote the cache for each view before attempting its
    # upload. Done up front (main thread) so the cache write is never racy.
    for view_name, view_data in view_items:
        cache.write_cached_view(view_name, view_data)

    # Upload the views CONCURRENTLY (PERF), the same way _write_task_and_views
    # (P1) does: remote.upload_json is thread-safe (each call writes a unique
    # tempfile + runs an independent subprocess; remote.py holds no shared
    # mutable state), so a small pool collapses the ~50 serial uploads into one
    # round-trip's wall-time — the second half of the reconcile-timeout fix.
    # Semantics are preserved exactly: per-view success is collected, any
    # failure (False OR a raise) lands in `failures`, and the partial-upload
    # handling below is unchanged.
    failures = []

    def _upload_one(item):
        view_name, view_data = item
        remaining = deadline - time.monotonic()
        # BUG 6b: the old guard was `remaining <= 0` with `timeout=max(1, int(
        # remaining))`. With 0<remaining<1 that floored the per-view timeout UP to
        # 1s, letting an upload run up to ~1s PAST the global reconcile deadline.
        # Treat any sub-1s budget as past-deadline (skip, count as a failed view)
        # so the deadline is a hard ceiling — consistent with the `<= 0` guard.
        if remaining < 1:
            return view_name, False
        vpath = _view_name_to_remote(view_name)
        # Treat a RAISING upload as a failed view, not an escape hatch: an
        # unguarded pool.map would re-raise out of cmd_reconcile, bypassing the
        # failures -> "preserve markers, return 1" path and crashing the
        # heartbeat. Catching keeps the contract: any failure is a failed view.
        try:
            ok = remote.upload_json(view_data, vpath, backend=backend,
                                    timeout=int(remaining))
        except Exception:
            ok = False
        return view_name, ok

    max_workers = min(8, len(view_items)) or 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        for view_name, ok in pool.map(_upload_one, view_items):
            if not ok:
                failures.append(view_name)

    if time.monotonic() - t0 > timeout:
        _err("Reconcile timeout exceeded mid-upload.")
        ops_log.log_op("reconcile", status="timeout")
        return 1

    # Rebuild the presence aggregate from the durable per-agent presence records,
    # mirroring how the task views self-heal here. Best-effort: a presence rebuild
    # failure must not fail a task-view reconcile, so it is reported but does not
    # count toward `failures`.
    _reconcile_presence(backend=backend)

    # Liveness-aware reroute sweep (best-effort; never fails a reconcile tick).
    # Runs AFTER the presence rebuild so it reads the freshly-reconciled
    # aggregate. Considers only kind:review directives; reroutes never-acted
    # reviews whose assignee fell below liveness floor, escalates on cap/miss,
    # freezes accepted-then-stalled ones. Whichever machine reconciles first
    # wins; others converge via the stale-observation re-read inside the sweep.
    try:
        _sweep_review_routes(all_tasks, backend=backend, now=now, deadline=deadline)
    except Exception:
        pass

    # Retention pass (best-effort, throttled to ~once/day, bounded + time-budgeted
    # against THIS reconcile's deadline so it never double-counts the 90s ceiling).
    # Never raises into the tick; logs its tally.
    try:
        ret = _run_retention(all_tasks, now=now, deadline=deadline, backend=backend)
        if not ret.get("skipped"):
            _info(f"  Retention: archived {ret['archived']} task(s) "
                  f"(deferred {ret['deferred']}), pruned {ret['pruned_markers']} marker(s), "
                  f"{ret['pruned_presence']} dead presence, {ret.get('pruned_health', 0)} health.")
    except Exception as e:
        _warn(f"  Retention pass error (skipped): {e}")

    if failures:
        _warn(f"  View upload failures: {failures}")
        ops_log.log_op("reconcile", status="partial", detail=f"failed views: {failures}")
        # Do NOT clear op markers — views are still broken and need another reconcile run.
        return 1

    # --- Self-reported per-host health record (spec v2 §1) -------------------
    # SUCCESS POINT: we are PAST the `if failures: return 1` guard above, so
    # failures == [] here. The health write is its OWN failure-isolated upload —
    # NOT a member of the parallel view-upload batch (which completes BEFORE the
    # failure verdict, so a batched health file would upload even on a FAILING
    # reconcile and falsely read healthy). It is also NOT gated on the best-effort
    # sub-passes (_sweep_review_routes / _run_retention ran above and never fail
    # the tick); gating on their flakiness would suppress a healthy heartbeat. A
    # health-write failure logs and NEVER changes this tick's return code.
    try:
        retention_last_run = None
        try:
            rmark = remote.download_json(remote.retention_marker_path(now), backend=backend)
            if isinstance(rmark, dict):
                retention_last_run = rmark.get("at") or rmark.get("date")
        except Exception:
            retention_last_run = None
        listener_last_fire = None
        try:
            surface = _inbox_surface_path(identity.resolve_agent())
            if surface.exists():
                listener_last_fire = _iso_z(datetime.fromtimestamp(
                    surface.stat().st_mtime, tz=timezone.utc))
        except Exception:
            listener_last_fire = None
        record = _build_health_record(
            now=now,
            duration_s=round(time.monotonic() - t0, 3),
            tasks_loaded=len(all_tasks),
            views_refreshed=len(all_views),
            repair_backlog=len(needs_repair),
            retention_last_run=retention_last_run,
            listener_last_fire=listener_last_fire,
            bus_task_count=len(all_tasks),
        )
        slug = views.agent_slug(identity.resolve_agent())
        if not remote.upload_json(record, remote.health_remote_path(slug), backend=backend):
            _warn("  Health record upload failed (best-effort; tick unaffected).")
    except Exception as e:
        _warn(f"  Health record write error (skipped): {e}")
    # ------------------------------------------------------------------------

    for m in needs_repair:
        cache.clear_op_marker(m["op_id"])

    ops_log.log_op("reconcile", status="ok", detail=f"{len(all_tasks)} tasks, {len(all_views)} views")
    _info(f"  Reconcile complete. {len(all_views)} views refreshed.")
    return 0


def cmd_search(args: Any, backend: Optional[list[str]] = None) -> int:
    """Search tasks by text across title, summary, tags."""
    query = args.query
    out_format = getattr(args, "format", "table")

    idx = cache.read_cached_view("search-index")
    if idx:
        records = idx.get("records", [])
        q = query.lower()
        results = []
        for r in records:
            text = " ".join([
                r.get("title", ""),
                r.get("summary", ""),
                r.get("workstream", ""),
                r.get("owner_agent", ""),
                " ".join(r.get("tags", [])),
            ]).lower()
            if q in text:
                results.append(r)
    else:
        # No cached search-index — search the summaries aggregate. search_tasks
        # reads title/current_summary/workstream/owner_agent/tags, all present on
        # a summary; no task body fetch. Falls back to a full load on an older bus.
        all_tasks = _load_task_summaries(backend=backend)
        results = views.search_tasks(query, all_tasks)

    # --archived (alias --all): additionally scan the cold archive index shards.
    # Default search stays hot-only (fast); the archive is O(archived) and paid
    # only when explicitly requested. Matches on the same fields as hot search.
    if getattr(args, "archived", False):
        q = query.lower()
        seen = {r.get("id") for r in results}
        for shard in _list_index_shards(backend=backend):
            if shard.get("id") in seen:
                continue
            text = " ".join([shard.get("title", ""), shard.get("workstream", ""),
                             shard.get("owner_agent", "")]).lower()
            if q in text:
                results.append({
                    "id": shard.get("id", ""), "title": shard.get("title", ""),
                    "status": shard.get("status", ""), "priority": "",
                    "workstream": shard.get("workstream", ""),
                    "owner_agent": shard.get("owner_agent", ""),
                    "archived": True, "archive_path": shard.get("archive_path", ""),
                })

    if out_format == "json":
        _print_json({"query": query, "count": len(results), "results": results})
        return 0

    if not results:
        _info(f"No tasks found matching {query!r}.")
        return 0

    _info(f"\n{len(results)} task(s) matching {query!r}:\n")
    for r in results:
        status = r.get("status", "?")
        task_id = r.get("id", "?")
        title = r.get("title", "")[:60]
        priority = r.get("priority", "??")
        print(f"  [{status}] [{priority}] {task_id[:28]}  {title}")
        # Search results may come from cached search-index ("summary") or
        # from task_summary() dicts ("current_summary") — handle both.
        summary_text = (r.get("summary") or r.get("current_summary") or "").strip()
        if summary_text:
            print(f"          {summary_text[:80]}")
    print()
    return 0


def cmd_restore(args: Any, backend: Optional[list[str]] = None) -> int:
    """Restore a cold-archived task back into the hot path.

    Reverses _archive_task: reads the task's archive/index/<id>.json shard for
    its archive_path, downloads the archived body, uploads it back to
    tasks/<id>.json, then deletes the index shard. The NEXT reconcile re-includes
    it in views (the body is back in the tasks/ listing the self-heal enumerates).
    Nothing is one-way. NOTE this is a bus-level MOVE, independent of the platform
    'fulcra file restore' (which restores a deleted file's prior VERSION by UUID);
    archived tasks were moved, not deleted, so we move them back ourselves.

    Order mirrors the archive's no-loss ordering: write the hot copy and VERIFY it
    landed before deleting the shard, so a crash leaves a recoverable state."""
    tid = args.task_id
    shard = _read_index_shard(tid, backend=backend)
    if not shard:
        _err(f"No archived task {tid!r} (no archive/index/{tid}.json shard).")
        return 1
    archive_path = shard.get("archive_path") or remote.archive_task_path(tid, "")
    body = remote.download_json(archive_path, backend=backend)
    if not body:
        _err(f"Archived body for {tid!r} not found at {archive_path}.")
        return 1
    task_path = remote.task_remote_path(tid)
    if not remote.upload_json(body, task_path, backend=backend):
        _err(f"Failed to restore body for {tid!r}.")
        return 1
    if remote.stat(task_path, backend=backend) is None:
        _err(f"Restore of {tid!r} did not verify; left archive shard intact.")
        return 1
    remote.delete(remote.archive_index_path(tid), backend=backend)
    _info(f"Restored {tid} to {task_path}. Run reconcile to re-incorporate into views.")
    return 0


def cmd_capabilities(args: Any, backend: Optional[list[str]] = None) -> int:
    """Print this build's version + the commands it supports — a capability probe.

    ArcBot-2 flagged that onboarding instructions can drift ahead of the
    installed CLI: a doc tells an agent to run a subcommand its build doesn't
    have yet. This gives onboarding a machine-readable check —
    ``capabilities --format json`` returns ``{name, version, commands}`` so a
    script can verify e.g. ``"needs-me" in commands`` before relying on it,
    instead of discovering the gap via an argparse error. The command list is
    sourced from the dispatch table (``entry.COMMAND_MAP``) — the same registry
    ``main`` routes on, so it can never claim a command that won't run. The
    hidden hook-only ``__session-task`` is excluded (not part of the public
    surface). Read-only; never touches the bus."""
    from . import __version__
    # Lazy import: entry imports this module at load, so importing entry at cli
    # module scope would be circular. Inside the function it resolves fine.
    from .entry import COMMAND_MAP

    commands = sorted(k for k in COMMAND_MAP if not k.startswith("__"))
    out_format = getattr(args, "format", "table")

    if out_format == "json":
        _print_json({"name": "fulcra-coord", "version": __version__,
                     "commands": commands})
        return 0

    print(f"fulcra-coord {__version__}")
    print(f"commands ({len(commands)}): {' '.join(commands)}")
    return 0


def cmd_doctor(args: Any, backend: Optional[list[str]] = None) -> int:
    """Check configuration, CLI availability, and remote access."""
    import shutil
    from . import __version__, remote_root as get_remote_root

    _info(f"\nfulcra-coord doctor — v{__version__}")
    _info(f"{'='*50}")

    ok_all = True

    # Config
    _info(f"\n[Config]")
    _info(f"  Remote root:  {get_remote_root()}")
    _info(f"  Cache root:   {cache.cache_root()}")

    cli_env = os.environ.get("FULCRA_CLI_COMMAND", "")
    if cli_env:
        _info(f"  CLI command:  {cli_env} (FULCRA_CLI_COMMAND)")
    elif shutil.which("fulcra-api"):
        _info(f"  CLI command:  fulcra-api (found on PATH)")
    else:
        _info(f"  CLI command:  uv tool run fulcra-api (fallback)")

    # CLI availability
    _info(f"\n[CLI]")
    cli_ok, cli_msg = remote.check_cli_available(backend=backend)
    status = "OK" if cli_ok else "FAIL"
    _info(f"  CLI reachable: {status}  ({cli_msg})")
    if not cli_ok:
        ok_all = False
        _info("  -> Install Fulcra CLI: uv tool install fulcra-api")
        _info("  -> Or set FULCRA_CLI_COMMAND to your CLI invocation")

    # File command group probe — the #1 fresh-agent onboarding failure.
    #
    # The public PyPI `fulcra-api` build lacks the `file` command group that the
    # entire coordination bus is driven by, so an agent that pip-installs it sees
    # every bus op fail silently. This probe targets the *resolved real CLI* (not
    # the injected fake backend, which speaks the `file` subcommand protocol but
    # has no top-level `file` group), so it answers "does the installed CLI have
    # `file`?". Wrapped defensively: a hung or broken probe must degrade to FAIL,
    # never crash doctor.
    try:
        file_ok, file_msg = remote.check_file_commands()
    except Exception as e:  # defensive — check_file_commands shouldn't raise
        file_ok, file_msg = False, f"file probe error: {e}"
    file_status = "OK" if file_ok else "FAIL"
    _info(f"  File commands: {file_status}  ({file_msg})")
    if not file_ok:
        ok_all = False
        _info("  -> The installed Fulcra CLI lacks the `file` command group that "
              "fulcra-coord needs to drive the bus.")
        _info("  -> Install a file-capable build (the `file-management` branch of "
              "fulcradynamics/fulcra-api-python).")
        _info("  -> See docs/fulcra-cli-branch.md for the exact install command.")

    # Remote access
    _info(f"\n[Remote]")
    if cli_ok or backend:
        remote_ok, remote_msg = remote.check_remote_access(backend=backend)
        remote_status = "OK" if remote_ok else "FAIL"
        _info(f"  Remote access: {remote_status}  ({remote_msg})")
        if not remote_ok:
            ok_all = False
            _info("  -> Run: fulcra-api auth login  (see docs/auth.md)")
            _info("  -> Or check FULCRA_COORD_REMOTE_ROOT is correct")
    else:
        _info("  Remote access: SKIP (CLI not reachable)")

    # Pending operation markers
    _info(f"\n[Cache]")
    markers = cache.list_op_markers()
    needs_repair = [m for m in markers if m.get("needs_reconcile")]
    all_tasks_cached = cache.list_cached_tasks()
    _info(f"  Cached tasks:  {len(all_tasks_cached)}")
    _info(f"  Pending ops:   {len(markers)}")
    if needs_repair:
        _info(f"  Needs reconcile: {len(needs_repair)}")
        _info("  -> Run: fulcra-coord reconcile")
    else:
        _info(f"  Needs reconcile: 0")

    # Annotations (Agent-Tasks timeline writer)
    #
    # Surfaces, at a glance, WHY a timeline write would or wouldn't happen — the
    # diagnostic that would have told the operator immediately that the feature
    # was simply disabled. Reports the resolved mode, whether a bearer token is
    # obtainable (WITHOUT ever printing it), and the API base the writer targets.
    _info(f"\n[Annotations]")
    ann_mode, ann_source = lifecycle_annotations.resolve_mode_source()
    _info(f"  Mode:          {ann_mode}  (source: {ann_source})")
    if ann_mode == "off":
        _info("  -> disabled — run `fulcra-coord annotations on` to enable for "
              "every agent (or set FULCRA_COORD_ANNOTATIONS=http for this shell)")
    else:
        _info(f"  API base:      {lifecycle_annotations._api_base()}")
        # Resolve the token only to confirm one EXISTS; never echo its value.
        token = lifecycle_annotations._resolve_token()
        if token:
            src = ("FULCRA_ACCESS_TOKEN" if os.environ.get("FULCRA_ACCESS_TOKEN")
                   else "fulcra auth print-access-token")
            _info(f"  Token:         OK (via {src})")
        else:
            ok_all = False
            _info("  Token:         FAIL (no FULCRA_ACCESS_TOKEN and "
                  "`fulcra auth print-access-token` did not yield one)")
            _info("  -> Run: fulcra auth login   (or set FULCRA_ACCESS_TOKEN)")

    # Fleet health (the per-host coordination-machinery self-reports). Local
    # on-host checks above + fleet health here = the full picture. Wrapped
    # defensively: a fleet-health read error must degrade to a noted line, never
    # crash doctor (mirrors the file-probe guard above).
    _info(f"\n[Fleet health]")
    try:
        result = _assess_fleet(now=datetime.now(timezone.utc), backend=backend)
        _info(f"  Worst status: {result['worst_status']}")
        for h in result["hosts"]:
            reasons = ("; ".join(h["reasons"])) if h["reasons"] else "ok"
            _info(f"  [{h['status']}] {h['host']} — {reasons}")
        if not result["hosts"]:
            _info("  (no hosts reporting health records yet)")
        if result["bus"]["missed_digest_window"]:
            _info("  -> digest window appears MISSED (no recent digest marker)")
    except Exception as e:
        _info(f"  Fleet health: unavailable ({e})")

    _info(f"\n{'='*50}")
    _info("OK" if ok_all else "Issues detected — see above.")
    return 0 if ok_all else 1


def cmd_install_shim(args: Any, backend: Optional[list[str]] = None) -> int:
    """Install a fulcra-coord shim to PATH (~/.local/bin/fulcra-coord)."""
    import stat as stat_mod
    from pathlib import Path

    # Find the installed entry point for this package
    # Works whether installed as a package or run directly
    script_path = Path(sys.argv[0]).resolve()
    if script_path.name == "fulcra-coord" and script_path.exists():
        src = script_path
    else:
        # Derive from package location
        pkg_dir = Path(__file__).resolve().parent
        src = pkg_dir.parent / "scripts" / "fulcra-coord"

    bin_dir = Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim_path = bin_dir / "fulcra-coord"

    # Guard against writing a shim that calls itself (infinite loop).
    # This happens when `pip install --user` places the entry point directly at
    # ~/.local/bin/fulcra-coord — the same destination as the shim.
    src_is_shim_target = src.exists() and src.resolve() == shim_path.resolve()

    if src.exists() and not src_is_shim_target:
        shim_content = f"""#!/usr/bin/env bash
# fulcra-coord shim — auto-generated by fulcra-coord install-shim
exec "{src}" "$@"
"""
    else:
        # Fallback: invoke via python3 -m (works for installed packages where
        # fulcra_coord is on PYTHONPATH, and for source-tree dev installs).
        shim_content = f"""#!/usr/bin/env bash
# fulcra-coord shim — auto-generated by fulcra-coord install-shim
exec python3 -m fulcra_coord "$@"
"""

    shim_path.write_text(shim_content)
    shim_path.chmod(shim_path.stat().st_mode | stat_mod.S_IEXEC | stat_mod.S_IXGRP | stat_mod.S_IXOTH)
    _info(f"Shim installed: {shim_path}")
    _info(f"\nAdd to PATH if needed:")
    _info(f'  export PATH="$HOME/.local/bin:$PATH"')
    return 0
