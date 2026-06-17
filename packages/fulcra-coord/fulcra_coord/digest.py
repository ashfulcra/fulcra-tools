"""Operator situational-awareness output for fulcra-coord: digest + fleet health.

The PUSH surface (the twice-daily ``digest`` written to the timeline, its render +
window + per-window dedup marker recorded after a confirmed emit + the launchd installer) and the PULL surface
(the ``health`` fleet dashboard: load per-host health records, assess infra health,
and the bus-global digest-emit freshness). Both reuse the pure judgment in
views.assess_infra_health and the summaries aggregate; they read + annotate, never
mutating task state.

Extracted from cli.py behind stable re-exports; depends only on lower layers
(remote / views / identity / digest_schedule / annotations / the pure loops folds +
the io summaries loader and the output / timeutil leaf utils) and never imports
cli, so the split has no cycle. _build_health_record (the reconcile-side health WRITE) and the inbox-coupled
notify path deliberately stay in cli; cmd_doctor reaches _assess_fleet here via the
re-export.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import remote, views, identity, digest_schedule
from . import annotations as lifecycle_annotations
from . import loops as _loops
from .loop_snapshots import overlay_open_records_from_tasks
# Shared records-sweep + bounded-evidence-probe pair (_loop_board_summary).
# loop_ops is BELOW this module (it never imports digest/cli) — the single
# home for the load-bearing top-level-shard filter all three loop surfaces need.
from .loop_ops import load_loop_records, evidence_ids_for
from .io import _load_task_summaries
from .output import info as _info, print_json as _print_json
from .presence import _load_presence_agents
from .timeutil import iso_z as _iso_z

_DIGEST_BLOCK_CAP = 8


def _load_health_records(*, backend: Optional[list[str]] = None) -> list[dict]:
    """List health/*.json and download each, tolerating a missing/garbage file
    (None is skipped) and a non-json listing entry. Best-effort: a failed list
    yields []. Per the 0.8.x hardening, never raises into a caller."""
    return [rec for _, rec in remote.list_json(remote.health_prefix(), backend=backend)]


def _freshest_digest_emit(*, backend: Optional[list[str]] = None):
    """The bus-GLOBAL digest_last_emit: the freshest YYYY-MM-DD embedded in a
    digest/markers/<date>-<window>.json path. None if no marker. Dated from the
    PATH (no download) via views._MARKER_DATE_RE — the same date model the marker
    prune uses. Best-effort."""
    best = None
    try:
        for path in remote.list_files(remote.digest_markers_prefix(), backend=backend):
            m = views._MARKER_DATE_RE.search(path)
            if m and (best is None or m.group(1) > best):
                best = m.group(1)
    except Exception:
        pass
    return best


def _assess_fleet(*, now: datetime, backend: Optional[list[str]] = None,
                  summaries: Optional[list[dict]] = None) -> dict:
    """Load all health inputs (records + bus markers) and run the pure judgment.
    Shared by cmd_health, the doctor fold, and the digest (which passes the result
    into the pure builder). Best-effort reads — a missing marker leaves its field
    None, never an exception into the caller. ``summaries`` threads an
    already-loaded summary set through for the task count (perf loop-2 #5:
    cmd_digest loads the aggregate anyway — re-downloading it here just to
    ``len()`` it cost one spawn per digest); None (cmd_health, the doctor)
    keeps the self-load."""
    recs = _load_health_records(backend=backend)
    digest_emit = _freshest_digest_emit(backend=backend)
    retention_last_run = None
    try:
        rmark = remote.download_json(remote.retention_marker_path(now), backend=backend)
        if isinstance(rmark, dict):
            retention_last_run = rmark.get("at") or rmark.get("date")
    except Exception:
        retention_last_run = None
    task_count = None
    try:
        task_count = len(summaries if summaries is not None
                         else _load_task_summaries(backend=backend))
    except Exception:
        task_count = None
    return views.assess_infra_health(
        recs, now=now, digest_last_emit=digest_emit,
        retention_last_run=retention_last_run, task_count=task_count)


def cmd_health(args: Any, backend: Optional[list[str]] = None) -> int:
    """Fleet coordination-health dashboard: load health/*.json, judge via
    views.assess_infra_health (reconcile-staleness gating only, v1), print per
    host status + reasons + metrics and the bus block. --format json for tooling.
    Read-only; tolerant of a missing/garbage record (the 0.8.x hardening)."""
    out_format = getattr(args, "format", "table")
    now = datetime.now(timezone.utc)
    result = _assess_fleet(now=now, backend=backend)

    if out_format == "json":
        _print_json(result)
        return 0

    worst = result["worst_status"]
    _info(f"\nfleet health: {worst}")
    if not result["hosts"]:
        _info("  (no hosts reporting health records yet)")
    for h in result["hosts"]:
        reasons = ("; ".join(h["reasons"])) if h["reasons"] else "ok"
        _info(f"  [{h['status']}] {h['host']} — {reasons}")
        m = h["metrics"]
        _info(f"      reconcile_at={m.get('reconcile_at')} "
              f"duration_s={m.get('duration_s')} tasks={m.get('tasks_loaded')} "
              f"views={m.get('views_refreshed')} backlog={m.get('repair_backlog')}")
    b = result["bus"]
    miss = " (MISSED window)" if b["missed_digest_window"] else ""
    _info(f"  bus: digest_last_emit={b['digest_last_emit']}{miss} "
          f"retention_last_run={b['retention_last_run']} task_count={b['task_count']}")
    return 0


def _loop_board_summary(*, now: datetime,
                        backend: Optional[list[str]] = None,
                        summaries: Optional[list[dict[str, Any]]] = None) -> dict:
    """Coordination-loop counts for the digest's one-line section — the SAME
    pure ``loops.loop_board`` fold the health record uses, over the digest's
    own agent identity (the reader the "awaiting-you" count belongs to).

    Records load + bounded evidence probes live in
    ``loop_ops.load_loop_records`` / ``loop_ops.evidence_ids_for`` (the single
    home for the load-bearing top-level-shard filter; loop_ops is below this
    module, which never imports cli — the split invariant).

    May raise on a broken records read (load_loop_records is deliberately not
    best-effort) — the CALLER (cmd_digest) wraps the whole call so any failure
    simply omits the section, leaving the digest unchanged (the infra-line
    discipline)."""
    records = overlay_open_records_from_tasks(
        load_loop_records(backend=backend),
        backend=backend,
        tasks=summaries if summaries is not None
        else _load_task_summaries(backend=backend),
        fetch_missing=True,
    )
    me = identity.resolve_agent()
    evidence_ids = evidence_ids_for(me, records, now=now, backend=backend)
    board = _loops.loop_board(me, records, now=now, evidence_ids=evidence_ids)
    aw_me = board["awaiting_me"]
    aw_others = board["awaiting_others"]
    return {
        "open_loops": len(aw_me) + len(aw_others),
        "overdue": sum(1 for x in aw_others if x.get("overdue")),
        "awaiting_me": len(aw_me),
        "out_of_band": sum(1 for x in aw_others if x.get("out_of_band")),
    }


def _digest_lines(items: list[dict[str, Any]], fmt) -> list[str]:
    """Render up to _DIGEST_BLOCK_CAP items via ``fmt`` (item -> str), appending a
    '+N more' tail when the list is longer. Bounds every block identically."""
    head = items[:_DIGEST_BLOCK_CAP]
    lines = [fmt(s) for s in head]
    extra = len(items) - len(head)
    if extra > 0:
        lines.append(f"  …and {extra} more")
    return lines


def _render_digest(digest: dict[str, Any], *, window: str) -> tuple[str, str]:
    """Render the structured digest into a timeline (name, note). Pure, no I/O.

    ``name`` is the concise timeline label carrying the headline counts
    (``Agent digest — <window> (N on you, M upcoming)``); ``note`` is the body —
    compact markdown-ish text, one block per non-empty section, each line who /
    what / when. Empty blocks are SKIPPED entirely (no empty headers). Long lists
    are capped via ``_digest_lines`` ('+N more'). Every field is read with
    ``.get`` defaults so a summary missing an optional key renders instead of
    raising — this feeds a best-effort scheduled writer that must never crash."""
    blocked = digest.get("blocked_on_you") or []
    upcoming = digest.get("upcoming") or []
    per_agent = digest.get("per_agent") or []
    stale = digest.get("stale") or []

    name = (f"Agent digest — {window} "
            f"({len(blocked)} on you, {len(upcoming)} upcoming)")

    sections: list[str] = []

    if blocked:
        def _b(s):
            ask = (s.get("blocked_on") or s.get("next_action") or "").strip()
            who = s.get("owner_agent", "?")
            tail = f" — {ask}" if ask else ""
            return (f"  • [{(s.get('status') or '?').upper()}] "
                    f"{(s.get('title') or '')[:60]} (from {who}){tail}")
        sections.append("⛔ Blocked on you (" + str(len(blocked)) + "):")
        sections.extend(_digest_lines(blocked, _b))

    if upcoming:
        def _u(s):
            when = (s.get("not_before") or "").strip()
            return f"  • {(s.get('title') or '')[:60]}" + (f" (not before {when})" if when else "")
        sections.append("")
        sections.append("Upcoming (next 7d) (" + str(len(upcoming)) + "):")
        sections.extend(_digest_lines(upcoming, _u))

    if per_agent:
        sections.append("")
        sections.append("Per agent:")
        for a in per_agent:
            ws = ", ".join(a.get("workstreams", [])) or "(none)"
            sections.append(f"  {a.get('agent', '?')} [{a.get('liveness', '?')}] — {ws}")
            if a.get("summary"):
                sections.append(f"    on: {a['summary'][:80]}")
            done = a.get("finished_since") or []
            for s in done[:_DIGEST_BLOCK_CAP]:
                sections.append(f"    ✓ {(s.get('title') or '')[:60]}")
            if len(done) > _DIGEST_BLOCK_CAP:
                sections.append(f"    …and {len(done) - _DIGEST_BLOCK_CAP} more done")

    if stale:
        def _s(s):
            return f"  • {(s.get('title') or '')[:60]} (from {s.get('owner_agent', '?')})"
        sections.append("")
        sections.append("Stale (no update past threshold) (" + str(len(stale)) + "):")
        sections.extend(_digest_lines(stale, _s))

    # Coordination-loops line: one compact line from the pre-computed
    # loop_board counts (cmd_digest does the fold, best-effort — this renderer
    # stays pure). Skipped when the fold failed (key None/absent) AND when
    # every count is zero — a clean bus adds no section, the same
    # empty-blocks-are-skipped rule the task blocks above follow. Note
    # open_loops = awaiting_me + awaiting_others, so a zero open_loops means
    # nothing is overdue/out-of-band/awaiting either.
    loop_counts = digest.get("loops")
    if loop_counts and loop_counts.get("open_loops"):
        sections.append("")
        sections.append(
            f"Coordination loops: {loop_counts.get('open_loops', 0)} open "
            f"· {loop_counts.get('overdue', 0)} overdue "
            f"· {loop_counts.get('out_of_band', 0)} out-of-band "
            f"· awaiting-you: {loop_counts.get('awaiting_me', 0)}")

    # Infra line (v1 PUSH surface): one compact line from a pre-computed
    # assess_infra_health dict. The digest scheduler runs independently of
    # reconcile, so this reports a broken reconcile even on a single-host box.
    # All-healthy -> a brief affirmative ("N hosts healthy"); any unhealthy host
    # or a missed digest window -> "infra: ⚠ host reason · …".
    infra = digest.get("infra")
    if infra:
        hosts = infra.get("hosts") or []
        worst = infra.get("worst_status", "healthy")
        if worst == "healthy" and not infra.get("bus", {}).get("missed_digest_window"):
            healthy_n = sum(1 for h in hosts if h.get("status") == "healthy")
            if healthy_n:
                sections.append("")
                sections.append(f"infra: {healthy_n} hosts healthy")
        else:
            bad = [h for h in hosts if h.get("status") in ("degraded", "outage", "not_reporting")]
            parts = []
            for h in bad:
                reason = (h.get("reasons") or ["?"])[0]
                parts.append(f"{h.get('host', '?')} {reason}")
            if infra.get("bus", {}).get("missed_digest_window"):
                parts.append("digest window missed")
            sections.append("")
            sections.append("infra: ⚠ " + " · ".join(parts) if parts
                            else f"infra: {worst}")

    note = "\n".join(sections).strip()
    return name, note


def _digest_window_since(window: str, now: datetime) -> datetime:
    """The lookback boundary for a digest window (returns a tz-aware UTC datetime).

    morning → since the previous evening (~last 14h, so an overnight run still
    reports yesterday-evening's work); evening → since this morning (~last 10h);
    any other value (on-demand) → last 12h. Approximations on purpose: the digest
    is a human-paced glance, not an exact ledger, and the per_agent completion
    filter is a >= compare against this instant. Always parsed datetimes."""
    hours = {"morning": 14, "evening": 10}.get(window, 12)
    return now - timedelta(hours=hours)


def _digest_marker_path(window: str, now: datetime) -> str:
    """Files-bus path of the per-window digest dedup marker:
    ``<remote_root>/digest/markers/<YYYY-MM-DD>-<window>.json``. Keyed by the UTC
    DATE + window so morning and evening each get one marker per day, and any
    agent on any machine claims the SAME path (the whole point of the any-agent
    guard). ``now`` is injected for deterministic tests."""
    from . import remote_root
    day = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
    return f"{remote_root()}/digest/markers/{day}-{window}.json"


def _digest_marker_present(window: str, now: datetime, *,
                           backend: Optional[list[str]] = None) -> bool:
    """True iff this window's digest marker already exists (i.e. a prior tick
    CONFIRMED-wrote it — see _record_digest_marker). Absent OR unreadable →
    False (proceed): a transient read error self-heals on the next tick, and a
    re-emit is a harmless timeline double, never a silent daily drop. Keyed by
    UTC date + window so any agent on any machine checks the SAME path."""
    try:
        path = _digest_marker_path(window, now)
        return remote.download_json(path, backend=backend) is not None
    except Exception:
        return False


def _record_digest_marker(window: str, now: datetime, *,
                          backend: Optional[list[str]] = None) -> bool:
    """Record that this window's digest was written. Called ONLY after a
    CONFIRMED emit, so a failed/short-circuited emit leaves no marker and the
    next tick retries instead of dropping the window. Best-effort: a record
    failure just means a later tick may re-emit (a harmless double). Returns
    True on a successful upload. Never raises."""
    try:
        marker = {
            "schema": "fulcra.coordination.digest_marker.v1",
            "window": window,
            "date": now.astimezone(timezone.utc).strftime("%Y-%m-%d"),
            "by": identity.resolve_agent(),
            "claimed_at": _iso_z(now),  # field name kept for back-compat with existing markers
        }
        path = _digest_marker_path(window, now)
        return bool(remote.upload_json(marker, path, backend=backend))
    except Exception:
        return False


def cmd_digest(args: Any, backend: Optional[list[str]] = None) -> int:
    """Write the operator's situational-awareness digest to the Fulcra timeline.

    Loads the compact summaries aggregate + the presence roster (the same reads
    needs-me / presence use — one download each, no body fetch), computes the
    window's ``since``/``now``, builds the four-block digest, and renders it to a
    timeline (name, note). ``--dry-run`` prints the rendered text and writes
    NOTHING. ``--format json`` prints the structured digest (for tooling/tests).
    Otherwise it checks the per-window dedup marker (skipping if a prior tick
    already CONFIRMED-wrote this window), emits the moment on the
    ``Agent Tasks — Digest`` track, and records the marker ONLY after a confirmed
    emit — so a failed emit leaves no marker and the next tick retries instead of
    silently dropping the window for the rest of the day.

    BEST-EFFORT end to end: a failed emit is logged and returns 0 — a scheduled
    tick must never error out."""
    window = getattr(args, "window", None) or "ondemand"
    out_format = getattr(args, "format", "table")
    dry_run = getattr(args, "dry_run", False)
    human = getattr(args, "human", None) or identity.resolve_human()

    now = datetime.now(timezone.utc)
    since = _digest_window_since(window, now)

    summaries = _load_task_summaries(backend=backend)
    presence = _load_presence_agents(backend=backend)

    # v1 push surface: compute the fleet assessment once (best-effort; a read
    # failure leaves infra=None and the digest renders without the line) and pass
    # it into the pure builder so the builder stays I/O-free. The summaries
    # loaded above ride along for the task count (perf loop-2 #5 — no re-load).
    try:
        infra = _assess_fleet(now=now, backend=backend, summaries=summaries)
    except Exception:
        infra = None

    digest = views.build_operator_digest(
        summaries, presence, human=human, now=now, since=since, infra=infra)

    # Coordination-loops counts: the same fold the health record
    # uses, over THIS digest's agent identity. FULLY best-effort, mirroring
    # the infra line above — any failure leaves digest["loops"] None and the
    # rendered digest unchanged (a scheduled tick must never crash on an
    # optional section). Stamped onto the built digest (not threaded through
    # the pure builder) so views.build_operator_digest's surface is untouched.
    try:
        digest["loops"] = _loop_board_summary(
            now=now, backend=backend, summaries=summaries)
    except Exception:
        digest["loops"] = None

    if out_format == "json":
        _print_json(digest)
        return 0

    name, note = _render_digest(digest, window=window)

    if dry_run:
        _info(f"[dry-run] {name}")
        _info(note or "(nothing to report)")
        return 0

    # Any-agent dedup: a marker exists only after a CONFIRMED write, so skip if
    # this window already landed. Recording is DEFERRED to after a successful
    # emit (below) — claiming up-front permanently dropped the window on a
    # transient emit failure (the marker blocked every retry).
    if _digest_marker_present(window, now, backend=backend):
        _info(f"Digest for {window} already written — skipping.")
        return 0

    wrote = False
    try:
        wrote = lifecycle_annotations.emit_digest_annotation(
            name=name, note=note, window=window,
            agent=identity.resolve_agent(), backend=backend)
    except Exception:
        wrote = False
    if wrote:
        _record_digest_marker(window, now, backend=backend)
    _info(f"Digest ({window}): {'written' if wrote else 'not written (annotations off or error)'}.")
    return 0


def cmd_install_digest(args: Any, backend: Optional[list[str]] = None) -> int:
    """Install/uninstall the twice-daily scheduled ``fulcra-coord digest`` jobs.

    Calendar-scheduled (08:00 morning + 18:00 evening), unlike the interval
    heartbeat/listener: launchd StartCalendarInterval on macOS, fixed cron lines
    elsewhere. Installable on every machine — the any-agent dedup marker collapses
    concurrent ticks to one digest per window. Mirrors install-heartbeat's CLI
    contract (dry-run prints the plan, surgical uninstall)."""
    plan = digest_schedule.install_digest(
        uninstall=args.uninstall,
        dry_run=args.dry_run,
        target_dir=getattr(args, "target_dir", None),
        logs_dir=getattr(args, "logs_dir", None),
    )
    if args.dry_run:
        _info(f"[dry-run] Digest mechanism: {plan['mechanism']}")
        _info(f"[dry-run] Scheduled command: {plan['cli_command']} digest "
              f"--window {{morning@08:00, evening@18:00}}")
        for w in plan.get("writes", []):
            _info(f"  + would write {w}")
        for r in plan.get("removes", []):
            _info(f"  - would remove {r}")
        return 0
    if args.uninstall:
        _info(f"Removed fulcra-coord digest schedule ({plan['mechanism']}).")
        return 0
    _info(f"Installed fulcra-coord digest schedule ({plan['mechanism']}) — "
          f"morning 08:00 + evening 18:00.")
    for w in plan.get("writes", []):
        _info(f"  + {w}")
    if plan["mechanism"] == "launchd":
        for w in plan.get("writes", []):
            _info(f"Load it now (or at next login): launchctl load -w {w}")
    else:
        _info("Apply it now: crontab " + (plan["writes"][0] if plan["writes"] else ""))
    return 0
