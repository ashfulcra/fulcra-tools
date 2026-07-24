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

from . import aggregate, config, health as health_mod, jsonutil, model, okf
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
        try:
            changes = updates_fn(period, team=team)
        except TypeError:
            # Duck-typed transports from before the parsed/team-filtered feed
            # contract (including mixed-version test/adapter transports).
            changes = updates_fn(period)
        if changes is None:
            return False
        for c in changes:
            # Shape guard, fail-CLOSED: any entry we cannot positively parse is
            # doubt, and doubt means full pass — feed-shape drift must degrade
            # to full passes, never to false no-change evidence.
            if not isinstance(c, dict):
                return False
            name = c.get("path", c.get("full_name"))
            if not isinstance(name, str):
                return False
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
#: Enabled by default. Bounded per pass; throttled to once/day.
RETENTION_CAP_PER_PASS = 20
DEFAULT_RETENTION_DAYS = 14.0
REVIEW_RETENTION_DAYS = 7.0
PRESENCE_RETENTION_DAYS = 7.0

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


def settled_index_path(team: str) -> str:
    return f"team/{team}/_coord/retention/settled-reviews.json"


def _load_settled_index(transport: Any, team: str) -> set[str]:
    raw = transport.read(settled_index_path(team))
    try:
        doc = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return set()
    return {str(x) for x in doc.get("reviews", []) if x}


def _move_tree(transport: Any, src_prefix: str, dst_prefix: str) -> bool:
    """Crash-safe recursive prefix move; UNKNOWN listings keep the source hot."""
    try:
        entries = transport.list_dir(src_prefix)
    except TransportError:
        return False
    ok = True
    for entry in entries:
        name = str(entry.get("name") or "").rstrip("/")
        if not name:
            continue
        if entry.get("is_dir"):
            ok = _move_tree(transport, src_prefix + name + "/",
                            dst_prefix + name + "/") and ok
        else:
            ok = _crash_safe_move(transport, src_prefix + name,
                                  dst_prefix + name) and ok
    return ok


def _copy_tree_verified(transport: Any, src_prefix: str,
                        dst_prefix: str) -> tuple[bool, list[tuple[str, str]]]:
    """Copy a tree without deleting sources; return every verified copy."""
    try:
        entries = transport.list_dir(src_prefix)
    except TransportError:
        return False, []
    copied: list[tuple[str, str]] = []
    for entry in entries:
        name = str(entry.get("name") or "").rstrip("/")
        if not name:
            continue
        if entry.get("is_dir"):
            ok, nested = _copy_tree_verified(
                transport, src_prefix + name + "/", dst_prefix + name + "/"
            )
            copied.extend(nested)
            if not ok:
                return False, copied
        else:
            src, dst = src_prefix + name, dst_prefix + name
            if not _ensure_verified_copy(transport, src, dst):
                return False, copied
            copied.append((src, dst))
    return True, copied


def _verified_copy(transport: Any, src: str, dst: str) -> bool:
    if transport.read(dst) is not None:
        return False
    content = transport.read(src)
    if content is None or not transport.write(dst, content):
        return False
    if transport.read(dst) != content:
        return False  # verify failed; leave the original in place
    return True


def _ensure_verified_copy(transport: Any, src: str, dst: str) -> bool:
    """Idempotent archival copy: an identical verified destination is success."""
    source = transport.read(src)
    if source is None:
        return False
    existing = transport.read(dst)
    if existing is not None:
        return existing == source
    return bool(transport.write(dst, source) and transport.read(dst) == source)


def _crash_safe_move(transport: Any, src: str, dst: str) -> bool:
    """Copy -> verify -> delete (the incumbent's archival discipline: never a
    window where the doc exists nowhere)."""
    if not _ensure_verified_copy(transport, src, dst):
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

    # Settled reviews are immutable. Archive the whole family after seven quiet
    # days and record the slug in a compact index so hot folds never have to
    # classify the store's soft-deleted directory tombstone again.
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
        settled_index = _load_settled_index(transport, team)
        for slug in sorted(doc_slugs):
            if archived >= RETENTION_CAP_PER_PASS:
                break
            verdict_prefix = f"{review_prefix}{slug}/verdicts/"
            try:
                ventries = transport.list_dir(verdict_prefix)
            except TransportError:
                continue
            marker = next((e for e in ventries
                           if (e.get("name") or "") == ".settled"), None)
            if marker is None or not _quiet_mtime_old_enough(
                    marker.get("mtime"), now=now, days=REVIEW_RETENTION_DAYS):
                continue
            month = str(now)[:7]
            doc_src = f"{review_prefix}{slug}.md"
            doc_dst = f"{review_archive_prefix(team)}{month}/{slug}.md"
            family_dst = f"{review_archive_prefix(team)}{month}/{slug}/verdicts/"
            doc_copied = _ensure_verified_copy(transport, doc_src, doc_dst)
            family_ok, family_copies = _copy_tree_verified(
                transport, verdict_prefix, family_dst) if doc_copied else (False, [])
            deleted = False
            if doc_copied and family_ok and hasattr(transport, "delete"):
                delete_results = [transport.delete(src) for src, _ in family_copies]
                if all(delete_results):
                    deleted = transport.delete(doc_src)
            if deleted:
                archived += 1
                review_archived += 1
                settled_index.add(slug)
                notes.append(f"retention: archived settled review {slug} wholesale")
            elif hasattr(transport, "delete") and not family_ok:
                # Copy failed before any source delete: clean partial cold copies.
                transport.delete(doc_dst)
                for _, dst in family_copies:
                    transport.delete(dst)
        transport.write(settled_index_path(team), json.dumps({
            "schema": "coord.settled-reviews.v1", "reviews": sorted(settled_index)
        }, separators=(",", ":")))
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

    # Presence is ephemeral liveness state, not history. Prune agents that have
    # been dead for seven days; undatable/unreadable shards fail closed.
    presence_prefix = f"team/{team}/presence/"
    try:
        for entry in transport.list_dir(presence_prefix):
            name = str(entry.get("name") or "")
            if entry.get("is_dir") or not name.endswith(".md"):
                continue
            raw = transport.read(presence_prefix + name)
            fm = okf.parse_frontmatter(raw) or {}
            ts = fm.get("timestamp")
            age = age_hours(ts, now) if ts else float("inf")
            if (age != float("inf") and age > PRESENCE_RETENTION_DAYS * 24
                    and hasattr(transport, "delete")):
                transport.delete(presence_prefix + name)
    except TransportError:
        notes.append("retention: presence listing UNKNOWN; no presence pruned")

    # Canonicalize the historical singular namespace without dropping data.
    if not _move_tree(
            transport, f"team/{team}/artifact/", f"team/{team}/artifacts/"):
        notes.append(
            "retention: artifact namespace move UNKNOWN or FAILED; sources kept")
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


# --- E1: incremental reconcile (feed-cursor fold) ---------------------------
#
# Reconcile's default pass is a FEED DELTA, not a directory scan: consume the
# data-updates feed since a durable cursor, read ONLY the changed task shards,
# and update the aggregate rows in place. The full directory scan stays as (a)
# the fail-closed fallback on ANY cursor/feed doubt and (b) a periodic drift
# self-check. Normative: docs/coord/wake-router-ADDENDUM-1-event-substrate §3.1;
# the cursor reuses W4's proven watermark + processed-ledger pattern (router.py).

#: The cursor lives in summaries.json (already read + written every pass, so it
#: costs no transport op) under this key, shape ``{watermark, processed, streak}``.
#: An older host that predates this key wipes it on its own pass — the same
#: mixed-fleet hazard the ack anchor hit — but losing it is HARMLESS (no cursor
#: -> full scan, fail-closed), and ``build_aggregate``'s passthrough preserves it
#: for every host >= this version.
RECONCILE_CURSOR_KEY = "reconcile_cursor"

#: Force a full-scan drift self-check every Nth incremental pass, independent of
#: the ``MAX_FAST_PATH_HOURS`` time backstop (env ``COORD_RECONCILE_FULL_EVERY``;
#: positive int, bad value -> this default). The full scan is authoritative; a
#: divergence from the incremental-maintained view is logged LOUD and rebuilt.
DEFAULT_RECONCILE_FULL_EVERY = 72


def _parse_reconcile_cursor(
    raw: Any,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """(cursor, None) on a valid cursor; (None, reason) when reconcile must fall
    back to a full scan (missing or corrupt). Fail-closed by construction: any
    shape we cannot positively validate is UNKNOWN, never an empty cursor that
    would be read as "nothing to fold".

    Cursor shape: ``{"watermark": iso|None, "processed": {path: sig}, "streak": int}``.
    Accepts either the stored dict (normal path) or a JSON string (defensive).
    """
    if raw is None:
        return None, "cursor missing (first run or reclaimed state)"
    data: Any = raw
    if not isinstance(data, dict):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None, "cursor corrupt (unparseable JSON)"
    if not isinstance(data, dict) or not isinstance(data.get("processed"), dict):
        return None, "cursor corrupt (wrong shape)"
    wm = data.get("watermark")
    if wm is not None and (not isinstance(wm, str) or _parse_iso_utc(wm) is None):
        return None, "cursor corrupt (unparseable watermark)"
    streak = data.get("streak", 0)
    if not isinstance(streak, int) or isinstance(streak, bool) or streak < 0:
        streak = 0
    return {"watermark": wm,
            "processed": {str(k): str(v) for k, v in data["processed"].items()},
            "streak": streak}, None


def _mtime_from_uploaded_at(uploaded_at: Any) -> Optional[str]:
    """The feed's second-granular ISO ``uploaded_at`` -> the store's
    minute-granular listing format (``"2026-07-01 12:15PM UTC"``), so a row folded
    from the feed carries the SAME mtime a full scan's listing would stamp
    (byte-parity between the two paths). None if unparseable — the caller then
    treats the entry as doubt and full-scans."""
    dt = _parse_iso_utc(uploaded_at)
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%d %I:%M%p UTC")


_FEED_STATE_PRIORITY = {"archived": 0, "deleted": 1, "uploaded": 2}


def _feed_change_instant(change: dict[str, Any]) -> Optional[datetime]:
    """Return the lifecycle instant for one normalized data-updates row."""
    state = change.get("state")
    key = {"uploaded": "uploaded_at", "archived": "archived_at",
           "deleted": "deleted_at"}.get(state)
    value = change.get(key) if key else None
    # Archive/delete payloads can retain only uploaded_at.  It is still a
    # deterministic instant; total absence is feed doubt.
    return _parse_iso_utc(value or change.get("uploaded_at"))


def _collapse_feed_changes(
    changes: list[dict[str, Any]],
) -> Optional[dict[str, tuple[datetime, dict[str, Any]]]]:
    """Collapse normalized feed rows to one authoritative lifecycle per path.

    The feed does not promise order, and one window can contain both sides of a
    delete/recreate.  Latest instant wins; equal instants use the E2 lifecycle
    priority ``uploaded > deleted > archived`` so a same-second rewrite keeps
    the live shard.  ``None`` means an entry lacked a usable instant.
    """
    latest: dict[str, tuple[datetime, dict[str, Any]]] = {}
    for change in changes:
        path = str(change.get("path") or "")
        instant = _feed_change_instant(change)
        if instant is None:
            return None
        prior = latest.get(path)
        if prior is None or (
            instant, _FEED_STATE_PRIORITY[str(change.get("state"))]
        ) >= (
            prior[0], _FEED_STATE_PRIORITY[str(prior[1].get("state"))]
        ):
            latest[path] = (instant, change)
    return latest


def _feed_task_delta(
    transport: Any, team: str, *, cursor: dict[str, Any], now: str, log: Any
) -> Optional[tuple[dict[str, str], set, dict[str, str]]]:
    """What changed under ``task/`` since the cursor, via the data-updates feed.

    Returns ``(changed, deleted, new_processed)`` or **None for UNKNOWN** (=> the
    caller full-scans, fail-closed). ``changed`` maps slug -> the parity mtime for
    each ``uploaded`` task shard; ``deleted`` is the set of removed slugs;
    ``new_processed`` is the pruned processed-ledger to carry into the next pass.

    W4 pattern: the window is INCLUSIVE (``watermark`` back one skew margin) and a
    processed ledger keyed ``<path>:<state>:<uploaded_at>`` suppresses re-folding
    an entry already folded in a prior overlapping window. Every doubt (no feed
    support, feed error, an entry we cannot positively parse, an unparseable
    upload time) returns None — never a false "nothing changed"."""
    updates_fn = getattr(transport, "updates", None)
    if updates_fn is None:
        return None
    start = _parse_iso_utc(cursor.get("watermark"))
    end = _parse_iso_utc(now)
    if start is None or end is None:
        return None  # no usable watermark -> full scan seeds one
    span = (end - start).total_seconds() + FAST_PATH_SKEW_MARGIN_SECONDS
    # An anchor so old the query would 500 on an over-wide window is unusable —
    # the same ceiling the ack change-query respects. Skip to the full scan.
    if span <= 0 or span > ACKS_ANCHOR_MAX_HOURS * 3600 + FAST_PATH_SKEW_MARGIN_SECONDS:
        return None
    period = f"{int(span)} seconds"
    try:
        try:
            changes = updates_fn(period, team=team)
        except TypeError:
            changes = updates_fn(period)   # pre-team-kwarg duck-typed transports
    except Exception as e:
        log.warn("reconcile: data-updates delta raised; full scan",
                 team=team, error=str(e))
        return None
    if not isinstance(changes, list):
        return None
    task_pfx = task_prefix(team)
    prior_processed = cursor.get("processed", {})
    relevant: list[dict[str, Any]] = []
    margin_floor = end - timedelta(seconds=2 * FAST_PATH_SKEW_MARGIN_SECONDS)
    for c in changes:
        # Shape guard, fail-CLOSED: any entry we cannot positively parse is doubt,
        # and doubt is UNKNOWN — feed-shape drift degrades to a full scan, never to
        # false no-change evidence (the fast path's rule, extended).
        if not isinstance(c, dict):
            return None
        path = c.get("path", c.get("full_name"))
        state = c.get("state")
        if not isinstance(path, str) or not path.strip():
            return None
        norm = path.strip().lstrip("/")
        if not norm.startswith(task_pfx):
            continue  # not a task-namespace change
        rest = norm[len(task_pfx):]
        # Direct task shards only: index/log are derived OUTPUTS, and a nested path
        # (task/archive/…) is not a live task row.
        if "/" in rest or not rest.endswith(".md") or rest in ("index.md", "log.md"):
            continue
        # Only a direct task change can affect this fold.  Irrelevant feed rows
        # may be the older path-only shape accepted by the global fast path.
        if state not in ("uploaded", "archived", "deleted"):
            return None
        relevant.append({
            "path": norm,
            "state": state,
            "uploaded_at": c.get("uploaded_at"),
            "archived_at": c.get("archived_at"),
            "deleted_at": c.get("deleted_at"),
        })

    collapsed = _collapse_feed_changes(relevant)
    if collapsed is None:
        return None

    changed: dict[str, str] = {}
    deleted: set = set()
    new_processed: dict[str, str] = {}
    for norm, (instant, event) in collapsed.items():
        state = str(event.get("state"))
        slug = norm[len(task_pfx):-3]
        at = instant.isoformat().replace("+00:00", "Z")
        sig = f"{state}:{at}"
        already = prior_processed.get(norm) == sig
        # Keep only recent entries in the next ledger — those are the only ones an
        # overlapping next window can re-surface; older entries can't reappear.
        if instant >= margin_floor:
            new_processed[norm] = sig
        if already:
            continue
        if state == "uploaded":
            changed[slug] = instant.strftime("%Y-%m-%d %I:%M%p UTC")
        else:  # archived | deleted -> the row goes away
            deleted.add(slug)
    return changed, deleted, new_processed


def _project_task_row(
    transport: Any, team: str, slug: str, *, mtime: Any, size: Any, log: Any
) -> tuple[Optional[dict[str, Any]], Optional[str], bool]:
    """Read + project ONE task shard into a row, the identical way the full scan
    does (parity by construction). Returns ``(row_or_None, warning_or_None,
    read_failed)``. ``read_failed`` True means the shard would not READ — distinct
    from unparseable — so the incremental caller can fall back to a full scan
    rather than advance on a row it could not confirm.

    ``size`` may be None on the incremental path (the feed carries no byte size);
    the row is then stamped from the read content's UTF-8 length, which equals the
    store's listed byte size for the same bytes."""
    name = f"{slug}.md"
    content = transport.read(f"{task_prefix(team)}{name}")
    if content is None:
        return None, None, True
    fm = okf.parse_frontmatter(content)
    if fm is None:
        return None, f"{name}: unparseable frontmatter", False
    if not model.is_task(fm):
        return None, None, False  # not a Task concept doc — holds no row
    row = model.row_from_frontmatter(fm, name=slug, path=f"task/{name}", mtime=mtime)
    row["size"] = size if size is not None else f"{len(content.encode('utf-8'))}B"
    return row, None, False


def _full_scan_task_rows(
    transport: Any, team: str, listing: list, prior_by_name: dict[str, Any],
    last_reconcile_iso: Any, log: Any,
) -> tuple[list[dict[str, Any]], list[str], int, int]:
    """Fold the full ``task/`` listing into rows, reusing unchanged prior rows.
    The authoritative pass — the fail-closed fallback and the drift self-check's
    reference. Returns ``(rows, warnings, reused, parsed)``."""
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
        # Incremental reuse rests on THREE listing-only checks (see the long note
        # this loop carried before extraction): mtime unchanged, byte size
        # unchanged AND carried, the mtime-minute provably closed before our last
        # read, and the row-schema stamp current.
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
        row, warning, _read_failed = _project_task_row(
            transport, team, slug, mtime=entry_mtime, size=entry_size, log=log)
        if row is None:
            # unparseable / unreadable / non-task: never drop a task — keep prior.
            if warning is not None:
                if prior:
                    warnings.append(f"{warning}, kept prior row")
                    rows.append(prior)
                else:
                    warnings.append(f"{warning}, no prior row, skipped")
            continue
        rows.append(row)
        parsed += 1
    return rows, warnings, reused, parsed


def _incremental_reconcile_rows(
    transport: Any, team: str, prior_by_name: dict[str, Any], *,
    changed: dict[str, str], deleted: set, log: Any,
) -> Optional[tuple[list[dict[str, Any]], int, int, list[str]]]:
    """Fold ONLY the changed/deleted task shards into the prior rows, in place.

    Returns ``(rows, parsed, reused, warnings)`` or **None** if a changed shard
    would not READ — doubt about a row we know changed, so the caller full-scans.
    Rows come back name-sorted to match a full scan's (sorted) listing order, so
    the two paths' summaries.json are byte-identical for the same end state."""
    by_name = dict(prior_by_name)
    warnings: list[str] = []
    parsed = 0
    for slug, mt in sorted(changed.items()):
        row, warning, read_failed = _project_task_row(
            transport, team, slug, mtime=mt, size=None, log=log)
        if read_failed:
            log.warn("reconcile: changed shard would not read; full scan",
                     team=team, slug=slug)
            return None
        if row is None:
            # unparseable or no-longer-a-Task: mirror the full scan exactly.
            if warning is not None and slug in by_name:
                warnings.append(f"{warning}, kept prior row")
            elif warning is not None:
                warnings.append(f"{warning}, no prior row, skipped")
                by_name.pop(slug, None)
            else:
                by_name.pop(slug, None)  # no longer a Task doc -> drop its row
            continue
        by_name[slug] = row
        parsed += 1
    for slug in deleted:
        by_name.pop(slug, None)
    rows = [by_name[k] for k in sorted(by_name)]
    return rows, parsed, len(rows) - parsed, warnings


def _rows_diverged(incremental_rows: list, full_rows: list) -> list[str]:
    """Slugs where the incremental-maintained view differs from the authoritative
    full scan — the drift self-check. Compares the consumer-visible row dicts."""
    inc = aggregate.rows_by_name(incremental_rows)
    full = aggregate.rows_by_name(full_rows)
    return sorted(name for name in set(inc) | set(full)
                  if inc.get(name) != full.get(name))


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

    prefix = task_prefix(team)

    # --- E1 row build: feed-delta incremental fold, full scan as fallback -----
    # Cursor + backstops decide the pass shape. A cursor is the incremental path's
    # licence; the two backstops force an authoritative full scan (which doubles as
    # the drift self-check when an incremental view was available):
    #   * streak backstop — every Nth incremental pass, so a change the feed never
    #     reported cannot hide past a bounded number of passes;
    #   * time backstop  — MAX_FAST_PATH_HOURS, the same net the fast path uses.
    prior_cursor, cursor_reason = _parse_reconcile_cursor(
        (prior_agg or {}).get(RECONCILE_CURSOR_KEY))
    full_every = config.env_int(
        "COORD_RECONCILE_FULL_EVERY", DEFAULT_RECONCILE_FULL_EVERY)
    if not isinstance(full_every, int) or full_every < 1:
        full_every = DEFAULT_RECONCILE_FULL_EVERY
    rec_streak = prior_cursor["streak"] if prior_cursor else 0
    agg_age = age_hours(last_reconcile_iso, now) if last_reconcile_iso else None
    stale = agg_age is None or agg_age < 0 or agg_age > MAX_FAST_PATH_HOURS
    due_for_full = prior_cursor is None or rec_streak + 1 >= full_every or stale

    # Fast path — nothing relevant changed — only when NOT owed a full/drift pass.
    # It still advances the E1 cursor after independently confirming the feed
    # delta: unrelated events must not leave an old watermark to grow forever.
    if not due_for_full and _fast_path_no_changes(transport, team, prior_agg, now=now, log=log):
        fast_delta = _feed_task_delta(
            transport, team, cursor=prior_cursor, now=now, log=log)
        if fast_delta is not None:
            fast_changed, fast_deleted, fast_processed = fast_delta
            if not fast_changed and not fast_deleted:
                fast_agg = dict(prior_agg or {})
                fast_agg[RECONCILE_CURSOR_KEY] = {
                    "watermark": now,
                    "processed": fast_processed,
                    "streak": rec_streak + 1,
                }
                if not transport.write(
                        summaries_path(team), jsonutil.dumps(fast_agg)):
                    result = {
                        "degraded": True,
                        "reason": "reconcile cursor write failed",
                        "tasks": len(prior_rows),
                    }
                    _write_health_shard(
                        transport, team, host=host, now=now, result=result, log=log)
                    return result
                # NOTE: warnings from the prior aggregate are not resurfaced here;
                # they reappear on the next full pass (<= MAX_FAST_PATH_HOURS away).
                result = {
                    "tasks": len(prior_rows),
                    "parsed": 0,
                    "reused": len(prior_rows),
                    "transitions": 0,
                    "warnings": [],
                    "fast_path": True,
                }
                _write_health_shard(
                    transport, team, host=host, now=now, result=result, log=log)
                log.info(
                    "reconciled (fast path)", team=team, tasks=len(prior_rows))
                return result

    # Try the feed-delta fold when a cursor is usable; None at any step is doubt.
    inc: Optional[tuple] = None
    if prior_cursor is not None:
        delta = _feed_task_delta(transport, team, cursor=prior_cursor, now=now, log=log)
        if delta is not None:
            changed, deleted, new_processed = delta
            folded = _incremental_reconcile_rows(
                transport, team, prior_by_name,
                changed=changed, deleted=deleted, log=log)
            if folded is not None:
                inc = (*folded, new_processed)  # (rows, parsed, reused, warnings, processed)

    incremental = False
    drift_detected = False
    if inc is not None and not due_for_full:
        rows, parsed, reused, warnings, inc_processed = inc
        new_cursor = {"watermark": now, "processed": inc_processed,
                      "streak": rec_streak + 1}
        incremental = True
        log.info("reconciled (incremental)", team=team, tasks=len(rows),
                 parsed=parsed, reused=reused)
    else:
        # Full scan — authoritative. Reseeds the cursor (streak 0). When an
        # incremental view was available this pass (a backstop is due, not a feed
        # failure), compare the two: a divergence is the drift the self-check
        # exists to catch — logged LOUD, and rebuilt from this full scan.
        try:
            listing = transport.list_dir(prefix)
        except TransportError as e:
            log.error("list failed, pass aborted (prior artifacts intact)",
                      team=team, error=str(e))
            return {"degraded": True, "reason": str(e), "tasks": 0}
        rows, warnings, reused, parsed = _full_scan_task_rows(
            transport, team, listing, prior_by_name, last_reconcile_iso, log)
        new_cursor = {"watermark": now, "processed": {}, "streak": 0}
        if inc is not None:
            diverged = _rows_diverged(inc[0], rows)
            if diverged:
                drift_detected = True
                warnings.append(
                    "reconcile-drift: incremental view diverged from full scan on "
                    f"{diverged}; rebuilt from full scan")
                log.error("reconcile drift detected; rebuilt from full scan",
                          team=team, slugs=diverged)
        if cursor_reason:
            log.info("reconcile: full scan (cursor unusable)", team=team,
                     reason=cursor_reason)

    # --- retention sub-pass (enabled by default) ---
    # the fold budgets. Precedence: --retention-days flag > COORD_RETENTION_DAYS >
    # legacy FULCRA_COORD_RETENTION_DAYS. The legacy prefix is alias-ACCEPTED (an
    # operator copying old fulcra-coord docs still gets retention), but its old
    # 30-day default is not adopted. The dedicated parser accepts explicit zero
    # as a kill switch; invalid,
    # negative, or non-finite values fall back to the enabled safe default.
    archived_map: dict = {}
    days = config.retention_days(DEFAULT_RETENTION_DAYS, override=retention_days)
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
    # The E1 reconcile cursor rides here too (watermark + processed ledger + the
    # incremental-streak backstop counter). THIS build owns it — it is cut from the
    # passthrough below and rewritten in full every pass, so a stale value can never
    # resurrect. The fast path advances it in its own verified write above.
    ack_state[RECONCILE_CURSOR_KEY] = new_cursor
    # `prior` carries any top-level key a NEWER host wrote that this build does not
    # know about — see build_aggregate's invariant: rebuilding from a fixed key set
    # is what killed v1.6.8's anchor on the live mixed fleet. The ack keys are cut
    # from the passthrough first because THIS build owns them: `ack_state` above is
    # their complete, recomputed value, including the case where it deliberately
    # writes no anchor at all (inconclusive fold, no prior anchor). Passing them
    # through would resurrect a value this pass decided not to keep.
    prior_unknown = {k: v for k, v in (prior_agg or {}).items()
                     if k not in (ACKS_ANCHOR_KEY, ACKS_STREAK_KEY,
                                  RECONCILE_CURSOR_KEY)}
    agg = aggregate.build_aggregate(
        team, rows, generated_at=now, reconcile_host=host, warnings=warnings,
        state=ack_state, prior=prior_unknown,
    )
    if not transport.write(summaries_path(team), jsonutil.dumps(agg)):
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
        "incremental": incremental,
        "drift_detected": drift_detected,
    }
