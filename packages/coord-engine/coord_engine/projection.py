"""Read-side projection sections of ``_coord/summaries.json``.

ARCHITECTURE: annotations are the events and files are their bodies. Reconcile
is the single incremental materializer: it consumes ``data-updates`` once and
publishes engine-owned views. Read verbs never re-derive the stable corpus.
They read ``summaries.json`` once, gate its projection with the same change feed,
and fold only the bounded changed-slug head. A clean feed makes the unchanged
tail CURRENT independent of elapsed wall time; an unreadable feed is UNKNOWN
and forces a loud raw fallback. ``COORD_PROJECTION_MAX_AGE_HOURS`` remains an
outer diagnostic bound for that failure path, never a substitute for feed proof.

The write side of "the annotation thing" already keeps ``summaries.json`` fresh
on every reconcile (typed transitions, the E1 cursor, the ack fold state). This
module finishes the READ side's contract: reconcile also folds the review and
forge state the wake folds need into the SAME document, so a wake can answer
"what reviews/forge feedback need me?" from ONE summaries read instead of a
budget-bounded scan of hundreds of raw store files (the live
``review-fold-degraded: scanned 19 of 327`` class).

Two top-level sections ride the aggregate (``build_aggregate``'s unknown-key
passthrough carries them across mixed-fleet hosts; a host too old to preserve
them merely wipes them, which readers treat as "projection absent" and fall back
to the raw scan — fail-closed, never wrong):

``reviews`` — ``coord.reviews.projection.v2`` (v2 adds ``of`` + ``head``,
the OC5/C01 act-on-it fields; both are always present, None when the register
doc genuinely lacks them — a legacy headless review has no head to serve)::

    {"schema", "generated_at", "complete", "scanned", "total",
     "rows": [{"name", "state", "pending_required", "required",
               "requested_by", "artifact", "of", "head", "settled",
               "mtime", "size"}],
     "orphans": [slug], "orphans_unknown": [slug], "tombstones": [slug]}

``forge`` — ``coord.forge.projection.v1``::

    {"schema", "generated_at", "complete",
     "responsible": {pr_slug: [agent, ...]},
     "feedback": {pr_slug: [{"id": shard-stem, "author": str|None}, ...]}}

``needs_me`` — ``coord.needs-me.projection.v1``::

    {"schema", "generated_at", "complete", "scanned", "total",
     "rows": [{"name", "mtime", "acked_by": [agent, ...]}]}

The needs-me section binds reconciled acknowledgment state to name + mtime.  A
matching row is projection-covered; a new or modified caller row is the live
head and is raw-tallied immediately.  Thus a large stable tail costs no
per-directive reads without hiding work that arrived after the projection.

THE FRESHNESS DOCTRINE (no silent staleness): a fold may consume a section only
when it is FRESH — stamped by reconcile within ``COORD_PROJECTION_MAX_AGE_HOURS``
(default 24h) AND ``complete`` — and must SAY it did ("projection (as of T)").
Anything else (stale, incomplete, unparseable) is a LOUD fallback to the raw
scan; a section that simply does not exist (a team whose reconcile predates this
module) is a silent fallback, so such a team behaves exactly as before.

BUILD COST + CONVERGENCE: the build pays one ``review/`` listing per pass, then
per-slug work ONLY for rows the update feed says changed or not yet scanned.
Positive feed evidence makes every unchanged prior row a durable scan frontier,
including rows accumulated by an incomplete budget-cut pass; scanned counts
therefore move monotonically toward ``total`` instead of restarting at zero.
Without feed evidence the conservative legacy rule carries only settled rows
whose doc mtime+size still match (including the same-minute guard). The build
also drops the ``.settled`` marker on any tally it proves settleable. Until the
build completes inside its budget, incomplete work stays in the private
progress shard while readers keep serving the last complete public generation.
Only a missing, stale, or legacy pre-fence generation falls back to a loud raw
scan; an incomplete projection is never published as coverage.

Stdlib-only; transport duck-typed (``list_dir``/``read``/``write``); build
functions never raise (reconcile wraps them best-effort anyway).
"""

from __future__ import annotations

import hashlib
import json

from typing import Any, Optional

from datetime import datetime, timezone

from . import aggregate, config, forge, generation, okf, public_read, review, review_gc
from .budget import Deadline
from .log import get_logger
from .roles import age_hours
from .transport import TransportError

#: Top-level summaries.json keys the projection sections live under.
REVIEWS_KEY = "reviews"
FORGE_KEY = "forge"
NEEDS_ME_KEY = "needs_me"
PUBLICATION_FENCE_KEY = "projection_publication_fence"
FENCE_GENERATION_KEY = "publication_generation"
BUILD_PROGRESS_SCHEMA = "coord.projection-build-progress.v1"

REVIEWS_SCHEMA = "coord.reviews.projection.v2"
FORGE_SCHEMA = "coord.forge.projection.v1"
NEEDS_ME_SCHEMA = "coord.needs-me.projection.v1"
PUBLICATION_FENCE_SCHEMA = "coord.projection-publication-fence.v1"

REQUIRED_SECTIONS = (
    (REVIEWS_KEY, REVIEWS_SCHEMA),
    (FORGE_KEY, FORGE_SCHEMA),
    (NEEDS_ME_KEY, NEEDS_ME_SCHEMA),
)

# Review is the expensive source fold and forge is derived from its rows, so
# they form one atomic publication unit. needs_me has a separate retry-safety
# contract, but shares the aggregate write: when review/forge refusal keeps the
# prior public needs_me section, that staleness is safe-redundant (newly acked
# work may remain visible; owed work is not hidden). On a publishable pass, an
# inconclusive ack fold includes its held anchor and ``complete: false`` so the
# next quiet pass cannot fast-path past owed work.
ATOMIC_PUBLICATION_SECTIONS = (
    (REVIEWS_KEY, REVIEWS_SCHEMA),
    (FORGE_KEY, FORGE_SCHEMA),
)

#: Maximum age (hours) a projection section may have and still be served by a
#: fold (env ``COORD_PROJECTION_MAX_AGE_HOURS``). Generous by design: reconcile
#: heartbeats run far more often than daily, so a section older than this means
#: the heartbeat is broken and the raw scan is the honest path.
DEFAULT_MAX_AGE_HOURS = 24.0

#: Wall-clock budget (seconds) for ONE projection build inside a reconcile pass
#: (env ``COORD_PROJECTION_BUILD_BUDGET``). On breach the section is stamped
#: ``complete: false`` (readers keep raw-scanning, loudly) and the next pass
#: resumes converging — carried rows cost nothing, so each pass reaches further.
DEFAULT_BUILD_BUDGET = 240.0
#: The FORGE section's own budget, seconds. Deliberately smaller than the review
#: budget: the forge fold is one listing per watched PR over a far smaller
#: population, so it does not need parity — it needs a floor that a busy review
#: fold cannot take away.
DEFAULT_FORGE_BUDGET = 60.0

#: How many UNKNOWN slugs may be re-scanned once within the same pass. Small by
#: intent: a handful is a transient, a crowd is a budget cut, and only the first
#: is worth paying for inside the same deadline.
RETRY_UNKNOWN_MAX = 3

#: Skew tolerance (hours) for a section stamped slightly in the FUTURE of the
#: reading host's clock — the same 900s budget reconcile's fast path trusts
#: between hosts (``reconcile.FAST_PATH_SKEW_MARGIN_SECONDS``; not imported to
#: keep this module import-cycle-free under ``reconcile -> projection``).
_FUTURE_SKEW_HOURS = 0.25


def _publication_fence_reason(
    aggregate_doc: dict[str, Any], section: dict[str, Any], key: str,
) -> str:
    """Why ``section`` is incompatible with the aggregate's writer fence.

    No fence means a legacy aggregate and remains readable during rollout. Once
    a new writer publishes a fence, preserving old writers carry that unknown
    top-level key but cannot stamp rebuilt sections with its generation. New
    readers therefore reject the republish instead of trusting mixed-version
    bytes as one generation.
    """
    if PUBLICATION_FENCE_KEY not in aggregate_doc:
        return ""
    fence = aggregate_doc.get(PUBLICATION_FENCE_KEY)
    if (not isinstance(fence, dict)
            or fence.get("schema") != PUBLICATION_FENCE_SCHEMA
            or not isinstance(fence.get("generation"), str)
            or not fence.get("generation")):
        return f"{key} projection compatibility fence invalid"
    if section.get(FENCE_GENERATION_KEY) != fence["generation"]:
        return f"{key} projection compatibility fence mismatch"
    return ""

#: The review fold's settled-cache marker filename (mirrors ``cli.SETTLED_MARKER``
#: — defined there first; duplicated here because ``cli`` imports this module).
SETTLED_MARKER = ".settled"

#: ``review gc``'s terminal marker (mirrors ``review_gc.GC_MARKER``). A retired
#: entry is terminal but was never reviewed, so it is folded as RETIRED rather
#: than APPROVED — and, crucially, it is skipped HERE, in the reader. Writing the
#: marker without teaching the readers would have retired nothing: the entry
#: would still be scanned, still tallied pending, and still consume the exact
#: projection budget the verb exists to recover (codex-reviewer, review-gc r1).
VFP_KEY = "vfp"   #: verdicts-listing fingerprint recorded on each scanned row
#: Did this row's evidence admit a cache binding at all — i.e. was every verdict
#: shard append-only (or the row backed by merge evidence)? Tier 1 carries a
#: settled row at ZERO ops, which is only sound when nothing under the slug can
#: change without changing the review DOC. A hand-written plain shard can be
#: rewritten in place, touching neither the doc nor its metadata, so a row built
#: over one may never take that tier (codex-reviewer, 595 r6). A row from a
#: build before this key existed lacks it and is demoted — fail closed.
BINDABLE_KEY = "ev_bindable"
GC_MARKER = ".gc-closed"


def max_age_hours() -> float:
    """Serve-threshold for projection sections, hours. Env
    ``COORD_PROJECTION_MAX_AGE_HOURS`` (see DEFAULT_MAX_AGE_HOURS)."""
    return config.env_float("COORD_PROJECTION_MAX_AGE_HOURS", DEFAULT_MAX_AGE_HOURS)


def build_budget() -> float:
    """Per-pass REVIEW projection build budget, seconds. Env
    ``COORD_PROJECTION_BUILD_BUDGET`` (see DEFAULT_BUILD_BUDGET)."""
    return config.env_float("COORD_PROJECTION_BUILD_BUDGET", DEFAULT_BUILD_BUDGET)


def forge_budget() -> float:
    """Per-pass FORGE projection build budget, seconds — its OWN, not a remainder.

    Both sections used to share one `Deadline` object, and the review fold spent
    it first. That is not a slow-forge problem, it is a never-forge one: measured
    on the live store 2026-08-10, the review fold cut at 192/219 and the forge
    section came back `scanned=None, total=None, rows=0` — never built at all, on
    every pass, so every consumer paid a 74.8s raw fallback to discover it.

    A section that always runs last inside a shared budget does not degrade
    gracefully; it starves deterministically. AGENTS.md already says a budget cut
    may only truncate the tail — this is the same rule one level up, applied
    between sections rather than within one.

    The cost is honest and worth stating: worst-case pass duration is now the SUM
    of the two budgets rather than one shared cap. Starving a section to keep a
    wall-clock bound was never the trade anyone chose; it was an accident of
    passing one object twice.
    """
    return config.env_float("COORD_FORGE_BUILD_BUDGET", DEFAULT_FORGE_BUDGET)


def fresh_section(
    aggregate_doc: Any, key: str, schema: str, *, now: str
) -> tuple[Optional[dict[str, Any]], str]:
    """The projection section a fold may consume, or why it must raw-scan.

    Returns ``(section, "")`` when the section is present, schema-recognized,
    stamped within ``COORD_PROJECTION_MAX_AGE_HOURS`` and ``complete`` — the ONLY
    state a fold may serve from. Otherwise ``(None, reason)``:

    * ``reason == ""`` — the aggregate carries NO such section (a team whose
      reconcile has never written it). The fold falls back SILENTLY, so such a
      team behaves exactly as before this module existed.
    * a non-empty ``reason`` — the section exists but must not be served
      (stale/incomplete/unrecognized). The fold falls back LOUDLY, naming the
      reason (the no-silent-staleness doctrine: old state is never presented as
      current, and a projection that stopped refreshing is made visible).
    """
    if not isinstance(aggregate_doc, dict) or key not in aggregate_doc:
        return None, ""
    section = aggregate_doc.get(key)
    if not isinstance(section, dict) or section.get("schema") != schema:
        return None, f"{key} projection unrecognized"
    fence_reason = _publication_fence_reason(aggregate_doc, section, key)
    if fence_reason:
        return None, fence_reason
    age = age_hours(section.get("generated_at"), now)
    if age == float("inf"):
        return None, f"{key} projection stamp unreadable"
    if age < -_FUTURE_SKEW_HOURS:
        return None, f"{key} projection stamped in the future"
    limit = max_age_hours()
    if age > limit:
        return None, (f"{key} projection stale "
                      f"({age:.1f}h old, max {limit:g}h)")
    if section.get("complete") is not True:
        scanned, total = section.get("scanned"), section.get("total")
        detail = (f" (scanned {scanned}/{total})"
                  if isinstance(scanned, int) and isinstance(total, int) else "")
        return None, f"{key} projection incomplete{detail}"
    return section, ""


def generation_section(
    transport: Any, team: str, key: str, *, now: Optional[datetime] = None,
) -> tuple[Optional[dict[str, Any]], str]:
    """Read one section from the digest-verified current generation.

    This is the v2 publication validation path.  Existing aggregate readers
    remain on the compatibility fence until each is migrated, but no new reader
    should treat ``summaries.json`` as its publication authority.
    """
    if getattr(transport, "public_read_v2_enabled", None) is True:
        authority = public_read.read_current(
            transport,
            team,
            now=now or datetime.now(timezone.utc),
            epsilon_seconds=getattr(transport, "public_read_epsilon_seconds", None),
            epsilon_verified=getattr(
                transport, "public_read_epsilon_verified", False,
            ),
        )
        if authority.rc != 0:
            observations = [
                f"{item.surface}={item.state.value}"
                + (f" ({item.reason})" if item.reason else "")
                for item in authority.coverage
                if item.state.value in ("UNKNOWN", "NOT_RUN")
            ]
            return None, "; ".join(observations) or "public read unknown"
        if key not in authority.sections:
            return None, f"{key} public-read section unrecognized"
        section = authority.section(key)
        if not isinstance(section, dict):
            return None, f"{key} public-read section unrecognized"
        return dict(section), ""

    current = generation.load_current(transport, team)
    if current is None:
        return None, "current generation absent or unverifiable"
    try:
        doc = json.loads(current.bytes)
        section = doc["sections"][key]
    except (KeyError, TypeError, ValueError):
        return None, f"{key} generation section unrecognized"
    if (not isinstance(section, dict)
            or section.get("state") not in generation.COMPLETE_STATES
            or not isinstance(section.get("value"), dict)):
        return None, f"{key} generation section incomplete"
    return section["value"], ""


def feed_fresh_section(
    aggregate_doc: Any, key: str, schema: str, *, now: str,
) -> tuple[Optional[dict[str, Any]], str]:
    """A section structurally eligible for CHANGE-FEED-gated serving.

    The caller must already hold positive evidence from ``data-updates`` for the
    window beginning at this section's ``generated_at``.  Consequently elapsed
    wall time is not freshness evidence here: the feed proves the stable tail,
    while changed slugs are overlaid separately.  We still reject unreadable or
    future stamps and incomplete/unrecognized sections.  If the feed is
    unavailable callers use :func:`fresh_section` only to enrich the loud raw
    fallback reason; UNKNOWN is never promoted to fresh.
    """
    if not isinstance(aggregate_doc, dict) or key not in aggregate_doc:
        return None, ""
    section = aggregate_doc.get(key)
    if not isinstance(section, dict) or section.get("schema") != schema:
        return None, f"{key} projection unrecognized"
    fence_reason = _publication_fence_reason(aggregate_doc, section, key)
    if fence_reason:
        return None, fence_reason
    age = age_hours(section.get("generated_at"), now)
    if age == float("inf"):
        return None, f"{key} projection stamp unreadable"
    if age < -_FUTURE_SKEW_HOURS:
        return None, f"{key} projection stamped in the future"
    if section.get("complete") is not True:
        scanned, total = section.get("scanned"), section.get("total")
        detail = (f" (scanned {scanned}/{total})"
                  if isinstance(scanned, int) and isinstance(total, int) else "")
        return None, f"{key} projection incomplete{detail}"
    return section, ""


def sections_owing_pass(aggregate_doc: Any) -> list[str]:
    """Required projections that do not prove a completed CURRENT build.

    Reconcile's no-change fast path is licensed to skip materialization only
    when every required section exists, has the recognized schema, completed,
    and was built in the same pass as its aggregate container. A missing section
    is an upgrade/mixed-fleet rebuild debt; ``complete: false`` is explicit
    convergence debt; and a stamp older than the container means a failed build
    carried prior state. All three must decline the fast path even on a quiet
    store, or an unusable projection can persist forever.
    """
    if not isinstance(aggregate_doc, dict):
        return [key for key, _schema in REQUIRED_SECTIONS]
    fence = aggregate_doc.get(PUBLICATION_FENCE_KEY)
    if (not isinstance(fence, dict)
            or fence.get("schema") != PUBLICATION_FENCE_SCHEMA
            or not isinstance(fence.get("generation"), str)
            or not fence.get("generation")):
        return [key for key, _schema in REQUIRED_SECTIONS]
    generated_at = aggregate_doc.get("generated_at")
    owed: list[str] = []
    for key, schema in REQUIRED_SECTIONS:
        section = aggregate_doc.get(key)
        if (not isinstance(section, dict)
                or section.get("schema") != schema
                or section.get("complete") is not True
                or section.get("generated_at") != generated_at
                or section.get(FENCE_GENERATION_KEY) != fence["generation"]):
            owed.append(key)
    return owed


def build_needs_me_projection(
    rows: list[dict[str, Any]], *, now: str, complete: bool,
) -> dict[str, Any]:
    """Snapshot the ack-dependent part of the task fold.

    The aggregate's task rows already contain every field used by
    :func:`query.needs_me`; the only expensive live fan-out is the per-directive
    acknowledgment check.  This compact sibling section positively binds the
    ack list to the task row's name + store mtime.  A reader may therefore use a
    row only while that pair still matches.  A newly written or changed
    caller-owned row is outside coverage and is raw-tallied immediately, the
    same head/tail split used by the review projection.

    ``complete`` is the ack fold's conclusiveness bit.  An inconclusive fold is
    still persisted for diagnosis/convergence, but ``fresh_section`` will never
    serve it as truth.
    """
    projected = []
    for row in rows:
        name = row.get("name")
        if not isinstance(name, str) or not name:
            continue
        projected.append({
            "name": name,
            "mtime": row.get("mtime"),
            "acked_by": sorted({str(a) for a in (row.get("acked_by") or [])
                                if str(a)}),
        })
    return {
        "schema": NEEDS_ME_SCHEMA,
        "generated_at": now,
        "complete": bool(complete),
        "scanned": len(projected) if complete else 0,
        "total": len(projected),
        "rows": projected,
    }


# ---------------------------------------------------------------------------
# Review projection build (runs inside reconcile)
# ---------------------------------------------------------------------------

def _review_prefix(team: str) -> str:
    return f"team/{team}/review/"


def _verdicts_prefix(team: str, slug: str) -> str:
    return f"{_review_prefix(team)}{slug}/verdicts/"


def _normalize_required(required: Any) -> list[str]:
    """Coerce a review doc's ``required:`` field (list or legacy comma-string)
    into a clean reviewer-name list (mirrors ``cli._normalize_required``)."""
    if isinstance(required, str):
        return [r.strip() for r in required.split(",") if r.strip()]
    if isinstance(required, list):
        return [str(r).strip() for r in required if str(r).strip()]
    return []


def _verdicts_fingerprint(ventries: list[dict[str, Any]]) -> str:
    """Order-independent fingerprint of a verdicts listing.

    A review's tally is a function of (required set from the doc) x (verdict
    files). Tier 3 below carries an UNSETTLED row when the doc is unchanged AND
    this fingerprint is unchanged — which is the same immutability argument
    tier 1 already makes, extended over the one input tier 1 does not cover.
    Name+size+mtime per shard, sorted, so listing order cannot perturb it."""
    parts = sorted(
        f"{e.get('name') or ''}\x1f{e.get('size')}\x1f{e.get('mtime')}"
        for e in ventries if not e.get("is_dir"))
    return hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()[:32]


def _shards_minutes_closed(ventries: list[dict[str, Any]],
                           prior_generated_at: Any) -> bool:
    """True iff EVERY verdict shard's mtime-minute closed before the prior pass.

    The fingerprint alone is not enough, and this is the half I got wrong in the
    first cut: I applied the same-minute guard to the review DOC and left the
    shards unguarded, in a change whose own description named minute-granular
    mtime as the hazard. codex-reviewer reproduced it at the exact head — an
    unsettled row projected at 15:00:30Z, its `approve` shard rewritten to an
    equal-length `changes` inside the same clock-minute, and the second build
    carried the stale PENDING row because name+size+mtime were all identical.

    A verdict flipping approve->changes at equal length inside one minute is not
    a contrived case: it is a reviewer correcting themselves, and carrying it
    would freeze a CHANGES review as PENDING durably. So: any shard whose minute
    is not provably closed forces a full rescan. Correct beats cheap, and only
    the recently-touched slugs pay."""
    from . import reconcile as rec  # lazy: reconcile imports projection
    for v in ventries:
        if v.get("is_dir"):
            continue
        if rec._same_minute_reuse_safe(v.get("mtime"), prior_generated_at) is not True:
            return False   # ambiguous or unprovable -> rescan, never carry
    return True


def _unsettled_carry_safe(prior_row: Any, entry: dict[str, Any],
                          prior_generated_at: Any) -> bool:
    """Doc-side half of the TIER-3 carry: is this row *eligible* for a
    one-listing carry? The verdicts fingerprint is compared separately, because
    obtaining it costs the one op tier 3 is budgeted for.

    Why tier 3 exists: unsettled rows never carry under tier 1, so a
    permanently-unsettleable entry sits in the FRESH set on every pass forever,
    consuming a full scan each time. That is a non-converging tax which breaks
    the convergence property this module documents — and it cannot be cleared by
    gc alone, which only retires entries it can *prove* dead (9 of 158 measured
    on 2026-08-07, against a 129/158 budget cut). Tier 3 fixes the cost
    structurally instead of trying to decide which entries are dead.

    Same guards as tier 1: doc mtime+size identical, and the same-minute reuse
    guard — mtime granularity is ONE MINUTE, so a same-minute edit at identical
    size is invisible and must not be trusted."""
    if not isinstance(prior_row, dict):
        return False
    if prior_row.get("settled") is True and prior_row.get(BINDABLE_KEY) is True:
        return False                      # tier 1's business, and it may have it
    # A SETTLED row whose evidence is not bindable lands HERE rather than in
    # tier 1: its plain shard can be rewritten in place, so it needs the one
    # listing this tier pays for. That is the demotion 595 r6 owed — and it is a
    # listing, not a full rescan, because the fingerprint compares name+size+
    # mtime per shard and `_shards_minutes_closed` refuses any unclosed minute,
    # which is strictly more than a name digest could ever see.
    if not prior_row.get(VFP_KEY):
        return False                      # no fingerprint recorded: cannot compare
    entry_mtime = entry.get("mtime")
    if not entry_mtime or prior_row.get("mtime") != entry_mtime:
        return False
    if prior_row.get("size") is None or prior_row.get("size") != entry.get("size"):
        return False
    from . import reconcile as rec  # lazy: reconcile imports projection
    return rec._same_minute_reuse_safe(entry_mtime, prior_generated_at) is not False


def _settled_carry_safe(prior_row: Any, entry: dict[str, Any],
                        prior_generated_at: Any) -> bool:
    """True iff ``prior_row`` may be carried WITHOUT re-reading its review.

    Only a SETTLED prior row with BINDABLE evidence qualifies. The old rule
    stopped at "settled", on the argument that a settled round is immutable and
    re-opening the slug rewrites the review doc — true of re-opening at a new
    head, and false of the one case this PR is about: a hand-written plain shard
    rewritten IN PLACE at the same head touches neither the doc nor its
    metadata, so this tier carried a stale APPROVED forever without ever
    reaching `review.settle_shortcircuit` (codex-reviewer, 595 r6). Fixing the
    two readers below it left the tier ABOVE them untouched — the third time in
    this PR that a rule landed one layer away from a sibling that needed it.

    Unsettled rows never carry here (their verdict shards can change without
    touching the doc), and neither do settled rows over mutable evidence: both
    fall to tier 3's one listing. The same-minute guard is reconcile's (imported
    lazily; ``reconcile`` imports this module)."""
    if not isinstance(prior_row, dict) or prior_row.get("settled") is not True:
        return False
    if prior_row.get(BINDABLE_KEY) is not True:
        return False
    entry_mtime = entry.get("mtime")
    if not entry_mtime or prior_row.get("mtime") != entry_mtime:
        return False
    if prior_row.get("size") is None or prior_row.get("size") != entry.get("size"):
        return False
    from . import reconcile as rec  # lazy: reconcile imports projection
    return rec._same_minute_reuse_safe(entry_mtime, prior_generated_at) is not False


def review_changed_slugs(team: str, changes: list[Any]) -> set[str]:
    """Review slugs touched by a positively read team updates window."""
    prefix = _review_prefix(team)
    out: set[str] = set()
    for change in changes:
        if not isinstance(change, dict):
            continue
        path = str(change.get("path") or change.get("full_name") or "").lstrip("/")
        if not path.startswith(prefix):
            continue
        rest = path[len(prefix):]
        if not rest:
            continue
        first = rest.split("/", 1)[0]
        slug = first[:-3] if first.endswith(".md") else first
        if slug and slug != "index":
            out.add(slug)
    return out


def _feed_carry_safe(prior_row: Any, entry: dict[str, Any], *,
                     slug: str, changed_slugs: Optional[set[str]],
                     prior_generated_at: Any) -> bool:
    """Carry any unchanged row when the feed proves its slug did not move.

    This is primarily the durable convergence cursor for incomplete builds:
    rows scanned before a budget cut survive the next pass, while any review
    doc or verdict-shard update puts the slug back in the raw head.  Without
    positive feed evidence we retain the older settled-only carry rule.
    """
    if changed_slugs is None:
        return _settled_carry_safe(prior_row, entry, prior_generated_at)
    if slug in changed_slugs or not isinstance(prior_row, dict):
        return False
    if prior_row.get("mtime") != entry.get("mtime"):
        return False
    if prior_row.get("size") is None or prior_row.get("size") != entry.get("size"):
        return False
    return True


def _store_mtime_iso(mtime: Any) -> Optional[str]:
    """Listing mtime -> comparable ISO, or None.

    Store mtimes render on a TWELVE-HOUR clock, so comparing them as strings
    inverts the midnight hour. Parsed through the one existing parser.
    """
    if not isinstance(mtime, str):
        return None
    dt = aggregate._parse_store_mtime(mtime)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _scan_review_slug(
    transport: Any, team: str, slug: str, entry: dict[str, Any], *,
    now: str, deadline: Deadline,
) -> tuple[Optional[dict[str, Any]], bool]:
    """Fresh-scan ONE review slug into a projection row.

    Returns ``(row, True)`` on a trustworthy scan, ``(None, False)`` when the
    slug is UNKNOWN (unlistable verdicts, unreadable doc, an unreadable verdict
    shard, or the budget expired mid-slug). UNKNOWN never yields a row — a
    partial tally is a floor a projection must not freeze (a lost CHANGES
    verdict would read APPROVED, durably)."""
    try:
        ventries = transport.list_dir(_verdicts_prefix(team, slug))
    except TransportError:
        return None, False
    doc_raw = transport.read(f"{_review_prefix(team)}{slug}.md")
    if doc_raw is None or deadline.expired():
        return None, False
    fm = okf.parse_frontmatter(doc_raw) or {}
    requested_by = fm.get("requested_by")
    head = review.normalize_head(fm.get("head"))
    of = fm.get("of")
    base: dict[str, Any] = {
        "name": slug,
        "required": _normalize_required(fm.get("required")),
        "requested_by": str(requested_by) if requested_by else None,
        "artifact": forge.pr_slug(forge.review_artifact(fm)),
        # OC5/C01 act-on-it fields: served on every row so a strict consumer
        # can act without a second lookup. None is the honest value when the
        # register doc itself lacks the field (legacy headless reviews).
        "of": str(of) if of else None,
        "head": head or None,
        "mtime": entry.get("mtime"),
        "size": entry.get("size"),
    }
    base[VFP_KEY] = _verdicts_fingerprint(ventries)
    vnames = {(v.get("name") or "") for v in ventries}
    # Recorded on EVERY row this function returns, because the zero-op tier-1
    # carry never lists this directory and so can never recompute it. Only a row
    # that says True here may skip straight past both readers below.
    base[BINDABLE_KEY] = review.evidence_is_immutable(vnames)
    if review_gc.is_terminal(vnames):
        # Retired by gc: OMITTED from the projection, and the scan counts as
        # COMPLETE. Round 2 of this review emitted a `state: RETIRED,
        # settled: true` row instead, and `_validated_review_projection` accepts
        # only PENDING/APPROVED/CHANGES and rejects any settled row that is not
        # APPROVED — so the first retired entry invalidated the WHOLE section and
        # every consumer fell back to the raw scan. Omission needs no new state
        # in a validated schema, and a retired entry carries no review
        # information a consumer wants: it owes nobody a verdict.
        return None, True
    marker_fm: dict = {}
    if SETTLED_MARKER in vnames:
        # A cache is only trustworthy if it still describes THIS directory.
        #
        # Deleting a stale cache cannot stop another writer recreating it: a
        # `review status` that read the old tally, paused, and resumed AFTER a
        # correction landed rewrote `.settled` from its stale snapshot, and this
        # short-circuit then answered APPROVED while the newest verdict was
        # CHANGES (codex-reviewer, 595 r4). No delete ordering fixes that.
        #
        # So VALIDATE rather than order — in the SHARED decision function, so
        # the fan-out obligation scan applies the identical rule. That rule now
        # also refuses any directory holding a mutable plain shard, whose
        # in-place rewrite a name digest cannot see (595 r5).
        marker_raw = transport.read(_verdicts_prefix(team, slug) + SETTLED_MARKER)
        marker_fm = okf.parse_frontmatter(marker_raw) or {}
        short = review.settle_shortcircuit(marker_fm, vnames)
        if short != review.SETTLE_NO:
            row = {**base, "state": review.APPROVED, "pending_required": [],
                   "settled": True}
            if short == review.SETTLE_MERGED:
                # MERGE EVIDENCE is bindable whatever the shards look like: it
                # records that a PR landed, and no later verdict can make that
                # untrue, so no rewrite under this slug can move the row. That
                # keeps terminal reviews — most of the register — at the zero-op
                # tier even when their shards are hand-written.
                row[BINDABLE_KEY] = True
            return row, True
        # Otherwise fall through and fold the shards for real.
    verdicts: list[dict[str, Any]] = []
    for v in ventries:
        n = v.get("name") or ""
        if v.get("is_dir") or not n.endswith(".md"):
            continue
        parsed_name = review.parse_verdict_filename(n, head=head)
        reviewer = parsed_name[0] if parsed_name else None
        parsed_ts = parsed_name[1] if parsed_name else None
        if reviewer is None:
            continue  # superseded head / foreign filename: zero reads
        if deadline.expired():
            return None, False
        raw_v = transport.read(_verdicts_prefix(team, slug) + n)
        if deadline.expired():
            return None, False
        if raw_v is None:
            return None, False  # listed shard unreadable -> the tally is a floor
        vfm = okf.parse_frontmatter(raw_v) or {}
        if head and review.normalize_head(vfm.get("head")) != head:
            continue  # the verdict must independently attest the exact head
        verdicts.append({
            "reviewer": reviewer,
            "verdict": vfm.get("verdict"),
            "name": n,
            # SAME fallback chain as `_tally_from_verdict_entries` — filename
            # ts, then frontmatter ts, then the LISTING MTIME. Projection used
            # to stop at frontmatter, so a plain hand-written shard with no `ts`
            # sorted as empty and lost to an older append shard: the direct
            # tally said CHANGES while the projection said APPROVED for the same
            # directory (codex-reviewer, 595 r3). Two readers disagreeing about
            # the same evidence is worse than either answer alone.
            "sort_key": (parsed_ts
                         or str(vfm.get("ts") or "")
                         or _store_mtime_iso(v.get("mtime")) or ""),
        })
    # FOLD NEWEST PER REVIEWER (coord-boss constraint 5, ruling b99fb8da).
    # Append-only verdicts mean one reviewer can have several shards, and this
    # projection built ONE ENTRY PER FILE — so a superseded CHANGES and its
    # newer APPROVE both reached `review.tally`, where a single blocker
    # dominates, and the stale CHANGES would have blocked the review forever.
    # Every register reader learns the fold, not just `review status`.
    kept, folded_away = review.fold_newest_per_reviewer(verdicts)
    verdicts = [{"reviewer": r["reviewer"], "verdict": r["verdict"]}
                for r in kept]
    tally = review.tally(verdicts, required=base["required"])
    if folded_away:
        tally["superseded_verdicts"] = folded_away
    settled = (tally["state"] == review.APPROVED
               and not tally["pending_required"] and bool(base["required"]))
    # Same proven-settle cache the read fold writes — accelerates BOTH the raw
    # fold's settled-skip and this build's own convergence. Best-effort.
    #
    # It goes through the SHARED field builder, and only when the evidence can
    # actually be bound. This write used to emit schema/state/ts alone, so the
    # marker it produced failed its own reader's validation on the very next
    # pass — a cache that could never hit, written every time (codex-reviewer,
    # 595 r5). And a directory holding a mutable plain shard gets no cache at
    # all: one whose digest a reader must refuse is cost without benefit.
    #
    # An EXISTING marker is overwritten only when this build can positively
    # identify it as a cache. Unreadable or unrecognised means it may be the
    # merge evidence a `review close` wrote, and that is never ours to clobber.
    evidence = (review.evidence_digest(vnames)
                if review.evidence_is_immutable(vnames) else "")
    prior_state = str(marker_fm.get("state") or "")
    may_write = (SETTLED_MARKER not in vnames) or prior_state == review.APPROVED
    if settled and evidence and may_write:
        try:
            transport.write(
                _verdicts_prefix(team, slug) + SETTLED_MARKER,
                okf.render_frontmatter(review.settled_marker_fields(
                    state=review.APPROVED, ts=now, evidence=evidence)))
        except Exception:
            pass
    return {**base, "state": tally["state"],
            "pending_required": tally["pending_required"],
            "settled": settled}, True


def build_review_projection(
    transport: Any, team: str, *, now: str, prior: Any,
    settled_index: "set[str]", deadline: Deadline, log: Any = None,
    feed_changes: Optional[list[Any]] = None,
) -> dict[str, Any]:
    """Build the ``reviews`` section for this reconcile pass. Never raises.

    With positive feed evidence, carries every unchanged prior row at zero ops;
    without it, carries only prior SETTLED rows whose doc mtime+size are safe.
    Fresh-scans everything else under ``deadline``. A slug that could not be
    resolved (budget cut or transport doubt) keeps its prior row for the NEXT
    pass's carry check but marks the section ``complete: false`` — readers then
    keep raw-scanning until a pass resolves every slug. On an unlistable review
    root the PRIOR section is returned untouched (its old stamp ages it out
    honestly rather than re-stamping unknown state as current)."""
    log = log or get_logger("projection")
    prior = prior if isinstance(prior, dict) else {}
    if prior and prior.get("schema") != REVIEWS_SCHEMA:
        # A schema bump changes what a row must carry (v2 added of/head), so a
        # prior-era row can never be carried into a section stamped with the
        # new schema — that would sign rows the schema's own validator rejects.
        # One full fresh scan re-derives every row under the current schema.
        log.warn("review projection: prior schema superseded; full rescan",
                 team=team, prior_schema=str(prior.get("schema")))
        prior = {}
    prior_rows = {str(r.get("name")): r for r in (prior.get("rows") or [])
                  if isinstance(r, dict) and r.get("name")}
    prior_generated_at = prior.get("generated_at")
    prior_tombstones = {str(s) for s in (prior.get("tombstones") or []) if s}
    changed_slugs = (review_changed_slugs(team, feed_changes)
                     if isinstance(feed_changes, list) else None)
    try:
        entries = transport.list_dir(_review_prefix(team))
    except TransportError as e:
        if prior:
            log.warn("review projection: root listing failed; prior carried",
                     team=team, error=str(e))
            return prior
        return {"schema": REVIEWS_SCHEMA, "generated_at": now, "complete": False,
                "scanned": 0, "total": 0, "rows": [], "orphans": [],
                "orphans_unknown": [], "tombstones": []}

    doc_entries = sorted(
        ((e.get("name") or "")[:-3], e) for e in entries
        if not e.get("is_dir") and (e.get("name") or "").endswith(".md")
        and (e.get("name") or "") != "index.md")
    rows: list[dict[str, Any]] = []
    unknown = 0
    budget_cut = False
    # Zero-op carries first, budgeted fresh scans second — so a budget cut always
    # lands on the scan frontier and each pass converges further than the last.
    fresh: list[tuple[str, dict[str, Any]]] = []
    maybe: list[tuple[str, dict[str, Any]]] = []
    for slug, e in doc_entries:
        if _feed_carry_safe(prior_rows.get(slug), e, slug=slug,
                            changed_slugs=changed_slugs,
                            prior_generated_at=prior_generated_at):
            rows.append(prior_rows[slug])
        elif _unsettled_carry_safe(prior_rows.get(slug), e, prior_generated_at):
            maybe.append((slug, e))
        else:
            fresh.append((slug, e))
    # TIER 3: one listing each. If no verdict shard was added, removed or
    # modified AND the doc is unchanged, the tally provably cannot have moved,
    # so carry instead of re-reading every shard. A listing failure or any
    # mismatch demotes the slug to a full scan — never to a guess.
    for slug, e in maybe:
        if deadline.expired():
            fresh.append((slug, e)); continue
        try:
            ventries = transport.list_dir(_verdicts_prefix(team, slug))
        except TransportError:
            fresh.append((slug, e)); continue
        if (_verdicts_fingerprint(ventries) == prior_rows[slug].get(VFP_KEY)
                and _shards_minutes_closed(ventries, prior_generated_at)):
            rows.append(prior_rows[slug])
        else:
            fresh.append((slug, e))
    retryable: list[tuple[str, dict[str, Any]]] = []
    for slug, e in fresh:
        if budget_cut or deadline.expired():
            budget_cut = True
            unknown += 1
            if slug in prior_rows:
                rows.append(prior_rows[slug])  # kept for next pass's carry check
            continue
        row, ok = _scan_review_slug(transport, team, slug, e, now=now,
                                    deadline=deadline)
        if not ok:
            unknown += 1
            retryable.append((slug, e))
            if slug in prior_rows:
                rows.append(prior_rows[slug])
            continue
        if row is None:
            # Scanned successfully and deliberately not projected (gc-retired).
            # Complete, not unknown: the pass DID resolve this slug.
            continue
        rows.append(row)

    # IN-PASS RETRY of a SMALL unknown remainder (coord-boss direction, 2026-08-11).
    #
    # Measured: with 223 slugs the fold lands at 222/223 — ONE slug UNKNOWN in one
    # pass — while every slug resolves fine when scanned on its own. That single
    # blip is not a broken directory; it is a transient read against a fold with no
    # headroom. But because the forge section's completeness follows this one, a
    # single transient denies forge to the whole fleet until a later pass converges.
    #
    # So: re-scan just those slugs, ONCE. A transient converts and the pass
    # completes honestly; a real failure stays UNKNOWN and the section stays
    # honestly incomplete. Completeness SEMANTICS are untouched — this is not a
    # tolerance that calls one-short complete, which would manufacture exactly the
    # false-clear this codebase exists to hunt (coord-boss withdrew that option).
    #
    # TWO CONSTRAINTS, both learned the expensive way this week:
    #   * bounded by the SAME `deadline` object, never a fresh one — a retry that
    #     opens its own budget is the shared-budget defect wearing a retry hat
    #     (PR 599: one Deadline passed to two consumers starved the second);
    #   * only a SMALL remainder, and never after a budget cut. A large remainder
    #     means the budget ran out, and re-scanning it would spend the next
    #     section's time re-doing work that simply needs another pass.
    if retryable and not budget_cut and len(retryable) <= RETRY_UNKNOWN_MAX:
        for slug, e in retryable:
            if deadline.expired():
                break
            row, ok = _scan_review_slug(transport, team, slug, e, now=now,
                                        deadline=deadline)
            if not ok:
                continue          # still UNKNOWN: the prior row stays as-is
            # RESOLVED. `ok` is the whole question; `row is None` is a separate
            # fact meaning gc-retired — resolved successfully and deliberately
            # OMITTED from rows. My first cut wrote `if not ok or row is None`
            # and even named gc-retired in the comment while treating it as a
            # failure, so a retry that conclusively retired a slug left the
            # section incomplete AND kept a stale prior row (codex-reviewer, 602
            # r1). `(None, True)` and `(None, False)` are different answers; the
            # tri-state lesson, one more time, in the branch I had just written
            # to fix a different collapse.
            rows = [r for r in rows if r.get("name") != slug]
            if row is not None:
                rows.append(row)
            unknown -= 1
    rows.sort(key=lambda r: str(r.get("name")))

    # Dir-only slugs (a `<slug>/` with no doc): classify orphan / tombstone /
    # unknown via one verdicts listing each. Tombstones are permanent ghost dirs
    # (soft deletes) — cached from the prior section so they cost one listing
    # EVER, not one per pass. Unknowns stay visibly degraded (never assumed
    # tombstone) but do not un-complete the section: the raw fold treats an
    # unclassifiable dir as a per-dir degraded row, not wholesale failure.
    doc_slugs = {slug for slug, _ in doc_entries}
    orphans: list[str] = []
    orphans_unknown: list[str] = []
    tombstones: list[str] = []
    dir_slugs = sorted({
        (e.get("name") or "").rstrip("/") for e in entries if e.get("is_dir")
    } - doc_slugs - settled_index - {""})
    for slug in dir_slugs:
        if slug in prior_tombstones:
            tombstones.append(slug)
            continue
        if deadline.expired():
            orphans_unknown.append(slug)
            continue
        try:
            ventries = transport.list_dir(_verdicts_prefix(team, slug))
        except TransportError:
            orphans_unknown.append(slug)
            continue
        if any(not v.get("is_dir") and (v.get("name") or "").endswith(".md")
               for v in ventries):
            orphans.append(slug)
        else:
            tombstones.append(slug)

    section = {
        "schema": REVIEWS_SCHEMA,
        "generated_at": now,
        "complete": unknown == 0,
        "scanned": len(doc_entries) - unknown,
        "total": len(doc_entries),
        "rows": rows,
        "orphans": orphans,
        "orphans_unknown": orphans_unknown,
        "tombstones": tombstones,
    }
    if unknown:
        log.warn("review projection incomplete", team=team,
                 scanned=section["scanned"], total=section["total"],
                 budget_cut=budget_cut)
    return section


# ---------------------------------------------------------------------------
# Forge projection build (runs inside reconcile, after the review build)
# ---------------------------------------------------------------------------

def build_forge_projection(
    transport: Any, team: str, *, now: str, review_rows: list[dict[str, Any]],
    reviews_complete: bool, prior: Any, deadline: Deadline, log: Any = None,
) -> dict[str, Any]:
    """Build the ``forge`` section: PR responsibility (watch registry union the
    review docs' ``requested_by``, keyed by PR slug) plus each responsible PR's
    feedback shard ids/authors. Ack state stays a READ-side concern (it is
    per-agent), so a fold consuming this section pays only one ack read per
    feedback item for its own agent. Never raises.

    ``reviews_complete`` gates completeness: half the responsibility map derives
    from the review projection rows, so an incomplete review scan makes this
    section a floor — marked ``complete: false``, never served as coverage.
    ``prior`` is accepted for signature symmetry / future carry use."""
    log = log or get_logger("projection")
    del prior  # feedback dirs are small; a carry optimization is not worth state
    complete = bool(reviews_complete)
    resp: dict[str, set] = {}
    watch_prefix = f"team/{team}/_coord/forge/watch/"
    try:
        watch_entries = transport.list_dir(watch_prefix)
    except TransportError:
        watch_entries = []
        complete = False
    for e in watch_entries:
        n = e.get("name") or ""
        if e.get("is_dir") or not n.endswith(".md"):
            continue
        if deadline.expired():
            complete = False
            break
        raw = transport.read(watch_prefix + n)
        if raw is None:
            complete = False  # a registered watch we cannot attribute: floor
            continue
        fm = okf.parse_frontmatter(raw) or {}
        slug = forge.pr_slug(fm.get("url")) or n[:-3]
        agent = fm.get("agent")
        if agent:
            resp.setdefault(slug, set()).add(str(agent))
    for r in review_rows:
        if not isinstance(r, dict):
            continue
        slug, who = r.get("artifact"), r.get("requested_by")
        if slug and who:
            resp.setdefault(str(slug), set()).add(str(who))

    feedback: dict[str, list[dict[str, Any]]] = {}
    for slug in sorted(resp):
        if deadline.expired():
            complete = False
            break
        fb_prefix = f"team/{team}/_coord/forge/feedback/{slug}/"
        try:
            fentries = transport.list_dir(fb_prefix)
        except TransportError:
            complete = False  # this PR's feedback is UNKNOWN
            continue
        items: list[dict[str, Any]] = []
        for e in fentries:
            n = e.get("name") or ""
            if e.get("is_dir") or not n.endswith(".md"):
                continue
            if deadline.expired():
                complete = False
                break
            raw = transport.read(fb_prefix + n)
            author = (okf.parse_frontmatter(raw) or {}).get("author") if raw else None
            # An unreadable shard keeps its item (the id — the ack key — came
            # from the listing; only the author is cosmetic), mirroring the raw
            # fold, which lists the item regardless of shard readability.
            items.append({"id": n[:-3], "author": str(author) if author else None})
        if items:
            feedback[slug] = items

    section = {
        "schema": FORGE_SCHEMA,
        "generated_at": now,
        "complete": complete,
        "responsible": {k: sorted(v) for k, v in sorted(resp.items())},
        "feedback": feedback,
    }
    if not complete:
        log.warn("forge projection incomplete", team=team,
                 responsible=len(resp))
    return section
