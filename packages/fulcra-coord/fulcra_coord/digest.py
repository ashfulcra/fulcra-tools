"""Operator situational-awareness output for fulcra-coord: digest + fleet health.

The PUSH surface (the twice-daily ``digest`` written to the timeline, its render +
window + per-window dedup-marker claim + the launchd installer) and the PULL surface
(the ``health`` fleet dashboard: load per-host health records, assess infra health,
and the bus-global digest-emit freshness). Both reuse the pure judgment in
views.assess_infra_health and the summaries aggregate; they read + annotate, never
mutating task state.

Extracted from cli.py behind stable re-exports; depends only on lower layers
(remote / views / identity / digest_schedule / annotations + the io summaries loader
and the output / timeutil leaf utils) and never imports cli, so the split has no
cycle. _build_health_record (the reconcile-side health WRITE) and the inbox-coupled
notify path deliberately stay in cli; cmd_doctor reaches _assess_fleet here via the
re-export.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import remote, views, identity, digest_schedule, remote_root
from . import annotations as lifecycle_annotations
from .io import _load_task_summaries
from .output import info as _info, print_json as _print_json
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


def _assess_fleet(*, now: datetime, backend: Optional[list[str]] = None) -> dict:
    """Load all health inputs (records + bus markers) and run the pure judgment.
    Shared by cmd_health, the doctor fold, and the digest (which passes the result
    into the pure builder). Best-effort reads — a missing marker leaves its field
    None, never an exception into the caller."""
    recs = _load_health_records(backend=backend)
    digest_emit = _freshest_digest_emit(backend=backend)
    retention_last_run = None
    try:
        rmark = remote.download_json(remote.retention_marker_path(now), backend=backend)
        if isinstance(rmark, dict):
            retention_last_run = rmark.get("at") or rmark.get("date")
    except Exception:
        retention_last_run = None
    return views.assess_infra_health(
        recs, now=now, digest_last_emit=digest_emit,
        retention_last_run=retention_last_run, task_count=len(recs) or None)


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


def _claim_digest_marker(window: str, now: datetime, *,
                         backend: Optional[list[str]] = None) -> bool:
    """Any-agent first-writer-wins claim for one window's digest. Returns True
    when THIS caller won the claim and should write the digest; False to skip.

    Protocol (spec §5): download the marker — if it exists, another agent already
    wrote this window, so NO-OP (return False). If absent, upload a marker
    stamping this agent + timestamp; on a successful upload, grant the claim.

    RACE (accepted): Fulcra Files has no compare-and-swap, so two agents firing
    in the same ~second can both see 'absent' and both write → a rare double
    digest. Harmless on a timeline; logged, not prevented (a single-owner schedule
    would remove the race but add a single point of failure — rejected per the
    any-agent decision). MARKER-CLAIM FAILURE (download or upload error) → return
    False (skip) so a transient bus error never risks a double; the next window
    retries. Never raises — best-effort like the rest of the digest path."""
    try:
        path = _digest_marker_path(window, now)
        existing = remote.download_json(path, backend=backend)
        if existing is not None:
            return False  # already claimed this window
        marker = {
            "schema": "fulcra.coordination.digest_marker.v1",
            "window": window,
            "date": now.astimezone(timezone.utc).strftime("%Y-%m-%d"),
            "by": identity.resolve_agent(),
            "claimed_at": _iso_z(now),
        }
        return bool(remote.upload_json(marker, path, backend=backend))
    except Exception:
        # Best-effort: a marker error must never raise into a scheduled tick, and
        # must skip (not write) so we never risk a double on an uncertain claim.
        return False


def cmd_digest(args: Any, backend: Optional[list[str]] = None) -> int:
    """Write the operator's situational-awareness digest to the Fulcra timeline.

    Loads the compact summaries aggregate + the presence roster (the same reads
    needs-me / presence use — one download each, no body fetch), computes the
    window's ``since``/``now``, builds the four-block digest, and renders it to a
    timeline (name, note). ``--dry-run`` prints the rendered text and writes
    NOTHING. ``--format json`` prints the structured digest (for tooling/tests).
    Otherwise it claims the per-window dedup marker (first writer wins; others
    no-op) and emits the moment on the ``Agent Tasks — Digest`` track.

    BEST-EFFORT end to end: a failed marker claim or a failed emit is logged and
    returns 0 — a scheduled tick must never error out."""
    window = getattr(args, "window", None) or "ondemand"
    out_format = getattr(args, "format", "table")
    dry_run = getattr(args, "dry_run", False)
    human = getattr(args, "human", None) or identity.resolve_human()

    now = datetime.now(timezone.utc)
    since = _digest_window_since(window, now)

    summaries = _load_task_summaries(backend=backend)
    agg = remote.download_json(remote.presence_view_path(), backend=backend)
    presence = (agg or {}).get("agents", []) if agg else []

    # v1 push surface: compute the fleet assessment once (best-effort; a read
    # failure leaves infra=None and the digest renders without the line) and pass
    # it into the pure builder so the builder stays I/O-free.
    try:
        infra = _assess_fleet(now=now, backend=backend)
    except Exception:
        infra = None

    digest = views.build_operator_digest(
        summaries, presence, human=human, now=now, since=since, infra=infra)

    if out_format == "json":
        _print_json(digest)
        return 0

    name, note = _render_digest(digest, window=window)

    if dry_run:
        _info(f"[dry-run] {name}")
        _info(note or "(nothing to report)")
        return 0

    # Any-agent dedup: claim the per-window marker first; if another agent
    # already wrote this window (or the claim errored), skip — never risk a
    # double, and never raise into a scheduled tick.
    if not _claim_digest_marker(window, now, backend=backend):
        _info(f"Digest for {window} already written (or marker claim failed) — skipping.")
        return 0

    wrote = False
    try:
        wrote = lifecycle_annotations.emit_digest_annotation(
            name=name, note=note, window=window,
            agent=identity.resolve_agent(), backend=backend)
    except Exception:
        wrote = False
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
