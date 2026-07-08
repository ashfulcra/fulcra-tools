"""CLI for fde-engine.

    fde-engine init <slug> --title "..."
    fde-engine status <slug> [--json]
    fde-engine phase <slug> <new-phase>
    fde-engine sync <slug> push|pull [--dir DIR]
    fde-engine resume <slug>
    fde-engine list [--json]

Command functions take an injected ``transport`` so they're testable without
the network; ``main`` builds the real ``FulcraFileTransport``. Timestamps are
generated only here (the modules stay pure folds)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from . import engagement, resume, sync
from .transport import FulcraFileTransport, TransportError


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_dir(slug: str) -> str:
    return f"fde/{slug}"


def cmd_init(args, transport) -> int:
    meta = engagement.init_engagement(
        transport, args.slug, args.title or args.slug, now=_now())
    print(f"initialized {meta['slug']} (phase: {meta['phase']}) at "
          f"{engagement.remote_path(meta['slug'])}/")
    return 0


def cmd_status(args, transport) -> int:
    st = engagement.status(transport, args.slug)
    if args.json:
        print(json.dumps(st, indent=2))
        return 0
    print(f"{st['slug']} — {st['title']}")
    print(f"phase: {st['phase']} (updated {st['updated_at']})")
    for rel, present in st["artifacts"].items():
        print(f"  [{'x' if present else ' '}] {rel}")
    print(f"next: {st['next']}")
    return 0


def cmd_phase(args, transport) -> int:
    meta = engagement.set_phase(transport, args.slug, args.new_phase, now=_now())
    print(f"{meta['slug']}: phase -> {meta['phase']}")
    return 0


def cmd_sync(args, transport) -> int:
    local_dir = args.dir or _default_dir(args.slug)
    if args.direction == "push":
        report = sync.push(transport, args.slug, local_dir)
        print(f"pushed: {', '.join(report['pushed']) or '(nothing)'} "
              f"(skipped {report['skipped']} unchanged)")
    else:
        report = sync.pull(transport, args.slug, local_dir)
        print(f"pulled: {', '.join(report['pulled']) or '(nothing)'} "
              f"(skipped {report['skipped']} unchanged)")
    return 0


def cmd_resume(args, transport) -> int:
    print(resume.resume_brief(transport, args.slug), end="")
    return 0


def cmd_list(args, transport) -> int:
    rows = engagement.list_engagements(transport)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
        print(f"{r['slug']}\t{r['phase']}\t{r['title']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fde-engine", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("init", help="create a new engagement (phase: intake)")
    i.add_argument("slug")
    i.add_argument("--title", default=None)
    i.set_defaults(func=cmd_init)

    s = sub.add_parser("status", help="phase + artifact checklist + next move")
    s.add_argument("slug")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    ph = sub.add_parser("phase", help="validated phase transition")
    ph.add_argument("slug")
    ph.add_argument("new_phase")
    ph.set_defaults(func=cmd_phase)

    sy = sub.add_parser("sync", help="explicit-direction local-mirror sync")
    sy.add_argument("slug")
    sy.add_argument("direction", choices=["push", "pull"])
    sy.add_argument("--dir", default=None,
                    help="local mirror dir (default: ./fde/<slug>)")
    sy.set_defaults(func=cmd_sync)

    r = sub.add_parser("resume", help="deterministic resume brief")
    r.add_argument("slug")
    r.set_defaults(func=cmd_resume)

    ls = sub.add_parser("list", help="all engagements with phase")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_list)

    return p


def main(argv=None, transport=None) -> int:
    args = build_parser().parse_args(argv)
    transport = transport or FulcraFileTransport()
    try:
        return args.func(args, transport)
    except (engagement.EngagementError, sync.SyncError, TransportError) as exc:
        print(f"fde-engine: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
