"""Entry point for the fulcra-coord CLI."""

from __future__ import annotations

import argparse
import os
import sys

from . import cli as _cli


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fulcra-coord",
        description=(
            "Shared agent coordination layer using Fulcra Files as a coordination bus.\n\n"
            "Environment variables:\n"
            "  FULCRA_CLI_COMMAND              Fulcra CLI invocation (default: fulcra-api)\n"
            "  FULCRA_COORD_REMOTE_ROOT        Remote root path (default: /coordination)\n"
            "  FULCRA_COORD_BACKEND            Override backend for testing\n"
            "  FULCRA_COORD_TIMEOUT_SECONDS    Read timeout in seconds (default: 5)\n"
            "  XDG_CACHE_HOME                  Cache base dir (default: ~/.cache)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)

    # ---- status ----
    sp = sub.add_parser("status", help="Show coordination status")
    sp.add_argument("--workstream", "-w", metavar="WS", help="Filter by workstream")
    sp.add_argument("--agent", "-a", metavar="AGENT", help="Filter by agent")
    sp.add_argument("--format", choices=["table", "json"], default="table")

    # ---- agents ----
    sp = sub.add_parser("agents",
                        help="Cross-agent digest: what each agent is working on "
                             "(active/waiting/blocked grouped by owner, stale-marked)")
    sp.add_argument("--mine", metavar="AGENT",
                    help="Filter to one agent (what was I working on)")
    sp.add_argument("--format", choices=["table", "json"], default="table")

    # ---- tell ----
    sp = sub.add_parser("tell",
                        help="Direct work at another agent: create a proposed "
                             "directive task assigned to them (sugar over start)")
    sp.add_argument("assignee", metavar="ASSIGNEE", help="Agent to direct the work at")
    sp.add_argument("title", metavar="TITLE", help="Short durable task objective")
    sp.add_argument("--next", "-n", default="", metavar="NEXT_ACTION")
    sp.add_argument("--workstream", "-w", default="general", metavar="WS")
    sp.add_argument("--priority", "-p", default="P2", metavar="PRIORITY",
                    help="P0|P1|P2|P3")
    sp.add_argument("--from", dest="from", default=None, metavar="AGENT",
                    help="Directing agent (owner); default: derived/env agent")
    sp.add_argument("--summary", "-s", default="", metavar="SUMMARY")

    # ---- broadcast ----
    sp = sub.add_parser("broadcast",
                        help="Direct work at EVERY agent: create a proposed "
                             "directive assigned to all (wildcard '*'), acked "
                             "per-agent (sugar over tell). Use `tell` for one agent.")
    sp.add_argument("title", metavar="TITLE", help="Short durable directive for all agents")
    sp.add_argument("--next", "-n", default="", metavar="NEXT_ACTION")
    sp.add_argument("--workstream", "-w", default="general", metavar="WS")
    sp.add_argument("--priority", "-p", default="P2", metavar="PRIORITY",
                    help="P0|P1|P2|P3")
    sp.add_argument("--from", dest="from", default=None, metavar="AGENT",
                    help="Directing agent (owner); default: derived/env agent")
    sp.add_argument("--summary", "-s", default="", metavar="SUMMARY")

    # ---- assign ----
    sp = sub.add_parser("assign", help="Set or redirect the assignee on a task")
    sp.add_argument("task_id", metavar="TASK-ID")
    sp.add_argument("assignee", metavar="ASSIGNEE")
    sp.add_argument("--agent", "-a", default=None, metavar="AGENT")

    # ---- inbox ----
    sp = sub.add_parser("inbox",
                        help="List open directives addressed to you "
                             "(--ack <id> to mark seen without claiming)")
    sp.add_argument("--agent", "-a", default=None, metavar="AGENT",
                    help="Whose inbox (default: $FULCRA_COORD_AGENT or derived)")
    sp.add_argument("--ack", default=None, metavar="TASK-ID",
                    help="Acknowledge a directive (records inbox_ack; stops re-notify)")
    sp.add_argument("--format", choices=["table", "json"], default="table")

    # ---- start ----
    sp = sub.add_parser("start", help="Create and start a new task")
    sp.add_argument("title", metavar="TITLE", help="Short durable task objective")
    sp.add_argument("--workstream", "-w", required=True, metavar="WS")
    sp.add_argument("--agent", "-a", required=True, metavar="AGENT")
    sp.add_argument("--kind", "-k", default="ops", metavar="KIND",
                    help="ops|feature|bug|research|infra|config|comms|other")
    sp.add_argument("--priority", "-p", default="P2", metavar="PRIORITY",
                    help="P0|P1|P2|P3")
    sp.add_argument("--summary", "-s", default="", metavar="SUMMARY")
    sp.add_argument("--next", "-n", default="", metavar="NEXT_ACTION")
    sp.add_argument("--surface", default=None, metavar="SURFACE",
                    help="e.g. local:claude-code, discord:#devops")

    # ---- update ----
    sp = sub.add_parser("update", help="Update task summary, next-action, or status")
    sp.add_argument("task_id", metavar="TASK-ID")
    sp.add_argument("--summary", "-s", metavar="SUMMARY")
    sp.add_argument("--next", "-n", metavar="NEXT_ACTION")
    sp.add_argument("--blocked-on", metavar="REASON")
    sp.add_argument(
        "--status", metavar="STATUS",
        choices=["active", "waiting", "blocked", "abandoned"],
        help="Status to transition to. Use 'done' command for marking done (requires --evidence).",
    )
    sp.add_argument("--agent", "-a", default=None, metavar="AGENT")

    # ---- block ----
    sp = sub.add_parser("block", help="Mark a task as blocked")
    sp.add_argument("task_id", metavar="TASK-ID")
    sp.add_argument("--blocked-on", required=True, metavar="REASON")
    sp.add_argument("--agent", "-a", default=None, metavar="AGENT")

    # ---- pause ----
    sp = sub.add_parser("pause", help="Pause a task (set to waiting)")
    sp.add_argument("task_id", metavar="TASK-ID")
    sp.add_argument("--next", "-n", required=True, metavar="NEXT_ACTION")
    sp.add_argument("--agent", "-a", default=None, metavar="AGENT")

    # ---- done ----
    sp = sub.add_parser("done", help="Mark a task as done (requires evidence)")
    sp.add_argument("task_id", metavar="TASK-ID")
    sp.add_argument("--evidence", "-e", required=True, metavar="EVIDENCE")
    sp.add_argument(
        "--verification-level",
        default="agent-verified",
        choices=["agent-verified", "human-verified", "automated", "unverified"],
        metavar="LEVEL",
        help="agent-verified|human-verified|automated|unverified (default: agent-verified)",
    )
    sp.add_argument("--confidence", metavar="CONFIDENCE")
    sp.add_argument("--agent", "-a", default=None, metavar="AGENT")

    # ---- abandon ----
    sp = sub.add_parser("abandon", help="Mark a task as abandoned")
    sp.add_argument("task_id", metavar="TASK-ID")
    sp.add_argument("--reason", "-r", required=True, metavar="REASON")
    sp.add_argument("--agent", "-a", default=None, metavar="AGENT")

    # ---- reconcile ----
    sub.add_parser("reconcile", help="Repair views and resolve pending operation markers")

    # ---- search ----
    sp = sub.add_parser("search", help="Search tasks by text")
    sp.add_argument("query", metavar="QUERY")
    sp.add_argument("--format", choices=["table", "json"], default="table")

    # ---- doctor ----
    sub.add_parser("doctor", help="Check configuration, CLI availability, and remote access")

    # ---- install-shim ----
    sub.add_parser("install-shim", help="Install fulcra-coord shim to ~/.local/bin/")

    # ---- install-claude-code ----
    sp = sub.add_parser("install-claude-code",
                        help="Install Claude Code lifecycle hooks (global by default)")
    sp.add_argument("--project", dest="scope", action="store_const",
                    const="project", default="global",
                    help="Install into ./.claude/settings.json instead of ~/.claude")
    sp.add_argument("--global", dest="scope", action="store_const", const="global",
                    help="Install into ~/.claude/settings.json (default)")
    sp.add_argument("--uninstall", action="store_true", help="Remove the managed hooks")
    sp.add_argument("--dry-run", action="store_true", help="Print intended changes, write nothing")

    # ---- install-openclaw ----
    sp = sub.add_parser("install-openclaw",
                        help="Install OpenClaw Track A coordination artifacts "
                             "(boot/heartbeat prompts + shutdown/bootstrap hooks)")
    sp.add_argument("--hooks-root", dest="hooks_root", default=None, metavar="DIR",
                    help="Explicit OpenClaw hooks dir (default: ~/.openclaw/hooks, "
                         "or $FULCRA_OPENCLAW_HOOKS_ROOT)")
    sp.add_argument("--uninstall", action="store_true", help="Remove the managed artifacts")
    sp.add_argument("--dry-run", action="store_true", help="Print intended changes, write nothing")
    sp.add_argument("--with-plugin", dest="with_plugin", action="store_true",
                    help="Also materialize the Track B Plugin-SDK plugin sources "
                         "(session_start/before_compaction/session_end). Source "
                         "drop only — finish with npm build + 'openclaw plugins install'.")
    sp.add_argument("--plugin-dir", dest="plugin_dir", default=None, metavar="DIR",
                    help="Target dir for --with-plugin sources "
                         "(default: ~/.openclaw/plugins/fulcra-coord, "
                         "or $FULCRA_OPENCLAW_PLUGIN_DIR)")

    # ---- install-codex ----
    sp = sub.add_parser("install-codex",
                        help="Install Codex lifecycle hooks (SessionStart + PreCompact) "
                             "into ~/.codex/hooks.json. No Stop hook by design — "
                             "Codex end-parking is delegated to the heartbeat.")
    sp.add_argument("--target-dir", dest="target_dir", default=None, metavar="DIR",
                    help="Override the Codex config dir (default: ~/.codex)")
    sp.add_argument("--uninstall", action="store_true", help="Remove the managed hooks")
    sp.add_argument("--dry-run", action="store_true", help="Print intended changes, write nothing")

    # ---- install-heartbeat ----
    sp = sub.add_parser("install-heartbeat",
                        help="Install a scheduled `fulcra-coord reconcile` heartbeat "
                             "(launchd on macOS, crontab elsewhere) — the safety net "
                             "that sweeps stale tasks for crashed/end-hook-less agents")
    sp.add_argument("--interval-min", dest="interval_min", type=int, default=20,
                    metavar="N", help="Run reconcile every N minutes (default: 20)")
    sp.add_argument("--target-dir", dest="target_dir", default=None, metavar="DIR",
                    help="Override the LaunchAgents/cron target dir (for testing)")
    sp.add_argument("--logs-dir", dest="logs_dir", default=None, metavar="DIR",
                    help="Override the directory for heartbeat stdout/stderr logs")
    sp.add_argument("--uninstall", action="store_true", help="Remove the heartbeat")
    sp.add_argument("--dry-run", action="store_true", help="Print intended changes, write nothing")

    # ---- install-listener ----
    sp = sub.add_parser("install-listener",
                        help="Install a scheduled `fulcra-coord notify-inbox` "
                             "listener (launchd on macOS, crontab elsewhere) — the "
                             "durable, per-agent way to notice directed work while idle")
    sp.add_argument("--agent", "-a", default=None, metavar="AGENT",
                    help="Agent whose inbox to watch (default: $FULCRA_COORD_AGENT or derived)")
    sp.add_argument("--interval-min", dest="interval_min", type=int, default=10,
                    metavar="N", help="Poll the inbox every N minutes (default: 10)")
    sp.add_argument("--target-dir", dest="target_dir", default=None, metavar="DIR",
                    help="Override the LaunchAgents/cron target dir (for testing)")
    sp.add_argument("--logs-dir", dest="logs_dir", default=None, metavar="DIR",
                    help="Override the directory for listener stdout/stderr logs")
    sp.add_argument("--uninstall", action="store_true", help="Remove the listener")
    sp.add_argument("--dry-run", action="store_true", help="Print intended changes, write nothing")

    # ---- notify-inbox ----
    sp = sub.add_parser("notify-inbox",
                        help="Poll the inbox for an agent and surface+notify if "
                             "directives exist (the call the listener runs each tick)")
    sp.add_argument("--agent", "-a", default=None, metavar="AGENT",
                    help="Whose inbox (default: $FULCRA_COORD_AGENT or derived)")

    # ---- identity ----
    sp = sub.add_parser("identity",
                        help="Show, set, or clear this host's declared agent id "
                             "(the identity handshake reused by every bus op)")
    sp.add_argument("--format", choices=["table", "json"], default="table")
    isub = sp.add_subparsers(dest="identity_action")
    isp_set = isub.add_parser("set", help="Persist <agent-id> as this host's identity")
    isp_set.add_argument("agent_id", metavar="AGENT-ID",
                         help="e.g. claude-code:DeskbookPro:fulcra-coord")
    isp_set.add_argument("--format", choices=["table", "json"], default="table")
    isp_clear = isub.add_parser("clear", help="Remove the persisted identity")
    isp_clear.add_argument("--format", choices=["table", "json"], default="table")

    # ---- __session-task (hidden, used by hooks) ----
    sp = sub.add_parser("__session-task", help=argparse.SUPPRESS)
    sp.add_argument("session_id", metavar="SESSION_ID")

    return p


COMMAND_MAP = {
    "status": _cli.cmd_status,
    "agents": _cli.cmd_agents,
    "tell": _cli.cmd_tell,
    "broadcast": _cli.cmd_broadcast,
    "assign": _cli.cmd_assign,
    "inbox": _cli.cmd_inbox,
    "start": _cli.cmd_start,
    "update": _cli.cmd_update,
    "block": _cli.cmd_block,
    "pause": _cli.cmd_pause,
    "done": _cli.cmd_done,
    "abandon": _cli.cmd_abandon,
    "reconcile": _cli.cmd_reconcile,
    "search": _cli.cmd_search,
    "doctor": _cli.cmd_doctor,
    "install-shim": _cli.cmd_install_shim,
    "install-claude-code": _cli.cmd_install_claude_code,
    "install-openclaw": _cli.cmd_install_openclaw,
    "install-heartbeat": _cli.cmd_install_heartbeat,
    "install-listener": _cli.cmd_install_listener,
    "notify-inbox": _cli.cmd_notify_inbox,
    "install-codex": _cli.cmd_install_codex,
    "identity": _cli.cmd_identity,
    "__session-task": _cli.cmd_session_task,
}


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    fn = COMMAND_MAP.get(args.command)
    if fn is None:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        return 1

    backend_env = os.environ.get("FULCRA_COORD_BACKEND", "").strip()
    backend = backend_env.split() if backend_env else None

    try:
        return fn(args, backend=backend) or 0
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
