"""View generation for fulcra-coord.

Generates materialized JSON views from a list of task dicts:
  - index.json               — global compact index with counts
  - views/active.json        — all active/waiting/blocked tasks
  - views/next.json          — proposed + waiting (candidates for starting)
  - views/recently-done.json — done/abandoned within retention window
  - views/search-index.json  — tag/title/summary records for search
  - views/needs-attention.json — active tasks gone stale
  - views/summaries.json     — the compact read-side aggregate (fast path)
  - workstreams/{ws}.json    — per-workstream active view

(The per-agent ``agents/{agent}.json`` and per-assignee
``views/inbox/{slug}.json`` views were retired 2026-06-11 — rebuilt and
uploaded on every write with zero readers; see build_all_views.)
"""

from __future__ import annotations

import re as _re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import env_float, task_file_path
from .schema import task_summary
from .schema import VALID_KINDS as _schema_valid_kinds

# Statuses at which a directive is "open" in someone's inbox: proposed (not yet
# acted on) or waiting (parked, still awaiting the assignee). active/blocked are
# in-flight work, terminal states are finished — none belong in an inbox.
INBOX_OPEN_STATUSES = ("proposed", "waiting")

# Wildcard assignee: a directive addressed to BROADCAST belongs in EVERY agent's
# inbox (feature #2 — all-agents broadcast). It is the durable "tell every agent
# X" primitive; per-agent acks (one inbox_ack event per `by`) let each agent
# clear it independently without removing it for the others. A real agent id can
# never be "*" (ids are kind:host:repo triples), so the sentinel can't collide.
BROADCAST = "*"

# Role-audience prefix. A directive whose assignee starts with "@" and names a
# non-empty role (e.g. "@coord-maintainer") is addressed to a logical ROLE, not a
# frozen agent id. It is delivered — at READ time — to whoever currently HOLDS
# that role (declared via presence capabilities), so a directive to "the coord
# maintainer" reaches the LIVE holder(s) instead of rotting in one stale agent's
# dead inbox. A real agent id is a "kind:host:repo" triple and never begins with
# "@", and the BROADCAST sentinel is "*", so the prefix can't collide with either.
ROLE_PREFIX = "@"


def is_role_audience(assignee: Any) -> bool:
    """True when ``assignee`` is a ROLE audience (``@<role>`` with a non-empty role).

    A role audience is resolved at delivery time against the calling agent's
    declared roles (see inbox_for), NOT at send time — that late binding is the
    whole point: the directive follows whoever HOLDS the role, not a fixed id.

    Strict: a bare ``@`` (no role name, or only whitespace after it) is malformed,
    not a role audience, so it can never accidentally match every agent or no agent
    ambiguously. A None/empty/concrete/broadcast assignee is not a role audience."""
    return (
        isinstance(assignee, str)
        and assignee.startswith(ROLE_PREFIX)
        and bool(assignee[len(ROLE_PREFIX):].strip())
    )


def role_of(assignee: str) -> str:
    """The role name behind a ``@<role>`` audience (the ``@`` stripped, trimmed).

    Only meaningful when is_role_audience(assignee) is True; the caller gates on
    that first. Trimmed so ``@ coord `` and ``@coord`` resolve to the same role."""
    return assignee[len(ROLE_PREFIX):].strip()


RECENTLY_DONE_DAYS = 7
SEARCH_INDEX_DONE_DAYS = 30
# Default age (days) after which a still-`proposed` BROADCAST directive drops out
# of the live inbox view. Broadcasts are informational fan-out ("X joined the
# mesh") — once a few days old they have served their purpose and only clutter
# every agent's inbox / SessionStart banner. Aging is a READ filter only: it
# never touches task status or the task file (a peer on an older CLI still sees
# it), and `inbox --all` bypasses it. CONCRETE-assignee directives (real asks)
# are NEVER aged out regardless of this threshold.
INBOX_AGE_DAYS_DEFAULT = 3
# Default age (days) after which a still-`proposed` BROADCAST is auto-EXPIRED
# (transitioned proposed->abandoned by the retention pass, then cold-archived on a
# later sweep; recoverable via `restore`). Distinct from INBOX_AGE_DAYS, which is a
# READ filter only: inbox age-out (3d) hides a broadcast from the live view but
# leaves it on the bus; expiry (14d) is the DESTRUCTIVE garbage-collection that
# finally clears never-claimed fan-out off the bus so `status` stops drowning in
# stale "X is LIVE — UPDATE NOW" directives. The wider window is deliberate: a
# broadcast must survive well past its inbox-relevance before we abandon it.
BROADCAST_EXPIRY_DAYS_DEFAULT = 14
# Default age (days) after which a still-`proposed` MESSAGE-CLASS directive (a
# tell / FYI / verdict echo with a concrete assignee and NO expected response)
# is auto-CLOSED (transitioned proposed->done by the retention pass, with an
# evidence string naming the TTL). Message-class records carry information,
# they don't request work: once delivered and aged they have served their
# purpose, but nobody ever marks them done, so they pile up as
# status=proposed forever and bloat every listing (the 2026-06-11 operational
# finding: 211 of ~480 hot tasks). Distinct from BROADCAST_EXPIRY_DAYS (fan-out
# has its own expiry pass) and DELIBERATELY NARROW: anything that expects a
# response — or carries a non-tell loop kind — is never touched (the
# closed-loop guarantee; see is_closable_message).
MESSAGE_TTL_DAYS_DEFAULT = 7
# Default staleness threshold (hours). An `active` task whose updated_at is older
# than this is "possibly forgotten" and surfaced in views/needs-attention.json.
STALE_HOURS_DEFAULT = 2


def _stale_hours(stale_hours: Optional[float] = None) -> float:
    """Resolve the staleness threshold: explicit arg > env > default.

    Centralizing this (Gap 2) means hooks, the status view, and the reconciler
    all agree on what "stale" means instead of recomputing it ad-hoc.
    """
    return env_float("FULCRA_COORD_STALE_HOURS", STALE_HOURS_DEFAULT, override=stale_hours)


# Wall-clock grace (seconds) the resolver tolerates BEYOND the idle->stale
# cutoff before treating an agent as below routing floor. A single missed
# heartbeat or a laptop sleep/wake must not drop a reviewer. Expressed as an
# ABSOLUTE duration (not a count of listener intervals) because listener
# cadence differs per machine, while presence last_seen is bus-global, so the
# grace evaluates identically on every machine (machine-agnostic invariant).
PRESENCE_GRACE_SECONDS_DEFAULT = 1200.0  # 20 min


def _presence_grace_seconds(grace: Optional[float] = None) -> float:
    """Resolve the routing presence grace (seconds): explicit arg > env > default."""
    return env_float("FULCRA_COORD_PRESENCE_GRACE_SECONDS",
                     PRESENCE_GRACE_SECONDS_DEFAULT, override=grace)


# Staleness ceiling (minutes) for the materialized READ views (the summaries
# aggregate and the presence aggregate). Views refresh only when a write/
# reconcile successfully UPLOADS them, so under backend write-throttling they
# can lag the durable task/presence files by hours while reads keep trusting
# them — the 2026-06-10 stale-view blindness (inboxes looked empty, a live
# reviewer looked dead). Past this age, readers fall back to listing the
# durable files directly. 20 minutes ≈ several reconcile cadences: a healthy
# bus never trips it, a throttled one degrades to slower-but-correct reads.
VIEW_STALE_MIN_DEFAULT = 20.0


def _view_stale_min(stale_min: Optional[float] = None) -> float:
    """Resolve the view-staleness ceiling (minutes): explicit arg > env > default.

    ``FULCRA_COORD_VIEW_STALE_MIN=0`` (or negative) disables the guard entirely
    — reads trust the materialized views unconditionally, the pre-guard
    behavior."""
    return env_float("FULCRA_COORD_VIEW_STALE_MIN", VIEW_STALE_MIN_DEFAULT,
                     override=stale_min)


def view_staleness_minutes(view: dict[str, Any], now: Optional[datetime] = None,
                           stale_min: Optional[float] = None) -> Optional[float]:
    """Age of a materialized view in minutes IF it is stale, else ``None``.

    ``None`` (read = "trust the view") in three cases:
      * the guard is disabled (``FULCRA_COORD_VIEW_STALE_MIN`` <= 0);
      * the view has NO ``generated_at`` — an older bus that predates the
        stamp. Back-compat: behave exactly as before the guard existed;
      * the view is younger than the ceiling.

    A PRESENT-but-unparseable ``generated_at`` ages to +inf (via ``_age_hours``)
    and so reads as stale — the same fail-toward-surfacing choice ``is_stale``
    makes: a new-format bus emitting garbage stamps degrades to the slower
    direct read (visible via the caller's warn) rather than silently trusting a
    view of unknown age."""
    threshold = _view_stale_min(stale_min)
    if threshold <= 0:
        return None
    generated_at = view.get("generated_at")
    if not generated_at:
        return None
    if now is None:
        now = _now()
    age_min = _age_hours(generated_at, now) * 60.0
    return age_min if age_min >= threshold else None


def _inbox_age_days(age_days: Optional[float] = None) -> float:
    """Resolve the broadcast inbox age cutoff (days): explicit arg > env > default.

    Mirrors _stale_hours so the knob is read in exactly one place. The env var
    FULCRA_COORD_INBOX_AGE_DAYS lets a fleet tune how long informational
    broadcasts linger; a non-numeric value falls back to the default rather than
    crashing a read path.
    """
    return env_float("FULCRA_COORD_INBOX_AGE_DAYS", INBOX_AGE_DAYS_DEFAULT,
                     override=age_days)


def is_aged_out_broadcast(task: dict[str, Any], now: Optional[datetime] = None,
                          age_days: Optional[float] = None) -> bool:
    """True when `task` is a stale informational BROADCAST that should drop out of
    the default inbox view.

    A directive ages out ONLY when ALL of these hold:
      * assignee == BROADCAST ("*") — it is fan-out, not a personal ask. A
        directive addressed to a CONCRETE agent is a real ask and is NEVER aged
        out here, regardless of age (callers rely on this exact guarantee).
      * status == "proposed" — still un-acted-on informational noise. (A waiting
        broadcast was deliberately parked; we don't second-guess that.)
      * its updated_at is older than the age cutoff (now - age_days).

    Age is measured from `updated_at` vs `now`. A missing/unparseable timestamp
    ages to +inf (via _age_hours), so a clock-less old broadcast is treated as
    aged — the same fail-toward-cleanup choice is_stale makes. This is a pure
    predicate over a read filter: it never mutates the task and never changes its
    status, so the underlying task file/aggregate is untouched and a peer on an
    older CLI still sees the broadcast.
    """
    if task.get("assignee") != BROADCAST:
        return False
    if task.get("status") != "proposed":
        return False
    if now is None:
        now = _now()
    cutoff_hours = _inbox_age_days(age_days) * 24.0
    return _age_hours(task.get("updated_at", ""), now) >= cutoff_hours


def _broadcast_expiry_days(expiry_days: Optional[float] = None) -> float:
    """Resolve the broadcast auto-expiry cutoff (days): explicit arg > env > default.

    Mirrors _inbox_age_days so the knob is read in exactly one place. The env var
    FULCRA_COORD_BROADCAST_EXPIRY_DAYS lets a fleet tune how long a never-claimed
    broadcast survives before it's abandoned; a non-numeric value falls back to the
    default rather than crashing the best-effort retention pass.
    """
    return env_float("FULCRA_COORD_BROADCAST_EXPIRY_DAYS",
                     BROADCAST_EXPIRY_DAYS_DEFAULT, override=expiry_days)


def is_expirable_broadcast(task: dict[str, Any], now: Optional[datetime] = None,
                           expiry_days: Optional[float] = None) -> bool:
    """True when `task` is a stale never-claimed BROADCAST that should be auto-
    EXPIRED (abandoned, then cold-archived on a later sweep, recoverable via
    `restore`).

    A broadcast expires ONLY when ALL of these hold:
      * assignee == BROADCAST ("*") — it is fan-out, not a personal ask. A
        directive addressed to a CONCRETE agent is a real ask and is NEVER expired,
        regardless of age (the same concrete-assignee guarantee is_aged_out_broadcast
        makes).
      * status == "proposed" — still un-acted-on. A waiting/active/terminal
        broadcast was deliberately picked up or parked; we never garbage-collect it.
      * its created_at is older than the expiry cutoff (now - expiry_days).

    Age is measured from `created_at`, NOT `updated_at`: a broadcast's created date
    is its true age, whereas updated_at can be bumped by view rebuilds — measuring
    from updated_at would keep resetting the expiry clock and never collect it.

    SAFE DIRECTION on a missing/unparseable created_at — the OPPOSITE of
    is_aged_out_broadcast: that predicate is a read-only filter, so a clockless
    broadcast fails toward aging OUT (via _age_hours -> +inf). This predicate drives
    a DESTRUCTIVE abandon->archive, so a clockless broadcast must FAIL SAFE: we
    return False and never expire what we can't date (exactly like is_archivable_task,
    via _parse_dt + `if dt is None: return False`). Boundary: age >= expiry_days
    qualifies (a broadcast created exactly N days ago expires), matching
    is_archivable_task's >= semantics.
    """
    if task.get("assignee") != BROADCAST:
        return False
    if task.get("status") != "proposed":
        return False
    if now is None:
        now = _now()
    dt = _parse_dt(task.get("created_at"))
    if dt is None:
        return False
    return (now - dt).total_seconds() / 86400.0 >= _broadcast_expiry_days(expiry_days)


def _message_ttl_days(ttl_days: Optional[float] = None) -> float:
    """Resolve the message-class TTL (days): explicit arg > env > default.

    Mirrors _broadcast_expiry_days so the knob is read in exactly one place. The
    env var FULCRA_COORD_MESSAGE_TTL_DAYS lets a fleet tune how long a delivered
    message lingers before auto-close; a non-numeric value falls back to the
    default rather than crashing the best-effort retention pass.
    """
    return env_float("FULCRA_COORD_MESSAGE_TTL_DAYS",
                     MESSAGE_TTL_DAYS_DEFAULT, override=ttl_days)


# Task-level kind tags, mirrored as literals (the directives.py pattern: the
# canonical constants live in routing / routing_ops, which sit ABOVE views in
# the layering, so importing them here would invert the dependency graph;
# test_message_kind_tags_match_routing pins the literals against the source).
#
#   * `kind:<task-kind>` (ops/feature/...) — schema.VALID_KINDS, the WORK
#     taxonomy build_tags stamps on every task. Says nothing about loops.
#   * loop-kind MEMBERSHIP markers — extra tags the directive dual-write maps
#     to loop kinds: kind:review (routing.REVIEW_TAG, an ask),
#     kind:dispatch (routing.DISPATCH_TAG, the expects_response marker),
#     kind:idea (routing.IDEA_TAG, the durable backlog pipeline),
#     kind:review-verdict (routing_ops.REVIEW_VERDICT_TAG, a delivered echo).
#
# is_closable_message ALLOWLISTS: a task is message-class only when every
# kind: tag it carries is either a plain work-kind or the verdict-echo marker.
# Any OTHER kind: tag (review/dispatch/idea, the registered-but-unwired
# question/signoff, or a future loop kind we don't know yet) FAILS SAFE to
# "keep" — an unknown kind must never be garbage-collected on a guess.
_TASK_KIND_TAGS = {f"kind:{k}" for k in _schema_valid_kinds}
_MESSAGE_KIND_TAGS = {"kind:tell", "kind:review-verdict"}
# The dispatch marker doubles as the on-task expects_response flag
# (`tell --expects-response` / handoff). Named here for the explicit
# closed-loop check below, although the allowlist alone would also catch it.
_DISPATCH_TAG = "kind:dispatch"


def is_closable_message(task: dict[str, Any], now: Optional[datetime] = None,
                        ttl_days: Optional[float] = None) -> bool:
    """True when `task` is a delivered MESSAGE-CLASS directive that should be
    auto-CLOSED (transitioned proposed->done by the retention pass, evidence
    naming the TTL — recoverable via `restore` after the later cold-archive).

    Message-class = a tell / FYI / verdict echo: it CARRIES information to a
    concrete recipient and expects nothing back. Once delivered and aged it has
    served its purpose, but nobody ever marks delivered messages done, so they
    accumulate as status=proposed forever (2026-06-11: 211 of ~480 hot tasks)
    and bloat every listing the platform gateway serves under its ~15s limit.

    A message closes ONLY when ALL of these hold:
      * assignee is a CONCRETE audience (truthy, not the BROADCAST "*"): a
        directive, not a self-owned work item — an aged proposed `start` task
        is BACKLOG and is never auto-done. Broadcasts have their OWN expiry
        (is_expirable_broadcast) and are never double-handled here.
      * expects_response is FALSY — THE CLOSED-LOOP GUARANTEE, the load-bearing
        exclusion: a loop that expects a response stays open until a bus-native
        response closes it, PERIOD; no age may override that. Both task-level
        representations are honored: the explicit field and the kind:dispatch
        marker tag.
      * its loop kind is `tell` (or absent — plain directives map to the legacy
        tell kind): every kind: tag must be a plain work-kind or the
        verdict-echo marker. review/dispatch/question/signoff are asks with
        their own lifecycles; idea is the durable backlog pipeline; unknown
        kinds fail safe to KEEP (see _TASK_KIND_TAGS / _MESSAGE_KIND_TAGS).
      * status == "proposed" — still un-acted-on. A picked-up / parked /
        terminal directive was handled deliberately; we never second-guess it.
      * its created_at is older than the TTL (now - ttl_days). Measured from
        created_at, not updated_at, for the same reason as broadcast expiry:
        view rebuilds bump updated_at and would reset the clock forever.

    SAFE DIRECTION on a missing/unparseable created_at: this predicate drives a
    TERMINAL transition, so parse-don't-compare — _parse_dt failing means we
    KEEP the message (exactly like is_expirable_broadcast / is_archivable_task).
    Boundary: age >= ttl_days qualifies, matching the sibling predicates."""
    assignee = task.get("assignee")
    if not assignee or assignee == BROADCAST:
        return False
    if task.get("status") != "proposed":
        return False
    # CLOSED-LOOP GUARANTEE: an expecting loop is never auto-closed.
    if task.get("expects_response"):
        return False
    tags = task.get("tags") or []
    if _DISPATCH_TAG in tags:
        return False
    for tag in tags:
        if (tag.startswith("kind:") and tag not in _TASK_KIND_TAGS
                and tag not in _MESSAGE_KIND_TAGS):
            return False  # ask-kind / pipeline / unknown loop kind: keep
    if now is None:
        now = _now()
    dt = _parse_dt(task.get("created_at"))
    if dt is None:
        return False
    return (now - dt).total_seconds() / 86400.0 >= _message_ttl_days(ttl_days)


def _age_hours(updated_at: str, now: datetime) -> float:
    """Age of a timestamp in hours. Missing/unparseable -> +inf (maximally
    stale): an active task that lost its clock is exactly the forgotten case we
    want to surface, never silently treat as fresh (consistent with the I4 fix)."""
    dt = _parse_dt(updated_at)
    if dt is None:
        return float("inf")
    return (now - dt).total_seconds() / 3600.0


def is_stale(task: dict[str, Any], now: Optional[datetime] = None,
             stale_hours: Optional[float] = None) -> bool:
    """True when an `active` task is older than the staleness threshold.

    Only `active` tasks can be stale — waiting/blocked are deliberately parked,
    and terminal tasks are finished, so neither is "forgotten work churning."
    """
    if task.get("status") != "active":
        return False
    if now is None:
        now = _now()
    return _age_hours(task.get("updated_at", ""), now) >= _stale_hours(stale_hours)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(iso: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp to a tz-AWARE UTC datetime, or None.

    BUG 8: a tz-less string parses to a NAIVE datetime; subtracting it from the
    aware ``now`` in ``_age_hours`` raised TypeError, breaking the +inf fail-safe
    that the liveness/aging docstrings promise (missing/unparseable -> +inf, i.e.
    maximally stale). Coerce a naive result to UTC here, in the shared helper, so
    every caller (_age_hours, presence_liveness, is_aged_out_broadcast, is_stale,
    inbox_for) is safe — matching how the schedule layer already assumes UTC."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _done_at(t: dict[str, Any]) -> str:
    """Resolve a task's done/abandoned timestamp from EITHER a full body (nested
    ``done.done_at``) OR a summary dict (flat ``done_at`` emitted by
    schema.task_summary), falling back to ``updated_at``.

    This is the linchpin of the summaries-as-view-source refactor: builders gate
    recently-done / search retention on this timestamp, and they must compute the
    SAME value whether handed full task bodies or task_summary dicts. Centralizing
    the lookup here (instead of the old inline ``t.get("done",{}).get("done_at")``,
    which returned None for a summary that has no nested ``done`` block) is what
    makes build_all_views(summaries) == build_all_views(full_bodies)."""
    return (t.get("done") or {}).get("done_at") or t.get("done_at") or t.get("updated_at", "")


# ---------------------------------------------------------------------------
# Retention / archival policy predicates (pure, zero-I/O) — live here beside
# the other age predicates (is_stale / is_aged_out_broadcast). Every gate goes
# through _parse_dt; NONE compares ISO strings lexically.
# ---------------------------------------------------------------------------

# Default age (days) after which a TERMINAL (done/abandoned) task is moved out
# of the hot path into the cold archive. 30d keeps a month of finished work
# instantly visible in recently-done/search before it cold-stores. Tunable via
# FULCRA_COORD_RETENTION_DAYS for a fleet that wants a longer/shorter hot window.
RETENTION_DAYS_DEFAULT = 30
# Spent digest dedup markers older than this are pruned (deleted). They are
# regenerable guards with no history value; 7d is ample slack past the daily
# windows that could still consult them.
MARKER_RETENTION_DAYS_DEFAULT = 7
# Dead-agent presence records older than this are pruned. Presence is a live
# snapshot, not history; a record untouched for 30d is a long-departed agent.
PRESENCE_RETENTION_DAYS_DEFAULT = 30


def _retention_days(days=None):
    """Resolve the task archive age (days): explicit arg > env > default (mirrors
    _stale_hours). A non-numeric FULCRA_COORD_RETENTION_DAYS falls back to the
    default rather than crashing the best-effort retention pass."""
    return env_float("FULCRA_COORD_RETENTION_DAYS", RETENTION_DAYS_DEFAULT,
                     override=days)


def _marker_retention_days(days=None):
    """Resolve the digest-marker prune age (days): explicit arg > env > default."""
    return env_float("FULCRA_COORD_MARKER_RETENTION_DAYS",
                     MARKER_RETENTION_DAYS_DEFAULT, override=days)


def _presence_retention_days(days=None):
    """Resolve the dead-presence prune age (days): explicit arg > env > default."""
    return env_float("FULCRA_COORD_PRESENCE_RETENTION_DAYS",
                     PRESENCE_RETENTION_DAYS_DEFAULT, override=days)


def is_archivable_task(task, now=None, retention_days=None):
    """True when a task is terminal (done/abandoned) AND aged past the retention
    window, so it should be cold-archived out of the hot path.

    Aged is measured from the done/abandoned timestamp (_done_at: nested
    done.done_at OR flat done_at, falling back to updated_at), PARSED via
    _parse_dt (never lexical). Non-terminal statuses (active/waiting/blocked/
    proposed) are live work and NEVER qualify regardless of age.

    SAFE DIRECTION on a missing/unparseable timestamp: unlike is_stale (which
    fails toward SURFACING a clockless task), archiving is a destructive MOVE, so
    a clockless terminal task is NOT archived — we never move what we can't date.
    Boundary: age >= retention_days qualifies (a task done exactly N days ago is
    archivable), matching the recently-done cutoff's >= semantics.

    RESTORED TASKS (F7, 2026-06-11 wave): ``cmd_restore`` stamps ``restored_at``
    on the hot body it re-uploads. A restored task is still terminal and still
    aged from its original done stamp, so aging from done_at alone re-archived
    it on the very next daily pass — silently undoing the operator's restore.
    Eligibility therefore ages from max(done_at, restored_at): a restore opens
    a FULL fresh retention window. A restored_at that is PRESENT but
    unparseable fails toward KEEPING (a restore demonstrably happened, at an
    unknown time — never move what we can't date), exactly like the done
    stamp; a restored_at older than the done stamp is inert under max() (a
    restore can never make a task MORE archivable)."""
    if task.get("status") not in ("done", "abandoned"):
        return False
    if now is None:
        now = _now()
    dt = _parse_dt(_done_at(task))
    if dt is None:
        return False
    if task.get("restored_at") is not None:
        rdt = _parse_dt(task.get("restored_at"))
        if rdt is None:
            return False  # undatable restore: fail toward keeping
        if rdt > dt:
            dt = rdt
    return (now - dt).total_seconds() / 86400.0 >= _retention_days(retention_days)


_MARKER_DATE_RE = _re.compile(r"/(\d{4}-\d{2}-\d{2})-[^/]+\.json$")


def is_prunable_marker(path, now=None, marker_days=None):
    """True when a digest dedup marker file is older than the marker-retention
    window and should be pruned (deleted).

    The marker path is digest/markers/<YYYY-MM-DD>-<window>.json (see
    cli._digest_marker_path). We extract the embedded UTC DATE and parse it via
    _parse_dt — never a lexical compare. A path that doesn't match the expected
    shape (no parseable date) is KEPT, not pruned: we never delete what we can't
    date. Boundary: age >= marker_days prunes."""
    if now is None:
        now = _now()
    m = _MARKER_DATE_RE.search(path)
    if not m:
        return False
    dt = _parse_dt(m.group(1) + "T00:00:00Z")
    if dt is None:
        return False
    return (now - dt).total_seconds() / 86400.0 >= _marker_retention_days(marker_days)


_ESCALATION_MARKER_DATE_RE = _re.compile(r"/escalations/(\d{4}-\d{2}-\d{2})\.json$")


def is_prunable_escalation_marker(path, now=None, marker_days=None):
    """True when a role vacancy-escalation daily marker is older than the
    MARKER retention window and should be pruned (deleted).

    WHY (2026-06-11 wave): ``remote.role_escalation_marker_path`` mints
    ``roles/<name>/escalations/<YYYY-MM-DD>.json`` once per vacant role per
    day (the first-writer-wins dedup guard behind "escalate a vacancy once a
    day, not once a tick"), but nothing ever pruned them — the digest-marker
    sweep covers only digest/markers/. They accumulated forever AND every
    roles listing paid to enumerate/download the growing pile. Same nature as
    digest markers (regenerable guard, zero history value), so the SAME
    retention window (_marker_retention_days) applies.

    SAFETY: the path must match ``.../escalations/<date>.json`` EXACTLY — the
    role record (roles/<name>.json) and lease files (.../leases/<agent>.json)
    share the roles/ prefix and must never look prunable however date-like
    their names are. Date parsed via _parse_dt, never compared lexically; an
    undatable path is KEPT (never delete what we can't date). Boundary:
    age >= marker_days prunes — matching is_prunable_marker."""
    if now is None:
        now = _now()
    m = _ESCALATION_MARKER_DATE_RE.search(path)
    if not m:
        return False
    dt = _parse_dt(m.group(1) + "T00:00:00Z")
    if dt is None:
        return False
    return (now - dt).total_seconds() / 86400.0 >= _marker_retention_days(marker_days)


def is_prunable_presence(record, now=None, presence_days=None):
    """True when a presence record's last_seen is older than the presence-
    retention window — a long-departed agent whose live snapshot is now noise.

    last_seen parsed via _parse_dt (never lexical). A missing/unparseable
    last_seen is KEPT (safe direction: don't delete an undatable record).
    Boundary: age >= presence_days prunes. Presence is a derived view, so a
    pruned record also drops from the presence aggregate on the next rebuild."""
    if now is None:
        now = _now()
    dt = _parse_dt(record.get("last_seen", ""))
    if dt is None:
        return False
    return (now - dt).total_seconds() / 86400.0 >= _presence_retention_days(presence_days)


def is_prunable_health(record, now=None, presence_days=None):
    """True when a health record's reconcile_at is older than the dead-presence
    retention window — a decommissioned host's record that would otherwise linger
    stale-forever. Reuses _presence_retention_days so health and dead presence
    prune in LOCKSTEP. reconcile_at parsed via _parse_dt (never lexical); a
    missing/unparseable reconcile_at is KEPT (fail-safe: never delete what we
    can't date)."""
    if now is None:
        now = _now()
    dt = _parse_dt(record.get("reconcile_at", ""))
    if dt is None:
        return False
    return (now - dt).total_seconds() / 86400.0 >= _presence_retention_days(presence_days)


HEALTH_OUTAGE_SECONDS_DEFAULT = 3 * 3600  # ~3h


def _health_degraded_seconds(seconds=None):
    """Age (s) past which a host's newest reconcile_at is 'degraded'.

    Default ties to the heartbeat interval (interval x 3) — not bare wall-clock —
    so one slow or skipped tick can't flap a host to degraded. interval has no env
    override; INTERVAL_MIN_DEFAULT (minutes) is the only source. Env
    FULCRA_COORD_HEALTH_DEGRADED_SECONDS overrides; non-numeric -> default."""
    # Default ties to the heartbeat interval (interval x 3). Computed here and
    # passed AS env_float's default so a non-numeric env value falls back to it
    # too (env_float's contract). The function-level import keeps views
    # import-light (no module-load cycle) — it's a cached dict lookup per call.
    from . import heartbeat
    default = heartbeat.INTERVAL_MIN_DEFAULT * 60 * 3
    return env_float("FULCRA_COORD_HEALTH_DEGRADED_SECONDS", default, override=seconds)


def _health_outage_seconds(seconds=None):
    """Age (s) past which a host is 'outage' (default ~3h). Env
    FULCRA_COORD_HEALTH_OUTAGE_SECONDS overrides; non-numeric -> default."""
    return env_float("FULCRA_COORD_HEALTH_OUTAGE_SECONDS",
                     HEALTH_OUTAGE_SECONDS_DEFAULT, override=seconds)


# `digest_last_emit` is DATE-only (the freshest YYYY-MM-DD in digest/markers/),
# normalized to that date's MIDNIGHT UTC — so its age is measured from midnight,
# not from the actual emit instant. That date-granularity is the trap: a healthy
# fleet's freshest marker, as seen at the next morning run, is yesterday's date.
#
# Two facts pin the threshold (both verified by enumerating every run instant):
#   * `_assess_fleet` runs at the TOP of cmd_digest, BEFORE the current window
#     claims its own marker — so at the 08:00 morning run the freshest marker is
#     YESTERDAY's (today's morning marker doesn't exist yet).
#   * Worst HEALTHY staleness is the 08:00 morning run vs. yesterday-midnight =
#     32h. The earliest a TRUE miss can show (a whole day's BOTH windows skipped,
#     first observed at the next 08:00) is day-2-midnight -> 56h.
# So the miss threshold must sit strictly between 32h and 56h or it either
# cries wolf every single morning (the old 20h did exactly this — < 32h) or
# misses a real outage. 44h is the midpoint, robust to clock skew either way.
HEALTH_DIGEST_MISS_HOURS = 44


def assess_infra_health(health_records, *, now=None, degraded_after_s=None,
                        outage_after_s=None, digest_last_emit=None,
                        retention_last_run=None, task_count=None):
    """Judge fleet infra health from per-host health records (PURE, no I/O).

    Status gates on RECONCILE-STALENESS ONLY (v1): newest reconcile_at within
    degraded_after_s -> healthy; older -> degraded; older than outage_after_s ->
    outage. A record whose reconcile_at can't be parsed is 'not_reporting' —
    informational, never escalates worst_status (an un-upgraded / heartbeat-less
    host must not raise a false alarm). Duration / repair_backlog / bus size are
    surfaced as METRICS, never gated (no baselined thresholds in v1).

    Bus block: missed_digest_window is True only on a TRUE miss — no marker, or a
    last emit older than HEALTH_DIGEST_MISS_HOURS (44h; see the constant — it must
    clear the 32h worst-case healthy staleness of a DATE-only marker at the next
    morning run, while still catching a 56h full-day-skipped miss). A normal
    overnight gap is healthy. digest_last_emit / retention_last_run are bus-GLOBAL
    (any-agent, dedup'd) so they live here, not in the per-host record. All
    datetime gates use _parse_dt; never lexical."""
    if now is None:
        now = _now()
    deg = _health_degraded_seconds(degraded_after_s)
    out = _health_outage_seconds(outage_after_s)

    hosts = []
    worst_rank = 0  # 0 healthy, 1 degraded, 2 outage; not_reporting does NOT raise it
    rank = {"healthy": 0, "degraded": 1, "outage": 2}

    # Dedup by host, freshest reconcile_at wins. A machine accrues several health
    # records over time — multiple worktrees, plus ORPHANS from now-deleted ones —
    # because the record is keyed per-cwd agent, not per-machine. Judging the
    # FRESHEST record per host means a live machine's current reconcile supersedes
    # its own stale/dead-worktree orphans, instead of one orphan pinning
    # worst_status to "outage" until the 30-day prune (the false-alarm bug). The
    # real signal is preserved: a host whose EVERY record is stale (genuinely not
    # reconciling) still reads outage, because its freshest is still stale. A
    # datable record always beats an undatable one for the same host.
    #
    # CAVEAT: "host" is the short hostname, so this assumes distinct physical
    # machines have distinct short hostnames. Two PHYSICAL machines sharing a
    # short hostname, one healthy + one down, would merge and the down one's
    # outage would hide. That collision domain is only slightly wider than the
    # pre-fix write path (which already clobbered same-hostname+same-repo agents
    # on one remote file); if a deployment ever spans non-unique short hostnames,
    # key health by a more specific machine id instead.
    freshest = {}  # host_key -> (record, parsed_dt_or_None)
    for rec in health_records:
        key = rec.get("host") or rec.get("agent") or "?"
        dt = _parse_dt(rec.get("reconcile_at") or "")
        prev = freshest.get(key)
        if prev is None or (dt is not None and (prev[1] is None or dt > prev[1])):
            freshest[key] = (rec, dt)

    for rec, dt in freshest.values():
        metrics = {
            "duration_s": rec.get("duration_s"),
            "tasks_loaded": rec.get("tasks_loaded"),
            "views_refreshed": rec.get("views_refreshed"),
            "repair_backlog": rec.get("repair_backlog"),
            "bus_task_count": rec.get("bus_task_count"),
            "retention_last_run": rec.get("retention_last_run"),
            "listener_last_fire": rec.get("listener_last_fire"),
            "reconcile_at": rec.get("reconcile_at"),
        }
        if dt is None:
            hosts.append({"host": rec.get("host") or rec.get("agent") or "?",
                          "agent": rec.get("agent"), "status": "not_reporting",
                          "reasons": ["no parseable reconcile_at"], "metrics": metrics})
            continue
        age = (now - dt).total_seconds()
        if age >= out:
            status, reasons = "outage", [f"reconcile stale {int(age // 60)}m (outage)"]
        elif age >= deg:
            status, reasons = "degraded", [f"reconcile stale {int(age // 60)}m"]
        else:
            status, reasons = "healthy", []
        worst_rank = max(worst_rank, rank[status])
        hosts.append({"host": rec.get("host") or rec.get("agent") or "?",
                      "agent": rec.get("agent"), "status": status,
                      "reasons": reasons, "metrics": metrics})

    # Bus-level digest miss: True if no marker OR last emit older than the slack
    # window. Datetime parse via _parse_dt (digest_last_emit is a YYYY-MM-DD date
    # string from the freshest digest/markers/ path, normalized to midnight UTC).
    missed = True
    if digest_last_emit:
        dt = _parse_dt(digest_last_emit) or _parse_dt(f"{digest_last_emit}T00:00:00Z")
        if dt is not None:
            missed = (now - dt).total_seconds() >= HEALTH_DIGEST_MISS_HOURS * 3600

    worst = {0: "healthy", 1: "degraded", 2: "outage"}[worst_rank]
    return {
        "hosts": hosts,
        "bus": {
            "digest_last_emit": digest_last_emit,
            "retention_last_run": retention_last_run,
            "task_count": task_count,
            "missed_digest_window": missed,
        },
        "worst_status": worst,
    }


def _acked_by(t: dict[str, Any], who: str) -> bool:
    """True when ``who`` has inbox-acked this task, resolved from EITHER a full
    body (an inbox_ack event whose ``by`` is who) OR a summary's flat ``acked_by``
    list (schema.task_summary). The inbox builders use this so they give the same
    answer whether handed full bodies or summaries — without it, a rebuilt inbox
    view (sourced from summaries, which carry no event log) would re-surface
    directives the assignee already acked."""
    if "acked_by" in t and "events" not in t:
        return who in (t.get("acked_by") or [])
    for e in t.get("events", []):
        if e.get("type") == "inbox_ack" and e.get("by") == who:
            return True
    return False


# ---------------------------------------------------------------------------
# Individual view builders
# ---------------------------------------------------------------------------

def build_index(tasks: list[dict[str, Any]], updated_at: Optional[str] = None) -> dict[str, Any]:
    """Global compact index with counts and active summaries."""
    if updated_at is None:
        updated_at = _now().isoformat(timespec="microseconds").replace("+00:00", "Z")

    counts_by_status: dict[str, int] = {}
    counts_by_workstream: dict[str, int] = {}
    counts_by_agent: dict[str, int] = {}
    active_summaries = []
    now = _now()
    sh = _stale_hours()

    for t in tasks:
        status = t.get("status", "unknown")
        ws = t.get("workstream", "unknown")
        agent = t.get("owner_agent", "unknown")

        counts_by_status[status] = counts_by_status.get(status, 0) + 1
        if status not in ("done", "abandoned"):
            counts_by_workstream[ws] = counts_by_workstream.get(ws, 0) + 1
            counts_by_agent[agent] = counts_by_agent.get(agent, 0) + 1

        if status in ("active", "waiting", "blocked"):
            # Stamp the stale flag (Gap 2) so the JSON status surface carries it.
            active_summaries.append(_summary_with_stale(t, now, sh))

    cutoff = _now() - timedelta(days=RECENTLY_DONE_DAYS)
    recent_done = []
    for t in tasks:
        if t.get("status") not in ("done", "abandoned"):
            continue
        done_at = _done_at(t)
        if done_at:
            dt = _parse_dt(done_at)
            if dt and dt >= cutoff:
                recent_done.append(task_summary(t))

    # Inbox counts per assignee-slug — a cheap fold so a session-start hook or
    # the agents digest can see "you have N directives" without loading every
    # inbox view. Computed from the same is_open_directive predicate.
    inbox = build_inbox(tasks)
    counts_by_inbox = {slug: len(items) for slug, items in inbox.items()}

    return {
        "schema": "fulcra.coordination.index.v1",
        "updated_at": updated_at,
        "counts": {
            "by_status": counts_by_status,
            "by_workstream": counts_by_workstream,
            "by_agent": counts_by_agent,
            "inbox": counts_by_inbox,
        },
        "active": active_summaries,
        "recent_done": recent_done,
    }


def _summary_with_stale(t: dict[str, Any], now: datetime,
                        stale_hours: float) -> dict[str, Any]:
    """task_summary() with a stale flag stamped on (Gap 2). The flag is the
    single source of truth so consumers read it instead of recomputing age."""
    s = task_summary(t)
    s["stale"] = is_stale(t, now, stale_hours)
    return s


def build_active(tasks: list[dict[str, Any]], updated_at: Optional[str] = None,
                 stale_hours: Optional[float] = None) -> dict[str, Any]:
    """All active, waiting, and blocked tasks. Active summaries carry a `stale`
    flag (Gap 2) so the status view can mark possibly-forgotten work."""
    if updated_at is None:
        updated_at = _now().isoformat(timespec="microseconds").replace("+00:00", "Z")
    now = _now()
    sh = _stale_hours(stale_hours)
    active = [
        _summary_with_stale(t, now, sh)
        for t in tasks if t.get("status") in ("active", "waiting", "blocked")
    ]
    return {
        "schema": "fulcra.coordination.view.v1",
        "view": "active",
        "updated_at": updated_at,
        "tasks": sorted(active, key=lambda x: (x.get("priority", "P9"), x.get("updated_at", ""))),
    }


def build_needs_attention(tasks: list[dict[str, Any]], updated_at: Optional[str] = None,
                          stale_hours: Optional[float] = None) -> dict[str, Any]:
    """Materialized view of `active` tasks that have gone stale (Gap 2).

    The safety-net surface: crashed agents (SessionEnd never fired) and ChatGPT
    (no end hook) leave active tasks dangling; this view collects them so the
    heartbeat reconciler / `agents` digest can flag them. Every entry carries
    stale=true (it would not be here otherwise) for symmetry with the active view.
    """
    if updated_at is None:
        updated_at = _now().isoformat(timespec="microseconds").replace("+00:00", "Z")
    now = _now()
    sh = _stale_hours(stale_hours)
    stale = []
    for t in tasks:
        if is_stale(t, now, sh):
            s = task_summary(t)
            s["stale"] = True
            stale.append(s)
    return {
        "schema": "fulcra.coordination.view.v1",
        "view": "needs-attention",
        "updated_at": updated_at,
        "stale_hours": sh,
        "tasks": sorted(stale, key=lambda x: x.get("updated_at", "")),
    }


def agent_slug(agent: str) -> str:
    """Filesystem-safe slug for an agent id, used as the ``index.counts.inbox``
    key, the presence record basename, and the local surface-file suffix. Agent
    ids look like ``claude-code:host:repo`` — the colons are not portable in
    filenames, so collapse every non-[a-z0-9-_.] run to a single ``-`` (mirrors
    cache._root_slug's approach). Lowercased so two ids differing only in case
    can't fork into two keys.

    The broadcast sentinel ``*`` is special-cased to ``broadcast`` (M3): every
    char of ``*`` is non-portable, so the generic path would strip to empty and
    fall back to the opaque ``agent``. ``broadcast`` makes the
    ``index.counts.inbox`` bucket human-legible. The literal ``*`` never
    reaches a filesystem path — this slug is the only place a broadcast
    assignee is turned into a path segment."""
    if agent == BROADCAST:
        return "broadcast"
    s = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in agent.lower())
    return s.strip("-") or "agent"


def agent_matches(me: str, assignee: str) -> bool:
    """True when a directive addressed to `assignee` belongs in `me`'s inbox.

    Match rule: ``assignee == me`` OR ``assignee``'s colon-segments are a prefix
    of ``me``'s colon-segments. So a directive addressed to the SHORT id
    ``claude-code`` reaches the full id ``claude-code:<host>:<repo>``,
    and ``claude-code:<host>`` does too — but ``openclaw`` (different kind)
    and ``claude-code:other`` (divergent segment) do NOT, and a MORE-specific
    assignee never matches a less-specific me.

    This is the correctness fix for the inbox bug: agents derive a full id, but
    another agent addressed directives to the short id, so the strict
    slug-equality match silently dropped them. Prefix matching is per-colon-
    segment (not per-character), so ``claude`` is not a prefix of ``claude-code``.

    Defensive (M2): a falsy ``me`` or ``assignee`` (None/empty) can never be a
    real match, and ``None.split(":")`` would raise into a best-effort read path,
    so return False early rather than crashing the caller.
    """
    if not me or not assignee:
        return False
    if assignee == BROADCAST:
        # A broadcast directive reaches every agent. This is the only place the
        # wildcard widens matching; concrete assignees below still match strictly
        # by colon-segment prefix, so "*" can't be reproduced by any real id.
        return True
    if is_role_audience(assignee):
        # A role audience (``@<role>``) is NEVER matched here: it is resolved at
        # delivery time against the caller's declared roles (inbox_for), not by
        # id-prefix. Returning False keeps this id-matching helper honest — a role
        # string is not an agent id — so any caller that asks "does this concrete
        # agent equal/prefix the assignee" (the index's self-owned check, the
        # human-plate filter, undelivered-directive detection) treats a role
        # audience as "not me", and only inbox_for's explicit role path delivers it.
        return False
    if assignee == me:
        return True
    me_parts = me.split(":")
    as_parts = assignee.split(":")
    if len(as_parts) >= len(me_parts):
        # Equal length is handled by the == check above; a longer assignee is
        # strictly more specific than me and must not match.
        return False
    return me_parts[:len(as_parts)] == as_parts


def _is_owners_own_directive(owner_agent: Optional[str], assignee: str) -> bool:
    """True when a directive is the OWNER's own work, not a cross-agent directive.

    BUG 3: a task whose assignee is a short-id PREFIX of its owner_agent (e.g.
    owner='claude-code:h:r', assignee='claude-code') is the owner's own task —
    the owner's read path (inbox_for) hides it because owner_agent matches `me`.
    The index used a bare exact `owner_agent == assignee` test, so it kept
    counting these even though the read path hid them. Using the SAME prefix-
    aware ownership relation here makes the count and the read path agree.
    agent_matches(owner, assignee) is True when assignee equals or prefixes the
    owner — exactly the "owner is (a refinement of) the assignee" relation.

    A BROADCAST directive (assignee == "*") is fan-out to every agent, NOT the
    owner's own work, so it is never self-owned — guarding here keeps broadcasts
    visible/counted (agent_matches treats "*" as matching everything).
    """
    if not owner_agent or assignee == BROADCAST:
        return False
    return agent_matches(owner_agent, assignee)


def is_open_directive(task: dict[str, Any], assignee: str,
                      now: Optional[datetime] = None,
                      include_aged: bool = False,
                      age_days: Optional[float] = None) -> bool:
    """True when `task` is an unacknowledged directive addressed to `assignee`.

    Open inbox item = assigned to me, in an open status (proposed/waiting),
    owned by someone *else* (a self-owned task is my own work, not a directive),
    and not yet acked by me (no inbox_ack event whose `by` is me). Claiming the
    task (status -> active, owner becomes me) also drops it from the inbox via
    both the status and owner checks.

    AGE-OUT: a stale informational broadcast (is_aged_out_broadcast) is treated
    as no longer open by default, so the index.counts.inbox fold doesn't keep
    counting fan-out that the live `inbox` read filter (inbox_for) already
    hides — the SessionStart banner count and the index buckets must agree with
    the read path. `include_aged=True` keeps the legacy "everything open"
    semantics. Concrete-assignee directives are never aged out
    (is_aged_out_broadcast guards on assignee == BROADCAST).
    """
    if task.get("assignee") != assignee:
        return False
    if task.get("status") not in INBOX_OPEN_STATUSES:
        return False
    if _is_owners_own_directive(task.get("owner_agent"), assignee):
        return False
    if _acked_by(task, assignee):
        return False
    if not include_aged and is_aged_out_broadcast(task, now, age_days):
        return False
    return True


def build_inbox(tasks: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group open directives by assignee slug -> list of compact summaries.

    Feeds the ``index.counts.inbox`` fold (build_index): one bucket per
    assignee that has at least one open directive; assignees with no open
    directives are omitted. (The per-assignee ``views/inbox/<slug>.json``
    files this also used to materialize were retired 2026-06-11 — zero
    readers; the live `inbox` read path recomputes via inbox_for. See
    build_all_views' RETIRED VIEWS note.)

    ROLE audiences (@<role>) are EXCLUDED: they are resolved at delivery time
    against each agent's declared roles (inbox_for + _my_roles), never by a
    per-slug bucket. Counting one here would create an index.counts.inbox
    entry that no agent ever reads as "their" inbox (no concrete agent's slug
    equals a role name) — a misleading operator-surface artifact. Skipping
    them keeps the counts describing only concrete/broadcast recipients,
    exactly mirroring what the slug-keyed read path can deliver.
    """
    inbox: dict[str, list[dict[str, Any]]] = {}
    for t in tasks:
        assignee = t.get("assignee")
        if not assignee or is_role_audience(assignee) or not is_open_directive(t, assignee):
            continue
        inbox.setdefault(agent_slug(assignee), []).append(task_summary(t))
    for slug in inbox:
        inbox[slug] = sorted(inbox[slug],
                             key=lambda x: (x.get("priority", "P9"), x.get("updated_at", "")))
    return inbox


def inbox_for(me: str, tasks: list[dict[str, Any]], now: Optional[datetime] = None,
              include_aged: bool = False,
              age_days: Optional[float] = None,
              roles: Optional[set[str]] = None) -> list[dict[str, Any]]:
    """Open directives addressed to `me`, using prefix-aware matching.

    Membership for a querying agent is decided by agent_matches (a directive
    addressed to a short id reaches the full-id agent it prefixes), NOT by exact
    slug equality. The materialized inbox view stays keyed by each assignee's own
    slug (build_inbox, unchanged); this is the read path that a concrete agent
    uses to gather every directive it should act on across those buckets.

    A directive is in my inbox when it is an open directive for its OWN assignee
    (open status, owned by someone else, not acked by me) AND agent_matches(me,
    assignee). The ack check is against `me`: my own ack clears it from my inbox
    even if the directive was addressed to a short id I prefix.

    ROLE AUDIENCE (`@<role>`): a directive addressed to a logical role is in
    `me`'s inbox WHEN `me` currently HOLDS that role — i.e. role_of(assignee) is
    in `roles`, the caller's declared capability/role set (loaded from `me`'s own
    presence record by the inbox.py caller). This is multi-holder fan-out: EVERY
    live agent holding the role sees the directive, and NObody who doesn't. The
    role match is delivery-time and replaces the id-prefix match for these (the
    id helper agent_matches never matches a role audience). `roles=None`/empty (an
    agent with no declared roles, or an old presence record without capabilities)
    means no role directives are ever surfaced — fully backward-compatible.

    AGE-OUT (default behaviour): a stale informational BROADCAST (see
    is_aged_out_broadcast) is excluded so old "X joined the mesh" fan-out stops
    cluttering every inbox / SessionStart. This is a VIEW filter only — the task
    is untouched. `include_aged=True` (the `inbox --all` path) bypasses it and
    shows everything. `now` is injectable for deterministic tests (like
    needs_human / is_stale). CONCRETE-assignee directives are never aged out.
    """
    held_roles = roles or set()
    items: list[dict[str, Any]] = []
    for t in tasks:
        assignee = t.get("assignee")
        if not assignee:
            continue
        # Membership: a role audience matches iff `me` holds the role; everything
        # else falls back to the existing id/broadcast prefix match. A role
        # audience is mutually exclusive with agent_matches (which returns False
        # for it), so these two branches never double-count a directive.
        if is_role_audience(assignee):
            if role_of(assignee) not in held_roles:
                continue
        elif not agent_matches(me, assignee):
            continue
        if t.get("status") not in INBOX_OPEN_STATUSES:
            continue
        # Hide my own work (owner_agent == me) and any directive that is the
        # owner's own (assignee equals/prefixes owner_agent) — the same
        # prefix-aware ownership relation the index now uses (BUG 3), so the
        # read path and index.counts.inbox agree.
        if t.get("owner_agent") == me or _is_owners_own_directive(t.get("owner_agent"), assignee):
            continue
        if _acked_by(t, me):
            continue
        if not include_aged and is_aged_out_broadcast(t, now, age_days):
            continue
        items.append(task_summary(t))
    return sorted(items, key=lambda x: (x.get("priority", "P9"), x.get("updated_at", "")))


def aged_out_inbox_count(me: str, tasks: list[dict[str, Any]],
                         now: Optional[datetime] = None,
                         age_days: Optional[float] = None) -> int:
    """How many of `me`'s otherwise-open directives are hidden purely by age-out.

    The default `inbox` surfaces this as a one-line "(N older broadcasts hidden —
    --all to show)" note so an agent knows the live view was trimmed and can opt
    back in. Defined as the difference between the full membership (include_aged)
    and the default membership, so the count can never disagree with what
    inbox_for actually hides — there is one filter, queried two ways.
    """
    full = {s["id"] for s in inbox_for(me, tasks, now, include_aged=True, age_days=age_days)}
    shown = {s["id"] for s in inbox_for(me, tasks, now, include_aged=False, age_days=age_days)}
    return len(full - shown)


# Statuses at which a task counts as "still on the human's plate": proposed
# (asked, not started), waiting (parked on them), or blocked (blocked --on-user).
# Terminal/active states are not awaiting the human, so they are excluded.
NEEDS_HUMAN_OPEN_STATUSES = ("proposed", "waiting", "blocked")


def _human_match(t: dict[str, Any], human: str) -> bool:
    """True when an OPEN task is directed at the human (the strict needs-me
    membership rule, factored out so needs_human and upcoming_for_human share
    one definition): open status AND (a CONCRETE non-broadcast assignee matching
    the human OR the explicit ``needs:human`` tag). A bare ``*`` broadcast never
    qualifies — see needs_human's docstring for why."""
    if t.get("status") not in NEEDS_HUMAN_OPEN_STATUSES:
        return False
    assignee = t.get("assignee")
    tags = t.get("tags") or []
    concrete_for_human = (
        assignee and assignee != BROADCAST and agent_matches(human, assignee)
    )
    return bool(concrete_for_human or "needs:human" in tags)


def _schedule_iso_z(value: Any) -> Optional[str]:
    """Normalize an optional stored schedule timestamp for comparisons.

    Scheduling is a visibility gate, so malformed persisted data must degrade to
    "no gate" rather than hiding a concrete human ask forever.
    """
    dt = _schedule_dt(value)
    if dt is None:
        return None
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _schedule_dt(value: Any) -> Optional[datetime]:
    """Parse an optional stored schedule timestamp to a tz-aware UTC datetime.

    BUG 7: the not_before/due gates must compare PARSED datetimes, never the
    stored ISO-Z strings. The wall-clock instant emits fractional seconds only
    when its microsecond != 0 while a stored value keeps its own precision, so a
    lexical compare of the mixed-width strings is unsound (``Z`` 0x5A sorts after
    ``.`` 0x2E) — a sub-second-past not_before could gate as future. A naive
    parsed value is assumed UTC (matching the rest of the schedule layer).
    Malformed/absent -> None ("no gate")."""
    dt = _parse_dt(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def needs_human(tasks: list[dict[str, Any]], human: str, *,
                now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Every OPEN task assigned to / blocked on the human that is ACTIONABLE NOW.

    The data layer behind ``needs-me`` and the SessionStart "BLOCKED ON YOU"
    banner: the human's situational-awareness glance of "what's on my plate from
    my agents." Membership is prefix-aware via ``agent_matches`` so both the
    neutral ``human`` and a personalized handle (``ash``) resolve, and an
    assignee addressed to a short id reaches the full one. Open = proposed /
    waiting / blocked (an active or terminal task is not awaiting the human).

    Returned oldest-first (by ``updated_at``) so the longest-waiting ask — the
    thing the human has been blocking the longest — sorts to the top.

    PRECISION (broadcast exclusion): a broadcast (``assignee == "*"``) reaches
    every agent's inbox via ``agent_matches``'s wildcard, but an all-agent
    announcement is NOT a personal ask blocked on the human. Including them
    floods the SessionStart "⛔ BLOCKED ON YOU" banner with join-announcement
    noise and buries the real asks — defeating the whole situational-awareness
    point. So membership here is the STRICT signal: a CONCRETE assignee that
    matches the human (``block --on-user`` sets ``assignee = human``), OR the
    explicit ``needs:human`` tag. The broadcast wildcard never qualifies.

    SCHEDULING (the not_before gate): a task with a FUTURE ``not_before`` is
    something the human CANNOT act on yet (e.g. a re-auth window that opens next
    week), so it is EXCLUDED from this DUE-NOW plate — it would otherwise show
    "BLOCKED ON YOU" every session for days. Such items are surfaced separately
    by ``upcoming_for_human``. A task with no/empty/past ``not_before`` behaves
    exactly as before — the gate only ever HIDES future work, never the rest."""
    now_dt = (now or _now()).astimezone(timezone.utc)
    items = []
    for t in tasks:
        if not _human_match(t, human):
            continue
        nb = _schedule_dt(t.get("not_before"))
        # Gate: a future not_before keeps it off the due-now plate. Empty/None
        # or malformed not_before is "no gate" -> always due-now (today's
        # behavior). Compare PARSED datetimes (BUG 7) so sub-second precision
        # differences between `now` and the stored value can't misgate.
        if nb and nb > now_dt:
            continue
        items.append(task_summary(t))
    return sorted(items, key=lambda x: x.get("updated_at", ""))


def upcoming_for_human(tasks: list[dict[str, Any]], human: str, *,
                       now: Optional[datetime] = None,
                       within_days: int = 7) -> list[dict[str, Any]]:
    """The human's NOT-YET-ACTIONABLE asks: open tasks directed at the human
    whose ``not_before`` is in the FUTURE but within ``within_days``.

    The companion to ``needs_human``: the not_before gate hides future work from
    the DUE-NOW plate, and this surfaces it as a compact "Upcoming (next 7d)"
    list so the human still has line-of-sight on what's coming — without it
    inflating the "BLOCKED ON YOU (N)" count. Same human-match / open-status /
    broadcast-exclusion rules as needs_human (via ``_human_match``).

    Sorted by ``not_before`` then ``due`` (soonest-actionable first, then by
    deadline) so the UI can render "in 3d / due Jun 8". Each entry carries
    ``not_before`` + ``due`` (already on task_summary)."""
    now_dt = (now or _now()).astimezone(timezone.utc)
    horizon_dt = now_dt + timedelta(days=within_days)
    items = []
    for t in tasks:
        if not _human_match(t, human):
            continue
        nb = _schedule_dt(t.get("not_before"))
        # Only FUTURE not_before items within the horizon. No/empty/malformed/
        # past not_before is due-now (handled by needs_human), not upcoming.
        # Parsed-datetime compare (BUG 7) so a sub-second-past not_before is
        # correctly excluded (it's due-now) rather than wrongly listed.
        if not nb or nb <= now_dt or nb > horizon_dt:
            continue
        s = task_summary(t)
        # Sort key stays a string (ISO-Z) — sorting only orders entries and is
        # unaffected by the lexical/precision issue the GATE had.
        items.append((_schedule_iso_z(s.get("not_before")) or "",
                      _schedule_iso_z(s.get("due")) or "", s))
    return [s for _, _, s in sorted(items, key=lambda x: (x[0], x[1]))]


# --- Unrouted PR-review detection ----------------------------------------
# `views` must NOT import `routing` (routing imports `views`), so mirror the
# kind:review marker here. Keep in sync with routing.REVIEW_TAG.
_REVIEW_TAG = "kind:review"
UNROUTED_REVIEW_OPEN_STATUSES = ("proposed", "active", "waiting", "blocked")
# Match an explicit PR reference: "PR #101", "PR 101", "PRs #101", a GitHub
# "/pull/101" URL, or "pull request 101". Deliberately NOT a bare "#101" — that
# would false-positive on issue refs and unrelated hashes; the whole point is to
# catch *PR* mentions an author forgot to route for review.
_PR_MENTION_RE = _re.compile(
    r"(?:\bPRs?\b\s*#?|/pull/|\bpull\s+request\s+#?)(\d{1,6})", _re.IGNORECASE)


def unrouted_pr_reviews(tasks: list[dict[str, Any]], agent: str) -> list[dict[str, Any]]:
    """Open tasks OWNED BY ``agent`` that name a PR in free text but were never
    routed for review.

    The failure this catches: an author opens a PR and leaves "review PR #N" as a
    next_action/summary instead of running ``request-review``. No ``kind:review``
    directive is ever created, so the review is assigned to nobody and surfaces
    on no reviewer's inbox/resume — it silently goes unreviewed (exactly how
    PR #101 sat unreviewed). Flagging it on the OWNER's resume nudges them to
    route it so a reviewer actually gets it.

    Read-only and summary-only (reads title/current_summary/next_action/tags —
    no body or events). A task that already carries the ``kind:review`` marker is
    a routed review directive and is excluded. Each returned summary gains a
    ``pr_mentions`` list of the referenced PR numbers (strings, de-duped)."""
    out: list[dict[str, Any]] = []
    for t in tasks:
        if t.get("owner_agent") != agent:
            continue
        if t.get("status") not in UNROUTED_REVIEW_OPEN_STATUSES:
            continue
        if _REVIEW_TAG in (t.get("tags") or []):
            continue  # already a routed review directive
        haystack = " ".join(
            str(t.get(f, "") or "")
            for f in ("title", "current_summary", "next_action"))
        prs = sorted({m.group(1) for m in _PR_MENTION_RE.finditer(haystack)},
                     key=int)
        if not prs:
            continue
        s = dict(t)
        s["pr_mentions"] = prs
        out.append(s)
    return sorted(out, key=lambda x: (x.get("priority", "P9"),
                                      x.get("updated_at", "")))


def build_next(tasks: list[dict[str, Any]], updated_at: Optional[str] = None) -> dict[str, Any]:
    """Proposed and waiting tasks — candidates for starting next."""
    if updated_at is None:
        updated_at = _now().isoformat(timespec="microseconds").replace("+00:00", "Z")
    candidates = [
        task_summary(t) for t in tasks if t.get("status") in ("proposed", "waiting")
    ]
    return {
        "schema": "fulcra.coordination.view.v1",
        "view": "next",
        "updated_at": updated_at,
        "tasks": sorted(candidates, key=lambda x: (x.get("priority", "P9"), x.get("updated_at", ""))),
    }


def build_recently_done(
    tasks: list[dict[str, Any]],
    updated_at: Optional[str] = None,
    days: int = RECENTLY_DONE_DAYS,
) -> dict[str, Any]:
    """Done and abandoned tasks within retention window."""
    if updated_at is None:
        updated_at = _now().isoformat(timespec="microseconds").replace("+00:00", "Z")
    cutoff = _now() - timedelta(days=days)
    recent = []
    for t in tasks:
        if t.get("status") not in ("done", "abandoned"):
            continue
        done_at = _done_at(t)
        if done_at:
            dt = _parse_dt(done_at)
            if dt and dt >= cutoff:
                recent.append(task_summary(t))
    return {
        "schema": "fulcra.coordination.view.v1",
        "view": "recently-done",
        "updated_at": updated_at,
        "retention_days": days,
        "tasks": sorted(recent, key=lambda x: x.get("updated_at", ""), reverse=True),
    }


def build_search_index(tasks: list[dict[str, Any]], updated_at: Optional[str] = None) -> dict[str, Any]:
    """Compact tag/title/summary records for search."""
    if updated_at is None:
        updated_at = _now().isoformat(timespec="microseconds").replace("+00:00", "Z")

    cutoff_done = _now() - timedelta(days=SEARCH_INDEX_DONE_DAYS)
    records = []
    for t in tasks:
        status = t.get("status", "")
        if status in ("done", "abandoned"):
            done_at = _done_at(t)
            if done_at:
                dt = _parse_dt(done_at)
                if not dt or dt < cutoff_done:
                    continue

        # .get() with defaults (not bracket access): a malformed task body missing
        # id/title must SURFACE in the search index with empty defaults, never
        # KeyError out of the rebuild — a single bad task would otherwise abort the
        # whole reconcile (views unrepaired, retention never runs). Same
        # render-don't-crash contract as task_summary (debug-sweep round 2-3).
        records.append({
            "id": t.get("id", ""),
            "title": t.get("title", ""),
            "status": status,
            "priority": t.get("priority", ""),
            "workstream": t.get("workstream", ""),
            "owner_agent": t.get("owner_agent", ""),
            "tags": t.get("tags", []),
            "summary": t.get("current_summary", ""),
            "task_file": task_file_path(t.get("id", "")),
            "updated_at": t.get("updated_at", ""),
        })

    return {
        "schema": "fulcra.coordination.search_index.v1",
        "updated_at": updated_at,
        "records": records,
    }


def build_workstream_view(
    workstream: str,
    tasks: list[dict[str, Any]],
    updated_at: Optional[str] = None,
    done_days: int = RECENTLY_DONE_DAYS,
) -> dict[str, Any]:
    """Per-workstream view: active/waiting/blocked + recent done."""
    if updated_at is None:
        updated_at = _now().isoformat(timespec="microseconds").replace("+00:00", "Z")
    cutoff = _now() - timedelta(days=done_days)
    ws_tasks = [t for t in tasks if t.get("workstream") == workstream]
    active = [
        task_summary(t) for t in ws_tasks if t.get("status") in ("active", "waiting", "blocked")
    ]
    recent_done = []
    for t in ws_tasks:
        if t.get("status") not in ("done", "abandoned"):
            continue
        done_at = _done_at(t)
        if done_at:
            dt = _parse_dt(done_at)
            if dt and dt >= cutoff:
                recent_done.append(task_summary(t))

    return {
        "schema": "fulcra.coordination.workstream_view.v1",
        "workstream": workstream,
        "updated_at": updated_at,
        "active": sorted(active, key=lambda x: (x.get("priority", "P9"), x.get("updated_at", ""))),
        "recent_done": sorted(recent_done, key=lambda x: x.get("updated_at", ""), reverse=True),
    }


# (build_agent_view lived here until the 2026-06-11 perf wave: the
# agents/<id>.json views it fed were uploaded on every write/reconcile and
# read by nothing — see build_all_views' RETIRED VIEWS note.)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_tasks(query: str, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Simple text search across title, summary, tags, workstream, agent."""
    q = query.lower()
    results = []
    for t in tasks:
        text = " ".join([
            t.get("title", ""),
            t.get("current_summary", ""),
            t.get("workstream", ""),
            t.get("owner_agent", ""),
            " ".join(t.get("tags", [])),
        ]).lower()
        if q in text:
            results.append(task_summary(t))
    return sorted(results, key=lambda x: x.get("updated_at", ""), reverse=True)


# ---------------------------------------------------------------------------
# Summaries aggregate (the read-side + rebuild source)
# ---------------------------------------------------------------------------

def build_summaries(tasks: list[dict[str, Any]],
                    updated_at: Optional[str] = None) -> dict[str, Any]:
    """The single durable aggregate of task_summary dicts (``views/summaries.json``).

    WHY this exists (the performance refactor): before this, every READ
    (status/agents/needs-me/resume/search) and every WRITE's view rebuild called
    _load_all_tasks, which fetches each task BODY one network round-trip at a
    time (~N round-trips). This aggregate is ONE file: reads load it directly, and
    the write path upserts the just-written summary into it and rebuilds all views
    from it — so neither path ever fetches N bodies again.

    Membership: ALL tasks passed in. Callers already pass exactly the set the
    other views collectively cover (all non-terminal tasks plus done/abandoned
    inside the recently-done / search cutoffs), so including everything passed is
    the simplest correct choice; the per-view cutoffs still apply inside each
    builder when views are rebuilt from this list.

    A LIST (not a map) of summaries, for consistency with the other views' task
    lists and so it round-trips as plain JSON without key-ordering concerns."""
    if updated_at is None:
        updated_at = _now().isoformat(timespec="microseconds").replace("+00:00", "Z")
    return {
        "schema": "fulcra.coordination.summaries.v1",
        "view": "summaries",
        "updated_at": updated_at,
        # Freshness stamp for the stale-view read guard (2026-06-10 blindness
        # fix). Mirrors the resolved updated_at — every call site resolves it to
        # build wall-clock — but is a SEPARATE key so (a) readers get an explicit
        # "when was this materialized" contract independent of updated_at's view
        # semantics and (b) its absence cleanly identifies a pre-guard bus
        # (view_staleness_minutes treats absent as back-compat-fresh). Additive:
        # old readers ignore it.
        "generated_at": updated_at,
        "summaries": [task_summary(t) for t in tasks],
    }


# ---------------------------------------------------------------------------
# Agent presence roster (situational awareness)
# ---------------------------------------------------------------------------

PRESENCE_VIEW_SCHEMA = "fulcra.coordination.presence_view.v1"


def presence_liveness(last_seen: str, now: Optional[datetime] = None,
                      stale_hours: Optional[float] = None) -> str:
    """Classify a presence record's recency into live / idle / stale.

    Reuses the SAME staleness threshold the task views use (``_stale_hours`` ->
    ``FULCRA_COORD_STALE_HOURS``, default 2h) so "stale" means one thing across
    the whole tool. Bands (documented in the design):

      * live  — younger than HALF the threshold: actively working right now.
      * idle  — between half and the full threshold: quiet but not yet suspect.
      * stale — at or past the threshold: probably crashed / forgotten session.

    Splitting at 0.5x gives a meaningful "live" band without a second env knob;
    a missing/unparseable ``last_seen`` ages to +inf (via _age_hours) and so
    classifies stale — the same fail-toward-surfacing choice is_stale makes."""
    threshold = _stale_hours(stale_hours)
    if now is None:
        now = _now()
    age = _age_hours(last_seen, now)
    if age < threshold * 0.5:
        return "live"
    if age < threshold:
        return "idle"
    return "stale"


_ROUTING_TIER = {"live": 0, "idle": 1}  # below-floor never appears here


def _effective_routing_liveness(last_seen: str, now: datetime,
                                grace_seconds: float,
                                stale_hours: Optional[float] = None) -> Optional[str]:
    """Recompute a candidate's liveness FOR ROUTING from bus-global last_seen.

    Owned entirely by the resolver — it does NOT trust an aggregate's stored
    `liveness` field, because a stale rebuild could under-report it (codex
    tightening #1). One consistent judgment, identical on every machine:
      * within the idle cutoff (presence_liveness live/idle bands) -> that band.
      * within stale_cutoff + grace_seconds -> 'idle' (the wall-clock grace
        window: one missed heartbeat / a sleep-wake must not drop a reviewer).
      * beyond -> None (below floor).
    A missing/unparseable last_seen ages to +inf -> below floor (None)."""
    band = presence_liveness(last_seen, now, stale_hours)  # live | idle | stale
    if band in ("live", "idle"):
        return band
    # band == "stale": apply the wall-clock grace before dropping below floor.
    age_seconds = _age_hours(last_seen, now) * 3600.0
    cutoff_seconds = _stale_hours(stale_hours) * 3600.0
    if age_seconds < cutoff_seconds + grace_seconds:
        return "idle"
    return None


def resolve_live_recipient(candidates: list[str], presence: list[dict[str, Any]],
                           *, floor: str = "idle", now: Optional[datetime] = None,
                           exclude: tuple[str, ...] = (),
                           grace_seconds: Optional[float] = None) -> Optional[str]:
    """Pick the live/idle candidate minimizing (effective_tier, preference_index).

    Pure + deterministic given `presence` + `now` (both injectable -> testable).
    `candidates` is in PREFERENCE order (canonical reviewer first). `floor`
    'idle' accepts live OR idle; 'live' accepts live only. Below-floor and
    `exclude`d agents are skipped. Returns None when nobody clears the floor
    (the caller then escalates to the human — never parks on a dead agent).

    Effective liveness is recomputed inside (via _effective_routing_liveness)
    from each candidate's bus-global last_seen + the wall-clock grace, so the
    stored aggregate tier is never trusted and the judgment is identical on
    every machine (machine-agnostic invariant)."""
    if now is None:
        now = _now()
    grace = _presence_grace_seconds(grace_seconds)
    floor_rank = _ROUTING_TIER.get(floor, 1)  # default to idle floor
    by_agent = {r.get("agent"): r for r in presence}
    best: Optional[tuple[int, int, str]] = None
    for idx, agent in enumerate(candidates):
        if agent in exclude:
            continue
        rec = by_agent.get(agent)
        if not rec:
            continue  # never connected -> below floor
        eff = _effective_routing_liveness(rec.get("last_seen", ""), now, grace)
        if eff is None:
            continue
        tier = _ROUTING_TIER[eff]
        if tier > floor_rank:
            continue  # below the requested floor (e.g. idle when floor=live)
        key = (tier, idx, agent)
        if best is None or key < best:
            best = key
    return best[2] if best else None


def build_presence(records: list[dict[str, Any]], now: Optional[datetime] = None,
                   updated_at: Optional[str] = None) -> dict[str, Any]:
    """Build the aggregate presence roster from per-agent presence records.

    Pure function over a list of ``make_presence`` dicts: each entry is carried
    through with a ``liveness`` annotation (live/idle/stale by last_seen age) and
    the roster is sorted most-recently-active first, so the human glancing at it
    sees who is actively working at the top. This is the read-side aggregate the
    ``presence`` / ``agents`` / ``resume`` surfaces consume in one download."""
    if updated_at is None:
        updated_at = _now().isoformat(timespec="microseconds").replace("+00:00", "Z")
    if now is None:
        now = _now()
    agents = []
    for rec in records:
        entry = dict(rec)
        entry["liveness"] = presence_liveness(rec.get("last_seen", ""), now)
        agents.append(entry)
    # Most-recently-seen first (descending last_seen). Sort by the PARSED
    # datetime, not the raw string (BUG 1): a same-second pair where one record
    # was stamped with microsecond=0 ("...45Z") and the other with µs>0
    # ("...45.5Z") would mis-order under a lexical compare ('.' < 'Z'), putting
    # a STALER agent on top. A missing/unparseable timestamp coerces to epoch
    # (datetime.min) so it sorts LAST under reverse — matching the prior intent.
    _epoch = datetime.min.replace(tzinfo=timezone.utc)
    agents.sort(key=lambda a: _parse_dt(a.get("last_seen", "")) or _epoch,
                reverse=True)
    return {
        "schema": PRESENCE_VIEW_SCHEMA,
        "view": "presence",
        "updated_at": updated_at,
        # Freshness stamp for the stale-view read guard — same contract as
        # build_summaries: build wall-clock, additive, absent on an older bus.
        "generated_at": updated_at,
        "agents": agents,
    }


# ---------------------------------------------------------------------------
# All views at once (for write fan-out)
# ---------------------------------------------------------------------------

def build_all_views(tasks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Build every standard view. Returns dict of name -> view data.

    INVARIANT (the linchpin of the perf refactor): this must produce IDENTICAL
    output whether ``tasks`` are full task bodies or ``schema.task_summary`` dicts.
    Every builder reads only fields task_summary emits (done_at and last_touched_by
    were added there for exactly this reason), so the write path can rebuild views
    from the summaries aggregate instead of re-fetching task bodies. The
    equivalence test (TestBuildAllViewsEquivalence) guards this property.

    RETIRED VIEWS (2026-06-11 perf wave item 3 — zero readers, real upload cost):

      * ``agents/<id>.json`` (build_agent_view) — one file per owner/toucher
        identity, rebuilt + uploaded on every write/reconcile, downloaded by
        NOTHING (cmd_agents/resume fold the summaries aggregate client-side;
        verified by audit + grep before cutting).
      * ``views/inbox/<slug>.json`` — one file per open-directive assignee,
        likewise read by nothing: cmd_inbox deliberately recomputes from the
        task set because this view goes stale once an inbox empties (the C1
        phantom-directive bug) — it was retained only as a materialized
        artifact. The ``index.counts.inbox`` fold (build_inbox, below via
        build_index) is the surviving read surface and is unchanged.

      Together they were ~A agents + I inboxes ≈ 35+ uploads per write/
      reconcile pass at current fleet size, scaling with fleet growth. The
      EXISTING remote files are deliberately NOT deleted here: bus-state
      cleanup is deferred pending a Fulcra service review — they are simply
      never rewritten again (inert) and age out later. The stale-view guard /
      summaries freshness beacon are untouched: they key off ``generated_at``,
      which neither retired view ever carried."""
    now = _now().isoformat(timespec="microseconds").replace("+00:00", "Z")
    result: dict[str, dict[str, Any]] = {
        "index": build_index(tasks, now),
        "active": build_active(tasks, now),
        "next": build_next(tasks, now),
        "recently-done": build_recently_done(tasks, now),
        "search-index": build_search_index(tasks, now),
        "needs-attention": build_needs_attention(tasks, now),
        # The summaries aggregate is itself a view, so it is rebuilt and uploaded
        # on every write alongside the others — keeping the read-side source fresh.
        "summaries": build_summaries(tasks, now),
    }

    workstreams = sorted(set(t.get("workstream", "") for t in tasks) - {""})
    for ws in workstreams:
        result[f"workstreams/{ws}"] = build_workstream_view(ws, tasks, now)

    return result


# ---------------------------------------------------------------------------
# Operator digest (situational-awareness fold, piece 7)
# ---------------------------------------------------------------------------

def _digest_due_key(s: dict[str, Any]) -> tuple:
    """Ranking key for the blocked-on-you block: due soonest first, then oldest.

    Both components are PARSED via _parse_dt (BUG 7 / PR #39): a missing/malformed
    ``due`` sorts LAST (datetime.max) so dated asks lead, and the age tiebreak is
    the parsed ``updated_at`` (oldest first) so the longest-waiting ask wins ties.
    We compare parsed datetimes, never the mixed-precision ISO-Z strings."""
    due = _parse_dt(s.get("due") or "")
    upd = _parse_dt(s.get("updated_at") or "")
    return (
        due or datetime.max.replace(tzinfo=timezone.utc),
        upd or datetime.max.replace(tzinfo=timezone.utc),
    )


def build_operator_digest(summaries: list[dict[str, Any]],
                          presence: list[dict[str, Any]], *,
                          human: str,
                          now: Optional[datetime] = None,
                          since: Optional[datetime] = None,
                          infra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Fold bus state into the operator's situational-awareness digest (pure).

    Four blocks, derived ONLY from task_summary dicts + presence records (no I/O,
    no body fetch). Deterministic given injected ``now``/``since`` (both injected
    for tests). Reuses the existing read-model so the digest can never disagree
    with ``needs-me`` / ``presence`` / ``needs-attention``:

      * ``blocked_on_you`` — ``needs_human`` (due-now only), RE-RANKED by due
        soonest then oldest updated_at (the human reads the most-urgent ask
        first). ``needs_human`` returns oldest-first; we re-sort by _digest_due_key.
      * ``upcoming`` — ``upcoming_for_human`` (future not_before within 7d).
      * ``per_agent`` — one entry per presence record: its agent id, workstreams,
        liveness, summary, and the tasks it FINISHED/transitioned since ``since``
        (done/abandoned with done_at >= since). Parsed-datetime ``since`` compare.
      * ``stale`` — active tasks past the stale threshold (``is_stale``), the same
        needs-attention safety-net set, sorted oldest-first.
      * ``infra`` — a pre-computed ``assess_infra_health`` dict (passed in; the
        pure builder does no I/O), rendered as one compact line by
        ``_render_digest``. None when not supplied.

    ``now``/``since`` default to wall-clock / (now - 12h) so a bare call still
    works, but the command always injects them explicitly."""
    now_dt = (now or _now()).astimezone(timezone.utc)
    since_dt = (since or (now_dt - timedelta(hours=12))).astimezone(timezone.utc)

    blocked = sorted(needs_human(summaries, human, now=now_dt), key=_digest_due_key)
    upcoming = upcoming_for_human(summaries, human, now=now_dt)

    # Index finished/transitioned-since tasks by the owning/touching agent, so a
    # per_agent entry can list what that agent wrapped up this window. A summary
    # carries done_at (flattened) — gate on a PARSED compare against since.
    by_agent_done: dict[str, list[dict[str, Any]]] = {}
    for s in summaries:
        if s.get("status") not in ("done", "abandoned"):
            continue
        done_dt = _parse_dt(s.get("done_at") or s.get("updated_at") or "")
        if done_dt is None or done_dt < since_dt:
            continue
        for who in {s.get("owner_agent"), s.get("last_touched_by")}:
            if who:
                by_agent_done.setdefault(who, []).append(s)

    per_agent = []
    for rec in presence:
        agent = rec.get("agent", "")
        per_agent.append({
            "agent": agent,
            "workstreams": list(rec.get("workstreams", [])),
            "summary": rec.get("summary", ""),
            "liveness": presence_liveness(rec.get("last_seen", ""), now_dt),
            "finished_since": sorted(
                by_agent_done.get(agent, []),
                key=lambda x: _parse_dt(x.get("done_at") or x.get("updated_at") or "")
                or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True),
        })

    stale = sorted(
        (s for s in summaries if is_stale(s, now_dt)),
        key=lambda x: _parse_dt(x.get("updated_at") or "")
        or datetime.min.replace(tzinfo=timezone.utc))

    return {
        "schema": "fulcra.coordination.operator_digest.v1",
        "human": human,
        "now": now_dt.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "since": since_dt.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "blocked_on_you": blocked,
        "upcoming": upcoming,
        "per_agent": per_agent,
        "stale": stale,
        "infra": infra,  # pre-computed assess_infra_health dict, or None (v1 push surface)
    }
