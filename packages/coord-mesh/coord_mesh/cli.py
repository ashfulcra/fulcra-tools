"""`coord-mesh` — the five verbs.

This module is the console entry point declared in pyproject
(``coord-mesh = "coord_mesh.cli:main"``). Its absence at head 0667bfb crashed
the installed command while 61 unit tests stayed green — the missing
smoke-test gap codex-coder caught. `tests/test_cli_smoke.py` now invokes the
entry point itself, so a declared-but-absent surface cannot pass again.

Every verb returns a process exit code, and the codes MEAN something:

    0  did the thing, verified
    2  usage / rail refusal (the caller asked for something forbidden)
    3  UNKNOWN — a read failed and the answer is not "clear"

rc 3 exists because `mesh queue` reporting "no messages" when it could not read
a peer is the worst failure this package could have.
"""
import argparse
import sys
from typing import Optional

from . import envelope, peers, safety, transport, wire

RC_OK = 0
RC_REFUSED = 2
RC_UNKNOWN = 3


def _channel(args) -> str:
    return args.channel


def cmd_init(args) -> int:
    """Create the OUTBOUND share (channel + reports prefix) at a named uid."""
    try:
        rc, out, err = transport.share_create(
            name=args.name, data_type=_channel(args), user_id=args.peer,
            file_prefix=args.reports)
    except safety.SafetyViolation as exc:
        print(f"mesh init REFUSED: {exc}", file=sys.stderr)
        return RC_REFUSED
    except transport.TransportError as exc:
        print(f"mesh init UNKNOWN: {exc}", file=sys.stderr)
        return RC_UNKNOWN
    if rc != 0:
        print(f"mesh init failed: {(err or out).strip()[:400]}", file=sys.stderr)
        return RC_UNKNOWN

    # Read-back verify: the plan requires it, and a share that "succeeded" but
    # is not in the roster is exactly the claim-vs-action gap the fleet keeps
    # finding. A create we cannot confirm is UNKNOWN, not done.
    back = transport.list_outgoing()
    if back.unknown:
        print(f"mesh init: created, but read-back UNKNOWN ({back.detail}) — "
              f"verify with `fulcra-api share list-outgoing` before relying on it",
              file=sys.stderr)
        return RC_UNKNOWN
    granted = [r for r in back.rows
               if args.peer in str(r.get("permissions") or r)]
    if not granted:
        print("mesh init: create returned 0 but the share is NOT in "
              "list-outgoing — treat as NOT granted", file=sys.stderr)
        return RC_UNKNOWN
    print(f"mesh init: granted {_channel(args)}"
          + (f" + {args.reports}" if args.reports else "")
          + f" -> {args.peer} (read-back verified)")
    return RC_OK


def cmd_peers(args) -> int:
    """Roster fold: incoming shares (the JOIN signal) + outgoing + local registry."""
    inc = transport.list_incoming()
    out = transport.list_outgoing()
    reg = peers.load()

    degraded = False
    if inc.unknown:
        print(f"  incoming: UNKNOWN — cannot claim clear ({inc.detail})", file=sys.stderr)
        degraded = True
    else:
        for r in inc.rows:
            uid = r.get("fulcra_userid")
            name = r.get("fulcra_user_name") or uid
            print(f"  [in ] {name} ({uid}) — share {r.get('datashare_name')!r}")
    if out.unknown:
        print(f"  outgoing: UNKNOWN — cannot claim clear ({out.detail})", file=sys.stderr)
        degraded = True
    else:
        for r in out.rows:
            for p in r.get("permissions") or []:
                print(f"  [out] {p.get('allowed_fulcra_userid')} — "
                      f"share {r.get('datashare_name')!r}")
    for sid, sp in (reg.get("spaces") or {}).items():
        print(f"  [reg] space {sid} kind={sp.get('kind')} "
              f"cursors={len(sp.get('cursors') or {})}")
    return RC_UNKNOWN if degraded else RC_OK


def cmd_send(args) -> int:
    """Write a `to_user`-addressed event to MY channel. Read-back verified."""
    try:
        note = envelope.build(to_user=args.to_user, kind=args.kind,
                              slug=args.slug, priority=args.priority,
                              ptr=args.ptr)
    except (safety.SafetyViolation, ValueError) as exc:
        print(f"mesh send REFUSED: {exc}", file=sys.stderr)
        return RC_REFUSED
    print(envelope.encode(note))
    print("mesh send: envelope built (writing is the next increment — this verb "
          "currently prints the exact payload for a hand-run `fulcra-api record`)",
          file=sys.stderr)
    return RC_OK


def cmd_queue(args) -> int:
    """Poll each peer outbox, fold to one inbox, per-peer cursors.

    Trusted-empty discipline: a failed peer read is UNKNOWN, never quiet, and
    one unreadable peer degrades the whole fold's exit code.
    """
    reg = peers.load()
    space = args.space
    uids = args.peer or []
    if not uids:
        print("mesh queue: no --peer given and no registry members to poll",
              file=sys.stderr)
        return RC_REFUSED

    degraded, shown = [], 0
    for uid in uids:
        res = transport.get_records(_channel(args), args.window, user_id=uid)
        if res.unknown:
            print(f"  peer {uid}: UNKNOWN — cannot claim clear ({res.detail})",
                  file=sys.stderr)
            degraded.append(uid)
            continue
        cursor = peers.get_cursor(reg, space, uid)
        newest: Optional[str] = None
        for row in res.rows:
            rid = wire.record_id(row)
            if not rid:
                # Unidentifiable row: cannot dedupe it, so cannot advance past it.
                print("  row with no id — position UNKNOWN, not advancing cursor",
                      file=sys.stderr)
                degraded.append(uid)
                continue
            newest = newest or rid
            if cursor and rid == cursor:
                break
            note = envelope.parse(wire.note_text(row))
            if not note or not envelope.addressed_to(note, args.me):
                continue
            shown += 1
            print(f"  [{note.get('pri')}] {note.get('kind')} {note.get('slug')} "
                  f"from {wire.sender(row) or 'UNKNOWN-author'} "
                  f"ptr={note.get('ptr') or '-'}")
        if newest and not args.no_advance and uid not in degraded:
            peers.set_cursor(reg, space, uid, newest)
    if not args.no_advance:
        peers.save(reg)

    if degraded:
        print(f"mesh queue: {shown} event(s) shown, but {len(degraded)} peer(s) "
              f"UNREADABLE — this is UNKNOWN, not empty", file=sys.stderr)
        return RC_UNKNOWN
    print(f"mesh queue: {shown} event(s)")
    return RC_OK


def cmd_doctor(args) -> int:
    """Per-peer health. LOUD on any UNKNOWN."""
    problems = 0
    inc = transport.list_incoming()
    if inc.unknown:
        print(f"  ! incoming roster UNREADABLE — cannot claim clear ({inc.detail})",
              file=sys.stderr)
        problems += 1
    else:
        print(f"  ok incoming roster: {len(inc.rows)} share(s)")

    own = transport.get_records(_channel(args), args.window)
    if own.unknown:
        print(f"  ! own channel UNREADABLE ({own.detail})", file=sys.stderr)
        problems += 1
    else:
        print(f"  ok own channel readable: {len(own.rows)} record(s) in {args.window}")
        bad = [r for r in own.rows if wire.missing_fields(r)]
        if bad:
            print(f"  ! {len(bad)} record(s) missing contract fields "
                  f"{wire.REQUIRED_FIELDS} — transport shape drifted",
                  file=sys.stderr)
            problems += 1

    try:
        peers.load()
        print("  ok peer registry readable")
    except (ValueError, OSError) as exc:
        print(f"  ! peer registry UNREADABLE ({exc})", file=sys.stderr)
        problems += 1

    if problems:
        print(f"mesh doctor: {problems} UNKNOWN/degraded check(s)", file=sys.stderr)
        return RC_UNKNOWN
    print("mesh doctor: all checks readable")
    return RC_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="coord-mesh", description=__doc__.split("\n")[0])
    p.add_argument("--channel", default=None,
                   help="MomentAnnotation/<uuid> outbox channel")
    sub = p.add_subparsers(dest="cmd")

    i = sub.add_parser("init", help="create the outbound share at a named peer uid")
    i.add_argument("peer"); i.add_argument("--name", default="mesh")
    i.add_argument("--reports", default="reports/")
    i.set_defaults(fn=cmd_init)

    pr = sub.add_parser("peers", help="roster fold")
    pr.set_defaults(fn=cmd_peers)

    s = sub.add_parser("send", help="build a to_user-addressed event")
    s.add_argument("--to-user", dest="to_user", required=True)
    s.add_argument("--kind", default="response")
    s.add_argument("--slug", required=True)
    s.add_argument("--priority", default="P2")
    s.add_argument("--ptr", default=None)
    s.set_defaults(fn=cmd_send)

    q = sub.add_parser("queue", help="poll peer outboxes into one inbox")
    q.add_argument("--peer", action="append")
    q.add_argument("--me", required=True, help="my uid, for to_user filtering")
    q.add_argument("--space", default="default")
    q.add_argument("--window", default="1 day")
    q.add_argument("--no-advance", action="store_true")
    q.set_defaults(fn=cmd_queue)

    d = sub.add_parser("doctor", help="per-peer health; loud on UNKNOWN")
    d.add_argument("--window", default="1 day")
    d.set_defaults(fn=cmd_doctor)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not getattr(args, "fn", None):
        build_parser().print_help()
        return RC_REFUSED
    if getattr(args, "channel", None) is None and args.cmd in ("init", "queue", "doctor"):
        print("--channel is required (MomentAnnotation/<uuid>)", file=sys.stderr)
        return RC_REFUSED
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
