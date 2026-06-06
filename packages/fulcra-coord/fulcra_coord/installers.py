"""Hook + scheduler installers for fulcra-coord.

The per-tool setup commands: install the SessionStart/PreCompact hooks for Claude
Code / OpenClaw / Codex, the launchd heartbeat and per-agent listener schedulers,
and the ``fulcra-coord`` shim. Each resolves the CLI invocation, writes the
hook/plist via its tool module, and reports the plan (``--dry-run`` prints, never
writes). Rarely-run, machine-local setup — off the bus hot path.

Extracted from cli.py behind stable re-exports; depends only on the tool modules
(claude_code / openclaw / codex / heartbeat / listener) + identity and the output
leaf utils, and never imports cli, so the split has no cycle. The per-function
inline imports (cli_invocation / openclaw_plugin / json / stat) travel verbatim with
their bodies. ``_derive_agent`` is the usual thin local alias.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

from . import claude_code, openclaw, codex, heartbeat, listener, identity
from .output import info as _info, warn as _warn


def _derive_agent() -> str:
    return identity.resolve_agent()

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
