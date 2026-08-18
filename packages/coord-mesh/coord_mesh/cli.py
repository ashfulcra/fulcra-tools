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
    # `--reports "   "` is a typo, not a request for a prefix named whitespace.
    # It used to reach file_data_type() and escape as a bare ValueError with a
    # traceback (codex-coder, r5 secondary): a crash is not one of this CLI's
    # three answers, and a caller cannot tell a crash from a refusal.
    reports = (args.reports or "").strip()
    if args.reports and not reports:
        print("mesh init REFUSED: --reports is whitespace only. Pass a real "
              "path (e.g. reports/) or omit the flag to mint a channel-only "
              "share.", file=sys.stderr)
        return RC_REFUSED

    # THE CAPABILITY FENCE (coord-boss's r6 shape, item 2). A client older than
    # 0.1.40 cannot express a file grant by ANY path — no `--file` option, and
    # `--data-type file:/reports/` refused by its own validation. Minting the
    # channel-only share anyway would be a silent narrowing: the operator asked
    # for reports and would be told "granted" about something smaller.
    if reports:
        try:
            capable = transport.supports_file_grants()
        except transport.CapabilityUnknown as exc:
            print(f"mesh init UNKNOWN: could not establish what the installed "
                  f"fulcra-api can do ({exc}) — refusing rather than guessing, "
                  f"because a file grant is impossible below "
                  f"{transport.MIN_FILE_GRANT_VERSION} and a share minted "
                  f"without it would report reports/ as granted when it is not",
                  file=sys.stderr)
            return RC_UNKNOWN
        if not capable:
            print(f"mesh init REFUSED: {transport.which_client()} cannot express "
                  f"a file grant, so {reports!r} could not be granted by any "
                  f"path. This needs >= {transport.MIN_FILE_GRANT_VERSION}; run "
                  f"`uv tool install --force fulcra-api=="
                  f"{transport.MIN_FILE_GRANT_VERSION}` (watch it — an "
                  f"unattended client upgrade is hard to roll back) and re-run. "
                  f"Refusing rather than minting a channel-only share, which "
                  f"would report success for less than you asked for.",
                  file=sys.stderr)
            return RC_REFUSED

    try:
        rc, out, err = transport.share_create(
            name=args.name, data_type=_channel(args), user_id=args.peer,
            file_prefix=reports or None)
    except safety.SafetyViolation as exc:
        print(f"mesh init REFUSED: {exc}", file=sys.stderr)
        return RC_REFUSED
    except ValueError as exc:
        # file_data_type() rejects a prefix that normalizes to nothing. Caught
        # here so the CLI answers with a code instead of a traceback.
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
    # Match the share we actually minted — peer AND data type AND name AND, when
    # a reports prefix was asked for, its `file:` data type too.
    # A uid-only match verifies an UNRELATED existing share: the first mesh peer
    # already holds a 2024 share-all from the operator, so uid-only would pass
    # before the mesh created anything (codex-coder, r2 on 3c1c78d).
    granted = transport.find_share(back.rows, peer_uid=args.peer,
                                   data_type=_channel(args), name=args.name,
                                   file_prefix=reports or None)
    if not granted:
        # Two different failures wear the same "no match" here, and telling the
        # operator which one they have is the difference between a re-run and a
        # platform bug report. Re-match WITHOUT the prefix to find out.
        partial = transport.find_share(back.rows, peer_uid=args.peer,
                                       data_type=_channel(args), name=args.name)
        if partial is not None and reports:
            want = transport.file_data_type(reports)
            print(f"mesh init: share {args.name!r} exists and grants "
                  f"{_channel(args)} to {args.peer}, but {want!r} is NOT among "
                  f"its data types {list(partial.get('fulcra_data_types') or [])} "
                  "— the CHANNEL is granted, the REPORTS PREFIX is not "
                  "confirmed. Treat ptr docs as unreachable until you verify "
                  "from the peer side.", file=sys.stderr)
            return RC_UNKNOWN
        print(f"mesh init: create returned 0 but no share named {args.name!r} "
              f"granting {_channel(args)} to {args.peer} is in list-outgoing — "
              "treat as NOT granted (a pre-existing share to the same uid is "
              "not evidence that ours exists)", file=sys.stderr)
        return RC_UNKNOWN
    # Say exactly what the read-back proves and no more. r3 said the reports
    # path was unobservable from `share list-outgoing`; the live run disproved
    # that — a file grant IS a data-type id (`file:/reports/`, measured on a
    # real row), so it is in the same `fulcra_data_types` list and is now
    # verified rather than disclaimed.
    detail = f"data type read-back verified in share {args.name!r}"
    if reports:
        detail += f"; reports prefix verified as {transport.file_data_type(reports)!r}"
    print(f"mesh init: granted {_channel(args)} -> {args.peer} ({detail})")
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
    payload = envelope.encode(note)
    if args.dry_run:
        # Explicit, opt-in, and NOT reported as sent.
        print(payload)
        print("mesh send: DRY RUN — nothing written", file=sys.stderr)
        return RC_UNKNOWN

    # Snapshot the channel BEFORE writing. Matching on slug+to_user alone
    # verifies a STALE same-slug event from an earlier run — a re-send of the
    # same slug would "verify" instantly without writing anything
    # (codex-coder, r3 on 472a8c6). Comparing against the pre-write id set
    # identifies the NEW record exactly, with no dependence on clock skew
    # between this process and the platform.
    before = transport.get_records(_channel(args), args.verify_window)
    if before.unknown:
        print(f"mesh send: pre-write snapshot UNKNOWN ({before.detail}) — "
              "refusing to write, because without it a read-back cannot tell a "
              "new event from an old one", file=sys.stderr)
        return RC_UNKNOWN
    seen_before = {wire.record_id(r) for r in before.rows if wire.record_id(r)}

    try:
        rc, out, err = transport.record(_channel(args), payload,
                                        source=args.source)
    except transport.TransportError as exc:
        print(f"mesh send UNKNOWN: {exc}", file=sys.stderr)
        return RC_UNKNOWN
    if rc != 0:
        print(f"mesh send failed: {(err or out).strip()[:400]}", file=sys.stderr)
        return RC_UNKNOWN

    # Read-back verify. A write that returned 0 is a CLAIM; the record appearing
    # in my own channel is the evidence. "A green command is not a delivered
    # message" — the principle this verb was violating by returning rc0 for a
    # write it never attempted (codex-coder, r2 on 3c1c78d).
    back = transport.get_records(_channel(args), args.verify_window)
    if back.unknown:
        print(f"mesh send: wrote, but read-back UNKNOWN ({back.detail}) — "
              "cannot confirm the event landed", file=sys.stderr)
        return RC_UNKNOWN
    for row in back.rows:
        rid = wire.record_id(row)
        if not rid or rid in seen_before:
            continue          # pre-existing: not evidence of THIS write
        got = envelope.parse(wire.note_text(row))
        if got and got.get("slug") == note["slug"] and got.get("to_user") == note["to_user"]:
            print(f"mesh send: {note['kind']} {note['slug']} -> {note['to_user']} "
                  f"(read-back verified, new record {rid})")
            return RC_OK
    print("mesh send: write returned 0 but NO NEW record matching this event is "
          "in my channel on read-back — treat as NOT sent", file=sys.stderr)
    return RC_UNKNOWN


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

        # ROW ORDER IS ASCENDING — oldest first. Measured on a 203-row, 12-hour
        # read of the live channel, strictly ascending. The first version of
        # this loop did `newest = newest or rid`, taking rows[0], and so
        # anchored the cursor to the OLDEST row in the window. Two symptoms,
        # and the quiet one was the dangerous one: every later read broke at
        # rows[0] (the cursor WAS rows[0]) and printed "0 event(s)" while real
        # addressed events sat unshown below it; then, when that row aged out
        # of the window, nothing matched and the whole window replayed. The
        # replay is what got noticed; the silence is what it cost.
        ids = [wire.record_id(row) for row in res.rows]
        if any(rid is None for rid in ids):
            # Unidentifiable row: we cannot say where we are in the stream, so
            # we do not claim a position. Show nothing rather than guess.
            print("  row with no id — position UNKNOWN, not advancing cursor",
                  file=sys.stderr)
            degraded.append(uid)
            continue

        # ORDER IS PROVEN PER READ, NOT ASSUMED (codex-coder on 051109f). The
        # previous commit fixed a cursor anchored to the wrong end of the window
        # — and then took ONE measured ascending response as a permanent
        # transport contract, which is the same unverified-assumption defect
        # wearing the opposite sign. If a response ever comes back descending or
        # disordered, `ids[-1]` is not the newest row, and the silent-loss half
        # of that bug returns. So: every row must carry a parseable time and the
        # sequence must be monotonic, or this peer is UNKNOWN.
        if res.rows and not wire.ascending(res.rows):
            print(f"  peer {uid}: rows are not provably in ascending "
                  f"recorded_at order (missing or unparseable timestamps, or "
                  f"out-of-order rows) — position UNKNOWN, not advancing "
                  f"cursor and not claiming this window was read",
                  file=sys.stderr)
            degraded.append(uid)
            continue

        start = 0
        if cursor:
            if cursor in ids:
                start = ids.index(cursor) + 1
            else:
                # At-least-once re-delivery is LEGAL, and this is it. Saying so
                # is not optional: a replay nobody can explain costs an
                # investigation every time it happens.
                print(f"  peer {uid}: cursor {cursor} is not in the {args.window} "
                      f"window (aged out) — replaying the window; re-delivery is "
                      f"expected under at-least-once, handle events idempotently",
                      file=sys.stderr)

        for row in res.rows[start:]:
            note = envelope.parse(wire.note_text(row))
            if not note or not envelope.addressed_to(note, args.me):
                continue
            shown += 1
            print(f"  [{note.get('pri')}] {note.get('kind')} {note.get('slug')} "
                  f"from {wire.sender(row) or 'UNKNOWN-author'} "
                  f"ptr={note.get('ptr') or '-'}")

        # The LAST row is the newest one; that is the position we have reached.
        if ids and not args.no_advance and uid not in degraded:
            peers.set_cursor(reg, space, uid, ids[-1])
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
    s.add_argument("--source", default="coord-mesh",
                   help="writer identity stamped on the record")
    s.add_argument("--verify-window", default="1 hour",
                   help="window used for the read-back check")
    s.add_argument("--dry-run", action="store_true",
                   help="print the payload and write NOTHING (exits UNKNOWN, "
                        "never success — a printed envelope is not a sent one)")
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
    if getattr(args, "channel", None) is None and args.cmd in ("init", "send", "queue", "doctor"):
        print("--channel is required (MomentAnnotation/<uuid>)", file=sys.stderr)
        return RC_REFUSED
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
