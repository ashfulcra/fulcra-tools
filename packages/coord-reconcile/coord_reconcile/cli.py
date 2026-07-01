"""CLI for coord-reconcile (L1).

    coord-reconcile reconcile <team>
    coord-reconcile status    <team> [--json]
    coord-reconcile board     <team> [--json]
    coord-reconcile needs-me  <team> --agent <id> [--json]
    coord-reconcile search    <team> <query> [--json]

Command functions take an injected ``transport`` so they're testable without the
network; ``main`` builds the real ``FulcraFileTransport``.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from . import aggregate, query
from . import reconcile as rec
from .transport import FulcraFileTransport

__all__ = ["main"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _host() -> str:
    return os.environ.get("FULCRA_COORD_AGENT") or f"coord-reconcile:{socket.gethostname()}"


def _load_rows(transport: Any, team: str) -> list[dict[str, Any]]:
    raw = transport.read(rec.summaries_path(team))
    if not raw:
        return []
    try:
        return aggregate.aggregate_rows(json.loads(raw))
    except Exception:
        return []


def _line(row: dict[str, Any]) -> str:
    return (
        f"  [{row.get('priority', '?'):>2}] {str(row.get('status', '?')):8} "
        f"{row.get('title') or row.get('name')}"
        + (f"  ({row.get('assignee')})" if row.get("assignee") else "")
    )


def cmd_reconcile(args: argparse.Namespace, transport: Any) -> int:
    dt = _now()
    res = rec.reconcile(
        transport, args.team, now=_iso(dt), today=dt.strftime("%Y-%m-%d"), host=_host()
    )
    if res.get("degraded"):
        print(f"reconcile degraded (no writes): {res.get('reason')}", file=sys.stderr)
        return 1
    print(
        f"reconciled team/{args.team}: {res['tasks']} tasks "
        f"({res['parsed']} parsed, {res['reused']} reused), "
        f"{res['transitions']} log entries, {len(res['warnings'])} warnings"
    )
    for w in res["warnings"]:
        print(f"  warn: {w}", file=sys.stderr)
    return 0


def cmd_status(args: argparse.Namespace, transport: Any) -> int:
    rows = _load_rows(transport, args.team)
    counts = query.status_counts(rows)
    if args.json:
        print(json.dumps(counts, indent=2))
    else:
        if not rows:
            print(f"(no aggregate for team/{args.team} — run `reconcile` first)")
        print(f"team/{args.team}: {len(rows)} tasks — " + ", ".join(
            f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


def cmd_board(args: argparse.Namespace, transport: Any) -> int:
    rows = _load_rows(transport, args.team)
    groups = query.board(rows)
    if args.json:
        print(json.dumps(groups, indent=2))
        return 0
    for section in ("active", "waiting", "blocked", "proposed"):
        items = groups.get(section, [])
        if items:
            print(f"{section.upper()} ({len(items)})")
            for r in items:
                print(_line(r))
    return 0


def cmd_needs_me(args: argparse.Namespace, transport: Any) -> int:
    rows = _load_rows(transport, args.team)
    got = query.needs_me(rows, args.agent, now=_iso(_now()))
    if args.json:
        print(json.dumps(got, indent=2))
    else:
        print(f"{len(got)} item(s) need {args.agent}:")
        for r in got:
            print(_line(r))
    return 0


def cmd_search(args: argparse.Namespace, transport: Any) -> int:
    rows = _load_rows(transport, args.team)
    got = query.search(rows, args.query)
    if args.json:
        print(json.dumps(got, indent=2))
    else:
        print(f"{len(got)} match(es) for {args.query!r}:")
        for r in got:
            print(_line(r))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="coord-reconcile", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    def add_json(sp):
        sp.add_argument("--json", action="store_true", help="emit JSON")

    r = sub.add_parser("reconcile", help="scan + heal a team's task views")
    r.add_argument("team")
    r.set_defaults(func=cmd_reconcile)

    s = sub.add_parser("status", help="counts by status")
    s.add_argument("team"); add_json(s); s.set_defaults(func=cmd_status)

    b = sub.add_parser("board", help="open work grouped by status")
    b.add_argument("team"); add_json(b); b.set_defaults(func=cmd_board)

    nm = sub.add_parser("needs-me", help="open work assigned to / blocking an agent")
    nm.add_argument("team"); nm.add_argument("--agent", required=True); add_json(nm)
    nm.set_defaults(func=cmd_needs_me)

    sc = sub.add_parser("search", help="substring search over tasks")
    sc.add_argument("team"); sc.add_argument("query"); add_json(sc)
    sc.set_defaults(func=cmd_search)
    return p


def main(argv: Optional[list[str]] = None, transport: Any = None) -> int:
    args = build_parser().parse_args(argv)
    transport = transport if transport is not None else FulcraFileTransport()
    try:
        return args.func(args, transport)
    except Exception as e:  # never dump a traceback at the user
        print(f"coord-reconcile: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
