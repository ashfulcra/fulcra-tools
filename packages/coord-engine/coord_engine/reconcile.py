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
from typing import Any, NamedTuple, Optional

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
    # NOT gated on the rows' schema stamp (`sv`), deliberately — v1.6.7 gated it
    # here and v1.6.9 removed it. The gate assumed the fleet CONVERGES: decline
    # until one full pass stamps every row, then resume. A mixed fleet never
    # converges. All hosts reconcile ONE shared index, and every pass by a
    # pre-stamp host (v1.6.6 predates both #388's text cap and `sv`) writes
    # unstamped rows straight back in. The gate then declines on every beat,
    # forever — strictly worse than no gate at all, since it costs a mixed fleet
    # MORE full passes than it did before the gate existed.
    #
    # Stale rows still heal, just not from here: the incremental-reuse gate
    # refuses to reuse a stale-projection row, forcing the reparse that re-caps +
    # re-stamps it, on any pass that finds real work. And MAX_FAST_PATH_HOURS
    # bounds a quiet fleet regardless — the fast path cannot run indefinitely, so
    # a periodic full pass sweeps whatever a quiet stretch left behind.
    #
    # The ack-anchor guard below is NOT the same shape of rule, and stays: it is
    # about a sub-fold OWING a pass, and it settles itself in one.
    #
    # That guard: the fast path may only fire when every sub-fold is SETTLED. The
    # ack fold advances its own anchor only when it read everything it
    # meant to (see ACKS_ANCHOR_KEY); an anchor behind generated_at means it is owed
    # a change from BEFORE this window — which this probe cannot see, because it
    # asks about generated_at onward. Skipping here would strand that change until
    # the periodic backstop, defeating the held anchor entirely. So: decline until
    # the ack fold settles, then resume. (A legacy aggregate has no anchor at all —
    # nothing has ever been verified — and declines the same way, until one full
    # pass records a conclusive fold.)
    if (prior_agg or {}).get(ACKS_ANCHOR_KEY) != gen:
        log.info("fast path declined: ack fold owes a pass (anchor behind generated_at)",
                 team=team, ack_anchor=(prior_agg or {}).get(ACKS_ANCHOR_KEY),
                 generated_at=gen)
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

#: How many passes may fold acks incrementally before one full fold is FORCED
#: (env ``COORD_ACKS_FULL_EVERY``; positive-finite, bad value -> this default).
#: The backstop bounds the blast radius of a change the query never reported: a
#: missed ack is corrected within this many passes, so it can never persist
#: indefinitely. It also carries the orphan-shard GC, which only rides the full
#: fold. ``1`` disables the incremental path entirely.
#:
#: Why 72 and not 12: a forced full fold was MEASURED at 1091s (~18min) on a
#: 1.2s/op remote transport. At 12 (~4h on a 20-min heartbeat) that is an
#: 18-minute stall every four hours on every remote host — a real cost to pay
#: for a check whose subject, the change query, was verified complete against an
#: independent listing (31/31, zero missed). 72 puts the true-full at ~daily on
#: the same heartbeat: still bounded, still catches a silently-dropped change
#: within a day, at a SIXTH of the recurring cost (72/12 = 6x less frequent;
#: the interval goes 4h -> ~24h, but the ratio is 6, not 24). The right end
#: state is a
#: change-driven backstop (query a WIDE window since the last CONCLUSIVE full
#: fold, reserving a true-full for anchor-loss/doubt) — that is a design change,
#: not a constant, so it is queued rather than rushed in here.
DEFAULT_ACKS_FULL_EVERY = 72

#: Key under which the aggregate carries the count of consecutive INCREMENTAL ack
#: folds since the last full one — the backstop's counter. It lives in
#: summaries.json (already read + written every pass) so the backstop costs zero
#: extra transport ops. Losing it (deleted aggregate) is harmless: no prior
#: aggregate means no prior acks and no anchor, which is a full fold anyway.
ACKS_STREAK_KEY = "acks_incremental_streak"

#: Key under which the aggregate carries the ack fold's OWN anchor: the instant
#: through which acks are provably folded. The change-query window starts here.
#:
#: Why not ``generated_at``: that anchor advances every pass, unconditionally. If
#: a pass knew a slug had changed but could not READ it, reusing generated_at
#: would consume the change — the next window would start past it and the new ack
#: would stay invisible until the periodic backstop. That is a FALSE ADVANCE (the
#: `listen` fold's discipline: a failed read must never mark unknown state as
#: seen). This anchor advances ONLY on a fold that read everything it meant to,
#: so an unread change stays inside the next pass's window. Absent (a legacy
#: aggregate, or a pass that never got a conclusive fold) means NO anchor — a
#: full fold — never a silent fallback to generated_at.
ACKS_ANCHOR_KEY = "acks_folded_through"

#: An anchor older than this makes the change query pointless: the endpoint 500s
#: on an over-wide window (verified at 30 days), so a host that has been down for
#: days would burn ~10s per pass to learn nothing. Skip straight to the full fold.
ACKS_ANCHOR_MAX_HOURS = 24.0


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _changed_ack_slugs(transport: Any, team: str, *, since: Any, now: str,
                       log: Any) -> Optional[set]:
    """The slugs whose ack shards changed between ``since`` and ``now``, via the
    store's tree-wide change query — or **None for UNKNOWN**.

    None is returned for every kind of doubt: no change-query capability, an
    unusable/too-old anchor, a query failure (the endpoint fails LOUD — HTTP 500
    on an over-wide window — it never truncates), or an entry we cannot positively
    parse. UNKNOWN is never an empty set: the caller must full-fold, never reuse.

    The window is widened by ``FAST_PATH_SKEW_MARGIN_SECONDS`` on both sides to
    absorb clock skew between the host that stamped ``generated_at``, this host,
    and the store's server-side ``uploaded_at`` (second-precision)."""
    query = getattr(transport, "recent_changes", None)
    if query is None:
        return None
    start, end = _parse_iso_utc(since), _parse_iso_utc(now)
    if start is None or end is None or end < start:
        return None
    if (end - start).total_seconds() > ACKS_ANCHOR_MAX_HOURS * 3600:
        return None
    margin = timedelta(seconds=FAST_PATH_SKEW_MARGIN_SECONDS)
    try:
        changes = query(_iso_z(start - margin), _iso_z(end + margin))
    except Exception as e:  # a capability must not be able to fail the pass
        log.warn("acks change query raised", team=team, error=str(e))
        return None
    if not isinstance(changes, list):
        return None
    prefix = "/" + _acks_prefix(team)
    slugs: set[str] = set()
    for c in changes:
        # Shape guard, fail-CLOSED (the fast path's rule): an entry we cannot
        # positively parse is doubt, and doubt is UNKNOWN — feed-shape drift must
        # degrade to full folds, never to false no-change evidence.
        if not isinstance(c, dict) or not isinstance(c.get("full_name"), str):
            return None
        name = c["full_name"].strip()
        if not name:
            return None
        name = "/" + name.lstrip("/")   # the feed shape pins nothing; normalize
        if not name.startswith(prefix):
            continue
        rest = name[len(prefix):]
        if "/" not in rest:
            continue  # a stray file directly under acks/ — not a slug's shard
        slugs.add(rest.split("/", 1)[0])
    return slugs


def _fold_slug(transport: Any, prefix: str, slug: str) -> Optional[list]:
    """The acked-by list for one slug's ack dir, or None if it can't be listed."""
    try:
        shard_files = [f for f in transport.list_dir(prefix + slug + "/")
                       if not f.get("is_dir") and (f.get("name") or "").endswith(".md")]
    except TransportError:
        return None
    agents = []
    for f in shard_files:
        stem = f["name"][:-3]
        fm = okf.parse_frontmatter(transport.read(prefix + slug + "/" + f["name"])) or {}
        claimed = str(fm.get("agent") or "")
        # trust frontmatter identity only when it matches the ACL-controlled
        # filename stem (review-layer precedent); else the filename wins.
        agents.append(claimed if claimed and agent_key(claimed) == stem else stem)
    return sorted(set(agents))


def _full_fold_and_gc(transport: Any, team: str, live_slugs: set, *, now: str,
                      prior_acks: dict, log: Any) -> tuple[dict, int, bool]:
    """List EVERY ack dir, fold the live ones, and GC shards whose parent task no
    longer exists — the shard-GC sub-pass the plan review required. This is the
    fold every failure of the incremental path falls back to; it is also the only
    path that can see orphan dirs, hence the GC's home.

    Returns ``(acks, gc_count, conclusive)``. ``conclusive`` is False when any
    listing this fold NEEDED failed, i.e. when the result is not a complete
    picture of the ack tree as of ``now``. The caller must not advance the ack
    anchor on an inconclusive fold.

    A failed listing PRESERVES the slug's prior acked_by rather than omitting it:
    the caller stamps every slug missing from this map to ``[]``, so an omission
    is not a neutral gap — it is a silent un-ack of a real acknowledgement. A
    transport failure must never cost us data we already had.

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
    except TransportError as e:
        # The whole tree is unreadable: keep every ack we already knew about
        # (never un-ack a task because a listing failed) and report inconclusive.
        log.warn("acks: root listing failed; prior acks preserved", team=team,
                 error=str(e))
        return {s: prior_acks[s] for s in live_slugs if s in prior_acks}, gc, False
    conclusive = True
    for e in entries:
        n = (e.get("name") or "").rstrip("/")
        if not e.get("is_dir") or not n:
            continue
        if n in live_slugs:
            agents = _fold_slug(transport, prefix, n)
            if agents is None:
                log.warn("acks: slug listing failed; prior acks preserved",
                         team=team, slug=n)
                conclusive = False
                if n in prior_acks:
                    acks[n] = prior_acks[n]
                continue
            acks[n] = agents
        elif live_slugs and hasattr(transport, "delete"):
            try:
                shard_files = [f for f in transport.list_dir(prefix + n + "/")
                               if not f.get("is_dir")
                               and (f.get("name") or "").endswith(".md")]
            except TransportError:
                continue
            for f in shard_files:
                fm = okf.parse_frontmatter(transport.read(prefix + n + "/" + f["name"])) or {}
                ts = fm.get("timestamp")
                if ts is None or age_hours(ts, now) <= GC_GRACE_HOURS:
                    continue  # undatable or within grace: keep (data-loss guard)
                if transport.delete(prefix + n + "/" + f["name"]):
                    gc += 1
    return acks, gc, conclusive


class AckFold(NamedTuple):
    """One ack fold's result.

    ``full``       — the full fold ran (resets the backstop counter).
    ``conclusive`` — every listing the fold needed succeeded, so ``acks`` is a
                     complete picture as of ``now``. ONLY a conclusive fold may
                     advance the ack anchor: an inconclusive one leaves the
                     unread change inside the next pass's window.
    """
    acks: dict
    gc: int
    full: bool
    conclusive: bool


def _fold_and_gc_acks(transport: Any, team: str, live_slugs: set, *, now: str,
                      prior_acks: Optional[dict] = None, since: Any = None,
                      force_full: bool = False, log: Any = None) -> AckFold:
    """Fold per-agent ack shards (_coord/acks/<slug>/<agent>.md) into
    {slug: [agent, ...]}. See :class:`AckFold` for the return.

    THE INVARIANT: the incremental path is an OPTIMIZATION, and its failure mode
    is ALWAYS "fall back to the full fold and say so" — never "assume unchanged".
    Every unknown (no change-query capability, a query error/500, feed-shape
    drift, no ``since`` anchor, an anchor too old to query, a slug the prior
    aggregate never saw, a slug we knew had changed but could not read) resolves
    to folding, not to reusing. Reuse happens only on POSITIVE evidence: the store
    answered, and it did not name this slug.

    Its COROLLARY, equally load-bearing: a fold that could not read what it meant
    to must not let the pass advance past it. Falling back to the full fold is
    only half the fix — the pass must also leave the ack anchor where it was
    (``conclusive=False``), or the change we failed to read is consumed by the
    window that reported it and stays invisible until the backstop. Failing
    closed means failing SLOW, not failing quiet.

    Why it exists: the full fold costs one ``list_dir`` per ack dir per pass (280
    dirs on the live bus = ~336s at a remote host's 1.2s/op), even though at a
    20-min heartbeat ~0-2 shards actually change. So we ask the store what changed
    since ``since`` (the ack anchor — see ``ACKS_ANCHOR_KEY``), re-fold only those
    slugs, and reuse ``prior_acks`` — the prior aggregate rows' ``acked_by`` — for
    the rest, at zero ops.

    GC rides the full fold ONLY (see ``_full_fold_and_gc``): it is cleanup, not
    correctness, and the incremental path deliberately never lists the ack root,
    so it cannot see orphan dirs. It is deferred, not dropped — the periodic
    backstop (``DEFAULT_ACKS_FULL_EVERY``) collects within a bounded number of
    passes, and the shard-GC grace is a day."""
    log = log or get_logger("reconcile")
    prior_acks = prior_acks or {}
    affected: Optional[set] = None
    if force_full:
        reason = "periodic backstop"
    elif not since:
        reason = "no ack anchor on the prior aggregate"
    elif not 0 <= age_hours(since, now) <= ACKS_ANCHOR_MAX_HOURS:
        # inf (unparseable), negative (clock skew / a future anchor), or a window
        # so wide the query would just 500 — don't spend the op to learn nothing.
        reason = f"anchor unusable or older than {ACKS_ANCHOR_MAX_HOURS}h"
    else:
        affected = _changed_ack_slugs(transport, team, since=since, now=now, log=log)
        reason = "change query unavailable or inconclusive" if affected is None else ""

    if affected is not None:
        fold = _incremental_fold(transport, team, live_slugs, prior_acks=prior_acks,
                                 affected=affected, log=log)
        if fold is not None:
            return fold
        # A slug we KNEW had changed would not list. Reusing its prior acks and
        # carrying on would be a false advance; re-fold everything instead.
        reason = "a changed slug could not be listed"

    # Visible by design: a degraded fold is 280 listings and must be attributable,
    # and a fold that silently stopped being change-driven is exactly the
    # regression this line makes findable.
    log.info("acks: full fold", team=team, reason=reason, dirs="all")
    acks, gc, conclusive = _full_fold_and_gc(transport, team, live_slugs, now=now,
                                             prior_acks=prior_acks, log=log)
    return AckFold(acks, gc, True, conclusive)


def _incremental_fold(transport: Any, team: str, live_slugs: set, *,
                      prior_acks: dict, affected: set, log: Any) -> Optional[AckFold]:
    """Fold only the slugs the change query named (plus any the prior aggregate
    never carried), reusing prior acks for the rest. Returns None if a slug we
    needed to read would not list — the caller escalates to the full fold rather
    than let the pass advance on a fold it could not complete."""
    prefix = _acks_prefix(team)
    acks: dict[str, list] = {}
    folded = 0
    for slug in sorted(s for s in live_slugs if s):
        # A slug the prior aggregate never carried (new/restored task) has no
        # prior acked_by to reuse — "not named by the query" is not evidence
        # about it, so fold it.
        if slug in affected or slug not in prior_acks:
            agents = _fold_slug(transport, prefix, slug)
            if agents is None:
                log.warn("acks: changed slug would not list; escalating to a full fold",
                         team=team, slug=slug)
                return None
            acks[slug] = agents
            folded += 1
        else:
            acks[slug] = prior_acks[slug]
    log.info("acks: incremental fold", team=team, folded=folded,
             reused=len(acks) - folded, changed_slugs=len(affected))
    return AckFold(acks, 0, False, True)


def archive_prefix(team: str) -> str:
    return f"team/{team}/task/archive/"


def review_archive_prefix(team: str) -> str:
    return f"team/{team}/_coord/archive/reviews/"


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


def _quiet_mtime_old_enough(value: Any, *, now: str, days: float) -> bool:
    """True only when the store mtime is known and older than the quiet window."""
    modified = aggregate._parse_store_mtime(value) if value else None
    current = _parse_iso_utc(now)
    if modified is None or current is None:
        return False
    return current - modified > timedelta(days=days)


def _run_retention(transport: Any, team: str, rows: list, *, now: str, today: str,
                   days: float, log: Any) -> tuple[list, list[str], dict]:
    """Cold-archive quiet work older than ``days``, reversibly and fail-closed.

    Eligible tasks are terminal or ``proposed``; ``active``/``waiting`` are never
    retention candidates. A doc-less review directory is eligible only when its
    verdict listing is KNOWN and proves exactly one ``codex-reviewer`` verdict.
    Every move is copy -> verify -> delete, capped per pass and throttled daily.
    """
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
        status = r.get("status")
        eligible_status = status in model.TERMINAL_STATUSES
        if status == "proposed":
            # Proposed work is archived only after BOTH its semantic timestamp and
            # its store mtime have been quiet for the window. A recent hand-edit
            # that forgot to refresh frontmatter must keep the task hot.
            eligible_status = _quiet_mtime_old_enough(
                r.get("mtime"), now=now, days=days)
        if (archived < RETENTION_CAP_PER_PASS and old_enough
                and eligible_status and ts):
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

    # Verdict-only review dirs are a separate cold population. The review-root
    # listing positively proves which slugs have no live review doc; a raised
    # listing is UNKNOWN, never an empty set. Per-slug verdict listings obey the
    # same rule. Empty tombstones, multi-verdict dirs, and non-codex singletons are
    # deliberately excluded rather than guessed settled.
    review_archived = 0
    review_prefix = f"team/{team}/review/"
    try:
        review_entries = transport.list_dir(review_prefix)
    except TransportError:
        review_entries = None
        notes.append("retention: review root listing UNKNOWN; no orphan reviews archived")
    if review_entries is not None:
        doc_slugs = {
            str(e.get("name"))[:-3]
            for e in review_entries
            if not e.get("is_dir") and str(e.get("name") or "").endswith(".md")
        }
        dir_slugs = sorted({
            str(e.get("name") or "").rstrip("/")
            for e in review_entries if e.get("is_dir")
        } - doc_slugs)
        for slug in dir_slugs:
            if archived >= RETENTION_CAP_PER_PASS:
                break
            verdict_prefix = f"{review_prefix}{slug}/verdicts/"
            try:
                verdict_entries = transport.list_dir(verdict_prefix)
            except TransportError:
                notes.append(
                    f"retention: review {slug} verdict listing UNKNOWN; kept hot")
                continue
            verdict_files = sorted(
                str(e.get("name") or "") for e in verdict_entries
                if not e.get("is_dir") and str(e.get("name") or "").endswith(".md")
            )
            if verdict_files != ["codex-reviewer.md"]:
                continue
            filename = verdict_files[0]
            src = verdict_prefix + filename
            raw = transport.read(src)
            fm = okf.parse_frontmatter(raw)
            if fm is None:
                notes.append(f"retention: review {slug} verdict unreadable; kept hot")
                continue
            if fm.get("type") != "Verdict" or fm.get("reviewer") != "codex-reviewer":
                continue
            verdict_entry = next(e for e in verdict_entries if e.get("name") == filename)
            if not _quiet_mtime_old_enough(
                verdict_entry.get("mtime"), now=now, days=days
            ):
                continue
            ts = fm.get("timestamp")
            age = age_hours(ts, now)
            if age == float("inf") or age <= days * 24.0:
                continue
            month = str(ts)[:7]
            if len(month) != 7 or month[4] != "-" or not (month[:4] + month[5:]).isdigit():
                notes.append(f"retention: review {slug} has a non-ISO timestamp; kept hot")
                continue
            dst = f"{review_archive_prefix(team)}{month}/{slug}/verdicts/{filename}"
            if _crash_safe_move(transport, src, dst):
                archived += 1
                review_archived += 1
                notes.append(f"retention: archived review {slug} -> reviews/{month}/")
            else:
                notes.append(f"retention: review move FAILED for {slug}; kept")
    if marker is None or today not in marker:
        transport.write(_retention_marker_path(team),
                        json.dumps({"last_run": today, "archived": archived}))
    if archived:
        log.info("retention", team=team, archived=archived,
                 review_archived=review_archived)
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
    # Prior acks, snapshotted BEFORE the fold re-stamps acked_by onto these same
    # row objects (reused rows are the prior dicts). A row that carries no
    # acked_by list is simply absent here -> its slug is folded, not assumed empty.
    prior_acks = {name: row["acked_by"] for name, row in prior_by_name.items()
                  if isinstance(row.get("acked_by"), list)}
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
        #       normally. This is what self-heals the legacy uncapped index, and
        #       since v1.6.9 it is the ONLY place the `sv` check lives: the fast
        #       path used to gate on it too, but a mixed fleet re-introduces
        #       unstamped rows continuously, so gating there never settled (see
        #       _fast_path_no_changes). Here it settles per-row, on contact.
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
    if days > 0:
        rows, notes, archived_map = _run_retention(
            transport, team, rows, now=now, today=today, days=days, log=log)
        warnings.extend(
            n for n in notes
            if "FAILED" in n or "kept hot" in n or "UNKNOWN" in n
        )

    # --- ack fold + shard-GC sub-pass ---
    # Change-driven: re-fold only the slugs the store says changed since our last
    # pass, reuse the prior rows' acked_by for the rest, and force a full fold
    # every Nth pass (and on ANY doubt — see _fold_and_gc_acks' invariant).
    full_every = config.env_int("COORD_ACKS_FULL_EVERY", DEFAULT_ACKS_FULL_EVERY)
    streak = (prior_agg or {}).get(ACKS_STREAK_KEY)
    streak = streak if isinstance(streak, int) and streak >= 0 else 0
    prior_anchor = (prior_agg or {}).get(ACKS_ANCHOR_KEY)
    prior_anchor = prior_anchor if isinstance(prior_anchor, str) else None
    fold = _fold_and_gc_acks(
        transport, team, {r.get("name") for r in rows}, now=now,
        prior_acks=prior_acks, since=prior_anchor,
        force_full=streak + 1 >= full_every, log=log,
    )
    for r in rows:
        r["acked_by"] = fold.acks.get(r.get("name"), [])
    if fold.gc:
        warnings.append(f"shard-GC: pruned {fold.gc} orphaned ack shard(s)")

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

    # --- ack fold state (not task state; consumers ignore both keys) ---
    # These ride the aggregate because it is already read + written every pass, so
    # they cost no transport op. The fast path returns before this and rewrites
    # nothing, so a skipped pass moves neither — correct: it did no ack fold.
    #
    # The anchor advances ONLY on a conclusive fold. An inconclusive one carries
    # the prior anchor forward unchanged (and writes none if there wasn't one), so
    # whatever it failed to read is still inside the next pass's query window
    # rather than consumed by this one. Likewise the streak: an inconclusive pass
    # neither spends a backstop pass nor resets the counter, so the forced full
    # fold keeps coming.
    ack_state: dict[str, Any] = {}
    if fold.conclusive:
        ack_state[ACKS_ANCHOR_KEY] = now
        ack_state[ACKS_STREAK_KEY] = 0 if fold.full else streak + 1
    else:
        if prior_anchor:
            ack_state[ACKS_ANCHOR_KEY] = prior_anchor
        ack_state[ACKS_STREAK_KEY] = streak
    # `prior` carries any top-level key a NEWER host wrote that this build does not
    # know about — see build_aggregate's invariant: rebuilding from a fixed key set
    # is what killed v1.6.8's anchor on the live mixed fleet. The ack keys are cut
    # from the passthrough first because THIS build owns them: `ack_state` above is
    # their complete, recomputed value, including the case where it deliberately
    # writes no anchor at all (inconclusive fold, no prior anchor). Passing them
    # through would resurrect a value this pass decided not to keep.
    prior_unknown = {k: v for k, v in (prior_agg or {}).items()
                     if k not in (ACKS_ANCHOR_KEY, ACKS_STREAK_KEY)}
    agg = aggregate.build_aggregate(
        team, rows, generated_at=now, reconcile_host=host, warnings=warnings,
        state=ack_state, prior=prior_unknown,
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
