"""L1 reconcile orchestration (spec §3, §8).

Scan a team's ``task/`` namespace, parse changed OKF Task docs, and heal the
engine-owned derived artifacts (``index.md``, ``log.md``, ``_coord/summaries.json``).
Transport is injected (duck-typed: ``list_dir``/``read``/``write``), so this is
fully testable without the network.

Orphan-proof by construction: rows are rebuilt from the live listing each pass,
never unioned with stale state.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import aggregate, config, health as health_mod, model, okf
from .log import get_logger
from .roles import age_hours
from .tasks import agent_key
from .transport import TransportError


def _parse_iso_utc(s: Any) -> Optional[datetime]:
    """Parse an ISO-8601 ``generated_at`` (``…Z`` or offset) to a tz-aware UTC
    datetime, or None. Never raises."""
    if not s:
        return None
    txt = str(s).strip()
    iso = (txt[:-1] + "+00:00") if txt.endswith(("Z", "z")) else txt
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _same_minute_reuse_safe(entry_mtime: Any, last_reconcile_iso: Any) -> Optional[bool]:
    """Skew-tolerant same-minute guard for incremental reuse.

    Store ``file list`` mtimes are MINUTE-granular, so a doc written twice inside
    one clock-minute keeps ONE mtime and an equal-length second write is invisible
    to a mtime+size compare. A prior row is provably unchanged only if our LAST
    reconcile READ happened after that minute fully CLOSED — i.e. the row's
    mtime-minute + 1 minute is at or before ``last_reconcile``. Returns:

    * True  — the minute closed before the last reconcile: safe to reuse.
    * False — the doc was touched in (or after) the last reconcile's minute, so it
              is same-minute-ambiguous: reparse (correct beats cheap; only recently
              touched docs pay the read).
    * None  — no reconcile anchor (legacy aggregate without ``generated_at``, or an
              unparseable mtime): the caller falls back to the mtime+size compare.

    Bias under host/store clock skew is toward reparse (a host clock BEHIND the
    store over-reparses; only a host clock >~1min AHEAD could under-read, the same
    now-vs-mtime assumption the retention sub-pass already makes)."""
    lr = _parse_iso_utc(last_reconcile_iso)
    if lr is None:
        return None
    em = aggregate._parse_store_mtime(entry_mtime) if entry_mtime else None
    if em is None:
        return False  # can't prove the minute closed -> reparse
    minute_close = em.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return lr >= minute_close


def task_prefix(team: str) -> str:
    return f"team/{team}/task/"


def index_path(team: str) -> str:
    return f"team/{team}/task/index.md"


def log_path(team: str) -> str:
    return f"team/{team}/task/log.md"


def summaries_path(team: str) -> str:
    return f"team/{team}/_coord/summaries.json"


def _acks_prefix(team: str) -> str:
    return f"team/{team}/_coord/acks/"


#: Fast path is only trusted while the prior aggregate is this fresh — a
#: periodic full pass bounds the blast radius of a missed/undelivered update.
#: The full pass also carries time-driven maintenance (retention archival,
#: orphan-ack GC), so the fast path defers those by at most this long too.
MAX_FAST_PATH_HOURS = 6.0

#: Overlap added to the probe window to absorb clock skew between the host that
#: wrote generated_at, the probing host (dateparser resolves the period on the
#: client clock), and the store's server-side uploaded_at. Hosts are assumed
#: NTP-synced to well under this margin.
FAST_PATH_SKEW_MARGIN_SECONDS = 900


def _fast_path_no_changes(transport: Any, team: str, prior_agg: dict, *, now: str, log: Any) -> bool:
    """True iff the store's data-updates feed proves nothing fold-relevant
    changed since the prior aggregate. ANY doubt (no feed support, feed error,
    stale/missing aggregate, unparseable entries) returns False -> full pass."""
    updates_fn = getattr(transport, "updates", None)
    if updates_fn is None:
        return False
    gen = (prior_agg or {}).get("generated_at")
    if not gen:
        return False
    # A wholesale reuse of prior_agg would carry PROJECTION-stale rows forward
    # untouched — on a quiet fleet (no fold-relevant feed changes) forever. If any
    # prior row lacks the current row-schema stamp (e.g. a pre-#388 uncapped row),
    # decline the fast path and force a full pass so it reparses+caps+stamps. Once
    # a full pass has stamped every row, the fast path resumes. Cheap: an in-memory
    # scan of the already-loaded prior rows, no extra reads.
    if any(row.get("sv") != model.ROW_SCHEMA_VERSION
           for row in aggregate.aggregate_rows(prior_agg)):
        log.info("fast path declined: prior aggregate has stale-schema rows", team=team)
        return False
    age = age_hours(gen, now)
    if age is None or age < 0 or age > MAX_FAST_PATH_HOURS:
        return False
    period = f"{int(age * 3600) + FAST_PATH_SKEW_MARGIN_SECONDS} seconds"
    relevant = (f"/team/{team}/task/", f"/team/{team}/_coord/acks/")
    # Derived artifacts are OUTPUTS of reconcile, not inputs — a prior pass's own
    # index/log writes must not poison the next pass's no-change evidence. (Cost:
    # hand-corruption of index/log self-heals within MAX_FAST_PATH_HOURS instead
    # of one beat — accepted, the engine owns those files.)
    derived = (f"/team/{team}/task/index.md", f"/team/{team}/task/log.md")
    try:
        changes = updates_fn(period)
        if changes is None:
            return False
        for c in changes:
            # Shape guard, fail-CLOSED: any entry we cannot positively parse is
            # doubt, and doubt means full pass — feed-shape drift must degrade
            # to full passes, never to false no-change evidence.
            if not isinstance(c, dict) or not isinstance(c.get("full_name"), str):
                return False
            name = c["full_name"]
            if not name.strip():
                return False
            name = "/" + name.lstrip("/")   # feed shape pins nothing; normalize
            if name.startswith(relevant) and name not in derived:
                return False
    except Exception as e:
        log.warn("data-updates probe failed; full pass", error=str(e))
        return False
    log.info("fast path: no fold-relevant changes in feed", team=team, window=period,
             feed_entries=len(changes))
    return True


def _write_health_shard(transport: Any, team: str, *, host: str, now: str,
                        result: dict, log: Any) -> None:
    """Best-effort health beat + retention GC — never fails the pass."""
    try:
        from . import __version__ as _v
        shard = health_mod.build_shard(host=host, now=now, engine_version=_v, result=result)
        transport.write(f"{health_mod.health_prefix(team)}{agent_key(host)}.json",
                        json.dumps(shard, indent=1))
        for e in transport.list_dir(health_mod.health_prefix(team)):
            n = e.get("name") or ""
            if e.get("is_dir") or not n.endswith(".json"):
                continue
            sh = health_mod.parse_shard(transport.read(health_mod.health_prefix(team) + n))
            ts = (sh or {}).get("at")
            if ts and age_hours(ts, now) > health_mod.SHARD_RETENTION_HOURS                     and hasattr(transport, "delete"):
                transport.delete(health_mod.health_prefix(team) + n)
    except Exception as e:  # never fail the pass, but never go silently dark either
        log.warn("health shard write/gc failed (host will look dark)", error=str(e))


#: Retention: terminal tasks older than this many days are archived during
#: reconcile when retention is enabled (env COORD_RETENTION_DAYS or --retention-days).
#: OPTIONAL — off unless configured. Bounded per pass; throttled to once/day.
RETENTION_CAP_PER_PASS = 20

GC_GRACE_HOURS = 24.0  #: never GC a shard younger than this (or undatable)


def _fold_and_gc_acks(transport: Any, team: str, live_slugs: set, *,
                      now: str) -> tuple[dict, int]:
    """Fold per-agent ack shards (_coord/acks/<slug>/<agent>.md) into
    {slug: [agent, ...]}, and GC shards whose parent task no longer exists —
    the shard-GC sub-pass the plan review required.

    GC is guarded against the data-loss case the code review flagged (a silently
    TRUNCATED task listing makes live tasks look deleted): never GC when the
    live set is empty, and only delete a shard that is DATABLE and older than
    ``GC_GRACE_HOURS`` (undatable -> keep; the 0.15.16 age-discriminator lesson).
    A transient truncation therefore can't erase recent acks; older ones go only
    when the slug is still absent on a later healthy pass."""
    prefix = _acks_prefix(team)
    acks: dict[str, list] = {}
    gc = 0
    try:
        entries = transport.list_dir(prefix)
    except TransportError:
        return acks, gc
    for e in entries:
        n = (e.get("name") or "").rstrip("/")
        if not e.get("is_dir") or not n:
            continue
        try:
            shard_files = [f for f in transport.list_dir(prefix + n + "/")
                           if not f.get("is_dir") and (f.get("name") or "").endswith(".md")]
        except TransportError:
            continue
        if n in live_slugs:
            agents = []
            for f in shard_files:
                stem = f["name"][:-3]
                fm = okf.parse_frontmatter(transport.read(prefix + n + "/" + f["name"])) or {}
                claimed = str(fm.get("agent") or "")
                # trust frontmatter identity only when it matches the ACL-controlled
                # filename stem (review-layer precedent); else the filename wins.
                agents.append(claimed if claimed and agent_key(claimed) == stem else stem)
            acks[n] = sorted(set(agents))
        elif live_slugs and hasattr(transport, "delete"):
            for f in shard_files:
                fm = okf.parse_frontmatter(transport.read(prefix + n + "/" + f["name"])) or {}
                ts = fm.get("timestamp")
                if ts is None or age_hours(ts, now) <= GC_GRACE_HOURS:
                    continue  # undatable or within grace: keep (data-loss guard)
                if transport.delete(prefix + n + "/" + f["name"]):
                    gc += 1
    return acks, gc


def archive_prefix(team: str) -> str:
    return f"team/{team}/task/archive/"


def _retention_marker_path(team: str) -> str:
    return f"team/{team}/_coord/retention/last-run.json"


def _verified_copy(transport: Any, src: str, dst: str) -> bool:
    if transport.read(dst) is not None:
        return False
    content = transport.read(src)
    if content is None or not transport.write(dst, content):
        return False
    if transport.read(dst) != content:
        return False  # verify failed; leave the original in place
    return True


def _crash_safe_move(transport: Any, src: str, dst: str) -> bool:
    """Copy -> verify -> delete (the incumbent's archival discipline: never a
    window where the doc exists nowhere)."""
    if not _verified_copy(transport, src, dst):
        return False
    return transport.delete(src) if hasattr(transport, "delete") else False


def _run_retention(transport: Any, team: str, rows: list, *, now: str, today: str,
                   days: float, log: Any) -> tuple[list, list[str], dict]:
    """Archive terminal tasks older than ``days``: move the task doc to
    task/archive/<YYYY-MM>/ and its ack/response shards to _coord/archive/,
    verified move-not-delete, capped per pass, throttled to once per day."""
    notes: list[str] = []
    archived_map: dict = {}  # slug -> (month, title), for the log's Archived bullets
    marker = transport.read(_retention_marker_path(team))
    if marker is not None and today in marker:
        return rows, notes, archived_map  # already ran today
    keep: list = []
    archived = 0
    for r in rows:
        ts = r.get("timestamp")
        age = age_hours(ts, now)
        old_enough = age != float("inf") and age > days * 24.0
        if (archived < RETENTION_CAP_PER_PASS and old_enough
                and r.get("status") in model.TERMINAL_STATUSES and ts):
            slug = str(r.get("name"))
            month = str(ts)[:7]  # YYYY-MM
            # malformed timestamp would mint a garbage archive dir — keep hot instead
            if len(month) != 7 or month[4] != "-" or not (month[:4] + month[5:]).isdigit():
                notes.append(f"retention: {slug} has a non-ISO timestamp; kept hot")
                keep.append(r)
                continue
            src = f"{task_prefix(team)}{slug}.md"
            dst = f"{archive_prefix(team)}{month}/{slug}.md"
            if _verified_copy(transport, src, dst):
                shards_moved = True
                archived += 1
                archived_map[slug] = (month, r.get("title") or slug)
                # move coordination shards WITH the task (plan-review requirement)
                for kind in ("acks", "responses"):
                    pfx = f"team/{team}/_coord/{kind}/{slug}/"
                    try:
                        for f in transport.list_dir(pfx):
                            fn = f.get("name") or ""
                            if not f.get("is_dir") and fn:
                                if not _crash_safe_move(
                                    transport, pfx + fn,
                                    f"team/{team}/_coord/archive/{kind}/{slug}/{fn}"
                                ):
                                    shards_moved = False
                    except TransportError:
                        shards_moved = False
                if shards_moved and hasattr(transport, "delete") and transport.delete(src):
                    notes.append(f"retention: archived {slug} -> archive/{month}/")
                    continue
                archived -= 1
                if hasattr(transport, "delete"):
                    transport.delete(dst)
            notes.append(f"retention: move FAILED for {slug}; kept")
        keep.append(r)
    if marker is None or today not in marker:
        transport.write(_retention_marker_path(team),
                        json.dumps({"last_run": today, "archived": archived}))
    if archived:
        log.info("retention", team=team, archived=archived)
    return keep, notes, archived_map


def _load_prior_aggregate(transport: Any, team: str) -> Optional[dict[str, Any]]:
    raw = transport.read(summaries_path(team))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def reconcile(
    transport: Any,
    team: str,
    *,
    now: str,
    today: str,
    host: str,
    logger: Any = None,
    retention_days: Any = None,
) -> dict[str, Any]:
    """Run one reconcile pass. Returns a summary dict.

    On a listing failure the pass aborts and writes nothing (leaves prior derived
    artifacts intact) — never publish a truncated index (§8).
    """
    log = logger or get_logger("reconcile")

    prior_agg = _load_prior_aggregate(transport, team)
    prior_rows = aggregate.aggregate_rows(prior_agg)
    prior_by_name = aggregate.rows_by_name(prior_rows)
    # When our LAST full pass ran — the anchor for the same-minute reuse guard
    # (see _same_minute_reuse_safe). Absent on a legacy aggregate -> guard falls
    # back to mtime+size.
    last_reconcile_iso = (prior_agg or {}).get("generated_at")

    if _fast_path_no_changes(transport, team, prior_agg, now=now, log=log):
        # NOTE: warnings from the prior aggregate are not resurfaced here; they
        # reappear on the next full pass (<= MAX_FAST_PATH_HOURS away).
        result = {"tasks": len(prior_rows), "parsed": 0, "reused": len(prior_rows),
                  "transitions": 0, "warnings": [], "fast_path": True}
        _write_health_shard(transport, team, host=host, now=now, result=result, log=log)
        log.info("reconciled (fast path)", team=team, tasks=len(prior_rows))
        return result

    prefix = task_prefix(team)
    try:
        listing = transport.list_dir(prefix)
    except TransportError as e:
        log.error("list failed, pass aborted (prior artifacts intact)", team=team, error=str(e))
        return {"degraded": True, "reason": str(e), "tasks": 0}

    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    reused = parsed = 0

    for entry in listing:
        name = entry.get("name") or ""
        if entry.get("is_dir") or not name.endswith(".md") or name in ("index.md", "log.md"):
            continue
        slug = name[:-3]
        prior = prior_by_name.get(slug)
        entry_mtime = entry.get("mtime")
        entry_size = entry.get("size")
        # Incremental reuse. The store listing carries only size + a MINUTE-granular
        # mtime + name — NO per-file content fingerprint (the `Version:` UUID lives
        # in per-file `file stat`, one read per doc, too expensive for a fold). So
        # reuse rests on THREE listing-only checks, all of which must hold:
        #   (a) mtime unchanged — a new write bumps it to a new minute;
        #   (b) byte size unchanged AND the prior row actually CARRIES a size —
        #       a legacy pre-`size` row (or any length-changing edit) is reparsed
        #       ONCE and re-stamped, so no row lingers on mtime-only reuse;
        #   (c) the mtime-minute provably CLOSED before our last reconcile read
        #       (_same_minute_reuse_safe) — mtime+size alone cannot see a
        #       SAME-length edit made in the SAME clock-minute (the fossil: the row
        #       lies stale until an unrelated write). This is the honest narrow
        #       guarantee: same-minute-TOUCHED docs are reparsed, not reused; it is
        #       the index-side companion to PR #356's read-side doc-authoritative
        #       status guard. When there is no anchor (legacy aggregate w/o
        #       generated_at) (c) is skipped and reuse falls back to (a)+(b).
        #   (d) the prior row carries the CURRENT row-schema stamp — a row projected
        #       by an older `row_from_frontmatter` (e.g. pre-#388, uncapped title/
        #       description, no `sv`) is NOT content-stale but PROJECTION-stale, so
        #       mtime+size can't detect it (the doc never changed). Force one reparse
        #       so the current projection (cap + stamp) applies; it then reuses
        #       normally. This is what self-heals the legacy uncapped index.
        minute_safe = _same_minute_reuse_safe(entry_mtime, last_reconcile_iso)
        reusable = (
            prior is not None
            and entry_mtime
            and prior.get("mtime") == entry_mtime
            and prior.get("size") is not None
            and prior.get("size") == entry_size
            and minute_safe is not False
            and prior.get("sv") == model.ROW_SCHEMA_VERSION
        )
        if reusable:
            rows.append(prior)
            reused += 1
            continue
        content = transport.read(f"{prefix}{name}")
        fm = okf.parse_frontmatter(content)
        if fm is None:
            # unparseable / unreadable: never drop a task — keep the prior row.
            if prior:
                warnings.append(f"{name}: unparseable frontmatter, kept prior row")
                rows.append(prior)
            else:
                warnings.append(f"{name}: unparseable frontmatter, no prior row, skipped")
            continue
        if not model.is_task(fm):
            continue  # not a Task concept doc — silently ignore
        row = model.row_from_frontmatter(
            fm, name=slug, path=f"task/{name}", mtime=entry_mtime
        )
        # Stamp the listed size so the NEXT pass can sub-minute-compare (above).
        row["size"] = entry_size
        rows.append(row)
        parsed += 1

    # --- retention sub-pass (OPTIONAL: only when configured) ---
    # Off unless configured (default 0.0 -> disabled): NO positive fallback, unlike
    # the fold budgets. Precedence: --retention-days flag > COORD_RETENTION_DAYS >
    # legacy FULCRA_COORD_RETENTION_DAYS. The legacy prefix is alias-ACCEPTED (an
    # operator copying old fulcra-coord docs still gets retention) but the legacy
    # default of 30 is NOT adopted — coord-engine stays opt-in. Routing through the
    # shared parser also gives retention the NaN/inf guard the fold budgets have
    # (ENG-1-8: an inf/NaN value now disables cleanly instead of running unbounded).
    archived_map: dict = {}
    days = config.env_float(
        "COORD_RETENTION_DAYS", 0.0,
        override=retention_days,
        aliases=("FULCRA_COORD_RETENTION_DAYS",),
    )
    if days > 0 and rows:
        rows, notes, archived_map = _run_retention(
            transport, team, rows, now=now, today=today, days=days, log=log)
        warnings.extend(n for n in notes if "FAILED" in n or "kept hot" in n)

    # --- ack fold + shard-GC sub-pass ---
    acks, gc_count = _fold_and_gc_acks(transport, team, {r.get("name") for r in rows}, now=now)
    for r in rows:
        r["acked_by"] = acks.get(r.get("name"), [])
    if gc_count:
        warnings.append(f"shard-GC: pruned {gc_count} orphaned ack shard(s)")

    # --- heal engine-owned derived artifacts ---
    if not transport.write(index_path(team), okf.render_index(rows)):
        warnings.append("index.md write failed")

    prior_for_diff = [r for r in prior_rows if str(r.get("name")) not in archived_map]
    transitions = aggregate.diff_rows(prior_for_diff, rows)
    transitions += [
        f"* **Archived**: [{title}](archive/{month}/{slug}.md) → archive/{month}/."
        for slug, (month, title) in sorted(archived_map.items())
    ]
    if transitions:
        existing_log = transport.read(log_path(team))
        if not transport.write(
            log_path(team), okf.merge_log(existing_log, transitions, date=today)
        ):
            warnings.append("log.md write failed")

    # --- structured transitions for the projection fold (ADDITIVE; the bullet
    # strings + log.md above are untouched). Persist this pass's transitions to
    # the bus so a SEPARATE `annotate project` invocation (the heartbeat runs it
    # right after reconcile) can fold them onto the timeline. Gated by the bus
    # resolution level and defaulting OFF, so a team that never opted in sees no
    # extra artifact and existing reconcile behavior is unchanged. Best-effort:
    # a local import (annotate imports a constant from this module) + never-raise
    # helpers keep it from ever affecting the core pass.
    from . import annotate as _annotate
    if _annotate.read_resolution(transport, team) in _annotate.LIVE_PROJECTING:
        structured = aggregate.diff_transitions(prior_for_diff, rows)
        _annotate.write_pending(transport, team, structured, now=now)

    agg = aggregate.build_aggregate(
        team, rows, generated_at=now, reconcile_host=host, warnings=warnings
    )
    if not transport.write(summaries_path(team), json.dumps(agg, indent=2)):
        warnings.append("summaries.json write failed")

    # --- fleet health shard (best-effort; never fails the pass) ---
    _write_health_shard(transport, team, host=host, now=now,
                        result={"tasks": len(rows), "parsed": parsed,
                                "reused": reused, "warnings": warnings}, log=log)

    log.info(
        "reconciled", team=team, tasks=len(rows), reused=reused, parsed=parsed,
        transitions=len(transitions), warnings=len(warnings),
    )
    return {
        "degraded": False,
        "tasks": len(rows),
        "reused": reused,
        "parsed": parsed,
        "transitions": len(transitions),
        "warnings": warnings,
        "rows": rows,
    }
