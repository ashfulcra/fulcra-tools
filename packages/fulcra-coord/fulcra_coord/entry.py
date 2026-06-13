"""Entry point for the fulcra-coord CLI."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from . import cli as _cli
# The one sanctioned forge poller dispatches DIRECTLY from here, not
# via a cli re-export like every other command: cli.py is a core module, and
# the reverse fitness pin (test_no_core_module_imports_forge_mirror) forbids
# core from importing the mirror — entry.py sits above core, so this is the
# one place the production-side bridge may be wired in.
from . import forge_mirror as _forge_mirror
from . import listener_tick as _listener_tick
from . import selfupdate as _selfupdate


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fulcra-coord",
        description=(
            "Shared agent coordination layer using Fulcra Files as a coordination bus.\n\n"
            "Environment variables:\n"
            "  FULCRA_CLI_COMMAND              Fulcra CLI invocation (default: fulcra-api)\n"
            "  FULCRA_COORD_REMOTE_ROOT        Remote root path (default: /coordination)\n"
            "  FULCRA_COORD_BACKEND            Override backend for testing\n"
            "  FULCRA_COORD_TIMEOUT_SECONDS    Read timeout in seconds (default: 30)\n"
            "  XDG_CACHE_HOME                  Cache base dir (default: ~/.cache)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # `--version` works even though a subcommand is required — argparse's
    # version action fires before the subparser-required check. A reviewer flagged
    # the CLI as having no usable version signal across breaking subcommand
    # additions; this plus the dynamic version makes `--version` authoritative.
    p.add_argument("--version", action="version",
                   version=f"fulcra-coord {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    # ---- status ----
    sp = sub.add_parser("status", help="Show coordination status")
    sp.add_argument("--workstream", "-w", metavar="WS", help="Filter by workstream")
    sp.add_argument("--agent", "-a", metavar="AGENT", help="Filter by agent")
    sp.add_argument("--format", choices=["table", "json"], default="table")

    # ---- board ----
    sp = sub.add_parser("board",
                        help="Render the coordination-loop board: loops awaiting "
                             "you, your unanswered asks (⚠ overdue / ◈ out-of-band), "
                             "open loops by kind, and the ideas pipeline")
    sp.add_argument("--agent", "-a", metavar="AGENT",
                    help="Whose board (default: $FULCRA_COORD_AGENT or derived)")
    sp.add_argument("--format", choices=["table", "json"], default="table")

    # ---- agents ----
    sp = sub.add_parser("agents",
                        help="Cross-agent digest: what each agent is working on "
                             "(active/waiting/blocked grouped by owner, stale-marked)")
    sp.add_argument("--mine", metavar="AGENT",
                    help="Filter to one agent (what was I working on)")
    sp.add_argument("--format", choices=["table", "json"], default="table")

    # ---- connect ----
    sp = sub.add_parser("connect",
                        help="Record this agent's presence on connect: declared "
                             "workstream(s) + a one-line 'what I'm on', so the "
                             "human sees what each agent is working on even with "
                             "no active task (SessionStart hooks call this)")
    sp.add_argument("--workstream", "-w", default=None, metavar="WS",
                    help="Comma-separated workstream(s); UNIONed with the "
                         "distinct workstream of your open tasks")
    sp.add_argument("--summary", "-s", default="", metavar="SUMMARY",
                    help="One-line 'what I'm currently on'")
    sp.add_argument("--agent", "-a", default=None, metavar="AGENT",
                    help="Who is connecting (default: $FULCRA_COORD_AGENT or derived)")
    sp.add_argument("--can-review", dest="can_review", action="store_true",
                    help="Declare this agent can review PRs (sugar for --role review)")
    sp.add_argument("--role", action="append", default=None, metavar="ROLE",
                    help="Declare a capability/role (repeatable), e.g. --role review")
    sp.add_argument("--clear-roles", dest="clear_roles", action="store_true",
                    help="EXPLICITLY drop previously declared roles (a bare "
                         "connect preserves them; 2026-06-11 bug hunt C4)")
    sp.add_argument("--format", choices=["table", "json"], default="table")

    # ---- workstream ----
    sp = sub.add_parser("workstream",
                        help="Declare/update THIS agent's presence workstreams "
                             "(set/add/clear). Bare `workstream` shows current presence.")
    sp.add_argument("--summary", "-s", default=None, metavar="SUMMARY",
                    help="Update the one-line 'what I'm on'")
    sp.add_argument("--agent", "-a", default=None, metavar="AGENT",
                    help="Whose presence (default: $FULCRA_COORD_AGENT or derived)")
    sp.add_argument("--format", choices=["table", "json"], default="table")
    wsub = sp.add_subparsers(dest="ws_action")
    wsp_set = wsub.add_parser("set", help="REPLACE the workstream list")
    wsp_set.add_argument("workstreams", metavar="WS", help="Comma-separated workstream(s)")
    wsp_set.add_argument("--summary", "-s", default=None, metavar="SUMMARY")
    wsp_set.add_argument("--agent", "-a", default=None, metavar="AGENT")
    wsp_set.add_argument("--format", choices=["table", "json"], default="table")
    wsp_add = wsub.add_parser("add", help="APPEND to the workstream list")
    wsp_add.add_argument("workstreams", metavar="WS", help="Comma-separated workstream(s)")
    wsp_add.add_argument("--summary", "-s", default=None, metavar="SUMMARY")
    wsp_add.add_argument("--agent", "-a", default=None, metavar="AGENT")
    wsp_add.add_argument("--format", choices=["table", "json"], default="table")
    wsp_clear = wsub.add_parser("clear", help="Empty the workstream list")
    wsp_clear.add_argument("--summary", "-s", default=None, metavar="SUMMARY")
    wsp_clear.add_argument("--agent", "-a", default=None, metavar="AGENT")
    wsp_clear.add_argument("--format", choices=["table", "json"], default="table")

    # ---- presence ----
    sp = sub.add_parser("presence",
                        help="Show the agent presence roster: who is working on "
                             "what right now, with last-seen age + live/idle/stale")
    sp.add_argument("--format", choices=["table", "json"], default="table")

    # ---- roles ----
    sp = sub.add_parser("roles",
                        help="Role registry + lease status: the durable "
                             "identities sessions claim leases on. Bare "
                             "`roles` lists every role with HELD/VACANT/"
                             "CONTESTED status; set/claim/release manage them.")
    sp.add_argument("--format", choices=["table", "json"], default="table")
    rsub = sp.add_subparsers(dest="roles_action")
    rsp_set = rsub.add_parser(
        "set", help="Create or update a role registry record (upsert; an "
                    "update preserves fields you don't pass)")
    rsp_set.add_argument("name", metavar="NAME", help="The role, e.g. reviewer")
    rsp_set.add_argument("--description", "-d", default=None, metavar="TEXT",
                         help="What this role is for")
    rsp_set.add_argument("--instructions", default=None, metavar="TEXT",
                         help="Standing instructions — the job description any "
                              "fresh session that claims the role follows")
    rsp_set.add_argument("--policy", choices=["shared", "exclusive"],
                         default=None,
                         help="shared = fan-out to every fresh holder; "
                              "exclusive = one holder (double-hold reads "
                              "CONTESTED)")
    rsp_set.add_argument("--sla-hours", dest="sla_hours", type=int,
                         default=None, metavar="H",
                         help="Vacancy SLA: vacant longer than this escalates "
                              "to the maintainer (daily)")
    rsp_set.add_argument("--maintainer", default=None, metavar="WHO",
                         help="Who fixes a vacancy (an agent id, @role, or the "
                              "human handle) — the escalation edge")
    rsp_set.add_argument("--format", choices=["table", "json"], default="table")
    rsp_claim = rsub.add_parser(
        "claim", help="Claim a lease on a role for this agent (connect --role "
                      "does this automatically; the lease stays fresh while "
                      "your presence does)")
    rsp_claim.add_argument("name", metavar="NAME")
    rsp_claim.add_argument("--agent", "-a", default=None, metavar="AGENT")
    rsp_claim.add_argument("--format", choices=["table", "json"], default="table")
    rsp_release = rsub.add_parser(
        "release", help="Release this agent's own lease on a role (other "
                        "holders are never touched)")
    rsp_release.add_argument("name", metavar="NAME")
    rsp_release.add_argument("--agent", "-a", default=None, metavar="AGENT")
    rsp_release.add_argument("--format", choices=["table", "json"], default="table")

    # ---- tell ----
    sp = sub.add_parser("tell",
                        help="Direct work at another agent: create a proposed "
                             "directive task assigned to them (sugar over start)")
    # assignee is OPTIONAL: omit it and use --route-capability to resolve a LIVE
    # recipient at send time instead of pinning a fixed agent.
    sp.add_argument("assignee", metavar="ASSIGNEE", nargs="?", default=None,
                    help="Agent to direct the work at (omit with --route-capability)")
    sp.add_argument("title", metavar="TITLE", help="Short durable task objective")
    sp.add_argument("--next", "-n", default="", metavar="NEXT_ACTION")
    sp.add_argument("--workstream", "-w", default="general", metavar="WS")
    sp.add_argument("--priority", "-p", default="P2", metavar="PRIORITY",
                    help="P0|P1|P2|P3")
    sp.add_argument("--from", dest="from", default=None, metavar="AGENT",
                    help="Directing agent (owner); default: derived/env agent")
    sp.add_argument("--summary", "-s", default="", metavar="SUMMARY")
    sp.add_argument("--route-capability", dest="route_capability", default=None, metavar="CAP",
                    help="Resolve a LIVE recipient declaring CAP instead of a fixed assignee")
    sp.add_argument("--floor", choices=["live", "idle"], default="idle",
                    help="Minimum liveness for --route-capability resolution (default: idle)")
    sp.add_argument("--expects-response", dest="expects_response",
                    action="store_true",
                    help="Make this an ASK, not an FYI: opens a kind=dispatch "
                         "loop (SLA-tracked) that stays open until the "
                         "recipient closes it with `respond`")

    # ---- later ----
    sp = sub.add_parser("later",
                        help="Capture a 'do later' item as backlog ON THE BUS: "
                             "a kind=idea loop addressed to the @backlog role "
                             "(durable + board-visible, spams nobody's inbox; "
                             "route it later with `assign`)")
    sp.add_argument("title", metavar="TITLE", help="Short durable backlog item")
    sp.add_argument("--summary", "-s", default="", metavar="SUMMARY")
    sp.add_argument("--workstream", "-w", default="general", metavar="WS")
    sp.add_argument("--priority", "-p", default="P3", metavar="PRIORITY",
                    help="P0|P1|P2|P3 (default: P3 — it's deferred work)")
    sp.add_argument("--from", dest="from", default=None, metavar="AGENT",
                    help="Capturing agent (owner); default: derived/env agent")

    # ---- handoff ----
    sp = sub.add_parser("handoff",
                        help="Hand work to another agent/role WITH its resume "
                             "state: opens a kind=dispatch loop whose payload "
                             "carries a continuity checkpoint ref. The "
                             "recipient's claim surfaces the ref (+ rendered "
                             "resume brief when fulcra-continuity is "
                             "installed); closing the loop = the work continued")
    sp.add_argument("--to", dest="to", default=None, metavar="AGENT|@ROLE",
                    help="Recipient: an agent id or a @role audience")
    sp.add_argument("--checkpoint", default=None, metavar="REF|FILE",
                    help="Continuity checkpoint to carry: an opaque ref "
                         "(forwarded verbatim) or a local checkpoint JSON "
                         "file (published to the remote continuity tree; the "
                         "remote path becomes the ref)")
    sp.add_argument("--title", required=True, metavar="TITLE",
                    help="Short durable objective of the handed-off work")
    sp.add_argument("--summary", "-s", default="", metavar="SUMMARY")
    sp.add_argument("--next", "-n", default="", metavar="NEXT_ACTION")
    sp.add_argument("--workstream", "-w", default="general", metavar="WS")
    sp.add_argument("--priority", "-p", default="P2", metavar="PRIORITY",
                    help="P0|P1|P2|P3")
    sp.add_argument("--from", dest="from", default=None, metavar="AGENT",
                    help="Handing-off agent (owner); default: derived/env agent")

    # ---- remind ----
    sp = sub.add_parser("remind",
                        help="Create a scheduled directive that appears in an "
                             "agent's inbox at WHEN (ISO date/datetime or "
                             "relative 5d/36h/10m)")
    sp.add_argument("assignee", metavar="ASSIGNEE",
                    help="Agent or @role to remind")
    sp.add_argument("when", metavar="WHEN",
                    help="ISO date/datetime or relative offset like 5d/36h/10m")
    sp.add_argument("title", metavar="TITLE",
                    help="Short durable reminder objective")
    sp.add_argument("--next", "-n", default="", metavar="NEXT_ACTION")
    sp.add_argument("--summary", "-s", default="", metavar="SUMMARY")
    sp.add_argument("--workstream", "-w", default="general", metavar="WS")
    sp.add_argument("--priority", "-p", default="P3", metavar="PRIORITY",
                    help="P0|P1|P2|P3")
    sp.add_argument("--due", default=None, metavar="WHEN",
                    help="Optional deadline (ISO date/datetime or relative "
                         "5d/36h/10m); informational, not the visibility gate")
    sp.add_argument("--from", dest="from", default=None, metavar="AGENT",
                    help="Directing agent (owner); default: derived/env agent")

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
    sp.add_argument("--all", action="store_true",
                    help="Include aged-out informational broadcasts "
                         "(older than FULCRA_COORD_INBOX_AGE_DAYS) that are "
                         "hidden from the default inbox")
    sp.add_argument("--format", choices=["table", "json"], default="table")

    # ---- start ----
    sp = sub.add_parser("start", help="Create and start a new task")
    # NOT a task id: an id-shaped TITLE (TASK-YYYYMMDD-...) is REFUSED by
    # cmd_start (2026-06-11 live find — `start TASK-...` minted junk tasks
    # titled after ids when the operator meant to CLAIM one).
    sp.add_argument("title", metavar="TITLE",
                    help="Short durable task objective — a NEW task's title, "
                         "never an existing TASK-id (to claim one: "
                         "'update <id> --status active')")
    sp.add_argument("--workstream", "-w", required=True, metavar="WS")
    # --agent is OPTIONAL (auto-resolved via identity when omitted) — parity with
    # every sibling write-command; it used to uniquely require it. Stays an override.
    sp.add_argument("--agent", "-a", default=None, metavar="AGENT")
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
    sp = sub.add_parser("block",
                        help="Mark a task as blocked. --blocked-on for an "
                             "agent/external blocker; --on-user to block on the "
                             "human (assigns to them + needs:human, lands on needs-me)")
    sp.add_argument("task_id", metavar="TASK-ID")
    sp.add_argument("--blocked-on", default=None, metavar="REASON",
                    help="What an agent/external thing is blocking on")
    sp.add_argument("--on-user", dest="on_user", default=None, metavar="ASK",
                    help="What you need the HUMAN to do — assigns the task to the "
                         "resolved human handle + tags needs:human")
    sp.add_argument("--not-before", dest="not_before", default=None, metavar="WHEN",
                    help="Don't surface as 'blocked on you NOW' until this time "
                         "(ISO date/datetime or relative like 5d/36h/10m). Keeps "
                         "a not-yet-actionable ask off the needs-me plate / banner "
                         "until it's due, listing it under 'upcoming' meanwhile")
    sp.add_argument("--due", default=None, metavar="WHEN",
                    help="The deadline (ISO date/datetime or relative 5d/36h/10m) — "
                         "drives upcoming ordering + urgency display; informational, "
                         "does not gate visibility")
    sp.add_argument("--agent", "-a", default=None, metavar="AGENT")

    # ---- pause ----
    sp = sub.add_parser("pause", help="Pause a task (set to waiting)")
    sp.add_argument("task_id", metavar="TASK-ID")
    sp.add_argument("--next", "-n", required=True, metavar="NEXT_ACTION")
    sp.add_argument("--agent", "-a", default=None, metavar="AGENT")
    sp.add_argument("--snapshot", action="store_true",
                    help="Also write a Fulcra Continuity checkpoint for this pause")

    # ---- snapshot ----
    sp = sub.add_parser("snapshot", help="Write a Fulcra Continuity checkpoint without changing task state")
    sp.add_argument("task_id", metavar="TASK-ID")
    sp.add_argument("--reason", default="manual", metavar="REASON",
                    help="Why this checkpoint is being written, e.g. pre-compact or idle")
    sp.add_argument("--next", "-n", default=None, metavar="NEXT_ACTION",
                    help="Optional next action override for the checkpoint")
    sp.add_argument("--transcript-path", default="", metavar="PATH",
                    help="Optional transcript/session log path for resume context")
    sp.add_argument("--agent", "-a", default=None, metavar="AGENT")

    # ---- checkpoint ----
    sp = sub.add_parser("checkpoint",
                        help="Read or update a ROLE's durable resume point "
                             "(registry checkpoint_ref). With --ref: set it "
                             "(preserving every other field); without: show "
                             "the current ref + best-effort resume brief. "
                             "Claiming the role (roles claim / connect "
                             "--role) prints the same resume.")
    sp.add_argument("--role", required=True, metavar="NAME",
                    help="The role whose checkpoint_ref to read/update")
    sp.add_argument("--ref", default=None, metavar="REF",
                    help="Opaque checkpoint ref (e.g. the remote continuity "
                         "path handoff/park publish). Omit to show current.")
    sp.add_argument("--format", choices=["table", "json"], default="table")

    # ---- park ----
    sp = sub.add_parser("park",
                        help="Best-effort session-exit checkpoint of every "
                             "role this session holds: writes a continuity "
                             "checkpoint per held role (needs the optional "
                             "fulcra-continuity CLI), publishes it to the "
                             "bus, and points the role's checkpoint_ref at "
                             "it. Silent no-op without continuity or held "
                             "roles; NEVER exits nonzero (hook-safe).")
    sp.add_argument("--agent", "-a", default=None, metavar="AGENT",
                    help="Whose held roles to park (default: "
                         "$FULCRA_COORD_AGENT or derived)")
    sp.add_argument("--summary", "-s", default="", metavar="TEXT",
                    help="Optional objective line for the checkpoint(s)")

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

    # ---- request-review ----
    sp = sub.add_parser(
        "request-review",
        help="Route a review of an artifact to a live/idle reviewer "
             "(capability-based, self-healing); escalates to the human if "
             "nobody qualifies")
    # `dest` stays "pr" so args.pr keeps working (lower churn), but the artifact
    # is now an OPAQUE ref — a PR#, MR#, branch, commit SHA, URL, or patch id —
    # not specifically a GitHub PR. --repo is OPTIONAL (forge-agnostic refs like
    # a branch or URL carry their own context).
    sp.add_argument("pr", metavar="ARTIFACT",
                    help="What to review: PR#/MR#/branch/commit SHA/URL/patch id")
    sp.add_argument("--repo", required=False, default=None, metavar="REPO")
    sp.add_argument("--agent", "-a", default=None, metavar="AGENT",
                    help="The author (default: derived) — selects the canonical reviewer")
    sp.add_argument("--candidate-list", dest="candidate_list", default=None, metavar="A,B,C",
                    help="Explicit preference-ordered pool override (advanced)")
    sp.add_argument("--note", default=None, metavar="TEXT",
                    help="Optional request context carried to the reviewer")
    sp.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="Print ranked pool / tiers / excluded / winner / reason; write nothing")
    sp.add_argument("--format", choices=["table", "json"], default="table")

    # ---- review-done ----
    sp = sub.add_parser(
        "review-done",
        help="Land a reviewer's verdict as a bus directive to the artifact's "
             "author (forge-agnostic — never a GitHub-comment-only signal). "
             "The verdict always reaches the author's inbox.")
    sp.add_argument("artifact", metavar="ARTIFACT",
                    help="What was reviewed: PR#/MR#/branch/commit SHA/URL/patch id")
    sp.add_argument("--verdict", required=True, choices=["approve", "changes"],
                    help="approve|changes — the review outcome")
    sp.add_argument("--note", default=None, metavar="TEXT",
                    help="Optional reviewer note carried in the directive")
    sp.add_argument("--with-fix", dest="with_fix", default=None, metavar="SHA",
                    help="Commit SHA for a reviewer fix pushed with this verdict")
    sp.add_argument("--repo", required=False, default=None, metavar="REPO")
    sp.add_argument("--to", dest="to", default=None, metavar="AGENT",
                    help="Explicit author override (skips author resolution)")
    sp.add_argument("--from", dest="from", default=None, metavar="REVIEWER",
                    help="The reviewer (default: derived identity)")
    sp.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="Print what would be posted; write nothing")
    sp.add_argument("--format", choices=["table", "json"], default="table")

    # ---- respond ----
    sp = sub.add_parser("respond",
                        help="Close/answer a coordination loop ON THE BUS — the "
                             "generic return leg (review verdicts use review-done; "
                             "this covers dispatch results, answers, signoffs)")
    sp.add_argument("loop_id", metavar="LOOP-ID",
                    help="The directive/loop id (DIR-... or LOOP-...)")
    sp.add_argument("--outcome", "-o", default="done", metavar="OUTCOME",
                    help="Terminal outcome/verdict (e.g. approve, delivered, answered)")
    sp.add_argument("--evidence", "-e", default="", metavar="EVIDENCE")
    sp.add_argument("--agent", "-a", default=None, metavar="AGENT")
    sp.add_argument("--format", choices=["table", "json"], default="table")

    # ---- forge-mirror ----
    sp = sub.add_parser(
        "forge-mirror",
        help="Mirror verdict-shaped GitHub signals (merge, review states, "
             "verdict comments) for open review loops into the evidence "
             "sub-log — marked source=forge-mirror, flags the loop "
             "out-of-band, NEVER closes it (the requester closes "
             "explicitly, citing the evidence)")
    sp.add_argument("--once", action="store_true",
                    help="Run a single sweep — the default and only mode. "
                         "Scheduling rides the existing listener/digest "
                         "cadence later; this is deliberately not a daemon.")
    sp.add_argument("--repo", default=None, metavar="REPO",
                    help="Only probe loops whose artifact_ref targets REPO")
    sp.add_argument("--format", choices=["table", "json"], default="table")

    # ---- reconcile ----
    sub.add_parser("reconcile", help="Repair views and resolve pending operation markers")

    # ---- search ----
    sp = sub.add_parser("search", help="Search tasks by text")
    sp.add_argument("query", metavar="QUERY")
    sp.add_argument("--format", choices=["table", "json"], default="table")
    sp.add_argument("--archived", "--all", dest="archived", action="store_true",
                    help="Also search the cold archive (archive/index shards). "
                         "Default search is hot-only (fast).")

    # ---- restore ----
    sp = sub.add_parser("restore",
                        help="Restore a cold-archived task back into the hot path")
    sp.add_argument("task_id", metavar="TASK-ID")
    sp.add_argument("--format", choices=["table", "json"], default="table")

    # ---- doctor ----
    sub.add_parser("doctor", help="Check configuration, CLI availability, and remote access")

    # ---- capabilities ----
    sp = sub.add_parser("capabilities",
                        help="Print this build's version + supported commands "
                             "(a probe so onboarding can detect whether the "
                             "installed CLI has a given subcommand before using it)")
    sp.add_argument("--format", choices=["table", "json"], default="table")

    # ---- announce-version ----
    sp = sub.add_parser("announce-version",
                        help="Publish this build's version as the canonical "
                             "version manifest (runtime/version.json) — the "
                             "maintainer runs this at each release so the "
                             "fleet self-updates instead of needing manual "
                             "'UPDATE NOW' broadcasts. The manifest is a "
                             "POINTER (version + commit + min-supported), "
                             "never code or commands.")
    sp.add_argument("--min-supported", dest="min_supported", default=None,
                    metavar="VERSION",
                    help="Optional compatibility floor: the oldest version "
                         "still expected to read the bus correctly")
    # 2026-06-11 bug hunt C8 (a): announcing a dev/prerelease build silently
    # froze fleet self-update (hosts can't compare non-X.Y.Z versions), so
    # announce-version now refuses them unless this explicit override is set.
    sp.add_argument("--allow-prerelease", dest="allow_prerelease",
                    action="store_true", default=False,
                    help="Announce a non-X.Y.Z (dev/prerelease) version "
                         "anyway — hosts will NOT update toward it; they "
                         "mark themselves stale instead. Loudly warned.")
    sp.add_argument("--format", choices=["table", "json"], default="table")

    # ---- health ----
    sp = sub.add_parser("health",
                        help="Fleet coordination-system health dashboard")
    sp.add_argument("--format", choices=["table", "json"], default="table")

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
    sp.add_argument("--can-review", dest="can_review", action="store_true",
                    help="Bake review capability declaration into the installed "
                         "SessionStart connect hook")
    sp.add_argument("--role", action="append", default=None, metavar="ROLE",
                    help="Bake a capability/role declaration into the installed "
                         "SessionStart connect hook (repeatable)")
    # Host wake-exec add-on: seed this agent's entry in the per-adopter
    # wake.json so the durable listener can SPAWN a headless session when
    # directed work arrives (not just notify). The written command is a
    # documented placeholder the operator must review — the config file is the
    # customization point.
    sp.add_argument("--with-wake", dest="with_wake", action="store_true",
                    help="Also write a wake.json entry so the host listener can "
                         "wake this agent (spawn a headless session) when "
                         "directed work arrives. Review the written command — "
                         "it runs unattended with the host's default permissions")
    sp.add_argument("--agent", "-a", dest="agent", default=None, metavar="AGENT",
                    help="Agent id the wake entry is keyed by (default: "
                         "$FULCRA_COORD_AGENT or derived). Only used with "
                         "--with-wake")

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
    # Bundle the durable bus-pickup path in one command, so a fresh OpenClaw
    # agent HEARS directed work without a separate install-heartbeat /
    # install-listener step (the OpenClaw analogue of ensure-codex-watch).
    sp.add_argument("--agent", "-a", dest="agent", default=None, metavar="AGENT",
                    help="Agent whose inbox the bundled listener watches "
                         "(default: $FULCRA_COORD_AGENT or derived). Only used "
                         "with --with-listener.")
    sp.add_argument("--with-heartbeat", dest="with_heartbeat", action="store_true",
                    help="Also install the machine-global reconcile heartbeat "
                         "(reuses install-heartbeat — the crashed-agent safety net)")
    sp.add_argument("--with-listener", dest="with_listener", action="store_true",
                    help="Also install the per-agent inbox listener (reuses "
                         "install-listener) so this agent hears directed work while idle")
    sp.add_argument("--listener-interval-min", dest="listener_interval_min",
                    type=int, default=None, metavar="N",
                    help="Bundled listener poll cadence in minutes (default: 10)")
    sp.add_argument("--heartbeat-interval-min", dest="heartbeat_interval_min",
                    type=int, default=None, metavar="N",
                    help="Bundled heartbeat cadence in minutes (default: 20)")
    sp.add_argument("--schedule-target-dir", dest="schedule_target_dir",
                    default=None, metavar="DIR",
                    help="Override the LaunchAgents/cron target dir for the "
                         "bundled heartbeat + listener (for testing)")
    sp.add_argument("--logs-dir", dest="logs_dir", default=None, metavar="DIR",
                    help="Override the stdout/stderr logs dir for the bundled "
                         "heartbeat + listener")

    # ---- install-codex ----
    sp = sub.add_parser("install-codex",
                        help="Install Codex lifecycle hooks (SessionStart + PreCompact) "
                             "into ~/.codex/hooks.json. No Stop hook by design — "
                             "Codex end-parking is delegated to the heartbeat.")
    sp.add_argument("--target-dir", dest="target_dir", default=None, metavar="DIR",
                    help="Override the Codex config dir (default: ~/.codex)")
    sp.add_argument("--with-wake", dest="with_wake", action="store_true",
                    help="Also write a wake.json entry so the host listener can "
                         "wake this Codex agent with `codex exec` when directed "
                         "work arrives. Review the written command before relying on it")
    sp.add_argument("--agent", "-a", dest="agent", default=None, metavar="AGENT",
                    help="Agent id the wake entry is keyed by (default: "
                         "$FULCRA_COORD_AGENT or derived). Only used with "
                         "--with-wake")
    sp.add_argument("--uninstall", action="store_true", help="Remove the managed hooks")
    sp.add_argument("--dry-run", action="store_true", help="Print intended changes, write nothing")
    sp.add_argument("--role", action="append", default=None, metavar="ROLE",
                    help="Bake an additional capability/role declaration into "
                         "the installed SessionStart connect hook (repeatable; "
                         "Codex still declares review by default)")

    # ---- ensure-codex-watch ----
    sp = sub.add_parser("ensure-codex-watch",
                        help="Idempotently arm Codex hooks + per-agent inbox "
                             "listener; safe to run at every Codex SessionStart")
    sp.add_argument("--agent", "-a", default=None, metavar="AGENT",
                    help="Agent to arm (default: $FULCRA_COORD_AGENT or derived)")
    sp.add_argument("--set-identity", dest="set_identity", default=None, metavar="AGENT",
                    help="Persist this declared identity for the current cwd first")
    sp.add_argument("--no-connect", dest="no_connect", action="store_true",
                    help="Skip the presence refresh after arming")
    sp.add_argument("--can-review", dest="can_review", action="store_true",
                    help="Declare review capability when refreshing presence")
    sp.add_argument("--role", action="append", default=None, metavar="ROLE",
                    help="Declare a capability/role when refreshing presence (repeatable)")
    sp.add_argument("--summary", default=None, metavar="TEXT",
                    help="One-line 'what I'm on' for the presence refresh")
    sp.add_argument("--interval-min", dest="interval_min", type=int, default=None,
                    metavar="N", help="Listener poll cadence in minutes (default: 10)")
    sp.add_argument("--codex-target-dir", dest="codex_target_dir", default=None,
                    metavar="DIR", help="Override the Codex config dir (default: ~/.codex)")
    sp.add_argument("--listener-target-dir", dest="listener_target_dir", default=None,
                    metavar="DIR", help="Override the listener LaunchAgents/cron dir")
    sp.add_argument("--listener-logs-dir", dest="listener_logs_dir", default=None,
                    metavar="DIR", help="Override the listener stdout/stderr logs dir")
    sp.add_argument("--no-load", dest="no_load", action="store_true",
                    help="Skip the best-effort launchctl load of the listener plist")
    sp.add_argument("--thread-id", dest="thread_id", default=None, metavar="ID",
                    help="Codex thread/session id to attach the managed heartbeat "
                         "automation to (SessionStart passes this automatically)")
    sp.add_argument("--automation-interval-min", dest="automation_interval_min",
                    type=int, default=None, metavar="N",
                    help="Managed Codex heartbeat automation cadence in minutes "
                         "(default: 15)")
    sp.add_argument("--with-wake", dest="with_wake", action="store_true",
                    help="Also write a wake.json entry so pending inbox work can "
                         "spawn a headless Codex wake via `codex exec`")
    sp.add_argument("--uninstall", action="store_true",
                    help="Tear down the Codex hooks + per-agent listener")
    sp.add_argument("--dry-run", action="store_true",
                    help="Print intended changes, write nothing, no load/connect")

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
    sp.add_argument("--with-forge-mirror", dest="with_forge_mirror",
                    action="store_true",
                    help="Schedule listener-tick --forge-mirror so review "
                         "verdict-shaped forge signals are mirrored before "
                         "the inbox poll")
    sp.add_argument("--dry-run", action="store_true", help="Print intended changes, write nothing")

    # ---- listener-tick ----
    sp = sub.add_parser("listener-tick",
                        help="Run one scheduled listener tick; optionally "
                             "mirror forge review signals before notify-inbox")
    sp.add_argument("--agent", "-a", default=None, metavar="AGENT",
                    help="Whose inbox (default: $FULCRA_COORD_AGENT or derived)")
    sp.add_argument("--forge-mirror", dest="forge_mirror", action="store_true",
                    help="Run forge-mirror once before notify-inbox")
    sp.add_argument("--repo", default=None, metavar="REPO",
                    help="Only mirror review loops for REPO")
    sp.add_argument("--format", choices=["table", "json"], default="table")

    # ---- notify-inbox ----
    sp = sub.add_parser("notify-inbox",
                        help="Poll the inbox for an agent and surface+notify if "
                             "directives exist (the call the listener runs each tick)")
    sp.add_argument("--agent", "-a", default=None, metavar="AGENT",
                    help="Whose inbox (default: $FULCRA_COORD_AGENT or derived)")

    # ---- digest ----
    sp = sub.add_parser("digest",
                        help="Write the operator situational-awareness digest "
                             "(blocked on you / upcoming / per-agent / stale) to "
                             "the Fulcra timeline on its own 'Agent Tasks — Digest' track")
    sp.add_argument("--window", choices=["morning", "evening"], default=None,
                    help="Cadence window (sets the lookback + label); omit for on-demand")
    sp.add_argument("--human", default=None, metavar="HANDLE",
                    help="Whose plate (default: $FULCRA_COORD_HUMAN or persisted handle)")
    sp.add_argument("--format", choices=["table", "json"], default="table")
    sp.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="Render + print the digest, write nothing to the timeline")

    # ---- install-digest ----
    sp = sub.add_parser("install-digest",
                        help="Install the twice-daily scheduled `fulcra-coord digest` "
                             "jobs (launchd 08:00/18:00 on macOS, cron elsewhere) — "
                             "the push side of the operator digest")
    sp.add_argument("--target-dir", dest="target_dir", default=None, metavar="DIR",
                    help="Override the LaunchAgents/cron target dir (for testing)")
    sp.add_argument("--logs-dir", dest="logs_dir", default=None, metavar="DIR",
                    help="Override the directory for digest stdout/stderr logs")
    sp.add_argument("--uninstall", action="store_true", help="Remove the digest schedule")
    sp.add_argument("--dry-run", action="store_true", help="Print intended changes, write nothing")

    # ---- identity ----
    sp = sub.add_parser("identity",
                        help="Show, set, or clear this host's declared agent id "
                             "(the identity handshake reused by every bus op)")
    sp.add_argument("--format", choices=["table", "json"], default="table")
    isub = sp.add_subparsers(dest="identity_action")
    isp_set = isub.add_parser("set", help="Persist <agent-id> as this host's identity")
    isp_set.add_argument("agent_id", metavar="AGENT-ID",
                         help="e.g. claude-code:<host>:<repo>")
    isp_set.add_argument("--format", choices=["table", "json"], default="table")
    isp_clear = isub.add_parser("clear", help="Remove the persisted identity")
    isp_clear.add_argument("--format", choices=["table", "json"], default="table")
    isp_migrate = isub.add_parser(
        "migrate",
        help="Copy a legacy global identity into this repo's per-cwd entry "
             "(the legacy global is no longer resolved automatically)")
    isp_migrate.add_argument("--format", choices=["table", "json"], default="table")

    # ---- resume ----
    sp = sub.add_parser("resume",
                        help="Pick-up-where-you-left-off briefing for an agent: "
                             "your active/waiting work, what's blocked on you, "
                             "what you owe others, and what's blocked on the human")
    sp.add_argument("--agent", "-a", default=None, metavar="AGENT",
                    help="Whose briefing (default: $FULCRA_COORD_AGENT or derived)")
    sp.add_argument("--with-continuity", action="store_true",
                    help="Include latest Fulcra Continuity checkpoint summaries for active/waiting tasks")
    sp.add_argument("--format", choices=["table", "json"], default="table")

    # ---- briefing ----
    sp = sub.add_parser("briefing",
                        help="Session-start briefing in ONE process: resolved "
                             "identity + status + inbox + needs-me sections "
                             "from a single summaries load (what the "
                             "SessionStart hooks consume)")
    sp.add_argument("--agent", "-a", default=None, metavar="AGENT",
                    help="Whose briefing (default: $FULCRA_COORD_AGENT or "
                         "derived; the hooks deliberately omit this so a "
                         "persisted identity wins)")
    sp.add_argument("--format", choices=["table", "json"], default="json")

    # ---- needs-me ----
    sp = sub.add_parser("needs-me",
                        help="What's blocked on YOU (the human): every open task "
                             "assigned to / blocked on you across all agents, with "
                             "who's waiting, the ask, and how long it's been")
    sp.add_argument("--human", default=None, metavar="HANDLE",
                    help="Whose plate (default: $FULCRA_COORD_HUMAN or persisted "
                         "human handle or 'human')")
    sp.add_argument("--all", dest="all", action="store_true",
                    help="Also list each upcoming (future not_before) item inline, "
                         "not just the count")
    sp.add_argument("--format", choices=["table", "json"], default="table")

    # ---- human ----
    sp = sub.add_parser("human",
                        help="Show, set, or clear the human operator's handle — "
                             "the addressable identity tasks are 'blocked on ME' "
                             "against (default 'human'; personalize with set)")
    sp.add_argument("--format", choices=["table", "json"], default="table")
    hsub = sp.add_subparsers(dest="human_action")
    hsp_set = hsub.add_parser("set", help="Persist <handle> as the human operator")
    hsp_set.add_argument("handle", metavar="HANDLE", help="e.g. ash")
    hsp_set.add_argument("--format", choices=["table", "json"], default="table")
    hsp_clear = hsub.add_parser("clear", help="Remove the persisted human handle")
    hsp_clear.add_argument("--format", choices=["table", "json"], default="table")

    # ---- annotations ----
    sp = sub.add_parser("annotations",
                        help="Enable/disable/inspect the Agent-Tasks timeline "
                             "annotations writer (persisted so every agent emits "
                             "without a per-shell FULCRA_COORD_ANNOTATIONS export)")
    sp.add_argument("--format", choices=["table", "json"], default="table")
    asub = sp.add_subparsers(dest="annotations_action")
    asp_on = asub.add_parser("on", help="Persist annotations on (mode: http)")
    asp_on.add_argument("--format", choices=["table", "json"], default="table")
    asp_off = asub.add_parser("off", help="Remove the persisted mode (off unless env set)")
    asp_off.add_argument("--format", choices=["table", "json"], default="table")
    asp_status = asub.add_parser("status", help="Show resolved mode, source, and token state")
    asp_status.add_argument("--format", choices=["table", "json"], default="table")

    # ---- __session-task (hidden, used by hooks) ----
    sp = sub.add_parser("__session-task", help=argparse.SUPPRESS)
    sp.add_argument("session_id", metavar="SESSION_ID")

    return p


COMMAND_MAP = {
    "status": _cli.cmd_status,
    "board": _cli.cmd_board,
    "agents": _cli.cmd_agents,
    "connect": _cli.cmd_connect,
    "workstream": _cli.cmd_workstream,
    "presence": _cli.cmd_presence,
    "roles": _cli.cmd_roles,
    "tell": _cli.cmd_tell,
    "broadcast": _cli.cmd_broadcast,
    "later": _cli.cmd_later,
    "remind": _cli.cmd_remind,
    "handoff": _cli.cmd_handoff,
    "assign": _cli.cmd_assign,
    "inbox": _cli.cmd_inbox,
    "start": _cli.cmd_start,
    "update": _cli.cmd_update,
    "block": _cli.cmd_block,
    "pause": _cli.cmd_pause,
    "snapshot": _cli.cmd_snapshot,
    "checkpoint": _cli.cmd_checkpoint,
    "park": _cli.cmd_park,
    "done": _cli.cmd_done,
    "abandon": _cli.cmd_abandon,
    "request-review": _cli.cmd_request_review,
    "review-done": _cli.cmd_review_done,
    "respond": _cli.cmd_respond,
    "forge-mirror": _forge_mirror.cmd_forge_mirror,
    # Like forge-mirror, announce-version dispatches straight from its own
    # module: it is a maintainer-only release tool, not a coordination verb
    # cli.py needs to re-export.
    "announce-version": _selfupdate.cmd_announce_version,
    "reconcile": _cli.cmd_reconcile,
    "search": _cli.cmd_search,
    "restore": _cli.cmd_restore,
    "doctor": _cli.cmd_doctor,
    "capabilities": _cli.cmd_capabilities,
    "health": _cli.cmd_health,
    "install-shim": _cli.cmd_install_shim,
    "install-claude-code": _cli.cmd_install_claude_code,
    "install-openclaw": _cli.cmd_install_openclaw,
    "install-heartbeat": _cli.cmd_install_heartbeat,
    "install-listener": _cli.cmd_install_listener,
    "listener-tick": _listener_tick.cmd_listener_tick,
    "notify-inbox": _cli.cmd_notify_inbox,
    "install-codex": _cli.cmd_install_codex,
    "ensure-codex-watch": _cli.cmd_ensure_codex_watch,
    "identity": _cli.cmd_identity,
    "human": _cli.cmd_human,
    "annotations": _cli.cmd_annotations,
    "needs-me": _cli.cmd_needs_me,
    "briefing": _cli.cmd_briefing,
    "digest": _cli.cmd_digest,
    "install-digest": _cli.cmd_install_digest,
    "resume": _cli.cmd_resume,
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
