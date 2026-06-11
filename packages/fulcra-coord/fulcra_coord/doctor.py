"""Diagnostics for fulcra-coord: ``capabilities`` + ``doctor``.

``capabilities`` lists the available commands (from the entry dispatch table).
``doctor`` is the onboarding/health self-check: it probes the resolved CLI + Fulcra
Files reachability, the annotations writer, the local identity/human config, and
folds in the fleet infra-health assessment — the single command a fresh agent runs
to find out why the bus isn't working.

Extracted from cli.py behind stable re-exports; depends only on lower layers
(cache / remote / annotations + the digest fleet-assessment and the output leaf
utils) and never imports cli, so the split has no cycle. The call-time inline
imports (entry.COMMAND_MAP, __version__, remote_root) travel verbatim with the
bodies — the entry import is resolved at call time, when entry is already loaded.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from typing import Any, Optional

from . import cache, remote
from . import annotations as lifecycle_annotations
from .output import info as _info, print_json as _print_json
from .digest import _assess_fleet


def cmd_capabilities(args: Any, backend: Optional[list[str]] = None) -> int:
    """Print this build's version + the commands it supports — a capability probe.

    A reviewer flagged that onboarding instructions can drift ahead of the
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
    # The coordination bus is driven by Fulcra Files (`fulcra file ...`). The
    # standard CLI ships that group today, but a stale install or a mispointed
    # FULCRA_CLI_COMMAND can still resolve to a binary without it, making bus
    # ops fail silently. This probe targets the *resolved real CLI* (not the
    # injected fake backend, which speaks the `file` subcommand protocol but has
    # no top-level `file` group), so it answers "does the installed CLI have
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
        _info("  -> The resolved Fulcra CLI lacks the `file` command group that "
              "fulcra-coord needs to drive the bus.")
        _info("  -> Reinstall the standard CLI (`uv tool install --reinstall "
              "--force fulcra-api`) or fix a mispointed FULCRA_CLI_COMMAND.")
        _info("  -> See docs/fulcra-cli-branch.md for verification and "
              "FULCRA_CLI_COMMAND examples.")

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
