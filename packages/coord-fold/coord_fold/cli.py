"""Six verbs. Wiring only; the guarantee that no verb enumerates is G29's harness, not this file's shape."""
from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, timezone

from . import channel, checkpoint, events, fold, pointers
from .transport import CliPointerReader, CliPointerWriter, TransportUnavailable

RC_OK, RC_REFUSED, RC_UNKNOWN = 0, 2, 3


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_transports() -> tuple[CliPointerReader, CliPointerWriter]:
    from fulcra_common.client import find_fulcra_cli
    cli = find_fulcra_cli()
    if not cli:
        print("coord-fold: fulcra-api CLI not found on PATH", file=sys.stderr)
        raise SystemExit(RC_REFUSED)
    return CliPointerReader(cli=[cli]), CliPointerWriter(cli=[cli])


def _row_sort_key(item: tuple) -> tuple:
    return (item[1]["pri"], item[1]["at"])


def _render_open(state: dict) -> str:
    lines = []
    for slug, r in sorted(state["open"].items(), key=_row_sort_key):
        claimed = f"  claimed_by={r['claimed_by']}" if r.get("claimed_by") else ""
        lines.append(f"  [{r['pri']}] {slug}  from={r['from']}  ptr={r['ptr']}{claimed}")
    return "\n".join(lines) if lines else "  (nothing open)"


def _report_unknowns(state: dict) -> None:
    if state.get("unread_events"):
        print(f"fold: applied through {state['cursor']}; {state['unread_events']} events remain — bounded by new events, the next pass gets them")
    for slug in state.get("unreadable_pointers", []):
        print(f"fold: pointer for {slug} unreadable — that one row is UNKNOWN", file=sys.stderr)


def _emit_kind(reader, writer, team, *, sender, to, kind, slug, pri, ptr, at) -> int:
    try:
        cfg = channel.resolve(reader, team)
        payload = events.build_payload(at=at, sender=sender, to=to, kind=kind, slug=slug, pri=pri, ptr=ptr)
    except (channel.ChannelUnresolved, ValueError) as exc:
        print(f"{kind}: refused — {exc}", file=sys.stderr)
        return RC_REFUSED
    if not writer.write_event(cfg, payload, sender=sender):
        print(f"{kind}: UNKNOWN — the record write did not confirm", file=sys.stderr)
        return RC_UNKNOWN
    print(f"{kind} {slug} -> {to}")
    return RC_OK


def _owed_row(reader, team, agent, slug) -> tuple:
    """(row, load_state). A None row with load_state 'error' is UNKNOWN, never 'not owed'."""
    state, src = checkpoint.load(reader, team, agent)
    if src != "ok":
        return None, src
    return state["open"].get(slug), src


def cmd_fold(args, reader, writer) -> int:
    try:
        out = fold.run(reader, writer, args.team, args.agent, now=args.now, writer_id=f"{args.agent}:{uuid.uuid4().hex[:8]}", max_events=args.max_events, verify_pointers=args.verify_pointers, rebuild=getattr(args, "rebuild", False))
    except (channel.ChannelUnresolved, fold.FoldRefused) as exc:
        print(f"fold: refused — {exc}", file=sys.stderr)
        return RC_REFUSED
    except fold.FoldContended as exc:
        print(f"fold: REFUSED, not overwriting — {exc}", file=sys.stderr)
        return RC_REFUSED
    except TransportUnavailable as exc:
        print(f"fold: UNKNOWN — event read did not complete ({exc}); cursor not advanced", file=sys.stderr)
        return RC_UNKNOWN
    print(f"fold [{args.agent}] cursor={out.state['cursor']} applied={out.applied} open={len(out.state['open'])} source={out.source}")
    print(_render_open(out.state))
    _report_unknowns(out.state)
    return out.rc


def cmd_emit(args, reader, writer) -> int:
    return _emit_kind(reader, writer, args.team, sender=args.sender, to=args.to, kind=args.kind, slug=args.slug, pri=args.pri, ptr=args.ptr, at=args.at)


def cmd_claim(args, reader, writer) -> int:
    row, src = _owed_row(reader, args.team, args.agent, args.slug)
    if src == "error":
        print(f"claim: UNKNOWN — checkpoint unreadable; cannot tell whether {args.slug} is owed", file=sys.stderr)
        return RC_UNKNOWN
    if row is None:
        print(f"claim: refused — {args.slug} is not open in {args.agent}'s checkpoint", file=sys.stderr)
        return RC_REFUSED
    return _emit_kind(reader, writer, args.team, sender=args.agent, to=row["from"], kind="claim", slug=args.slug, pri=row["pri"], ptr=row["ptr"], at=args.at)


def cmd_release(args, reader, writer) -> int:
    row, src = _owed_row(reader, args.team, args.agent, args.slug)
    if src == "error":
        print(f"release: UNKNOWN — checkpoint unreadable; cannot tell whether {args.slug} is owed", file=sys.stderr)
        return RC_UNKNOWN
    if row is None:
        print(f"release: refused — {args.slug} is not open in {args.agent}'s checkpoint", file=sys.stderr)
        return RC_REFUSED
    return _emit_kind(reader, writer, args.team, sender=args.agent, to=row["from"], kind="release", slug=args.slug, pri=row["pri"], ptr=row["ptr"], at=args.at)


def cmd_close(args, reader, writer) -> int:
    row, src = _owed_row(reader, args.team, args.agent, args.slug)
    if src == "error":
        print(f"close: UNKNOWN — checkpoint unreadable; cannot tell whether {args.slug} is owed", file=sys.stderr)
        return RC_UNKNOWN
    if row is None:
        print(f"close: refused — {args.slug} is not open in {args.agent}'s checkpoint", file=sys.stderr)
        return RC_REFUSED
    _body, st = reader.read_classified(pointers.qualify(args.team, args.evidence))   # same resolution as --verify-pointers
    if st == "absent":
        print(f"close: refused — evidence {args.evidence} is absent", file=sys.stderr)
        return RC_REFUSED
    if st == "error":
        print(f"close: UNKNOWN — evidence {args.evidence} unreadable; not closing on a read that did not answer", file=sys.stderr)
        return RC_UNKNOWN
    return _emit_kind(reader, writer, args.team, sender=args.agent, to=row["from"], kind="close", slug=args.slug, pri=row["pri"], ptr=args.evidence, at=args.at)


def cmd_status(args, reader, writer) -> int:
    state, src = checkpoint.load(reader, args.team, args.agent)
    if src == "fresh":
        print(f"status: {args.agent} has never folded — run `coord-fold fold`", file=sys.stderr)
        return RC_REFUSED
    if src == "corrupt":
        print("status: refused — checkpoint corrupt", file=sys.stderr)
        return RC_REFUSED
    if src == "error":
        print("status: UNKNOWN — checkpoint unreadable", file=sys.stderr)
        return RC_UNKNOWN
    print(f"status [{args.agent}] cursor={state['cursor']} open={len(state['open'])}")
    print(_render_open(state))
    _report_unknowns(state)
    return RC_UNKNOWN if state.get("unreadable_pointers") else RC_OK


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--now", default=None)
    common.add_argument("--at", default=None)
    p = argparse.ArgumentParser(prog="coord-fold")
    sub = p.add_subparsers(dest="verb", required=True)
    e = sub.add_parser("emit", parents=[common])
    e.add_argument("team")
    for flag in ("--from", "--to", "--kind", "--slug", "--pri"):
        e.add_argument(flag, dest="sender" if flag == "--from" else flag[2:], required=True)
    e.add_argument("--ptr", default=None)
    e.set_defaults(func=cmd_emit)
    for name, fn in (("fold", cmd_fold), ("claim", cmd_claim), ("release", cmd_release), ("close", cmd_close), ("status", cmd_status)):
        sp = sub.add_parser(name, parents=[common])
        sp.add_argument("team")
        if name in ("claim", "release", "close"):
            sp.add_argument("slug")
        sp.add_argument("--agent", required=True)
        if name == "close":
            sp.add_argument("--evidence", required=True)
        if name == "fold":
            sp.add_argument("--max-events", type=int, default=5000)
            sp.add_argument("--verify-pointers", action="store_true")
            sp.add_argument("--rebuild", action="store_true",
                            help="recompute the open set from the stream (epoch cursor) under the current relevance rule; "
                                 "generation/writer kept so a concurrent writer is still refused")
        sp.set_defaults(func=fn)
    return p


def main(argv: list[str] | None = None, *, reader=None, writer=None) -> int:
    args = build_parser().parse_args(argv)
    if args.now is None:
        args.now = _now()
    if args.at is None:
        args.at = args.now
    if reader is None or writer is None:
        reader, writer = _default_transports()
    return int(args.func(args, reader, writer))


if __name__ == "__main__":
    raise SystemExit(main())
