"""coord-engine dropped-threads command — the `threads` fold (fulcra-agent).

Extracted verbatim from ``cli.py`` (behavior-preserving module split): the bus
adapter for ``threads.classify`` (candidate rows + ash-activity attribution +
intent windows) and ``cmd_threads``. The window/budget knobs parse through the
one shared ``config.env_float`` policy (``_threads_window`` carries the flag>env
delegator form). Shared cli-level helpers (``_now``/``_iso`` and the task/ack/
responses path helpers, ``_load_rows_status``) are reached through the ``cli``
module so ``monkeypatch.setattr(cli, "_now", …)`` still steers them and there is
no module-load cycle. Dispatch stays wired in ``cli.build_parser``; every public
name here (incl. the ``DEFAULT_THREADS_*`` knobs) is re-exported from ``cli``.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Optional

from . import config, directives, jsonutil, okf, threads as threads_mod
from . import cli
from .budget import Deadline


#: Aggregate deadline (seconds) for the `threads` fold's per-candidate shard/doc
#: reads (ash-activity attribution + intent_by). Bounds the same slow-bleed class
#: the overlay/briefing budgets do: N principal candidates x per-doc transport
#: timeout under a degraded transport. On breach the fold STOPS and emits a
#: `threads-degraded` row (never silence, never crash).
DEFAULT_THREADS_FOLD_BUDGET = 30.0

#: `threads` window defaults (spec §Surface 1). CLI flags override these, and env
#: `COORD_THREADS_SILENCE_DAYS` / `COORD_THREADS_INTENT_GRACE_HOURS` override the
#: defaults when no flag is passed (flag > env > default).
DEFAULT_THREADS_SILENCE_DAYS = 3.0
DEFAULT_THREADS_INTENT_GRACE_HOURS = 48.0


# --- dropped threads (fulcra-agent — 2026-07-11-dropped-threads) -------------
#
# The bus ADAPTER for `threads.classify`. Reversibility (Ash requirement): the
# pure fold consumes a NEUTRAL row shape; this adapter is the ONLY place bus
# specifics live, so a GitHub/fulcra-pm source later is a new adapter emitting the
# same rows, not a rewrite. v1 reads ONE source — coord bus items on the team
# where the principal is involved (assignee/owner, or an intent:/blocked-on: tag).

def _threads_fold_budget() -> float:
    """Aggregate deadline (seconds) for the `threads` fold's per-candidate reads.
    Env ``COORD_THREADS_FOLD_BUDGET`` (see the DEFAULT_THREADS_FOLD_BUDGET
    rationale); on breach the fold emits a `threads-degraded` row."""
    return config.env_float("COORD_THREADS_FOLD_BUDGET", DEFAULT_THREADS_FOLD_BUDGET)


def _threads_window(flag: Optional[float], env: str, default: float) -> float:
    """Resolve a `threads` window: flag > env > default, through the shared
    positive-finite parser (``config.env_float``) — the ``override`` carries the
    flag>env precedence and NaN/inf/≤0 fall back to the default, so these knobs
    obey the same env-contract the README states for every numeric knob (no more
    `inf` leaking a window)."""
    return config.env_float(env, default, override=flag)


def _threads_is_principal(row: dict[str, Any], principal: str, tags: list[str]) -> bool:
    return (row.get("assignee") == principal or row.get("owner") == principal
            or f"intent:{principal}" in tags or f"blocked-on:{principal}" in tags)


def _threads_blocked_signal(row: dict[str, Any], principal: str,
                            tags: list[str]) -> Optional[str]:
    """Which blocked-on-principal signal fires (spec mode 2), or None. Order is
    just for the human evidence label — any one is sufficient."""
    if row.get("assignee") == principal:
        return f"assignee: {principal}"
    if f"blocked-on:{principal}" in tags:
        return f"blocked-on:{principal} tag"
    if str(row.get("blocked_on") or "") == principal:
        return f"blocked_on: {principal}"
    if "needs:human" in tags and (row.get("assignee") == principal
                                  or row.get("owner") == principal):
        return "needs:human block"
    return None


def _threads_ash_activity(transport: Any, team: str, slug: str, principal: str,
                          response_slugs: set[str], row_ts: Optional[str],
                          ) -> tuple[Optional[str], bool, str]:
    """Last activity ATTRIBUTABLE to the principal for a mode-1 candidate.

    Reads the principal's ack shard + any response shards the principal authored
    (only when the slug is known to have responses — the shared responses listing
    tells us that, so we never list/read for a slug that has none). Returns
    ``(ts, attributed, source)``: when no principal-attributable event is found we
    FALL BACK to the item's own ``timestamp`` (the last doc write, NOT attributable
    to the principal) and FLAG that in ``attributed=False`` — honesty over
    cleverness. A shard read that FAILS is skipped (best-effort attribution never
    crashes the fold); the aggregate budget in the caller bounds the total cost."""
    best: Optional[str] = None
    source = ""

    def _consider(ts: Any, label: str) -> None:
        nonlocal best, source
        if isinstance(ts, str) and ts and (best is None or ts > best):
            best, source = ts, label

    # ack shard: path is principal-keyed, so its existence IS the attribution.
    try:
        ack = transport.read(cli._ack_path(team, slug, principal))
    except Exception:
        ack = None
    if ack:
        fm = okf.parse_frontmatter(ack) or {}
        _consider(fm.get("timestamp"), "ack shard")

    # response shards authored by the principal (only if this slug has responses).
    if slug in response_slugs:
        prefix = cli._responses_prefix(team) + slug + "/"
        try:
            entries = transport.list_dir(prefix)
        except Exception:
            entries = []
        for e in entries:
            n = e.get("name") or ""
            if e.get("is_dir") or not n.endswith(".md"):
                continue
            try:
                raw = transport.read(prefix + n)
            except Exception:
                raw = None
            fm = okf.parse_frontmatter(raw) or {}
            if str(fm.get("agent") or "") == principal:
                _consider(fm.get("timestamp"), "response shard")

    if best is not None:
        return best, True, source
    return (row_ts, False, "item timestamp")  # fallback, flagged


def _threads_candidate_rows(
    transport: Any, team: str, principal: str,
) -> tuple[list[dict[str, Any]], bool, str]:
    """Build the NEUTRAL rows `threads.classify` consumes, from summaries + the
    freshness overlay (inherited free via ``_load_rows_status`` — fresh intents ARE
    visible), filtered to principal items. Per-candidate reads only for the signals
    summaries lack: ``intent_by`` (intent window) and ash-activity attribution.

    Returns ``(rows, ok, reason)``: ``ok`` False (with a reason) whenever the
    summaries/overlay load degraded OR the fold budget was exhausted with candidates
    still unread — the caller surfaces a ``threads-degraded`` row, never silence."""
    from . import model
    summary_rows, ok, reason = cli._load_rows_status(transport, team)

    # One shared responses-root listing: tells us (a) which slugs have a response
    # shard (the mode-3 `responded` signal) and (b) which slugs are worth reading
    # for principal-authored activity. One list_dir, not per-candidate.
    response_slugs: set[str] = set()
    try:
        for e in transport.list_dir(cli._responses_prefix(team)):
            n = e.get("name") or ""
            if e.get("is_dir") or n.endswith("/"):
                response_slugs.add(n.rstrip("/"))
    except Exception:
        # A responses-listing failure only weakens the `responded` suppression
        # signal + activity attribution — degrade visibly, keep folding.
        ok = False
        reason = reason or "responses listing unreadable"

    dl = Deadline.open(_threads_fold_budget())
    budget_hit = False
    rows: list[dict[str, Any]] = []
    for r in summary_rows:
        if dl.expired():
            # After-op discipline, checked at the TOP of each candidate: the
            # PREVIOUS candidate's reads breached the budget — detected before
            # any further reads (and a breach on the FINAL candidate, with
            # nothing left to read, never false-degrades). Stop, serve what we
            # have, degrade visibly.
            budget_hit = True
            break
        if not isinstance(r, dict):
            continue
        tags = [str(t) for t in (r.get("tags") or []) if isinstance(r.get("tags"), list)]
        if not _threads_is_principal(r, principal, tags):
            continue
        slug = str(r.get("name") or r.get("id") or "")
        is_intent = f"intent:{principal}" in tags
        followup_ref = next(
            (t.split(":", 1)[1] for t in tags if t.startswith("followed-up-by:")), None)

        # Authoritative TERMINAL status. The summaries row's status can be STALE:
        # a close (`respond`/`task done`) that lands in the SAME mtime-minute as the
        # last indexed write is never re-read by reconcile's minute-resolution
        # incremental reuse, so the row indexes 'proposed' forever while the doc is
        # 'done' (the live acceptance-ping leak — a directive done since 7/02 that
        # surfaced as mode-1 silence off the item-timestamp fallback). A terminal
        # item is NEVER a dropped thread; when summaries already reads terminal we
        # trust it (terminal is sticky), otherwise we CONFIRM against the doc's own
        # status. For a terminal row we then SKIP the ash-activity signal reads
        # (suspenders: no per-candidate reads for a row the fold will refuse) — the
        # authoritative status rides the row and threads.classify does the refusing.
        status = str(r.get("status") or "")
        declared_window = None
        ash_ts: Optional[str] = None
        attributed = True
        source = ""
        # Read the doc when we need something summaries lack: the intent window,
        # or a terminal-status confirmation for a non-terminal-indexed candidate.
        if is_intent or status not in model.TERMINAL_STATUSES:
            try:
                doc = transport.read(cli._task_path(team, slug))
            except Exception:
                doc = None
            fm = okf.parse_frontmatter(doc) if doc is not None else None
            if is_intent and fm is None:
                # Intent needs intent_by (summaries lack it). A MISSED read (raise
                # OR None) — or a doc that parses to garbage — means the window is
                # UNKNOWN: ripeness cannot be decided, so this intent is EXCLUDED
                # from this pass (silently windowing it to capture+grace would
                # manufacture a false mode-3 drop) AND the fold degrades visibly. It
                # returns, correctly windowed, once readable. Only a doc that
                # reads+parses fine and GENUINELY lacks intent_by is legitimately
                # undeclared — the capture+grace fallback below stands for that case.
                ok = False
                reason = reason or f"intent window unreadable: {slug}"
                continue
            if fm is not None:
                # The doc is the source of truth for status; a stale-proposed
                # summaries row cannot leak a closed item past the fold belt. A
                # non-intent doc that won't read falls back to the summaries status
                # (best-effort — never over-degrade a mode-1/2 candidate on a
                # transient read; no worse than before this guard existed).
                doc_status = fm.get("status")
                if doc_status:
                    status = str(doc_status)
                declared_window = fm.get("intent_by")

        terminal = status in model.TERMINAL_STATUSES
        if not is_intent and not terminal:
            # Mode-1/2 candidates: attribute ash-activity from shards. Skipped for
            # terminal rows — the fold refuses them, so their signal reads are waste.
            ash_ts, attributed, source = _threads_ash_activity(
                transport, team, slug, principal, response_slugs, r.get("timestamp"))

        rows.append({
            "id": r.get("id") or slug,
            "title": r.get("title") or slug,
            "status": status,
            "tags": tags,
            "intent": is_intent,
            "blocked_on_principal": bool(_threads_blocked_signal(r, principal, tags)),
            "blocked_signal": _threads_blocked_signal(r, principal, tags) or "",
            "parked": r.get("assignee") == directives.BACKLOG,
            "not_before": r.get("not_before"),
            "ash_activity_ts": ash_ts,
            "ash_activity_attributed": attributed,
            "ash_activity_source": source,
            "declared_window": declared_window,
            "captured_ts": r.get("timestamp"),
            "followup": {
                "status": status if is_intent else str(r.get("status") or "proposed"),
                "responded": slug in response_slugs,
                "followup_ref": followup_ref,
            },
        })

    if budget_hit:
        ok = False
        reason = "threads fold budget exhausted"
    return rows, ok, reason


def cmd_threads(args: argparse.Namespace, transport: Any) -> int:
    """`coord-engine threads <team> --for <principal>` — the dropped-threads fold.

    Windows: ``--silence-days`` (default 3, env ``COORD_THREADS_SILENCE_DAYS``),
    ``--intent-grace-hours`` (default 48, env ``COORD_THREADS_INTENT_GRACE_HOURS``);
    flag > env > default. Text is grouped by mode, oldest-first; ``--json`` emits
    ONE JSON value — an array of ``{mode, id, title, age, window, evidence}``
    objects, with a trailing ``{"type": "threads-degraded", ...}`` element when a
    source was not fully readable. Never crashes, never silently empties on
    failure, and (--json purity) never lets a notice reach stdout as prose."""
    principal = args.principal
    silence_days = _threads_window(getattr(args, "silence_days", None),
                                   "COORD_THREADS_SILENCE_DAYS",
                                   DEFAULT_THREADS_SILENCE_DAYS)
    grace_hours = _threads_window(getattr(args, "intent_grace_hours", None),
                                  "COORD_THREADS_INTENT_GRACE_HOURS",
                                  DEFAULT_THREADS_INTENT_GRACE_HOURS)
    rows, ok, reason = _threads_candidate_rows(transport, args.team, principal)
    dropped = threads_mod.classify(rows, now=cli._iso(cli._now()),
                                   silence_days=silence_days,
                                   intent_grace_hours=grace_hours)

    if args.json:
        # ONE parseable value: the dropped list, plus the degraded marker as a
        # trailing element (a JSON ROW in the SAME array), never a second document.
        # This was the known leak — the fold streamed one object PER LINE, so
        # `json.loads(stdout)` on 2+ threads raised on the trailing data.
        result = list(dropped)
        if not ok:
            result.append({"type": "threads-degraded",
                           "reason": reason or "threads source degraded"})
        print(jsonutil.dumps(result))
        return 0

    if not ok:
        # Degraded notice on STDERR: stdout stays the clean, pipeable thread
        # list (a consumer grepping/parsing stdout never confuses a degradation
        # notice for a thread), while the degradation is still impossible to
        # miss interactively — the house stdout/stderr split.
        print(f"threads degraded (partial): {reason or 'source unreadable'}",
              file=sys.stderr)
    labels = {1: "started-then-silent", 2: "blocked-on-ash", 3: "intent-never-started"}
    if not dropped:
        print(f"threads — {principal}: nothing dropped")
        return 0
    print(f"threads — {principal}: {len(dropped)} dropped")
    # DELIBERATE divergence from --json: text orders groups 2,3,1 (awaited-now
    # first for the human eye); --json stays mode-ascending (classify's order).
    for mode in (2, 3, 1):  # awaited-now first, then commitments, then silence
        group = [o for o in dropped if o["mode"] == mode]
        if not group:
            continue
        print(f"\n[{mode}] {labels[mode]} ({len(group)})")
        for o in group:  # classify already sorts oldest-first within a mode
            print(f"  {o['id']}: {o['title']} — {o['evidence']}")
    return 0
