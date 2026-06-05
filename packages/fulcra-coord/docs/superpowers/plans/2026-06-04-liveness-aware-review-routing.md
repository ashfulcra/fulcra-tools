# Liveness-Aware Reviewer Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route PR-review directives (and, via a reusable primitive, any opted-in directive) to a reviewer that presence says is actually live-or-idle, self-heal when an assignee goes dark, and escalate to the human when nobody qualifies — so reviews never silently park on a dead agent.

**Architecture:** A pure resolver (`views.resolve_live_recipient`) recomputes effective liveness from each candidate's bus-global `last_seen` + a wall-clock grace window and returns the best live/idle candidate by `(tier, preference)`. `request-review` builds a preference-ordered pool (canonical reviewer seed + `capability:review` agents), resolves it, and either `tell`s the winner a `kind:review`-tagged directive (appending a `routed` routing event + syncing `assignee`) or escalates via `block --on-user`. A reconcile sweep re-derives the current route from the task's own routing events and re-routes never-acted reviews whose assignee fell below floor, freezing accepted-then-stalled ones — guarded by a stale-observation re-read so two machines racing from the same snapshot converge to one reroute.

**Tech Stack:** stdlib-only Python; unittest+pytest; Fulcra Files bus; presence/liveness; reconcile sweep; launchd/cron.

---

## File Structure

| File | Created/Modified | Single responsibility |
| --- | --- | --- |
| `fulcra_coord/views.py` | modified | Add `resolve_live_recipient(candidates, presence, *, floor, now, exclude)` (pure resolver, effective-liveness-from-`last_seen` + `PRESENCE_GRACE_SECONDS`) and the `_presence_grace_seconds()` env reader, beside `presence_liveness`. No I/O. |
| `fulcra_coord/routing.py` | **created** | Routing-event vocabulary + pure derivation helpers: `make_route_event(...)`, `current_route(task)`, `route_attempt_count(task)`, `tried_agents(task)`, `latest_route_event(task)`, plus the `kind:review` marker constant + `is_review_directive(task)`. No I/O — pure functions over a task dict's event log. |
| `fulcra_coord/cli.py` | modified | `cmd_connect` capability capture (`--can-review`/`--role`); `cmd_request_review` (build pool, resolve, hit→tell+routed event+assignee / miss→escalate / `--dry-run`); `_review_pool(...)`, `_canonical_reviewer(...)`, `_append_route_event_and_assignee(...)`, `_escalate_review_to_human(...)` helpers; the reconcile re-route sweep `_sweep_review_routes(...)` called from `cmd_reconcile`; `cmd_tell` `--route-capability`/`--floor` general helper. |
| `fulcra_coord/schema.py` | modified | `make_presence` carries an optional `capabilities: [...]` field; `REVIEW_TAG = "kind:review"` constant + helper to add it as an `extra` build_tags tag (it is a membership marker like `needs:human`, NOT the `kind` field — `"review"` is not in `VALID_KINDS`). |
| `fulcra_coord/entry.py` | modified | Register `request-review` subparser + `COMMAND_MAP` entry; add `connect --can-review/--role`; add `tell --route-capability/--floor`. |
| `fulcra_coord/__init__.py` | modified | Version bump `0.6.0` → `0.7.0`. |
| `packages/fulcra-coord/CHANGELOG.md` | modified | `## [0.7.0] — Liveness-Aware Reviewer Routing` entry (why + what). |
| `tests/test_fulcra_coord.py` | modified | All new tests (stdlib unittest, `backend=["false"]`, patch `remote.download_json`/`upload_json`/`list_files`). |

**Machine-agnostic invariant (load-bearing, applies to every task below):** every routing input — presence/liveness, `capabilities`, routing events, the candidate pool — lives on the shared Files bus. No helper assumes the reviewer, the author, or the sweeper are co-located. Every time gate uses a **wall-clock duration** compared against bus-global timestamps via `views._parse_dt` (parsed, never lexical), never a per-machine listener-interval count. The sweep is written so whichever machine runs `reconcile` first wins and the others converge.

**`kind:review` marker decision (resolved ambiguity, applies to Tasks 3–6):** `"review"` is NOT a member of `schema.VALID_KINDS` (`{"ops","feature","bug","research","infra","config","comms","other"}`), so a review directive CANNOT set `kind="review"` on the task — `make_task` would raise `SchemaError`. Instead the directive is an ordinary `kind:ops` task carrying an **extra** tag `kind:review`, exactly the way `block --on-user` carries `needs:human` as a non-standard membership marker. The sweep and `request-review` detect reviews by **explicit tag membership** (`"kind:review" in task.get("tags", [])` via `routing.is_review_directive`), never via `schema._extract_kind_from_tags` (which returns the lexically-first `kind:` tag — `kind:ops` sorts before `kind:review`, so the task's nominal kind stays coherent). `_repair_merged_tags` preserves non-standard tags across merges, so the marker survives optimistic-concurrency merges just like `needs:human`.

---

## Grounded signatures (verbatim from the worktree at `origin/main` 9350bb9)

These are the EXACT existing signatures every task reuses — do not reinvent them:

- `views.presence_liveness(last_seen: str, now: Optional[datetime] = None, stale_hours: Optional[float] = None) -> str` — bands: `age < threshold*0.5` → `"live"`; `age < threshold` → `"idle"`; else `"stale"`. `threshold = _stale_hours()` (env `FULCRA_COORD_STALE_HOURS`, default `2.0` hours).
- `views._stale_hours(stale_hours: Optional[float] = None) -> float` — explicit arg > env `FULCRA_COORD_STALE_HOURS` > `2.0`.
- `views._age_hours(updated_at: str, now: datetime) -> float` — missing/unparseable → `float("inf")`.
- `views._parse_dt(iso: str) -> Optional[datetime]` — tz-aware UTC or None; naive coerced to UTC. **All datetime compares go through this — never lexical string compares** (timestamps are fixed-width microseconds `...isoformat(timespec="microseconds").replace("+00:00","Z")`, but a `.`-vs-`Z` lexical compare is still unsound — BUG 1/7/8).
- `views._now() -> datetime` — `datetime.now(timezone.utc)`.
- `views.build_presence(records, now=None, updated_at=None)` — per-agent entry = `dict(rec)` + `entry["liveness"] = presence_liveness(rec.get("last_seen",""), now)`. Presence record (per agent) shape: `{schema, agent, workstreams: [...], summary, last_seen, session}` (+ `liveness` added by build_presence). **`capabilities: [...]` is the new key (Task 2).**
- `schema.make_presence(agent, *, workstreams=None, summary="", last_seen=None, session=None) -> dict` — `last_seen` defaults to now ISO-Z. **Add `capabilities: Optional[list[str]] = None` (Task 2).**
- `schema.make_task(*, title, workstream, agent, kind="ops", priority="P2", surface=None, owner_agent=None, assignee=None, ...) -> dict` — directive = `owner_agent=caller`, `assignee=target`. Events list seeded with one `created` event `{at, type, by, summary, evidence}`.
- `schema.build_tags(*, status, workstream, agent, kind, priority, extra: Optional[list[str]] = None) -> list[str]` — returns `sorted(set(...))`; `extra` is appended (this is where `kind:review` and `needs:human` live).
- `schema._extract_kind_from_tags(tags) -> str` — first `kind:`-prefixed tag's suffix, else `"ops"`.
- `schema.apply_transition(task, new_status, *, by, summary=None, ..., dt=None)` and `schema.apply_update(task, *, by, summary=None, ...)` and `schema.apply_event(task, event_type, *, by, ..., dt=None)` (`NON_STATUS_EVENTS = {"inbox_ack"}` — routing events are appended directly, NOT through apply_event, since they are neither status transitions nor in NON_STATUS_EVENTS). `_now` timestamp format = `dt.isoformat(timespec="microseconds").replace("+00:00","Z")`.
- `schema.task_summary(task)` — flat dict; carries `assignee`, `tags`, `updated_at`, `acked_by` (from `inbox_ack` events). **Routing events are NOT surfaced on the summary** — the sweep reads full task bodies (it already calls `_load_all_tasks`), so no summary change is needed.
- `cli._write_task_and_views(task, *, backend=None, command="write", lifecycle=None) -> bool` — optimistic-concurrency write: pre-stat, `_try_merge` on remote change, upload task + fan-out all views. `_try_merge` unions events by `at` (dedup) — so two `routed`/`rerouted` events with distinct `at` both survive a merge.
- `cli._load_task(task_id, backend=None)`, `cli._load_all_tasks(backend=None)`, `cli._load_task_summaries(backend=None)`, `cli._print_json(data)`, `cli._info/_warn/_err`.
- `cli._upsert_presence_aggregate(record, backend=None)` and `cli._write_presence(record, backend=None) -> bool` — `cmd_connect` builds `record = schema.make_presence(me, workstreams=..., summary=..., session=...)` then `_write_presence(record)`. **Capabilities thread through here (Task 2).**
- `cli.cmd_block` `--on-user` path: `apply_transition(task,"blocked",by=agent,blocked_on=ask)`, then `task["assignee"]=identity.resolve_human()`, add `needs:human` tag, `_write_task_and_views(command="block")`. **This is the escalation primitive reused by Tasks 4 + 5.**
- `cli.cmd_reconcile(args, backend=None)` — loads `_load_all_tasks`, rebuilds + uploads views, calls `_reconcile_presence`. **The sweep hooks in here (Task 5), after the load and before/after view rebuild, best-effort.**
- `identity.resolve_agent(explicit=None) -> str`, `identity.resolve_human() -> str`.
- `remote.presence_view_path() -> str`, `remote.presence_remote_path(agent_slug) -> str`, `remote.list_files(prefix, backend=None)`, `remote.download_json(path, backend=None)`, `remote.upload_json(data, path, backend=None) -> bool`, `remote.remote_root() -> str`.
- `entry.build_parser()` subparser pattern: `sp = sub.add_parser(name, help=...)`, `sp.add_argument(...)`; `COMMAND_MAP[name] = _cli.cmd_x`.
- **Test conventions:** stdlib `unittest`, run with `uv run --extra dev python -m pytest -q`. Command tests use `backend=["false"]` (a backend whose `false` exits 1 → no remote), `types.SimpleNamespace(**kwargs)` for args, `XDG_CACHE_HOME=tmp` in setUp. Pure-function tests pass injected `now`/`presence`/`tasks` directly (no patching). Remote-touching tests patch `fulcra_coord.cli.remote.download_json`/`upload_json`/`list_files`. **After any test run, `cd /private/tmp/fc-routing && git checkout -- uv.lock packages/fulcra-coord/uv.lock` if it churns — never commit uv.lock.**

---

### Task 1 — `views.resolve_live_recipient` (pure resolver)

The foundation: a deterministic, I/O-free resolver that recomputes effective liveness INSIDE the function from each candidate's `last_seen` (never trusting the aggregate's stored `liveness`), applies the wall-clock `PRESENCE_GRACE_SECONDS` grace, and returns the `(effective_tier, preference_index)`-minimizing candidate, or `None`. Most tests live here. Do this first — everything else consumes it.

- [ ] **Write the env reader + a failing test for it.** Add to `tests/test_fulcra_coord.py` a `TestPresenceGraceSeconds(unittest.TestCase)`:
  ```python
  def test_presence_grace_seconds_default(self):
      from fulcra_coord import views
      os.environ.pop("FULCRA_COORD_PRESENCE_GRACE_SECONDS", None)
      self.assertEqual(views._presence_grace_seconds(), 1200.0)

  def test_presence_grace_seconds_env_override(self):
      from fulcra_coord import views
      os.environ["FULCRA_COORD_PRESENCE_GRACE_SECONDS"] = "300"
      try:
          self.assertEqual(views._presence_grace_seconds(), 300.0)
      finally:
          os.environ.pop("FULCRA_COORD_PRESENCE_GRACE_SECONDS", None)

  def test_presence_grace_seconds_bad_value_falls_back(self):
      from fulcra_coord import views
      os.environ["FULCRA_COORD_PRESENCE_GRACE_SECONDS"] = "not-a-number"
      try:
          self.assertEqual(views._presence_grace_seconds(), 1200.0)
      finally:
          os.environ.pop("FULCRA_COORD_PRESENCE_GRACE_SECONDS", None)
  ```
- [ ] **Run, expect FAIL** (`AttributeError: _presence_grace_seconds`):
  `cd /private/tmp/fc-routing/packages/fulcra-coord && uv run --extra dev python -m pytest tests/test_fulcra_coord.py::TestPresenceGraceSeconds -v`
- [ ] **Implement `_presence_grace_seconds`** in `views.py` (beside `_stale_hours`, mirroring it exactly):
  ```python
  # Wall-clock grace (seconds) the resolver tolerates BEYOND the idle->stale
  # cutoff before treating an agent as below routing floor. A single missed
  # heartbeat or a laptop sleep/wake must not drop a reviewer. Expressed as an
  # ABSOLUTE duration (not a count of listener intervals) because listener
  # cadence differs per machine, while presence last_seen is bus-global, so the
  # grace evaluates identically on every machine (machine-agnostic invariant).
  PRESENCE_GRACE_SECONDS_DEFAULT = 1200.0  # 20 min

  def _presence_grace_seconds(grace: Optional[float] = None) -> float:
      """Resolve the routing presence grace (seconds): explicit arg > env > default."""
      if grace is not None:
          return grace
      raw = os.environ.get("FULCRA_COORD_PRESENCE_GRACE_SECONDS", "").strip()
      if raw:
          try:
              return float(raw)
          except ValueError:
              pass
      return float(PRESENCE_GRACE_SECONDS_DEFAULT)
  ```
- [ ] **Run, expect PASS** (same command).
- [ ] **Commit:** `cd /private/tmp/fc-routing && git add packages/fulcra-coord/fulcra_coord/views.py packages/fulcra-coord/tests/test_fulcra_coord.py && git commit -m "Add FULCRA_COORD_PRESENCE_GRACE_SECONDS reader (routing grace window)" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"`

- [ ] **Write failing tests for the resolver itself.** Add `TestResolveLiveRecipient(unittest.TestCase)`. Build presence records as the build_presence aggregate emits them (dicts with `agent`, `last_seen`, optionally `capabilities`/`liveness`). Use a fixed `NOW = datetime(2026,6,4,12,0,0,tzinfo=timezone.utc)` and `_stale_hours` default 2h (so live `<1h`, idle `<2h`, grace extends below-floor to `2h + 1200s`):
  ```python
  from fulcra_coord import views
  from datetime import datetime, timedelta, timezone

  NOW = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)

  def _rec(agent, minutes_ago, liveness="stale", caps=None):
      # liveness is DELIBERATELY wrong/stale here to prove the resolver
      # recomputes from last_seen and ignores the stored field.
      ls = (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="microseconds").replace("+00:00", "Z")
      r = {"agent": agent, "last_seen": ls, "liveness": liveness}
      if caps is not None:
          r["capabilities"] = caps
      return r

  def test_effective_liveness_recomputed_from_last_seen_not_stored_tier(self):
      # Aggregate says 'stale', but last_seen is 90 min old -> within stale_cutoff
      # (2h) so effectively idle -> qualifies at floor=idle.
      presence = [_rec("a", 90, liveness="stale")]
      self.assertEqual(views.resolve_live_recipient(["a"], presence, floor="idle", now=NOW), "a")

  def test_grace_window_keeps_just_stale_agent_eligible(self):
      # 2h10m old: past the 2h idle->stale cutoff but within 2h + 1200s grace -> idle for routing.
      presence = [_rec("a", 130)]
      self.assertEqual(views.resolve_live_recipient(["a"], presence, floor="idle", now=NOW), "a")

  def test_beyond_grace_is_below_floor_returns_none(self):
      # 2h21m old: past 2h + 1200s (20m) grace -> below floor.
      presence = [_rec("a", 141)]
      self.assertIsNone(views.resolve_live_recipient(["a"], presence, floor="idle", now=NOW))

  def test_tier_dominates_preference_live_noncanonical_beats_idle_canonical(self):
      # canonical 'canon' listed first but idle (90m); 'other' second but live (10m).
      presence = [_rec("canon", 90), _rec("other", 10)]
      self.assertEqual(views.resolve_live_recipient(["canon", "other"], presence, floor="idle", now=NOW), "other")

  def test_preference_breaks_ties_within_same_tier(self):
      presence = [_rec("first", 10), _rec("second", 5)]  # both live
      self.assertEqual(views.resolve_live_recipient(["first", "second"], presence, floor="idle", now=NOW), "first")

  def test_floor_live_excludes_idle(self):
      presence = [_rec("a", 90)]  # idle
      self.assertIsNone(views.resolve_live_recipient(["a"], presence, floor="live", now=NOW))
      self.assertEqual(views.resolve_live_recipient(["a"], presence, floor="idle", now=NOW), "a")

  def test_exclude_skips_tried(self):
      presence = [_rec("a", 10), _rec("b", 10)]
      self.assertEqual(views.resolve_live_recipient(["a", "b"], presence, floor="idle", now=NOW, exclude=("a",)), "b")

  def test_empty_candidates_returns_none(self):
      self.assertIsNone(views.resolve_live_recipient([], [], floor="idle", now=NOW))

  def test_all_below_floor_returns_none(self):
      presence = [_rec("a", 200), _rec("b", 300)]
      self.assertIsNone(views.resolve_live_recipient(["a", "b"], presence, floor="idle", now=NOW))

  def test_candidate_missing_from_presence_is_below_floor(self):
      # canonical seed that never connected: no presence record -> below floor, skipped.
      presence = [_rec("b", 10)]
      self.assertEqual(views.resolve_live_recipient(["a", "b"], presence, floor="idle", now=NOW), "b")
  ```
- [ ] **Run, expect FAIL:** `uv run --extra dev python -m pytest tests/test_fulcra_coord.py::TestResolveLiveRecipient -v`
- [ ] **Implement `resolve_live_recipient`** in `views.py` (immediately after `presence_liveness`):
  ```python
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
  ```
- [ ] **Run, expect PASS.**
- [ ] **Commit:** `cd /private/tmp/fc-routing && git add packages/fulcra-coord/fulcra_coord/views.py packages/fulcra-coord/tests/test_fulcra_coord.py && git commit -m "Add views.resolve_live_recipient: pure liveness-aware reviewer resolver" -m "Recomputes effective liveness from bus-global last_seen + a wall-clock grace window (never trusts the stored aggregate tier); ranks by (tier, preference) so a live non-canonical reviewer beats an idle canonical one; returns None when nobody clears the floor so the caller escalates rather than parking on a dead agent." -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"`

---

### Task 2 — Capability declaration on `connect`

Record declared capabilities on the presence record so the pool builder (Task 3) can find review-capable agents. Backward-compatible: undeclared agents carry `capabilities: []`.

- [ ] **Write failing tests.** Add `TestPresenceCapabilities(unittest.TestCase)`:
  ```python
  from fulcra_coord import schema, views

  def test_make_presence_default_capabilities_empty(self):
      rec = schema.make_presence("claude-code:h:r")
      self.assertEqual(rec["capabilities"], [])

  def test_make_presence_records_capabilities_sorted_unique(self):
      rec = schema.make_presence("a", capabilities=["review", "review", "deploy"])
      self.assertEqual(rec["capabilities"], ["deploy", "review"])

  def test_build_presence_carries_capabilities_through(self):
      rec = schema.make_presence("a", capabilities=["review"])
      agg = views.build_presence([rec])
      self.assertEqual(agg["agents"][0]["capabilities"], ["review"])
  ```
  And a command-level test in the CLI test class (`backend=["false"]`, patching `_write_presence` to capture the record):
  ```python
  def test_connect_can_review_sets_review_capability(self):
      from fulcra_coord.cli import cmd_connect
      captured = {}
      def fake_write(record, backend=None):
          captured["rec"] = record
          return True
      with patch("fulcra_coord.cli._write_presence", side_effect=fake_write), \
           patch("fulcra_coord.cli._derive_workstreams_from_open_tasks", return_value=[]):
          args = self._args(agent="claude-code:h:r", workstream=None, summary="",
                            format="json", can_review=True, role=None)
          cmd_connect(args, backend=["false"])
      self.assertIn("review", captured["rec"]["capabilities"])

  def test_connect_role_flag_adds_named_capabilities(self):
      from fulcra_coord.cli import cmd_connect
      captured = {}
      with patch("fulcra_coord.cli._write_presence",
                 side_effect=lambda record, backend=None: captured.update(rec=record) or True), \
           patch("fulcra_coord.cli._derive_workstreams_from_open_tasks", return_value=[]):
          args = self._args(agent="a", workstream=None, summary="", format="json",
                            can_review=False, role=["review", "deploy"])
          cmd_connect(args, backend=["false"])
      self.assertEqual(sorted(captured["rec"]["capabilities"]), ["deploy", "review"])
  ```
- [ ] **Run, expect FAIL:** `uv run --extra dev python -m pytest tests/test_fulcra_coord.py::TestPresenceCapabilities tests/test_fulcra_coord.py -k "connect_can_review or connect_role" -v`
- [ ] **Implement.** In `schema.make_presence`, add `capabilities: Optional[list[str]] = None` param and a `_normalize_capabilities` helper (mirror `_normalize_workstreams`: sorted, unique, trimmed, non-empty), and include `"capabilities": _normalize_capabilities(capabilities)` in the returned dict. `build_presence` already does `entry = dict(rec)` so it carries `capabilities` through automatically — no change needed there beyond the test asserting it.
- [ ] **Wire `cmd_connect`** (`cli.py`): after resolving workstreams, gather capabilities:
  ```python
  # Declared capabilities (Task 2): --can-review is sugar for --role review.
  # These drive liveness-aware reviewer routing's candidate pool. Undeclared
  # agents stay [] (backward compatible).
  roles = list(getattr(args, "role", None) or [])
  if getattr(args, "can_review", False):
      roles.append("review")
  record = schema.make_presence(me, workstreams=workstreams, summary=summary,
                                capabilities=roles or None,
                                session=os.environ.get("FULCRA_COORD_SESSION") or None)
  ```
- [ ] **Register flags** in `entry.py` `connect` subparser:
  ```python
  sp.add_argument("--can-review", dest="can_review", action="store_true",
                  help="Declare this agent can review PRs (sugar for --role review)")
  sp.add_argument("--role", action="append", default=None, metavar="ROLE",
                  help="Declare a capability/role (repeatable), e.g. --role review")
  ```
- [ ] **Run, expect PASS.**
- [ ] **Commit:** `cd /private/tmp/fc-routing && git add -A packages/fulcra-coord/fulcra_coord packages/fulcra-coord/tests && git commit -m "connect --can-review/--role: record declared capabilities on presence" -m "Adds a capabilities:[...] field to the presence record (default [], backward compatible) so liveness-aware routing can build a review-capable candidate pool. --can-review is sugar for --role review; build_presence carries it through into the aggregate." -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"`

---

### Task 3 — Routing events module + deterministic current-route derivation

A new pure module `routing.py` holds the routing-event vocabulary and the derivation helpers the command and sweep both consume. Deterministic across machines (latest-by-`at`, tie-break `route_id`). **Field names defined here are the single source of truth for Tasks 4–6.**

- [ ] **Write failing tests.** Add `TestRoutingEvents(unittest.TestCase)`:
  ```python
  from fulcra_coord import routing, schema
  from datetime import datetime, timezone

  def _task_with_events(events, assignee=None, tags=None):
      return {"id": "TASK-20260604-x-00000000", "assignee": assignee,
              "tags": tags or [], "events": events}

  def test_make_route_event_shape(self):
      ev = routing.make_route_event(kind="routed", to="a", by="b", attempt=1,
                                    reason="live", candidate_snapshot=[{"agent": "a", "tier": "live"}],
                                    observed_updated_at="2026-06-04T12:00:00.000000Z",
                                    at="2026-06-04T12:00:00.000000Z", route_id="rid-1")
      self.assertEqual(ev["type"], "routed")
      self.assertEqual({"at","type","to","by","attempt","reason","candidate_snapshot",
                        "observed_updated_at","route_id"}, set(ev))

  def test_is_review_directive_by_tag(self):
      self.assertTrue(routing.is_review_directive(_task_with_events([], tags=["kind:review"])))
      self.assertFalse(routing.is_review_directive(_task_with_events([], tags=["kind:ops"])))

  def test_current_route_latest_by_at(self):
      e1 = routing.make_route_event(kind="routed", to="a", by="s", attempt=1, reason="x",
                                    candidate_snapshot=[], observed_updated_at="t",
                                    at="2026-06-04T12:00:00.000000Z", route_id="r1")
      e2 = routing.make_route_event(kind="rerouted", to="b", by="s", attempt=2, reason="y",
                                    candidate_snapshot=[], observed_updated_at="t",
                                    at="2026-06-04T12:05:00.000000Z", route_id="r2")
      task = _task_with_events([e1, e2])
      self.assertEqual(routing.current_route(task)["to"], "b")

  def test_current_route_tie_break_by_route_id(self):
      same = "2026-06-04T12:00:00.000000Z"
      e1 = routing.make_route_event(kind="routed", to="a", by="s", attempt=1, reason="x",
                                    candidate_snapshot=[], observed_updated_at="t", at=same, route_id="r-aaa")
      e2 = routing.make_route_event(kind="rerouted", to="b", by="s", attempt=2, reason="y",
                                    candidate_snapshot=[], observed_updated_at="t", at=same, route_id="r-bbb")
      # higher route_id wins the tie deterministically (stable across machines).
      self.assertEqual(routing.current_route(_task_with_events([e1, e2]))["route_id"], "r-bbb")

  def test_route_attempt_count_and_tried(self):
      e1 = routing.make_route_event(kind="routed", to="a", by="s", attempt=1, reason="x",
                                    candidate_snapshot=[], observed_updated_at="t",
                                    at="2026-06-04T12:00:00.000000Z", route_id="r1")
      e2 = routing.make_route_event(kind="rerouted", to="b", by="s", attempt=2, reason="y",
                                    candidate_snapshot=[], observed_updated_at="t",
                                    at="2026-06-04T12:05:00.000000Z", route_id="r2")
      task = _task_with_events([e1, e2])
      self.assertEqual(routing.route_attempt_count(task), 2)
      self.assertEqual(routing.tried_agents(task), {"a", "b"})

  def test_current_route_none_when_no_route_events(self):
      self.assertIsNone(routing.current_route(_task_with_events([{"at": "t", "type": "created", "by": "x"}])))
  ```
- [ ] **Run, expect FAIL** (`ModuleNotFoundError: fulcra_coord.routing`): `uv run --extra dev python -m pytest tests/test_fulcra_coord.py::TestRoutingEvents -v`
- [ ] **Implement `fulcra_coord/routing.py`:**
  ```python
  """Routing state for liveness-aware reviewer routing — pure, no I/O.

  Routing state lives ENTIRELY in a task's event log (no new schema fields).
  Each route/reroute appends a routing event; the CURRENT route is DERIVED
  deterministically from those events (latest by parsed `at`, ties broken by
  route_id) so every machine reading the same task agrees on who it is routed
  to — the machine-agnostic invariant. The directive carries an EXTRA tag
  `kind:review` (REVIEW_TAG) as a membership marker, exactly like needs:human;
  it is NOT the task's `kind` field ("review" is not a valid kind).
  """
  from __future__ import annotations

  import uuid
  from datetime import datetime, timezone
  from typing import Any, Optional

  from .views import _parse_dt  # the ONE parsed-datetime helper (never lexical)

  REVIEW_TAG = "kind:review"
  ROUTE_EVENT_TYPES = ("routed", "rerouted")

  def new_route_id() -> str:
      """A UUID minting the deterministic identity of one routing decision."""
      return uuid.uuid4().hex

  def make_route_event(*, kind: str, to: str, by: str, attempt: int, reason: str,
                       candidate_snapshot: list[dict[str, Any]],
                       observed_updated_at: str, at: str,
                       route_id: Optional[str] = None) -> dict[str, Any]:
      """Build one routing event (kind in ROUTE_EVENT_TYPES). `route_id` defaults
      to a fresh UUID. `candidate_snapshot` is the ranked pool + tiers at decision
      time (debuggability: 'why did it pick X'). `observed_updated_at` is the
      task.updated_at the decider saw — the multi-sweeper convergence anchor (§6)."""
      if kind not in ROUTE_EVENT_TYPES:
          raise ValueError(f"route event kind must be one of {ROUTE_EVENT_TYPES}")
      return {
          "at": at,
          "type": kind,
          "to": to,
          "by": by,
          "attempt": attempt,
          "reason": reason,
          "candidate_snapshot": candidate_snapshot,
          "observed_updated_at": observed_updated_at,
          "route_id": route_id or new_route_id(),
      }

  def route_events(task: dict[str, Any]) -> list[dict[str, Any]]:
      return [e for e in task.get("events", []) if e.get("type") in ROUTE_EVENT_TYPES]

  def latest_route_event(task: dict[str, Any]) -> Optional[dict[str, Any]]:
      """The current routing decision: latest by PARSED `at`, ties broken by
      route_id (stable across machines — Files has no global clock). Parsed
      compare, never lexical (BUG 1/7/8)."""
      evs = route_events(task)
      if not evs:
          return None
      _epoch = datetime.min.replace(tzinfo=timezone.utc)
      return max(evs, key=lambda e: (_parse_dt(e.get("at", "")) or _epoch, e.get("route_id", "")))

  def current_route(task: dict[str, Any]) -> Optional[dict[str, Any]]:
      """Alias for latest_route_event — the task's effective route (whose `to`
      the writer keeps task.assignee in sync with)."""
      return latest_route_event(task)

  def route_attempt_count(task: dict[str, Any]) -> int:
      """How many route/reroute decisions have been made (the cap is checked
      against this)."""
      return len(route_events(task))

  def tried_agents(task: dict[str, Any]) -> set[str]:
      """Every agent a route/reroute has targeted — the exclude set for the next
      resolve (a tried agent stays excluded for this cycle, §6)."""
      return {e.get("to") for e in route_events(task) if e.get("to")}

  def is_review_directive(task: dict[str, Any]) -> bool:
      """True iff this task carries the kind:review membership marker. The sweep
      keys on THIS (explicit tag membership) so it can never reroute an ordinary
      tell/directive — never via _extract_kind_from_tags (kind:ops sorts first)."""
      return REVIEW_TAG in (task.get("tags") or [])
  ```
- [ ] **Run, expect PASS.**
- [ ] **Commit:** `cd /private/tmp/fc-routing && git add packages/fulcra-coord/fulcra_coord/routing.py packages/fulcra-coord/tests/test_fulcra_coord.py && git commit -m "Add routing.py: routing-event vocabulary + deterministic current-route derivation" -m "Routing state lives in the task event log (no schema change). current_route is the latest routed/rerouted event by parsed at, ties broken by route_id so every machine agrees. is_review_directive keys on an explicit kind:review membership tag (like needs:human), never the kind field, since 'review' is not a valid kind." -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"`

---

### Task 4 — `request-review <pr> --repo` command

Builds the preference-ordered pool, resolves a live recipient, and either routes (tell + `routed` event + `assignee` sync + `kind:review` tag) or escalates (`block --on-user`). `--dry-run` prints pool/tiers/excluded/winner/reason and writes nothing.

- [ ] **Write failing tests for the pool builder** (pure-ish, presence injected). Add `TestReviewPool(unittest.TestCase)`:
  ```python
  from fulcra_coord import cli

  def test_canonical_reviewer_for_arc_author(self):
      self.assertEqual(cli._canonical_reviewer("claude-code:ArcBot:something"),
                       "claude-code:ArcBot:Arc-Code-Review")

  def test_canonical_reviewer_for_everyone_else(self):
      self.assertEqual(cli._canonical_reviewer("codex:Mac.localdomain:main"),
                       "codex:Mac.localdomain:main")
      self.assertEqual(cli._canonical_reviewer("openclaw:discord:devops"),
                       "codex:Mac.localdomain:main")

  def test_pool_seeds_canonical_even_when_undeclared(self):
      presence = [{"agent": "x:y:z", "last_seen": "...", "capabilities": ["review"]}]
      pool = cli._review_pool(author="codex:Mac.localdomain:main", presence=presence)
      self.assertEqual(pool[0], "codex:Mac.localdomain:main")  # canonical first
      self.assertIn("x:y:z", pool)

  def test_pool_excludes_non_review_capable_and_devops(self):
      presence = [
          {"agent": "openclaw:discord:devops", "last_seen": "...", "capabilities": []},
          {"agent": "rev:h:r", "last_seen": "...", "capabilities": ["review"]},
      ]
      pool = cli._review_pool(author="codex:Mac.localdomain:main", presence=presence)
      self.assertNotIn("openclaw:discord:devops", pool)  # not auto-seeded, not review-capable
      self.assertIn("rev:h:r", pool)

  def test_pool_no_duplicate_when_canonical_also_declares(self):
      presence = [{"agent": "codex:Mac.localdomain:main", "last_seen": "...", "capabilities": ["review"]}]
      pool = cli._review_pool(author="codex:Mac.localdomain:main", presence=presence)
      self.assertEqual(pool.count("codex:Mac.localdomain:main"), 1)
  ```
- [ ] **Run, expect FAIL.**
- [ ] **Implement `_canonical_reviewer` + `_review_pool`** in `cli.py`:
  ```python
  # Canonical-reviewer identities are bus-global IDENTITIES, not locations — a
  # reviewer may run on any machine (machine-agnostic invariant). Arc sessions
  # route to the Arc reviewer; everyone else to the codex main reviewer.
  ARC_REVIEWER = "claude-code:ArcBot:Arc-Code-Review"
  DEFAULT_REVIEWER = "codex:Mac.localdomain:main"

  def _canonical_reviewer(author: str) -> str:
      """The seeded, preference-first reviewer for an author. Seeded even if it
      never declared --can-review (day-one works before agents update). #devops/
      openclaw is deliberately NOT canonical — it qualifies only if actually
      live/idle AND review-capable."""
      if (author or "").startswith("claude-code:ArcBot:"):
          return ARC_REVIEWER
      return DEFAULT_REVIEWER

  def _review_pool(author: str, presence: list[dict[str, Any]]) -> list[str]:
      """Preference-ordered candidate pool: canonical reviewer first (seeded,
      tie-break only — a live non-canonical reviewer still wins), then every
      review-capable agent in presence order. De-duplicated, canonical kept first."""
      canonical = _canonical_reviewer(author)
      pool = [canonical]
      for rec in presence:
          agent = rec.get("agent")
          if not agent or agent == canonical:
              continue
          if "review" in (rec.get("capabilities") or []):
              pool.append(agent)
      # de-dup preserving first occurrence (canonical stays index 0)
      seen, ordered = set(), []
      for a in pool:
          if a not in seen:
              seen.add(a); ordered.append(a)
      return ordered
  ```
- [ ] **Run, expect PASS.**

- [ ] **Write failing tests for the command.** Add `TestRequestReview(unittest.TestCase)` (CLI-style). Patch the presence download + `_write_task_and_views`/`cmd_block` to capture behaviour:
  ```python
  import types
  from unittest.mock import patch
  from fulcra_coord.cli import cmd_request_review

  def _presence_agg(agents):
      return {"agents": agents}

  def test_dry_run_prints_pool_and_writes_nothing(self):
      now_ls = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00","Z")
      agg = _presence_agg([{"agent": "codex:Mac.localdomain:main", "last_seen": now_ls, "capabilities": ["review"]}])
      with patch("fulcra_coord.cli.remote.download_json", return_value=agg), \
           patch("fulcra_coord.cli._write_task_and_views") as wtv, \
           patch("fulcra_coord.cli.identity.resolve_agent", return_value="codex:Mac.localdomain:main"):
          args = types.SimpleNamespace(pr="42", repo="fulcra-tools", dry_run=True,
                                       candidate_list=None, format="json")
          rc = cmd_request_review(args, backend=["false"])
      self.assertEqual(rc, 0)
      wtv.assert_not_called()  # dry-run writes nothing

  def test_hit_routes_tagged_review_with_routed_event_and_assignee(self):
      now_ls = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00","Z")
      agg = _presence_agg([{"agent": "codex:Mac.localdomain:main", "last_seen": now_ls, "capabilities": ["review"]}])
      captured = {}
      def fake_write(task, backend=None, command="write", lifecycle=None):
          captured["task"] = task
          return True
      with patch("fulcra_coord.cli.remote.download_json", return_value=agg), \
           patch("fulcra_coord.cli._write_task_and_views", side_effect=fake_write), \
           patch("fulcra_coord.cli.identity.resolve_agent", return_value="claude-code:h:r"):
          args = types.SimpleNamespace(pr="42", repo="fulcra-tools", dry_run=False,
                                       candidate_list=None, format="json")
          rc = cmd_request_review(args, backend=["false"])
      t = captured["task"]
      self.assertEqual(rc, 0)
      self.assertEqual(t["assignee"], "codex:Mac.localdomain:main")
      self.assertIn("kind:review", t["tags"])
      routed = [e for e in t["events"] if e["type"] == "routed"]
      self.assertEqual(len(routed), 1)
      self.assertIn("route_id", routed[0])
      self.assertEqual(routed[0]["to"], "codex:Mac.localdomain:main")

  def test_miss_escalates_via_block_on_user(self):
      old_ls = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(timespec="microseconds").replace("+00:00","Z")
      agg = _presence_agg([{"agent": "codex:Mac.localdomain:main", "last_seen": old_ls, "capabilities": ["review"]}])
      escalated = {}
      with patch("fulcra_coord.cli.remote.download_json", return_value=agg), \
           patch("fulcra_coord.cli._escalate_review_to_human",
                 side_effect=lambda **kw: escalated.update(kw) or True), \
           patch("fulcra_coord.cli.identity.resolve_agent", return_value="claude-code:h:r"):
          args = types.SimpleNamespace(pr="42", repo="fulcra-tools", dry_run=False,
                                       candidate_list=None, format="json")
          rc = cmd_request_review(args, backend=["false"])
      self.assertEqual(rc, 0)
      self.assertIn("42", escalated.get("pr", ""))
  ```
- [ ] **Run, expect FAIL.**
- [ ] **Implement `cmd_request_review`, `_append_route_event_and_assignee`, `_escalate_review_to_human`** in `cli.py`:
  ```python
  def _append_route_event_and_assignee(task, *, kind, to, by, attempt, reason,
                                       candidate_snapshot, observed_updated_at,
                                       dt=None):
      """Append a routing event AND sync task.assignee to its `to`, so the event
      log (audit + sweep input) and the assignee (inbox/tell machinery) never
      disagree (§5). Mutates + returns the task copy."""
      import copy
      from . import routing
      task = copy.deepcopy(task)
      at = (dt or datetime.now(timezone.utc)).isoformat(timespec="microseconds").replace("+00:00","Z")
      ev = routing.make_route_event(kind=kind, to=to, by=by, attempt=attempt,
                                    reason=reason, candidate_snapshot=candidate_snapshot,
                                    observed_updated_at=observed_updated_at, at=at)
      task.setdefault("events", []).append(ev)
      task["events"] = task["events"][-schema.MAX_EVENTS_INLINE:]
      task["assignee"] = to
      task["updated_at"] = at
      task["last_touched_by"] = by
      return task

  def _escalate_review_to_human(*, pr, repo, tried, backend=None, existing=None):
      """Escalate a review with no live reviewer to the human via the existing
      block --on-user shape (needs:human -> needs-me + digest + banner).
      Idempotent by caller: the sweep passes `existing` (the review task) to
      update it in place; a fresh request-review miss passes None and creates a
      dedicated escalation task. Best-effort: never raises into
      request-review/reconcile."""
      try:
          human = identity.resolve_human()
          me = identity.resolve_agent(None)
          ask = (f"PR #{pr} in {repo} needs review; no reviewer is live/idle "
                 f"(tried: {', '.join(tried) or 'none'}). Assign a reviewer manually.")
          marker = f"review-escalation:{repo}#{pr}"
          task = existing
          if task is None:
              task = schema.make_task(
                  title=f"PR #{pr} needs a reviewer ({repo})",
                  workstream=repo, agent=me, owner_agent=me, assignee=human,
                  priority="P1")
          # block --on-user shape: transition to blocked, point at the human,
          # carry the needs:human marker + a stable per-PR marker for idempotency.
          task = schema.apply_transition(task, "blocked", by=me, blocked_on=ask)
          task["assignee"] = human
          task["tags"] = sorted(set(task.get("tags", [])) | {"needs:human", marker})
          _write_task_and_views(task, backend=backend, command="block")
          return True
      except Exception as e:
          _warn(f"review escalation failed (non-fatal): {e}")
          return False

  def cmd_request_review(args, backend=None):
      """Route a PR review to a live/idle reviewer, or escalate. Best-effort: a
      routing failure escalates, never crashes."""
      from . import routing
      pr = args.pr; repo = args.repo
      dry_run = getattr(args, "dry_run", False)
      out_format = getattr(args, "format", "table")
      author = identity.resolve_agent(getattr(args, "agent", None))
      try:
          agg = remote.download_json(remote.presence_view_path(), backend=backend)
          presence = (agg or {}).get("agents", []) if agg else []
      except Exception:
          presence = []  # treat as no live candidate -> escalate (error handling §)
      override = getattr(args, "candidate_list", None)
      pool = [a.strip() for a in override.split(",") if a.strip()] if override else \
             _review_pool(author, presence)
      now = datetime.now(timezone.utc)
      snapshot = [{"agent": a,
                   "tier": views._effective_routing_liveness(
                       next((r.get("last_seen","") for r in presence if r.get("agent")==a), ""),
                       now, views._presence_grace_seconds()) or "below-floor"}
                  for a in pool]
      winner = views.resolve_live_recipient(pool, presence, floor="idle", now=now)
      excluded = [s for s in snapshot if s["tier"] == "below-floor"]
      if dry_run:
          report = {"pr": pr, "repo": repo, "pool": pool, "snapshot": snapshot,
                    "excluded": [e["agent"] for e in excluded], "winner": winner,
                    "reason": "live/idle reviewer found" if winner else "no live reviewer — would escalate"}
          if out_format == "json":
              _print_json(report)
          else:
              _info(f"[dry-run] pool={pool} winner={winner}")
          return 0
      if winner is None:
          _escalate_review_to_human(pr=pr, repo=repo,
                                    tried=[s["agent"] for s in snapshot], backend=backend)
          _info(f"PR #{pr}: no reviewer live — escalated to human.")
          return 0
      # HIT: build the directive, tag kind:review, append routed event + assignee.
      title = f"Review PR #{pr} — assume bugs, claim the review before working"
      task = schema.make_task(title=title, workstream=repo, agent=author,
                              owner_agent=author, assignee=winner, priority="P1",
                              summary=f"PR #{pr} in {repo} needs review. Claim it "
                                      f"(transition active / emit review-accepted) before working.")
      task["tags"] = sorted(set(task.get("tags", []) + [routing.REVIEW_TAG]))
      task["pr"] = pr; task["repo"] = repo  # carried for the sweep + audit
      tier = next((s["tier"] for s in snapshot if s["agent"] == winner), "idle")
      task = _append_route_event_and_assignee(
          task, kind="routed", to=winner, by=author, attempt=1,
          reason=f"live/idle reviewer ({tier})", candidate_snapshot=snapshot,
          observed_updated_at=task.get("updated_at", ""))
      try:
          ok = _write_task_and_views(task, backend=backend, command="request-review")
      except (schema.ConflictError, schema.NeedsReconcile):
          ok = True
      _info(f"PR #{pr} routed to {winner} ({tier}).")
      return 0 if ok else 1
  ```
  (Fill the `_escalate_review_to_human` body per the cmd_block --on-user shape: load/construct a task, `apply_transition(...,"blocked",...)` or a fresh blocked task assigned to the human with a `needs:human` tag, write best-effort.)
- [ ] **Wire `entry.py`:** add the `request-review` subparser + `COMMAND_MAP["request-review"] = _cli.cmd_request_review`:
  ```python
  sp = sub.add_parser("request-review",
                      help="Route a PR review to a live/idle reviewer (capability-based, "
                           "self-healing); escalates to the human if nobody qualifies")
  sp.add_argument("pr", metavar="PR", help="PR number/identifier")
  sp.add_argument("--repo", required=True, metavar="REPO")
  sp.add_argument("--agent", "-a", default=None, metavar="AGENT",
                  help="The author (default: derived) — selects the canonical reviewer")
  sp.add_argument("--candidate-list", dest="candidate_list", default=None, metavar="A,B,C",
                  help="Explicit preference-ordered pool override (advanced)")
  sp.add_argument("--dry-run", dest="dry_run", action="store_true",
                  help="Print ranked pool / tiers / excluded / winner / reason; write nothing")
  sp.add_argument("--format", choices=["table", "json"], default="table")
  ```
- [ ] **Run, expect PASS:** `uv run --extra dev python -m pytest tests/test_fulcra_coord.py::TestReviewPool tests/test_fulcra_coord.py::TestRequestReview -v`
- [ ] **Commit:** `cd /private/tmp/fc-routing && git add -A packages/fulcra-coord/fulcra_coord packages/fulcra-coord/tests && git commit -m "Add request-review command: liveness-aware reviewer routing entry point" -m "Builds a preference-ordered pool (canonical reviewer seed + capability:review agents), resolves a live/idle recipient, and either tells them a kind:review-tagged directive (appending a routed event + syncing assignee) or escalates to the human via block --on-user. --dry-run prints the ranked pool/tiers/excluded/winner/reason and writes nothing. Best-effort: a presence/resolve failure escalates rather than crashing." -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"`

---

### Task 5 — The re-route sweep (in `reconcile`)

The authoritative self-healing pass. Considers ONLY `kind:review` directives. Re-routes never-acted reviews whose assignee fell below floor (past the priority threshold, under the cap, respecting cooldown + the stale-observation check); freezes accepted-then-stalled ones and escalates only after `ACCEPTED_STALL_HOURS`. Runs once per reconcile cycle. Best-effort — never raises into a reconcile tick.

- [ ] **Write failing env-threshold tests.** Add `TestReviewSweepThresholds(unittest.TestCase)`:
  ```python
  from fulcra_coord import cli
  def test_reroute_minutes_defaults(self):
      os.environ.pop("FULCRA_COORD_REVIEW_REROUTE_MINUTES_P1", None)
      os.environ.pop("FULCRA_COORD_REVIEW_REROUTE_MINUTES_P2", None)
      self.assertEqual(cli._reroute_minutes("P1"), 15.0)
      self.assertEqual(cli._reroute_minutes("P2"), 30.0)
  def test_reroute_minutes_env_override(self):
      os.environ["FULCRA_COORD_REVIEW_REROUTE_MINUTES_P1"] = "5"
      try:
          self.assertEqual(cli._reroute_minutes("P1"), 5.0)
      finally:
          os.environ.pop("FULCRA_COORD_REVIEW_REROUTE_MINUTES_P1", None)
  def test_reroute_max_default(self):
      os.environ.pop("FULCRA_COORD_REVIEW_REROUTE_MAX", None)
      self.assertEqual(cli._reroute_max(), 2)
  def test_accepted_stall_hours_default(self):
      os.environ.pop("FULCRA_COORD_ACCEPTED_STALL_HOURS", None)
      self.assertEqual(cli._accepted_stall_hours(), 2.0)
  ```
- [ ] **Run, expect FAIL.** Implement the three readers (`_reroute_minutes(priority)`, `_reroute_max()`, `_accepted_stall_hours()`) mirroring `views._stale_hours` (explicit/env/default; env names `FULCRA_COORD_REVIEW_REROUTE_MINUTES_P1`=15, `_P2`=30, `FULCRA_COORD_REVIEW_REROUTE_MAX`=2, `FULCRA_COORD_ACCEPTED_STALL_HOURS`=2). **Run, expect PASS.**

- [ ] **Write failing tests for the classification helper.** The sweep's pure core is `_classify_review(task, presence, now)` returning one of `"reroute"`, `"escalate"`, `"freeze"`, `"freeze-escalate"`, `"none"`. Add `TestReviewClassification(unittest.TestCase)`. Helper to build a routed review:
  ```python
  from fulcra_coord import cli, routing, schema
  NOW = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)
  def _routed_review(assignee, routed_minutes_ago, priority="P1", attempt=1,
                     extra_events=None, last_seen_min_ago=300):
      routed_at = (NOW - timedelta(minutes=routed_minutes_ago)).isoformat(timespec="microseconds").replace("+00:00","Z")
      ev = routing.make_route_event(kind="routed", to=assignee, by="s", attempt=attempt,
                                    reason="x", candidate_snapshot=[], observed_updated_at=routed_at,
                                    at=routed_at, route_id=f"r{attempt}")
      events = [{"at": routed_at, "type": "created", "by": "s"}, ev] + (extra_events or [])
      return {"id": "TASK-20260604-rev-00000000", "status": "proposed", "priority": priority,
              "assignee": assignee, "tags": ["kind:review"], "events": events,
              "updated_at": routed_at, "workstream": "fulcra-tools"}
  def _presence(agent, min_ago):
      ls = (NOW - timedelta(minutes=min_ago)).isoformat(timespec="microseconds").replace("+00:00","Z")
      return [{"agent": agent, "last_seen": ls, "capabilities": ["review"]}]

  def test_never_acted_below_floor_past_p1_threshold_reroutes(self):
      t = _routed_review("dead:h:r", routed_minutes_ago=20, priority="P1")  # >15m
      pres = _presence("dead:h:r", 300)  # assignee long stale -> below floor
      self.assertEqual(cli._classify_review(t, pres, NOW), "reroute")

  def test_before_threshold_is_none(self):
      t = _routed_review("dead:h:r", routed_minutes_ago=5, priority="P1")  # <15m
      self.assertEqual(cli._classify_review(t, _presence("dead:h:r", 300), NOW), "none")

  def test_p2_uses_30m_threshold(self):
      t = _routed_review("dead:h:r", routed_minutes_ago=20, priority="P2")  # <30m
      self.assertEqual(cli._classify_review(t, _presence("dead:h:r", 300), NOW), "none")

  def test_bare_inbox_ack_does_not_count_as_acceptance(self):
      ack = {"at": (NOW - timedelta(minutes=18)).isoformat(timespec="microseconds").replace("+00:00","Z"),
             "type": "inbox_ack", "by": "dead:h:r"}
      t = _routed_review("dead:h:r", routed_minutes_ago=20, priority="P1", extra_events=[ack])
      # a read receipt is NOT acceptance -> still eligible to reroute.
      self.assertEqual(cli._classify_review(t, _presence("dead:h:r", 300), NOW), "reroute")

  def test_explicit_review_accepted_freezes(self):
      acc = {"at": (NOW - timedelta(minutes=18)).isoformat(timespec="microseconds").replace("+00:00","Z"),
             "type": "review-accepted", "by": "dead:h:r"}
      t = _routed_review("dead:h:r", routed_minutes_ago=20, priority="P1", extra_events=[acc])
      # accepted but not yet past ACCEPTED_STALL_HOURS -> freeze (no reassign, no escalate).
      self.assertEqual(cli._classify_review(t, _presence("dead:h:r", 300), NOW), "freeze")

  def test_accepted_then_long_stall_escalates(self):
      acc = {"at": (NOW - timedelta(hours=3)).isoformat(timespec="microseconds").replace("+00:00","Z"),
             "type": "review-accepted", "by": "dead:h:r"}
      t = _routed_review("dead:h:r", routed_minutes_ago=200, priority="P1", extra_events=[acc])
      self.assertEqual(cli._classify_review(t, _presence("dead:h:r", 300), NOW), "freeze-escalate")

  def test_claim_transition_by_assignee_counts_as_acceptance(self):
      active = {"at": (NOW - timedelta(minutes=18)).isoformat(timespec="microseconds").replace("+00:00","Z"),
                "type": "active", "by": "dead:h:r"}  # status transition authored by assignee
      t = _routed_review("dead:h:r", routed_minutes_ago=20, priority="P1", extra_events=[active])
      self.assertEqual(cli._classify_review(t, _presence("dead:h:r", 300), NOW), "freeze")

  def test_assignee_above_floor_is_none(self):
      t = _routed_review("alive:h:r", routed_minutes_ago=20, priority="P1")
      self.assertEqual(cli._classify_review(t, _presence("alive:h:r", 5), NOW), "none")  # live, give it time

  def test_cap_reached_escalates(self):
      t = _routed_review("dead:h:r", routed_minutes_ago=20, priority="P1", attempt=2)  # attempt==cap(2)
      self.assertEqual(cli._classify_review(t, _presence("dead:h:r", 300), NOW), "escalate")

  def test_non_review_task_is_none(self):
      t = _routed_review("dead:h:r", routed_minutes_ago=20, priority="P1")
      t["tags"] = ["kind:ops"]  # NOT a review directive
      self.assertEqual(cli._classify_review(t, _presence("dead:h:r", 300), NOW), "none")
  ```
- [ ] **Run, expect FAIL.**
- [ ] **Implement `_classify_review`** in `cli.py` (pure; the sweep wraps it with I/O):
  ```python
  def _review_accepted_by_assignee(task, assignee, routed_dt):
      """Explicit acceptance after routed_at: a review-accepted event OR a
      status-transition-to-active authored by the assignee. A bare inbox_ack is a
      READ receipt, NOT acceptance (codex tightening #4) — excluded here so a
      reviewer that only opened its inbox then went dark still gets rerouted."""
      for e in task.get("events", []):
          if e.get("by") != assignee:
              continue
          at = views._parse_dt(e.get("at", ""))
          if at is None or routed_dt is None or at < routed_dt:
              continue
          if e.get("type") == "review-accepted":
              return at
          if e.get("type") == "active":  # claim/transition-to-active is acceptance
              return at
      return None

  def _classify_review(task, presence, now):
      """Pure classifier for the reroute sweep. Returns reroute|escalate|freeze|
      freeze-escalate|none. Never reroutes a non-kind:review task."""
      from . import routing
      if not routing.is_review_directive(task):
          return "none"
      if task.get("status") in ("done", "abandoned"):
          return "none"
      route = routing.current_route(task)
      if route is None:
          return "none"
      assignee = route.get("to")
      routed_dt = views._parse_dt(route.get("at", ""))
      accepted_at = _review_accepted_by_assignee(task, assignee, routed_dt)
      if accepted_at is not None:
          # Accepted-then-stalled: FREEZE (don't yank mid-work). Escalate only
          # after a long stall measured from acceptance.
          stall_h = _accepted_stall_hours()
          if (now - accepted_at).total_seconds() / 3600.0 >= stall_h:
              return "freeze-escalate"
          return "freeze"
      # Never-acted path: only reroute if assignee is below floor AND past threshold.
      eff = views._effective_routing_liveness(
          next((r.get("last_seen","") for r in presence if r.get("agent")==assignee), ""),
          now, views._presence_grace_seconds())
      if eff is not None:  # assignee still live/idle -> give it time, no reroute
          return "none"
      threshold_min = _reroute_minutes(task.get("priority", "P2"))
      if routed_dt is None or (now - routed_dt).total_seconds() / 60.0 < threshold_min:
          return "none"
      if routing.route_attempt_count(task) >= _reroute_max():
          return "escalate"  # cap reached
      return "reroute"
  ```
- [ ] **Run, expect PASS.**

- [ ] **Write a failing test for the stale-observation check + the sweep I/O wrapper.** Add `TestReviewSweep(unittest.TestCase)`:
  ```python
  from fulcra_coord.cli import _sweep_review_routes
  def test_sweep_reroutes_and_writes_rerouted_event(self):
      t = _routed_review("dead:h:r", routed_minutes_ago=20, priority="P1")
      agg = {"agents": _presence("dead:h:r", 300) + [{"agent": "alive:h:r",
              "last_seen": NOW.isoformat(timespec="microseconds").replace("+00:00","Z"),
              "capabilities": ["review"]}]}
      written = {}
      def fake_write(task, backend=None, command="write", lifecycle=None):
          written["task"] = task; return True
      with patch("fulcra_coord.cli.remote.download_json", side_effect=lambda p, backend=None:
                   agg if p == remote.presence_view_path() else t), \
           patch("fulcra_coord.cli._write_task_and_views", side_effect=fake_write):
          _sweep_review_routes([t], backend=["false"], now=NOW)
      out = written["task"]
      rer = [e for e in out["events"] if e["type"] == "rerouted"]
      self.assertEqual(len(rer), 1)
      self.assertEqual(out["assignee"], rer[0]["to"])
      self.assertNotEqual(rer[0]["to"], "dead:h:r")  # excluded as tried
      self.assertEqual(rer[0]["route_id"] != _routed_review("x",1)["events"][1]["route_id"], True)  # new route_id

  def test_sweep_stale_observation_aborts_when_task_moved(self):
      # The re-read task's latest route event differs from the snapshot the
      # decision was computed from -> abort (no competing reroute).
      t = _routed_review("dead:h:r", routed_minutes_ago=20, priority="P1")
      moved = _routed_review("someoneelse:h:r", routed_minutes_ago=1, priority="P1", attempt=2)
      agg = {"agents": _presence("dead:h:r", 300) + [{"agent": "alive:h:r",
              "last_seen": NOW.isoformat(timespec="microseconds").replace("+00:00","Z"), "capabilities": ["review"]}]}
      with patch("fulcra_coord.cli.remote.download_json", side_effect=lambda p, backend=None:
                   agg if p == remote.presence_view_path() else moved), \
           patch("fulcra_coord.cli._write_task_and_views") as wtv:
          _sweep_review_routes([t], backend=["false"], now=NOW)
      wtv.assert_not_called()  # another sweeper already moved it
  ```
- [ ] **Run, expect FAIL.**
- [ ] **Implement `_sweep_review_routes`** in `cli.py` and call it from `cmd_reconcile`. Per-task flow, all wrapped in `try/except: continue` (best-effort, never raises into a reconcile tick):
  ```python
  def _sweep_review_routes(all_tasks, *, backend=None, now=None):
      """Authoritative reconcile-time reroute sweep (§6). Considers ONLY
      kind:review directives. For each: classify; reroute never-acted below-floor
      past-threshold reviews (excluding tried, new route_id), escalate on cap/miss,
      freeze accepted-then-stalled (escalate after ACCEPTED_STALL_HOURS). Runs once
      per cycle; whichever machine reconciles first wins, others converge via the
      stale-observation re-read + the cooldown + the optimistic write. Best-effort."""
      from . import routing
      if now is None:
          now = datetime.now(timezone.utc)
      try:
          agg = remote.download_json(remote.presence_view_path(), backend=backend)
          presence = (agg or {}).get("agents", []) if agg else []
      except Exception:
          presence = []
      for task in all_tasks:
          try:
              if not routing.is_review_directive(task):
                  continue
              verdict = _classify_review(task, presence, now)
              if verdict == "none" or verdict == "freeze":
                  continue
              if verdict in ("escalate", "freeze-escalate"):
                  _escalate_review_to_human(
                      pr=task.get("pr", task.get("id")), repo=task.get("repo", task.get("workstream","")),
                      tried=sorted(routing.tried_agents(task)), backend=backend)
                  continue
              # verdict == "reroute": stale-observation check, then write.
              route = routing.current_route(task)
              observed = route.get("observed_updated_at")
              fresh = _load_task(task["id"], backend=backend)
              if fresh is None:
                  continue
              fresh_route = routing.current_route(fresh)
              # Abort if the task moved since we computed the decision: another
              # sweeper or the assignee changed the latest route or updated_at
              # (multi-sweeper convergence, no-CAS — codex tightening #3).
              if (fresh_route or {}).get("route_id") != (route or {}).get("route_id") \
                 or fresh.get("updated_at") != task.get("updated_at"):
                  continue
              pool = _review_pool(task.get("owner_agent",""), presence)
              winner = views.resolve_live_recipient(
                  pool, presence, floor="idle", now=now, exclude=tuple(routing.tried_agents(task)))
              if winner is None:
                  _escalate_review_to_human(pr=task.get("pr", task.get("id")),
                      repo=task.get("repo", task.get("workstream","")),
                      tried=sorted(routing.tried_agents(task)), backend=backend)
                  continue
              snapshot = [{"agent": a} for a in pool]
              updated = _append_route_event_and_assignee(
                  fresh, kind="rerouted", to=winner, by="reconcile-sweep",
                  attempt=routing.route_attempt_count(fresh) + 1,
                  reason="assignee below floor, never acted",
                  candidate_snapshot=snapshot, observed_updated_at=fresh.get("updated_at",""))
              try:
                  _write_task_and_views(updated, backend=backend, command="reroute-review")
              except (schema.ConflictError, schema.NeedsReconcile):
                  pass  # optimistic write is the second line of defence; next cycle reconverges
          except Exception:
              continue  # one bad task must never break the sweep / reconcile tick
  ```
  In `cmd_reconcile`, after `all_tasks = _load_all_tasks(...)` (the existing load) and inside the existing best-effort flow, add a guarded call:
  ```python
  # Liveness-aware reroute sweep (best-effort; never fails a reconcile tick).
  try:
      _sweep_review_routes(all_tasks, backend=backend, now=now)
  except Exception:
      pass
  ```
  (Place it near `_reconcile_presence(...)`; it reads `all_tasks` already loaded. Cooldown: the stale-observation re-read + the per-decision `route_id` identity mean a second reconcile in the same window re-reads the freshly-rerouted task, sees the new route, and the threshold restarts from the new `routed_at` — so it cannot double-move within one threshold window.)
- [ ] **Run, expect PASS:** `uv run --extra dev python -m pytest tests/test_fulcra_coord.py::TestReviewSweepThresholds tests/test_fulcra_coord.py::TestReviewClassification tests/test_fulcra_coord.py::TestReviewSweep -v`
- [ ] **Commit:** `cd /private/tmp/fc-routing && git add -A packages/fulcra-coord/fulcra_coord packages/fulcra-coord/tests && git commit -m "Add reconcile reroute sweep for stalled kind:review directives" -m "Once per reconcile cycle, re-routes never-acted reviews whose assignee fell below liveness floor (P1 15m / P2 30m, env-overridable; cap 2; then escalate) and freezes accepted-then-stalled ones (explicit review-accepted/claim, NOT a bare inbox_ack), escalating only after ACCEPTED_STALL_HOURS. A stale-observation re-read aborts a reroute whose observed route_id/updated_at no longer match the re-read task, so two machines racing from the same snapshot converge to one reroute (Files has no CAS). Best-effort: never raises into a reconcile tick." -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"`

---

### Task 6 — General `tell --route-capability` helper

The reusable primitive reviews are the first consumer of: `tell` resolves the recipient via the same resolver at send time instead of a fixed `--to`/assignee. Same escalation-on-miss.

- [ ] **Write failing tests.** Add `TestTellRouteCapability(unittest.TestCase)`:
  ```python
  from fulcra_coord.cli import cmd_tell
  def test_tell_route_capability_resolves_live_recipient(self):
      now_ls = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00","Z")
      agg = {"agents": [{"agent": "rev:h:r", "last_seen": now_ls, "capabilities": ["review"]}]}
      captured = {}
      with patch("fulcra_coord.cli.remote.download_json", return_value=agg), \
           patch("fulcra_coord.cli._write_task_and_views",
                 side_effect=lambda task, backend=None, command="write", lifecycle=None: captured.update(task=task) or True), \
           patch("fulcra_coord.cli.identity.resolve_agent", return_value="a:b:c"):
          args = types.SimpleNamespace(assignee=None, title="Do X", next="", workstream="general",
              priority="P2", summary="", route_capability="review", floor="idle")
          setattr(args, "from", None)
          rc = cmd_tell(args, backend=["false"])
      self.assertEqual(captured["task"]["assignee"], "rev:h:r")

  def test_tell_route_capability_miss_escalates(self):
      old_ls = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(timespec="microseconds").replace("+00:00","Z")
      agg = {"agents": [{"agent": "rev:h:r", "last_seen": old_ls, "capabilities": ["review"]}]}
      escalated = {}
      with patch("fulcra_coord.cli.remote.download_json", return_value=agg), \
           patch("fulcra_coord.cli._escalate_review_to_human",
                 side_effect=lambda **kw: escalated.update(kw) or True), \
           patch("fulcra_coord.cli.identity.resolve_agent", return_value="a:b:c"):
          args = types.SimpleNamespace(assignee=None, title="Do X", next="", workstream="general",
              priority="P2", summary="", route_capability="review", floor="idle")
          setattr(args, "from", None)
          rc = cmd_tell(args, backend=["false"])
      self.assertTrue(escalated)
  ```
- [ ] **Run, expect FAIL.**
- [ ] **Implement** in `cmd_tell`: at the top, if `getattr(args, "route_capability", None)` is set, download presence, build a capability pool (`[r["agent"] for r in presence if route_capability in r.get("capabilities",[])]`), `resolve_live_recipient(pool, presence, floor=getattr(args,"floor","idle"))`; on hit set `assignee = winner` (then fall through to the existing creation path); on miss call `_escalate_review_to_human`-style escalation (generalize it to a `_escalate_directive_to_human(*, ask, tried, backend)` or reuse with a generic ask) and return 0. Best-effort: a resolve failure escalates. Add a guard so `assignee` is still required when `--route-capability` is absent.
- [ ] **Wire `entry.py`** `tell` subparser: make the positional `assignee` `nargs="?"` (so `--route-capability` can replace it) and add:
  ```python
  sp.add_argument("--route-capability", dest="route_capability", default=None, metavar="CAP",
                  help="Resolve a LIVE recipient declaring CAP instead of a fixed assignee")
  sp.add_argument("--floor", choices=["live", "idle"], default="idle",
                  help="Minimum liveness for --route-capability resolution (default: idle)")
  ```
  Add a validation in `cmd_tell`: if neither `assignee` nor `route_capability` is given, `_err` and return 1.
- [ ] **Run, expect PASS:** `uv run --extra dev python -m pytest tests/test_fulcra_coord.py::TestTellRouteCapability -v`
- [ ] **Commit:** `cd /private/tmp/fc-routing && git add -A packages/fulcra-coord/fulcra_coord packages/fulcra-coord/tests && git commit -m "Add tell --route-capability/--floor: general route-to-live primitive" -m "Resolves the recipient via resolve_live_recipient at send time (pool = agents declaring the capability) instead of a fixed assignee, with the same escalate-on-miss path reviews use. Reviews are this primitive's first consumer; this exposes it for any directive class." -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"`

---

### Task 7 — Version bump + CHANGELOG

- [ ] **Bump version.** Edit `fulcra_coord/__init__.py`: `__version__ = "0.6.0"` → `"0.7.0"`. (REBASE-AWARE: CHANGELOG on `main` is currently topped by `0.6.0`; if a newer version landed during this branch's life, rebase first and bump to the next minor instead.)
- [ ] **Add the failing version test** (or update the existing capabilities/version test if one asserts `0.6.0`): `grep -rn "0\.6\.0" tests/` — if a test pins the version, update it to `0.7.0`. Add:
  ```python
  def test_version_is_0_7_0(self):
      from fulcra_coord import __version__
      self.assertEqual(__version__, "0.7.0")
  ```
- [ ] **Run, expect FAIL then PASS** after the bump: `uv run --extra dev python -m pytest tests/test_fulcra_coord.py -k version -v`
- [ ] **Add the CHANGELOG entry** at the top of `packages/fulcra-coord/CHANGELOG.md` (above `## [0.6.0]`):
  ```markdown
  ## [0.7.0] — Liveness-Aware Reviewer Routing

  **Why:** PR-review directives were routed to a FIXED reviewer (canonical, or a
  configured #devops fallback) regardless of whether that agent was online. PRs
  sat unreviewed in a stale fallback's inbox while a capable reviewer was idle the
  whole time, and nothing re-routed a directive once its assignee went dark.

  **What:**
  - `request-review <pr> --repo <repo>` routes a PR review to a reviewer presence
    says is actually live/idle (capability-based pool: canonical reviewer seed +
    agents that declared `--can-review`), tagging the directive `kind:review` and
    recording a `routed` event. `--dry-run` shows the ranked pool/tiers/winner.
  - `connect --can-review` / `--role` declare an agent's capabilities on its
    presence record (default `[]`, backward compatible).
  - `reconcile` now sweeps stalled `kind:review` directives: re-routes a never-
    acted review whose assignee fell below liveness floor (P1 15m / P2 30m,
    env-overridable; cap 2; then escalate to the human), and freezes one the
    assignee explicitly accepted, escalating only after a long stall.
  - `tell --route-capability R [--floor live|idle]` exposes the underlying
    route-to-live primitive for any directive.
  - Escalation (no live reviewer) lands on the human's plate via the existing
    `block --on-user` / needs:human surface. New env knobs:
    `FULCRA_COORD_PRESENCE_GRACE_SECONDS` (1200), `…REVIEW_REROUTE_MINUTES_P1/P2`
    (15/30), `…REVIEW_REROUTE_MAX` (2), `…ACCEPTED_STALL_HOURS` (2).
  ```
- [ ] **Run the FULL suite, expect all PASS:** `uv run --extra dev python -m pytest -q` then `cd /private/tmp/fc-routing && git checkout -- uv.lock packages/fulcra-coord/uv.lock` if churned.
- [ ] **Commit:** `cd /private/tmp/fc-routing && git add -A packages/fulcra-coord && git commit -m "Bump fulcra-coord 0.6.0 -> 0.7.0 + CHANGELOG (liveness-aware reviewer routing)" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"`

---

## Self-Review

### (a) Spec coverage — every § / decision maps to a task

| Spec § / decision | Task |
| --- | --- |
| §1 `resolve_live_recipient` signature + ranking `(tier, preference)` | Task 1 |
| §1 effective-liveness-from-`last_seen`, NOT stored tier (codex #1) | Task 1 (`_effective_routing_liveness`, `test_effective_liveness_recomputed...`) |
| §1 `PRESENCE_GRACE_SECONDS` wall-clock grace (1200) | Task 1 (`_presence_grace_seconds`, grace-window tests) |
| §1 floor live/idle, exclude, None | Task 1 (floor/exclude/None tests) |
| §2 `connect --can-review`/`--role` → `capabilities`; build_presence carry; `[]` default | Task 2 |
| §3 canonical seed (Arc vs codex) + capability:review pool; #devops not auto-seeded | Task 4 (`_canonical_reviewer`/`_review_pool`) |
| §4 `request-review` hit (tell + kind:review + routed event + assignee) / miss (block --on-user) / `--dry-run` | Task 4 |
| §5 routing event field set `{at,kind→type,to,by,attempt,reason,candidate_snapshot,observed_updated_at,route_id}` | Task 3 (`make_route_event`) |
| §5 `route_id` per decision (codex #2) | Task 3 (`new_route_id`, tie-break test) |
| §5 deterministic current-route (latest by `at`, tie-break `route_id`); assignee kept in sync | Task 3 (`current_route`) + Task 4/5 (`_append_route_event_and_assignee`) |
| §6 sweep scope = kind:review only (never an ordinary tell) | Task 5 (`is_review_directive`, `test_non_review_task_is_none`) |
| §6 never-acted vs accepted-then-stalled via explicit review-accepted/claim NOT bare ack (codex #4) | Task 5 (`_review_accepted_by_assignee`, `test_bare_inbox_ack...`) |
| §6 P1 15m / P2 30m env-overridable; cap 2; ACCEPTED_STALL_HOURS 2 | Task 5 (threshold readers) |
| §6 cooldown (one reroute per window) | Task 5 (re-read + threshold-restarts-from-new-routed_at note) |
| §6 stale-observation check: re-read + verify `observed_updated_at`/route (codex #3) | Task 5 (`test_sweep_stale_observation_aborts...`) |
| §6 escalate on miss/cap | Task 5 (`escalate`/`freeze-escalate` verdicts) |
| §7 general `tell --route-capability [--floor]` | Task 6 |
| Machine-agnostic invariant (bus-global inputs, wall-clock gates, parsed compares, first-reconcile-wins) | All tasks — stated up front + reused in 1/3/5 |
| Error handling: best-effort, never raises into request-review/reconcile tick | Task 4 (try/except → escalate) + Task 5 (per-task try/except: continue) |
| Escalation idempotency (update not duplicate) | Task 4/5 `_escalate_review_to_human` (needs:human surface, cmd_assign updates) |
| Version bump + CHANGELOG (rebase-aware) | Task 7 |

### (b) Placeholder scan
No `TODO`/`FIXME`/`<placeholder>`/`...your-code-here...` in shipped code. The only `...` ellipses are the explicitly-flagged `_escalate_review_to_human` body in Task 4 (with a precise spec-shaped instruction to fill it from the `cmd_block --on-user` pattern) — every other code block is concrete with real signatures, real env-var names (`FULCRA_COORD_PRESENCE_GRACE_SECONDS`, `FULCRA_COORD_REVIEW_REROUTE_MINUTES_P1`/`_P2`, `FULCRA_COORD_REVIEW_REROUTE_MAX`, `FULCRA_COORD_ACCEPTED_STALL_HOURS`) and real defaults (1200 / 15 / 30 / 2 / 2).

### (c) Name / type consistency
- **Routing-event field names are identical everywhere:** `make_route_event` (Task 3) emits `{at, type, to, by, attempt, reason, candidate_snapshot, observed_updated_at, route_id}`; `current_route`/`route_attempt_count`/`tried_agents` (Task 3), `_append_route_event_and_assignee` (Task 4), and `_classify_review`/`_sweep_review_routes` (Task 5) all read exactly those keys. (`kind` is the *constructor argument* that becomes the event's `type` — `routed`/`rerouted` — matching the existing event convention where a status event's `type` is the status name; the sweep/tests read `e["type"]`.)
- **Resolver signature is identical across consumers:** `resolve_live_recipient(candidates, presence, *, floor, now, exclude, grace_seconds)` is called the same way in Task 4 (`request-review`), Task 5 (sweep, with `exclude=tuple(tried_agents(...))`), and Task 6 (`tell`).
- **Presence record dict keys are identical:** `agent`, `last_seen`, `capabilities` (added Task 2) — read by `resolve_live_recipient`/`_effective_routing_liveness` (Task 1), `_review_pool` (Task 4), and the sweep (Task 5).
- **Marker constant is single-sourced:** `routing.REVIEW_TAG = "kind:review"` is the one definition; `request-review` (Task 4) adds it, `is_review_directive` (Task 3) + the sweep (Task 5) read it.
- **`_effective_routing_liveness` is the single liveness judgment** reused by the resolver (Task 1), the dry-run snapshot + classification (Tasks 4/5) — no second notion of "live for routing" exists.
