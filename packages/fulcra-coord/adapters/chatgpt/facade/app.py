"""Fulcra Coordination write facade — FastAPI service.

WHY THIS EXISTS
---------------
A ChatGPT Custom GPT Action can READ coordination state directly off the Fulcra
HTTP API (a two-call resolve+download against pre-materialized view files). It
*cannot* WRITE a milestone, because a correct write is not a single HTTP call:
it must (1) upload a ``tasks/TASK-*.json`` task file AND (2) rebuild every
materialized view (``index`` + ``views/{active,next,recently-done,
search-index}`` + per-workstream/agent) with optimistic-concurrency + merge.
That reconciliation logic lives in the ``fulcra_coord`` package, not in any
single Fulcra endpoint. On top of that the Fulcra upload primitive is a
two-step presigned flow that a GPT Action can't drive.

This facade closes that gap. It exposes the one write endpoint a GPT Action
*can* call — ``POST /coordination/report`` — and implements it by calling the
SAME ``fulcra_coord`` functions the CLI uses (``schema.make_task`` /
``schema.apply_update`` / ``cli._write_task_and_views``). It does NOT
reimplement task writes or view rebuilds. It also re-exposes the status read
(``GET /coordination/status``) by building the index from the package's loader,
so a GPT has a single base URL for both halves.

IMPORT vs SUBPROCESS (the boundary choice)
------------------------------------------
We call ``fulcra_coord`` **in-process (import)** rather than shelling out to the
``fulcra-coord`` CLI. Rationale:
  * The package is pure-stdlib and importable wherever the facade runs (this
    branch ships it). No extra process spawn per request.
  * ``cli._write_task_and_views`` returns a bool and raises typed exceptions
    (``ConflictError`` / ``NeedsReconcile``) we can map straight onto HTTP
    status codes — far cleaner than parsing CLI stdout/exit codes.
  * The package's ``remote`` layer ALREADY shells out to ``fulcra-api`` for the
    actual Fulcra I/O, so the "facade uses the host's Fulcra CLI credentials"
    boundary is preserved either way. Importing just removes a redundant
    second subprocess hop (facade -> fulcra-coord CLI -> fulcra-api) in favour
    of (facade -> fulcra_coord lib -> fulcra-api).

AUTH MODEL (two distinct legs — do not conflate)
------------------------------------------------
  1. INBOUND (GPT -> facade): a single shared bearer token in
     ``FULCRA_COORD_FACADE_TOKEN``, checked constant-time on every request.
     Missing/wrong -> 401. This is the secret the Custom GPT Action sends.
  2. OUTBOUND (facade -> Fulcra): the facade uses whatever ``fulcra-api``
     credentials are already on the host it runs on (the package's ``remote``
     layer invokes ``fulcra-api file ...``). The facade holds NO Fulcra
     credential itself; it must run somewhere ``fulcra-api`` is logged in.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

# fulcra_coord is the core package (pure stdlib). We call its write+rebuild
# path directly rather than reimplementing it — see module docstring.
from fulcra_coord import cli, remote, schema, views


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FACADE_TOKEN_ENV = "FULCRA_COORD_FACADE_TOKEN"

# A stable tag we stamp on tasks so a later report for the same working session
# finds the SAME task instead of creating a duplicate. The bus has no native
# (agent, session) index, so we encode the session key as a task tag and match
# on it + owner_agent. Kept short and grep-friendly.
SESSION_TAG_PREFIX = "session:"

DEFAULT_WORKSTREAM = "general"

# Test seam: a fulcra-api-shaped command list the package's ``remote`` layer
# will invoke instead of resolving the host ``fulcra-api``. Production leaves
# this None so real host credentials are used; tests set it to a stateful fake
# backend script. Kept module-level (not a request param) so it never leaks
# into the public OpenAPI schema.
_BACKEND_OVERRIDE: Optional[list[str]] = None


def _backend() -> Optional[list[str]]:
    return _BACKEND_OVERRIDE


def _expected_token() -> Optional[str]:
    """Return the configured inbound token, or None if unset.

    Unset is a hard misconfiguration: we refuse all requests (500-style) rather
    than silently running open, so a deploy that forgot the token fails loudly
    instead of exposing an unauthenticated write endpoint.
    """
    tok = os.environ.get(FACADE_TOKEN_ENV, "").strip()
    return tok or None


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

def require_token(authorization: Optional[str] = Header(default=None)) -> None:
    """Constant-time check of the inbound bearer token.

    Accepts ``Authorization: Bearer <token>``. Any mismatch, missing header, or
    unconfigured server token -> 401. We use ``hmac.compare_digest`` so the
    comparison time does not leak how many leading characters matched.
    """
    expected = _expected_token()
    if expected is None:
        # Server has no token configured — fail closed. Do not reveal which
        # side is misconfigured to the caller.
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")

    presented = authorization[len("Bearer "):].strip()
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ReportRequest(BaseModel):
    """Body of POST /coordination/report.

    ``agent_id`` + ``session_key`` together identify the ChatGPT working
    session (there is no platform session id; the GPT mints the key). If
    ``task_id`` is given we update exactly that task; otherwise we find-or-
    create the task for this (agent_id, session_key).
    """

    # Length caps below exist to stop a single milestone from bloating EVERY
    # materialized view: summary/title/next_action are copied verbatim into the
    # task_summary that lands in index.json + active.json + per-agent/workstream
    # views, so an unbounded field is amplified across many remote files.
    agent_id: str = Field(..., min_length=1, max_length=200, description="Coordination participant id, e.g. chatgpt:fulcra-coord:ash")
    session_key: str = Field(..., min_length=1, max_length=200, description="Model-minted session token, stable for the conversation")
    summary: str = Field(..., min_length=1, max_length=4000, description="One-line current status / milestone")
    next_action: Optional[str] = Field(default=None, max_length=2000, description="What happens next")
    title: Optional[str] = Field(default=None, max_length=256, description="Task title (only used when creating)")
    workstream: Optional[str] = Field(default=None, max_length=100, description="Workstream label (only used when creating)")
    # Status is restricted to the values the facade can actually satisfy from a
    # find-or-create flow: ``active`` and ``waiting``. ``done`` is impossible
    # here (it needs evidence + verification_level the facade never supplies),
    # and ``blocked``/``abandoned`` need a reason and/or are illegal transitions
    # out of a freshly-``proposed`` task — both require the ``fulcra-coord`` CLI.
    # Accepting them in the schema only to 400 later misleads the GPT; rejecting
    # at the schema layer (422) is the honest contract.
    status: Optional[Literal["active", "waiting"]] = Field(
        default=None,
        description=(
            "Optional status transition. Only 'active' or 'waiting' are "
            "supported via the facade; 'done'/'block'/'abandon' require the "
            "fulcra-coord CLI (they need evidence/reason)."
        ),
    )
    task_id: Optional[str] = Field(default=None, max_length=200, description="Update this exact task instead of find-or-create")

    @field_validator("summary", "title")
    @classmethod
    def _not_blank(cls, v: Optional[str]) -> Optional[str]:
        """Reject whitespace-only summary/title.

        ``min_length=1`` passes a string of spaces, which would land a visually
        empty milestone in every view. We strip and reject so a blank report is
        a 422 rather than silently materializing an empty-looking task.
        """
        if v is not None and not v.strip():
            raise ValueError("must not be blank or whitespace-only")
        return v


class ReportResponse(BaseModel):
    task_id: str
    status: str
    created: bool
    needs_reconcile: bool = False


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Fulcra Coordination write facade",
    description=(
        "Thin HTTP facade that gives a ChatGPT Custom GPT Action a real "
        "POST /coordination/report by wrapping the fulcra_coord package "
        "(task write + view rebuild + optimistic concurrency)."
    ),
    version="1.0.0",
)


# ---------------------------------------------------------------------------
# Find-or-create
# ---------------------------------------------------------------------------

def _session_tag(session_key: str) -> str:
    return f"{SESSION_TAG_PREFIX}{session_key}"


def _session_task_id(
    agent_id: str,
    session_key: str,
    title: Optional[str] = None,
    dt: Optional[datetime] = None,
) -> str:
    """Derive a DETERMINISTIC task id for a (agent_id, session_key) session.

    WHY (the duplicate-task race this closes): ``schema.make_task_id`` suffixes
    a random ``uuid4`` hash, so two concurrent ``POST /coordination/report`` for
    the SAME new session both find no existing task and both mint *different*
    remote paths — the per-task optimistic-concurrency guard never sees a
    conflict, and BOTH writes succeed, leaving two duplicate tasks for one
    session. By hashing (agent_id, session_key) into the id suffix instead, both
    racers compute the SAME id → the SAME remote path, so the existing
    merge/optimistic-concurrency logic in ``cli._write_task_and_views``
    serializes them (the second writer sees the version changed and merges
    rather than duplicating).

    The id keeps the repo's canonical shape
    ``TASK-<YYYYMMDD>-<slug>-<hex8>`` (matches
    ``^TASK-\\d{8}-[a-z0-9-]+-[0-9a-f]{8}$``). The date is part of the id, so a
    session that spans midnight UTC could in principle derive a second id — an
    acceptable, rare edge versus the random-uuid duplicate it replaces; the
    dominant concurrent-burst case (same second/minute) is fully covered. The
    hex8 suffix is the first 8 hex chars of ``sha256(agent_id + '\\x00' +
    session_key)`` — a NUL separator so ("a","bc") and ("ab","c") never collide.
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    date_part = dt.strftime("%Y%m%d")
    slug = schema._slugify(title or "")[:24].rstrip("-")
    digest = hashlib.sha256(
        agent_id.encode("utf-8") + b"\x00" + session_key.encode("utf-8")
    ).hexdigest()[:8]
    return f"TASK-{date_part}-{slug}-{digest}"


def _find_task_for_session(
    agent_id: str,
    session_key: str,
    backend: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    """Find the existing task owned by agent_id carrying this session tag.

    PERF (1 + at most 1 spawns, was N+3): the match needs only ``owner_agent``
    and ``tags`` — both carried by the summaries aggregate — so we resolve the
    task id from ONE ``views/summaries.json`` download instead of the old
    ``cli._load_all_tasks`` (index + search-index + next + one body fetch per
    task on the bus, ~480 subprocess spawns at current bus size). Only the
    MATCHED task's full body is then fetched, because the caller mutates and
    re-writes the body (events/history/source blocks a summary doesn't carry).

    A failed body fetch for a matched id returns None → the caller creates.
    That matches the old degraded behavior (a body whose individual fetch
    failed was dropped from ``_load_all_tasks``' merged set too), and the
    deterministic ``_session_task_id`` keeps the create from forking a second
    remote path for the session.

    ``_load_task_summaries`` keeps the older-bus fallback (no aggregate yet →
    full load) and the stale-view guard, so correctness degrades exactly like
    every other summaries-reading surface.
    """
    want = _session_tag(session_key)
    summaries = cli._load_task_summaries(backend=backend)
    for s in summaries:
        if s.get("owner_agent") != agent_id:
            continue
        if want in (s.get("tags") or []):
            return cli._load_task(s.get("id"), backend=backend)
    return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness probe. Intentionally unauthenticated and Fulcra-free."""
    return {"status": "ok"}


@app.post(
    "/coordination/report",
    response_model=ReportResponse,
    dependencies=[Depends(require_token)],
)
def report(req: ReportRequest) -> ReportResponse:
    """Create-or-update a coordination task for a ChatGPT session.

    Flow:
      * task_id given  -> load+update that task.
      * else           -> find-or-create by (agent_id, session_key).
    In both cases the durable write goes through ``cli._write_task_and_views``
    so views are rebuilt with optimistic concurrency — we do not touch Fulcra
    directly here.
    """
    backend = _backend()
    created = False

    # 1. Resolve the target task.
    if req.task_id:
        task = cli._load_task(req.task_id, backend=backend)
        if task is None:
            raise HTTPException(status_code=404, detail=f"Task not found: {req.task_id}")
    else:
        task = _find_task_for_session(req.agent_id, req.session_key, backend=backend)

    # 2. Create if no existing task.
    if task is None:
        title = req.title or req.summary[:60]
        workstream = req.workstream or DEFAULT_WORKSTREAM
        # Deterministic id keyed on (agent_id, session_key) so two concurrent
        # creates for the same session collapse to one task path (see C2 /
        # _session_task_id) instead of racing into duplicates.
        session_task_id = _session_task_id(req.agent_id, req.session_key, title)
        try:
            task = schema.make_task(
                title=title,
                workstream=workstream,
                agent=req.agent_id,
                summary=req.summary,
                next_action=req.next_action or "",
                task_id=session_task_id,
            )
        except schema.SchemaError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # Stamp the session tag so the next report for this session finds it.
        tags = set(task.get("tags", []))
        tags.add(_session_tag(req.session_key))
        task["tags"] = sorted(tags)
        created = True

        # A brand-new task is born "proposed"; a report almost always means
        # work is in flight, so default it active unless the caller asked for
        # a specific status.
        target_status = req.status or "active"
        try:
            task = schema.apply_transition(
                task,
                target_status,
                by=req.agent_id,
                summary=req.summary,
                next_action=req.next_action,
            )
        except (schema.TransitionError, schema.SchemaError) as e:
            raise HTTPException(status_code=400, detail=str(e))
    else:
        # 3. Update an existing task — preserve the session tag if missing
        # (e.g. task supplied by explicit task_id).
        if not req.task_id:
            tags = set(task.get("tags", []))
            if _session_tag(req.session_key) not in tags:
                tags.add(_session_tag(req.session_key))
                task["tags"] = sorted(tags)

        if req.status:
            try:
                task = schema.apply_transition(
                    task,
                    req.status,
                    by=req.agent_id,
                    summary=req.summary,
                    next_action=req.next_action,
                )
            except (schema.TransitionError, schema.SchemaError) as e:
                raise HTTPException(status_code=400, detail=str(e))
        else:
            task = schema.apply_update(
                task,
                by=req.agent_id,
                summary=req.summary,
                next_action=req.next_action,
            )

    # 4. Durable write (task upload + view rebuild) via the package.
    needs_reconcile = False
    try:
        ok = cli._write_task_and_views(task, backend=backend, command="report")
    except schema.ConflictError as e:
        # Concurrent remote change the merge couldn't reconcile.
        raise HTTPException(status_code=409, detail=str(e))
    except schema.NeedsReconcile:
        # Task landed; some views failed and need a reconcile pass. The write
        # is durable enough to report success, but flag it.
        ok = True
        needs_reconcile = True

    if not ok:
        # Remote upload failed outright (e.g. host fulcra-api not authed).
        raise HTTPException(
            status_code=502,
            detail=(
                f"Task {task['id']} could not be written to Fulcra. The facade "
                "host's fulcra-api may not be authenticated."
            ),
        )

    return ReportResponse(
        task_id=task["id"],
        status=task["status"],
        created=created,
        needs_reconcile=needs_reconcile,
    )


@app.get(
    "/coordination/status",
    dependencies=[Depends(require_token)],
)
def status(
    agent_id: Optional[str] = Query(default=None),
    workstream: Optional[str] = Query(default=None),
) -> dict[str, Any]:
    """Return the active coordination view, optionally filtered.

    Reuses the package's summaries loader + index builder (the same data the
    CLI ``status --format json`` produces — cmd_status reads summaries too),
    so the read path here is consistent with what the GPT would otherwise
    download from pre-built view files.

    PERF (1 spawn, was N+3): everything this endpoint reads rides the
    summaries aggregate — the workstream/owner_agent filters match summary
    fields, and ``views.build_index`` is summary-complete by the
    build_all_views(summaries) == build_all_views(bodies) invariant
    (TestBuildAllViewsEquivalence; task_summary is idempotent). The old
    ``cli._load_all_tasks`` paid index + search-index + next + one body fetch
    per task (~480 spawns at current bus size) per /status poll.
    """
    backend = _backend()

    # Distinguish a real Fulcra outage from a genuinely-empty bus BEFORE
    # loading. ``cli._load_task_summaries`` swallows remote failures (degrades
    # toward cache/stale data), so without this probe an unreachable Fulcra
    # would masquerade as a successful 200 + empty index — the GPT would tell
    # the user "no in-flight work" when in fact we just couldn't see it.
    # ``probe_reachable`` keys off the backend process exiting cleanly, so a
    # reachable-but-empty bus still passes and yields a legitimate empty 200.
    if not remote.probe_reachable(backend=backend):
        raise HTTPException(
            status_code=503,
            detail=(
                "Coordination backend unreachable — the facade host's "
                "fulcra-api may be unauthenticated or Fulcra is unavailable. "
                "This is NOT an empty coordination state; do not report 'no work'."
            ),
        )

    all_tasks = cli._load_task_summaries(backend=backend)
    if workstream:
        all_tasks = [t for t in all_tasks if t.get("workstream") == workstream]
    if agent_id:
        all_tasks = [t for t in all_tasks if t.get("owner_agent") == agent_id]
    return views.build_index(all_tasks)
