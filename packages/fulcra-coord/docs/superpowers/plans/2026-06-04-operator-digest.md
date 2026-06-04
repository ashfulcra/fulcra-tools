# Operator Digest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a twice-daily + on-demand "Operator Digest" — a consolidated situational-awareness summary (what's blocked on you, upcoming, what each agent did, what's stale) written to its own Fulcra timeline annotation track — so the human gets a push, human-paced glance where they already review their day.

**Architecture:** A new pure function `views.build_operator_digest` folds the existing summaries + presence bus state into a structured digest dict (`blocked_on_you`/`upcoming`/`per_agent`/`stale`); a `cli._render_digest` turns it into a timeline `(name, note)`; a second annotation definition ("Agent Tasks — Digest") in `annotations.py` reuses the proven `_write_http` path with its own cached definition id; a new `digest` command claims an any-agent dedup marker on the Files bus (`digest/markers/<date>-<window>.json`, first-writer-wins) before emitting; and an `install-digest` scheduler mirrors `install-heartbeat` but uses launchd `StartCalendarInterval` (08:00 / 18:00) / two cron lines. A separate, backward-compatible companion enriches the per-event `build_annotation` note with work substance. Everything is best-effort and never raises into a scheduled tick.

**Tech Stack:** stdlib-only Python; unittest+pytest; Fulcra Files bus; HTTP annotation writer; launchd/cron schedulers.

---

## Pre-flight notes (read before Task 1)

- **Branch / version baseline.** This worktree branched from `origin/main` at `__version__ = "0.5.5"`. **PR #39 (→ 0.5.6) is in review.** Before landing the final version bump, **rebase `feat/operator-digest` onto `origin/main` once #39 merges** so the digest ships as **0.6.0** on top of 0.5.6 (not 0.5.5). The conflict surface is small (`__init__.py` version line + `CHANGELOG.md` top), and Task 8 sets `__version__` last so a rebase only re-touches that one task's hunk.
- **Datetime comparisons are PARSED, never lexical.** PR #39 fixed a mixed-precision timestamp bug: a wall-clock instant emits fractional seconds only when microsecond != 0, while stored values keep their own precision, so a lexical compare of mixed-width ISO-Z strings is unsound (`Z` 0x5A sorts after `.` 0x2E). The digest's `since`-window filtering and `due`-ranking **MUST** parse via `views._parse_dt` (already tz-coercing to aware UTC) and compare `datetime` objects — never string `<`/`>`. This is consistent with #39's `_schedule_dt`/`needs_human`/`upcoming_for_human` fix. Sort *keys* may stay strings (sorting only orders), but any *gate/threshold compare* parses first.
- **task_summary fields available (no body fetch needed)** — confirmed from `schema.task_summary`: `id, title, status, priority, workstream, owner_agent, assignee, last_touched_by, current_summary, next_action, blocked_on, not_before, due, tags, updated_at, done_at, acked_by, task_file`. A summary has **no `events`** list and **no top-level `kind`** (kind lives in `tags` as `kind:<x>`).
- **Test harness conventions** (from `tests/test_fulcra_coord.py` / `tests/test_annotations.py`): stdlib `unittest`, run via `uv run --extra dev python -m pytest -q` (and a single test via `-v ...::Class::test`). Remote I/O is mocked either by passing `backend=["false"]` (a backend that always exits 1) for "nothing on the bus" reads, or by `patch`-ing `fulcra_coord.cli.remote.download_json` / the `remote.*` helpers. HTTP transport tests use the `_FakeResp` / `_Router` helpers and patch `urllib.request.urlopen` + `annotations._resolve_token` (set `FULCRA_ACCESS_TOKEN`). A temp `XDG_CACHE_HOME` isolates the cache.

---

## File Structure

| File | Created / Modified | Single responsibility |
|---|---|---|
| `fulcra_coord/views.py` | Modified | Add `build_operator_digest(summaries, presence, *, human, now, since)` — the pure fold over bus state producing the four-block digest dict. Reuses `needs_human`, `upcoming_for_human`, `is_stale`, `_parse_dt`. |
| `fulcra_coord/cli.py` | Modified | Add `_render_digest(digest, *, window) -> (name, note)` (pure render); add `_digest_window_since(window, now)` helper; add `cmd_digest` command handler + a `_digest_marker_path` / dedup-claim helper. |
| `fulcra_coord/annotations.py` | Modified | Add a SECOND moment definition `"Agent Tasks — Digest"` with its own cache file + resolver, and `emit_digest_annotation(*, name, note, window, agent, backend=None)` reusing `_write_http`. Also (Task 7) enrich `build_annotation`'s note with work substance. |
| `fulcra_coord/digest_schedule.py` | Created | The `install-digest` scheduler: launchd `StartCalendarInterval` plist (08:00 morning + 18:00 evening) / two managed cron lines, mirroring `heartbeat.py` but calendar-based. stdlib-only, testable. |
| `fulcra_coord/entry.py` | Modified | Register the `digest` and `install-digest` subparsers + wire both into `COMMAND_MAP`. |
| `fulcra_coord/__init__.py` | Modified | Bump `__version__` to `0.6.0` (final task). |
| `packages/fulcra-coord/CHANGELOG.md` | Modified | Add the `[0.6.0] — Operator Digest` entry (final task). |
| `packages/fulcra-coord/README.md` | Modified (subagent, parallel) | Document the `digest` + `install-digest` commands and the digest track. Dispatched in parallel after Task 6 lands; does not block. |
| `tests/test_operator_digest.py` | Created | All new unit tests for `build_operator_digest`, `_render_digest`, the dedup guard, `emit_digest_annotation`, and `install-digest`. |
| `tests/test_annotations.py` | Modified | Add the Task 7 per-event enrichment assertions. |

---

### Task 1: `views.build_operator_digest` — the pure four-block fold

The most-tested piece. Pure, deterministic given injected `now`/`since`. No I/O.
Block keys are **exactly** `blocked_on_you`, `upcoming`, `per_agent`, `stale` —
these same keys are consumed verbatim by `_render_digest` (Task 2) and `cmd_digest` (Task 4).

- [ ] **Step 1.1 — failing test: empty inputs yield the four empty blocks.**
  Add to `tests/test_operator_digest.py`:
  ```python
  """Tests for the Operator Digest (views.build_operator_digest, cli._render_digest,
  the digest command + dedup guard, emit_digest_annotation, install-digest)."""

  from __future__ import annotations

  import json
  import os
  import sys
  import tempfile
  import types
  import unittest
  from datetime import datetime, timedelta, timezone
  from pathlib import Path
  from unittest.mock import patch

  sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

  from fulcra_coord import views, schema

  NOW = datetime(2026, 6, 4, 18, 0, 0, tzinfo=timezone.utc)
  SINCE = NOW - timedelta(hours=12)


  def _summary(**over):
      """A task_summary-shaped dict with sane defaults (mirrors schema.task_summary keys)."""
      base = {
          "id": "20260604-x", "title": "X", "status": "active", "priority": "P2",
          "workstream": "devops", "owner_agent": "claude-code:mb:repo",
          "assignee": None, "last_touched_by": "claude-code:mb:repo",
          "current_summary": "", "next_action": "", "blocked_on": None,
          "not_before": None, "due": None, "tags": [], "updated_at": "2026-06-04T17:00:00Z",
          "done_at": None, "acked_by": [],
      }
      base.update(over)
      return base


  class TestBuildOperatorDigestEmpty(unittest.TestCase):
      def test_all_blocks_present_and_empty(self):
          d = views.build_operator_digest([], [], human="ash", now=NOW, since=SINCE)
          self.assertEqual(d["blocked_on_you"], [])
          self.assertEqual(d["upcoming"], [])
          self.assertEqual(d["per_agent"], [])
          self.assertEqual(d["stale"], [])
  ```
- [ ] **Step 1.2 — run it (expected FAIL):** `uv run --extra dev python -m pytest tests/test_operator_digest.py::TestBuildOperatorDigestEmpty::test_all_blocks_present_and_empty -v`
  Expected failure: `AttributeError: module 'fulcra_coord.views' has no attribute 'build_operator_digest'`.
- [ ] **Step 1.3 — minimal implementation.** Append to `fulcra_coord/views.py` (after `build_presence`):
  ```python
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
                            since: Optional[datetime] = None) -> dict[str, Any]:
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
          "now": now_dt.isoformat().replace("+00:00", "Z"),
          "since": since_dt.isoformat().replace("+00:00", "Z"),
          "blocked_on_you": blocked,
          "upcoming": upcoming,
          "per_agent": per_agent,
          "stale": stale,
      }
  ```
  > Note: `is_stale` reads only `status` + `updated_at`, both on a summary, so it works on summaries unchanged (same as `build_needs_attention`).
- [ ] **Step 1.4 — run it (expected PASS):** `uv run --extra dev python -m pytest tests/test_operator_digest.py::TestBuildOperatorDigestEmpty -v`
- [ ] **Step 1.5 — commit:** `git add fulcra_coord/views.py tests/test_operator_digest.py && git commit -m "feat(digest): build_operator_digest pure fold (empty-state)

The operator-digest read model: a pure function over task summaries + presence
that produces the four situational-awareness blocks (blocked_on_you / upcoming /
per_agent / stale). This first commit lands the function with the empty-state
contract; ranking and since-window behaviour follow in the next steps.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"`

- [ ] **Step 1.6 — failing test: blocked_on_you ranks due-soonest then oldest.**
  Add `class TestBlockedRanking`:
  ```python
  class TestBlockedRanking(unittest.TestCase):
      def test_due_soonest_then_oldest_age(self):
          # Three blocked-on-user asks: B due first, A&C undated; among undated,
          # oldest updated_at leads. needs:human tag makes them blocked-on-user.
          a = _summary(id="A", status="blocked", tags=["needs:human"],
                       updated_at="2026-06-04T09:00:00Z", due=None)
          b = _summary(id="B", status="blocked", tags=["needs:human"],
                       updated_at="2026-06-04T17:00:00Z",
                       due="2026-06-05T00:00:00Z")
          c = _summary(id="C", status="blocked", tags=["needs:human"],
                       updated_at="2026-06-04T08:00:00Z", due=None)
          d = views.build_operator_digest([a, b, c], [], human="ash",
                                          now=NOW, since=SINCE)
          self.assertEqual([s["id"] for s in d["blocked_on_you"]], ["B", "C", "A"])
  ```
- [ ] **Step 1.7 — run (expected FAIL):** `uv run --extra dev python -m pytest tests/test_operator_digest.py::TestBlockedRanking -v` — expected: order is wrong (needs_human returns oldest-first `["C","A","B"]`) because `_digest_due_key` re-sort is the thing under test; **this passes already given the Step 1.3 implementation**, so instead assert it fails first by temporarily expecting the WRONG order is NOT produced. (If it already passes, that confirms 1.3 — keep the correct-order assertion and proceed; do not weaken the test.)
  > Practical note for the executor: because Step 1.3 already implements the ranking, write Step 1.6's test to encode the CORRECT order and treat a green run as confirmation. TDD discipline is preserved across the *task* (1.1 was red); within a task, a verifying test that passes on first run against freshly-written code is acceptable when it pins an exact, non-trivial ordering contract.
- [ ] **Step 1.8 — run (expected PASS):** `uv run --extra dev python -m pytest tests/test_operator_digest.py::TestBlockedRanking -v`
- [ ] **Step 1.9 — commit:** `git add tests/test_operator_digest.py && git commit -m "test(digest): pin blocked_on_you ranking (due soonest, then oldest)

Locks the situational-awareness ordering the human relies on: the most-urgent
ask (soonest due) leads, and undated asks fall back to longest-waiting-first.
Comparison is parsed-datetime (PR #39), never lexical on mixed-precision ISO-Z.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"`

- [ ] **Step 1.10 — failing test: per_agent lists finished-since + carries liveness; upcoming/stale populate.**
  Add `class TestPerAgentAndWindows`:
  ```python
  class TestPerAgentAndWindows(unittest.TestCase):
      def test_finished_since_filters_by_done_at(self):
          recent = _summary(id="R", status="done", owner_agent="claude-code:mb:repo",
                            done_at="2026-06-04T12:00:00Z")           # after SINCE
          old = _summary(id="O", status="done", owner_agent="claude-code:mb:repo",
                         done_at="2026-06-03T12:00:00Z")              # before SINCE
          presence = [{"agent": "claude-code:mb:repo",
                       "workstreams": ["devops"], "summary": "shipping",
                       "last_seen": "2026-06-04T17:55:00Z"}]
          d = views.build_operator_digest([recent, old], presence, human="ash",
                                          now=NOW, since=SINCE)
          self.assertEqual(len(d["per_agent"]), 1)
          entry = d["per_agent"][0]
          self.assertEqual(entry["liveness"], "live")
          self.assertEqual([s["id"] for s in entry["finished_since"]], ["R"])

      def test_upcoming_and_stale_blocks(self):
          # upcoming: future not_before within 7d, blocked-on-user.
          up = _summary(id="U", status="waiting", tags=["needs:human"],
                        not_before="2026-06-06T00:00:00Z")
          # stale: active, updated_at older than the 2h default threshold.
          st = _summary(id="S", status="active", updated_at="2026-06-04T10:00:00Z")
          d = views.build_operator_digest([up, st], [], human="ash",
                                          now=NOW, since=SINCE)
          self.assertEqual([s["id"] for s in d["upcoming"]], ["U"])
          self.assertEqual([s["id"] for s in d["stale"]], ["S"])
  ```
- [ ] **Step 1.11 — run (expected PASS, confirms full fold):** `uv run --extra dev python -m pytest tests/test_operator_digest.py -v`
- [ ] **Step 1.12 — commit:** `git add tests/test_operator_digest.py && git commit -m "test(digest): per_agent finished-since window + upcoming/stale blocks

Confirms the since-window completion filter (done_at parsed vs since), the
per-agent liveness annotation, and that upcoming/stale reuse the existing
needs_human / upcoming_for_human / is_stale read-model exactly.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"`

---

### Task 2: `cli._render_digest` — structured digest → (name, note)

Pure render. Skips empty blocks; caps long lists with "+N more"; never crashes on
missing fields. The `name` is the timeline label; the `note` is the markdown-ish body.

- [ ] **Step 2.1 — failing test: full digest renders name + all four sections.**
  Add `class TestRenderDigest` to `tests/test_operator_digest.py`:
  ```python
  from fulcra_coord import cli

  class TestRenderDigest(unittest.TestCase):
      def _full_digest(self):
          return {
              "schema": "fulcra.coordination.operator_digest.v1",
              "human": "ash", "now": NOW.isoformat().replace("+00:00", "Z"),
              "since": SINCE.isoformat().replace("+00:00", "Z"),
              "blocked_on_you": [
                  _summary(id="B1", title="Re-auth GitHub", status="blocked",
                           owner_agent="claude-code:mb:repo",
                           blocked_on="approve the OAuth scope"),
                  _summary(id="B2", title="Review PR", status="waiting",
                           owner_agent="codex:mb:main"),
              ],
              "upcoming": [_summary(id="U1", title="Rotate key",
                                    not_before="2026-06-06T00:00:00Z")],
              "per_agent": [{
                  "agent": "claude-code:mb:repo", "workstreams": ["devops"],
                  "summary": "shipping the digest", "liveness": "live",
                  "finished_since": [_summary(id="F1", title="Land annotations",
                                              status="done")],
              }],
              "stale": [_summary(id="S1", title="Old churn", status="active")],
          }

      def test_name_summarizes_counts(self):
          name, note = cli._render_digest(self._full_digest(), window="evening")
          self.assertIn("evening", name)
          self.assertIn("2 on you", name)
          self.assertIn("1 upcoming", name)

      def test_note_has_all_sections(self):
          _, note = cli._render_digest(self._full_digest(), window="evening")
          self.assertIn("Re-auth GitHub", note)
          self.assertIn("approve the OAuth scope", note)
          self.assertIn("Rotate key", note)
          self.assertIn("claude-code:mb:repo", note)
          self.assertIn("Land annotations", note)
          self.assertIn("Old churn", note)
  ```
- [ ] **Step 2.2 — run (expected FAIL):** `uv run --extra dev python -m pytest tests/test_operator_digest.py::TestRenderDigest -v` — `AttributeError: module 'fulcra_coord.cli' has no attribute '_render_digest'`.
- [ ] **Step 2.3 — minimal implementation.** Add to `fulcra_coord/cli.py` (near the other read helpers, after `_age_str`/`_until_str`):
  ```python
  #: Max items rendered per digest block before collapsing the tail into "+N more".
  #: Keeps the timeline note bounded (a 284-event-in-two-days bus could otherwise
  #: produce a wall of text) while always showing the most-salient head of each list.
  _DIGEST_BLOCK_CAP = 8


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

      note = "\n".join(sections).strip()
      return name, note
  ```
- [ ] **Step 2.4 — run (expected PASS):** `uv run --extra dev python -m pytest tests/test_operator_digest.py::TestRenderDigest -v`
- [ ] **Step 2.5 — failing test: empty digest renders a clean name + "all clear" note; missing fields don't crash; long lists cap.**
  Add to `TestRenderDigest`:
  ```python
      def test_empty_digest_is_clean(self):
          empty = {"blocked_on_you": [], "upcoming": [], "per_agent": [], "stale": []}
          name, note = cli._render_digest(empty, window="morning")
          self.assertIn("0 on you", name)
          self.assertEqual(note, "")  # no empty section headers

      def test_missing_fields_do_not_crash(self):
          # A digest with a sparse summary (only id/status) must still render.
          d = {"blocked_on_you": [{"id": "Z", "status": "blocked"}],
               "upcoming": [], "per_agent": [], "stale": []}
          name, note = cli._render_digest(d, window="morning")
          self.assertIn("1 on you", name)

      def test_long_block_caps_with_more(self):
          many = [_summary(id=f"B{i}", title=f"ask {i}", status="blocked")
                  for i in range(12)]
          d = {"blocked_on_you": many, "upcoming": [], "per_agent": [], "stale": []}
          _, note = cli._render_digest(d, window="evening")
          self.assertIn("…and 4 more", note)
  ```
- [ ] **Step 2.6 — run (expected PASS):** `uv run --extra dev python -m pytest tests/test_operator_digest.py::TestRenderDigest -v`
- [ ] **Step 2.7 — commit:** `git add fulcra_coord/cli.py tests/test_operator_digest.py && git commit -m "feat(digest): _render_digest — structured digest to timeline (name, note)

Renders the four-block digest into a concise timeline label plus a compact,
bounded body. Empty blocks are skipped, long lists collapse to '+N more', and
every field is read defensively so a sparse summary can never crash the
best-effort writer.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"`

---

### Task 3: "Agent Tasks — Digest" second annotation track + `emit_digest_annotation`

Reuses the proven `_write_http` path but targets a SECOND moment definition with
its own cached id, so the digest track never collides with the per-event "Agent Tasks" track.

- [ ] **Step 3.1 — failing test: emit_digest_annotation resolves the Digest definition + posts the record (HTTP mocked).**
  Add `class TestEmitDigestAnnotation` to `tests/test_operator_digest.py`:
  ```python
  import io
  import urllib.error
  from fulcra_coord import annotations


  class _FakeResp:
      def __init__(self, body, status=200):
          if isinstance(body, (dict, list)):
              body = json.dumps(body).encode()
          elif isinstance(body, str):
              body = body.encode()
          self._body = body or b""
          self.status = status
      def read(self): return self._body
      def __enter__(self): return self
      def __exit__(self, *a): return False


  class TestEmitDigestAnnotation(unittest.TestCase):
      def setUp(self):
          self.tmp = tempfile.mkdtemp()
          os.environ["XDG_CACHE_HOME"] = self.tmp
          self._saved = {k: os.environ.get(k) for k in
                         ("FULCRA_ACCESS_TOKEN", "FULCRA_API_BASE",
                          "FULCRA_COORD_REMOTE_ROOT")}
          os.environ["FULCRA_ACCESS_TOKEN"] = "tkn-abc"
          os.environ["FULCRA_API_BASE"] = "https://api.example.test"
          os.environ["FULCRA_COORD_REMOTE_ROOT"] = "/coordination-digesttest"

      def tearDown(self):
          os.environ.pop("XDG_CACHE_HOME", None)
          for k, v in self._saved.items():
              os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

      def test_writes_against_digest_definition(self):
          calls = []
          def fake_urlopen(req, *a, **k):
              method, url = req.get_method(), req.full_url
              calls.append((method, url, req.data))
              if method == "GET" and "/tag/name/" in url:
                  raise urllib.error.HTTPError(url, 404, "nf", None, io.BytesIO(b""))
              if method == "POST" and url.endswith("/user/v1alpha1/tag"):
                  return _FakeResp({"id": "tag-1"})
              if method == "GET" and url.endswith("/user/v1alpha1/annotation"):
                  return _FakeResp([])           # no existing defs -> create
              if method == "POST" and url.endswith("/user/v1alpha1/annotation"):
                  return _FakeResp({"id": "digest-def-1"})
              if method == "POST" and "/ingest/v1/record/batch" in url:
                  return _FakeResp(b"", status=202)
              raise AssertionError(f"unrouted: {method} {url}")
          with patch("urllib.request.urlopen", side_effect=fake_urlopen):
              ok = annotations.emit_digest_annotation(
                  name="Agent digest — evening (1 on you, 0 upcoming)",
                  note="⛔ Blocked on you (1):\n  • thing",
                  window="evening", agent="claude-code:mb:repo")
          self.assertTrue(ok)
          # The definition POST carried the DIGEST definition name, not "Agent Tasks".
          def_posts = [c for c in calls
                       if c[0] == "POST" and c[1].endswith("/user/v1alpha1/annotation")]
          self.assertEqual(len(def_posts), 1)
          self.assertIn(annotations.DIGEST_DEFINITION_NAME,
                        def_posts[0][2].decode())
          # The digest definition id was cached separately from "Agent Tasks".
          self.assertEqual(annotations._cached_digest_definition_id(), "digest-def-1")

      def test_best_effort_returns_false_on_no_token(self):
          os.environ.pop("FULCRA_ACCESS_TOKEN", None)
          with patch.object(annotations, "_resolve_token", return_value=None):
              ok = annotations.emit_digest_annotation(
                  name="n", note="b", window="morning", agent="claude-code:mb:repo")
          self.assertFalse(ok)
  ```
- [ ] **Step 3.2 — run (expected FAIL):** `uv run --extra dev python -m pytest tests/test_operator_digest.py::TestEmitDigestAnnotation -v` — `AttributeError: ... has no attribute 'emit_digest_annotation'` / `DIGEST_DEFINITION_NAME`.
- [ ] **Step 3.3 — minimal implementation.** Add to `fulcra_coord/annotations.py`. First the constants + digest definition cache (mirror `_definition_cache_path` / `_cached_definition_id` / `_store_definition_id`, but a SEPARATE file `digest-definition.json`):
  ```python
  #: The digest track's own moment definition — distinct from DEFINITION_NAME
  #: ("Agent Tasks") so the human-paced digest moments filter SEPARATELY from the
  #: granular per-event lifecycle moments (the per-event track is kept, untouched).
  DIGEST_DEFINITION_NAME = "Agent Tasks — Digest"
  DIGEST_DEFINITION_DESCRIPTION = (
      "Twice-daily + on-demand operator situational-awareness digests "
      "(what's blocked on you, upcoming, what each agent did, what's stale)."
  )
  #: Track tag shared by every digest moment, so the operator can pull up exactly
  #: their digests on the Fulcra timeline.
  DIGEST_TRACK_TAG = "agent-digest"


  def _digest_definition_cache_path():
      """Path to the cached ``Agent Tasks — Digest`` definition-id json.

      A SEPARATE file from ``_definition_cache_path`` (which caches the per-event
      "Agent Tasks" def): the two tracks are independent definitions, so caching
      both ids in one file would let one clobber the other. Same per-root cache
      dir so it's isolated per remote root like every other annotation handle."""
      return cache.annotations_dir() / "digest-definition.json"


  def _cached_digest_definition_id() -> Optional[str]:
      path = _digest_definition_cache_path()
      try:
          if path.exists():
              did = json.loads(path.read_text()).get("id")
              if did:
                  return did
      except (OSError, json.JSONDecodeError):
          pass
      return None


  def _store_digest_definition_id(def_id: str) -> None:
      """Persist the resolved digest definition id (best-effort; a write failure
      just re-resolves next time, never a failed annotation)."""
      try:
          cache.annotations_dir().mkdir(parents=True, exist_ok=True)
          _digest_definition_cache_path().write_text(json.dumps({"id": def_id}))
      except OSError:
          pass


  def _resolve_digest_definition_id(token: str, tag_ids: list[str]) -> str:
      """Return the ``Agent Tasks — Digest`` moment-definition id (resolve once + cache).

      Same resolve/create dance as ``_resolve_definition_id`` but matched on
      ``DIGEST_DEFINITION_NAME`` and cached in the digest-specific file, so the two
      tracks converge on two distinct definitions across machines."""
      cached = _cached_digest_definition_id()
      if cached:
          return cached
      base = _api_base()
      _, raw = _request("GET", f"{base}/user/v1alpha1/annotation", token)
      for d in json.loads(raw) or []:
          if d.get("name") == DIGEST_DEFINITION_NAME and not d.get("deleted_at"):
              _store_digest_definition_id(d["id"])
              return d["id"]
      body = json.dumps({
          "annotation_type": "moment",
          "name": DIGEST_DEFINITION_NAME,
          "description": DIGEST_DEFINITION_DESCRIPTION,
          "tags": tag_ids,
      }).encode()
      _, raw = _request("POST", f"{base}/user/v1alpha1/annotation", token, body=body)
      def_id = json.loads(raw)["id"]
      _store_digest_definition_id(def_id)
      return def_id
  ```
  Then the public emitter (a `_write_http`-shaped flow that swaps in the digest definition resolver):
  ```python
  def emit_digest_annotation(*, name: str, note: str, window: str, agent: str,
                             backend: Optional[list[str]] = None) -> bool:
      """Emit ONE operator-digest moment on the ``Agent Tasks — Digest`` track.

      BEST-EFFORT, NEVER RAISES (same contract as emit_lifecycle_annotation): a
      slow/missing/broken timeline write must never break — or even slow — the
      scheduled digest tick. Returns True only when a moment was actually written.

      Reuses the proven HTTP path (tag resolve/create -> definition resolve/create
      -> JSONL record POST) but against ``_resolve_digest_definition_id`` so the
      digest lands on its OWN track, never the per-event "Agent Tasks" one. Tags:
      ``[agent-digest, <window>, agent:<kind>]``. Honours the same gating as the
      lifecycle writer (off unless FULCRA_COORD_ANNOTATIONS / persisted mode is on)
      so a machine that hasn't opted in stays inert. No idempotency marker here —
      the per-window DEDUP GUARD (cli, Task 5) is what prevents a double digest."""
      try:
          if _mode() == "off":
              return False
          token = _resolve_token()
          if not token:
              return False
          kind = agent_kind(agent)
          tag_names = [DIGEST_TRACK_TAG, window, f"agent:{kind}"]
          tag_ids = [_resolve_tag_id(n, token) for n in tag_names if n]
          def_id = _resolve_digest_definition_id(token, tag_ids)

          inner: dict[str, Any] = {}
          if name.strip():
              inner["title"] = name.strip()
          if note.strip():
              inner["note"] = note.strip()
          source = [
              f"com.fulcradynamics.fulcra-coord.digest.{uuid.uuid4()}",
              f"com.fulcradynamics.annotation.{def_id}",
          ]
          record = {
              "specversion": 1,
              "data": json.dumps(inner, sort_keys=True),
              "metadata": {
                  "data_type": "MomentAnnotation",
                  "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                  "tags": tag_ids,
                  "source": source,
                  "content_type": "application/json",
              },
          }
          body = (json.dumps(record, sort_keys=True) + "\n").encode()
          _request("POST", f"{_api_base()}/ingest/v1/record/batch", token,
                   body=body, content_type="application/x-jsonl")
          return True
      except Exception:
          # Best-effort: a timeline write must be invisible to the scheduled tick.
          return False
  ```
- [ ] **Step 3.4 — run (expected PASS):** `uv run --extra dev python -m pytest tests/test_operator_digest.py::TestEmitDigestAnnotation -v`
- [ ] **Step 3.5 — commit:** `git add fulcra_coord/annotations.py tests/test_operator_digest.py && git commit -m "feat(digest): emit_digest_annotation on a separate 'Agent Tasks — Digest' track

A second moment definition with its own cached id, so the human-paced digest
filters separately from the granular per-event lifecycle track (which is kept,
untouched). Reuses the proven _write_http flow (tag/def resolve -> JSONL POST),
honours the same opt-in gating, and is best-effort: a timeline write never
breaks a scheduled digest tick.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"`

---

### Task 4: the `digest` command + COMMAND_MAP wiring

Resolves the human handle; loads summaries + presence; computes `since`/`now`;
builds + renders + (unless `--dry-run`) emits. `--format json` prints the
structured digest. The dedup guard is wired in Task 5 — this task emits unconditionally first.

- [ ] **Step 4.1 — failing test: `digest --dry-run` renders, writes nothing; `--format json` prints the structured digest; a real run calls emit.**
  Add `class TestDigestCommand` to `tests/test_operator_digest.py`:
  ```python
  from fulcra_coord import entry

  class TestDigestCommand(unittest.TestCase):
      def setUp(self):
          self.tmp = tempfile.mkdtemp()
          os.environ["XDG_CACHE_HOME"] = self.tmp
          self.summaries = [
              _summary(id="B1", title="Re-auth", status="blocked",
                       tags=["needs:human"], owner_agent="claude-code:mb:repo"),
          ]
          self.presence = {"agents": [{"agent": "claude-code:mb:repo",
                                       "workstreams": ["devops"], "summary": "x",
                                       "last_seen": "2026-06-04T17:55:00Z"}]}

      def tearDown(self):
          os.environ.pop("XDG_CACHE_HOME", None)

      def _args(self, **over):
          ns = types.SimpleNamespace(window="evening", format="table",
                                     dry_run=False, human="ash")
          for k, v in over.items():
              setattr(ns, k, v)
          return ns

      def test_dry_run_writes_nothing(self):
          with patch("fulcra_coord.cli._load_task_summaries", return_value=self.summaries), \
               patch("fulcra_coord.cli.remote.download_json", return_value=self.presence), \
               patch("fulcra_coord.cli.lifecycle_annotations.emit_digest_annotation") as emit:
              rc = cli.cmd_digest(self._args(dry_run=True), backend=["false"])
          self.assertEqual(rc, 0)
          emit.assert_not_called()

      def test_real_run_emits(self):
          with patch("fulcra_coord.cli._load_task_summaries", return_value=self.summaries), \
               patch("fulcra_coord.cli.remote.download_json", return_value=self.presence), \
               patch("fulcra_coord.cli._claim_digest_marker", return_value=True), \
               patch("fulcra_coord.cli.lifecycle_annotations.emit_digest_annotation",
                     return_value=True) as emit:
              rc = cli.cmd_digest(self._args(), backend=["false"])
          self.assertEqual(rc, 0)
          emit.assert_called_once()
          _, kw = emit.call_args
          self.assertEqual(kw["window"], "evening")
          self.assertIn("on you", kw["name"])

      def test_json_format_prints_structured_digest(self):
          import io, contextlib
          buf = io.StringIO()
          with patch("fulcra_coord.cli._load_task_summaries", return_value=self.summaries), \
               patch("fulcra_coord.cli.remote.download_json", return_value=self.presence), \
               contextlib.redirect_stdout(buf):
              rc = cli.cmd_digest(self._args(format="json"), backend=["false"])
          self.assertEqual(rc, 0)
          payload = json.loads(buf.getvalue())
          self.assertEqual(payload["schema"], "fulcra.coordination.operator_digest.v1")
          self.assertEqual([s["id"] for s in payload["blocked_on_you"]], ["B1"])

      def test_command_is_wired_into_map(self):
          self.assertIs(entry.COMMAND_MAP["digest"], cli.cmd_digest)
  ```
- [ ] **Step 4.2 — run (expected FAIL):** `uv run --extra dev python -m pytest tests/test_operator_digest.py::TestDigestCommand -v` — `AttributeError: ... has no attribute 'cmd_digest'` (and the map assertion fails).
- [ ] **Step 4.3 — minimal implementation (command + window/since helper).** Add to `fulcra_coord/cli.py`:
  ```python
  def _digest_window_since(window: str, now: datetime) -> datetime:
      """The lookback boundary for a digest window (returns a tz-aware UTC datetime).

      morning → since the previous evening (~last 14h, so an overnight run still
      reports yesterday-evening's work); evening → since this morning (~last 10h);
      any other value (on-demand) → last 12h. Approximations on purpose: the digest
      is a human-paced glance, not an exact ledger, and the per_agent completion
      filter is a >= compare against this instant. Always parsed datetimes."""
      hours = {"morning": 14, "evening": 10}.get(window, 12)
      return now - timedelta(hours=hours)


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

      digest = views.build_operator_digest(
          summaries, presence, human=human, now=now, since=since)

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
  ```
  Add the import-time stub for `_claim_digest_marker` so this task is runnable on its own (Task 5 replaces the body with the real bus-marker logic):
  ```python
  def _claim_digest_marker(window: str, now: datetime, *,
                           backend: Optional[list[str]] = None) -> bool:
      """Claim the per-window digest marker (real implementation in Task 5).

      Stubbed to always grant the claim so the command is testable in isolation;
      Task 5 replaces this with the Files-bus first-writer-wins guard."""
      return True
  ```
  Wire `entry.py` — add the subparser after `install-listener`/`notify-inbox` block:
  ```python
      # ---- digest ----
      sp = sub.add_parser("digest",
                          help="Write the operator situational-awareness digest "
                               "(blocked on you / upcoming / per-agent / stale) to "
                               "the Fulcra timeline on its own 'Agent Tasks — Digest' track")
      sp.add_argument("--window", choices=["morning", "evening"], default=None,
                      help="Cadence window (sets the lookback + label); omit for on-demand")
      sp.add_argument("--human", default=None, metavar="HANDLE",
                      help="Whose plate (default: $FULCRA_COORD_HUMAN or persisted handle)")
      sp.add_argument("--format", choices=["table", "json"], default="table")
      sp.add_argument("--dry-run", dest="dry_run", action="store_true",
                      help="Render + print the digest, write nothing to the timeline")
  ```
  And add to `COMMAND_MAP`:
  ```python
      "digest": _cli.cmd_digest,
  ```
- [ ] **Step 4.4 — run (expected PASS):** `uv run --extra dev python -m pytest tests/test_operator_digest.py::TestDigestCommand -v`
- [ ] **Step 4.5 — commit:** `git add fulcra_coord/cli.py fulcra_coord/entry.py tests/test_operator_digest.py && git commit -m "feat(digest): 'digest' command + COMMAND_MAP wiring

Adds 'fulcra-coord digest [--window morning|evening] [--format json] [--dry-run]':
loads summaries + presence, computes the window lookback, builds + renders the
four-block digest, and (unless dry-run / json) emits the moment on the digest
track. The dedup-marker claim is stubbed here and made real in the next task.
Best-effort end to end — a scheduled tick never errors.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"`

---

### Task 5: any-agent dedup marker guard (`digest/markers/<date>-<window>.json`)

Replaces the Task 4 stub with the real Files-bus first-writer-wins guard.
Documents (and accepts) the same-second race; tests the sequential first-wins path.

- [ ] **Step 5.1 — failing test: absent marker → claim writes it + grants; present marker → no-op; download/upload error → skip.**
  Add `class TestDigestMarker` to `tests/test_operator_digest.py`:
  ```python
  class TestDigestMarker(unittest.TestCase):
      def setUp(self):
          self.now = datetime(2026, 6, 4, 18, 0, 0, tzinfo=timezone.utc)

      def test_absent_marker_is_claimed_and_written(self):
          uploaded = {}
          def fake_download_json(path, *, backend=None, timeout=None):
              return None  # marker absent
          def fake_upload_json(data, path, *, backend=None, timeout=None):
              uploaded["path"] = path
              uploaded["data"] = data
              return True
          with patch("fulcra_coord.cli.remote.download_json", side_effect=fake_download_json), \
               patch("fulcra_coord.cli.remote.upload_json", side_effect=fake_upload_json):
              granted = cli._claim_digest_marker("evening", self.now, backend=["false"])
          self.assertTrue(granted)
          self.assertTrue(uploaded["path"].endswith("digest/markers/2026-06-04-evening.json"))
          self.assertEqual(uploaded["data"]["window"], "evening")

      def test_present_marker_is_noop(self):
          with patch("fulcra_coord.cli.remote.download_json",
                     return_value={"window": "evening", "by": "codex:mb:main"}), \
               patch("fulcra_coord.cli.remote.upload_json") as up:
              granted = cli._claim_digest_marker("evening", self.now, backend=["false"])
          self.assertFalse(granted)
          up.assert_not_called()

      def test_upload_failure_skips(self):
          with patch("fulcra_coord.cli.remote.download_json", return_value=None), \
               patch("fulcra_coord.cli.remote.upload_json", return_value=False):
              granted = cli._claim_digest_marker("evening", self.now, backend=["false"])
          self.assertFalse(granted)  # don't risk a double on a failed claim
  ```
- [ ] **Step 5.2 — run (expected FAIL):** `uv run --extra dev python -m pytest tests/test_operator_digest.py::TestDigestMarker -v` — the stub returns `True` unconditionally, so `test_present_marker_is_noop` and `test_upload_failure_skips` FAIL.
- [ ] **Step 5.3 — replace the stub.** In `fulcra_coord/cli.py`, swap the Task-4 `_claim_digest_marker` body for the real one, and add the path helper. Use the package-level `remote_root()` import (already available via `from . import ... remote`; the marker path is built from `remote.remote_root()`):
  ```python
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
              "claimed_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
          }
          return bool(remote.upload_json(marker, path, backend=backend))
      except Exception:
          # Best-effort: a marker error must never raise into a scheduled tick, and
          # must skip (not write) so we never risk a double on an uncertain claim.
          return False
  ```
- [ ] **Step 5.4 — run (expected PASS):** `uv run --extra dev python -m pytest tests/test_operator_digest.py::TestDigestMarker tests/test_operator_digest.py::TestDigestCommand -v`
- [ ] **Step 5.5 — commit:** `git add fulcra_coord/cli.py tests/test_operator_digest.py && git commit -m "feat(digest): any-agent first-writer-wins dedup marker on the Files bus

Before emitting, a digest tick claims digest/markers/<date>-<window>.json: if it
already exists, another agent wrote this window, so skip; if absent, write the
marker and proceed. A download/upload error skips (never risk a double). The
same-second no-CAS race is documented and accepted (a rare, harmless double
timeline entry) per the any-agent decision — no single point of failure.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"`

---

### Task 6: `install-digest` scheduler (calendar-based: 08:00 morning, 18:00 evening)

Mirrors `heartbeat.install_heartbeat` but **calendar-scheduled**, not interval:
launchd `StartCalendarInterval` (a list of two time-of-day entries running
`digest --window morning` at 08:00 and `digest --window evening` at 18:00), and
on non-macOS two managed cron lines. The dedup guard makes concurrent installs safe.

- [ ] **Step 6.1 — failing test: launchd plist carries two calendar entries + both windows; cron writes two managed lines; uninstall removes; dry-run writes nothing.**
  Add `class TestInstallDigest` to `tests/test_operator_digest.py`:
  ```python
  import plistlib
  from fulcra_coord import digest_schedule

  class TestInstallDigest(unittest.TestCase):
      def setUp(self):
          self.tmp = Path(tempfile.mkdtemp())

      def test_launchd_plist_has_both_windows_and_calendar(self):
          if not digest_schedule.scheduler_env.is_macos():
              self.skipTest("launchd path is macOS-only")
          plan = digest_schedule.install_digest(
              target_dir=self.tmp, logs_dir=self.tmp / "logs")
          self.assertEqual(plan["mechanism"], "launchd")
          # Two plists, one per window.
          names = sorted(Path(p).name for p in plan["writes"])
          self.assertEqual(names, ["com.fulcra.coord.digest.evening.plist",
                                   "com.fulcra.coord.digest.morning.plist"])
          morning = plistlib.loads(
              (self.tmp / "com.fulcra.coord.digest.morning.plist").read_bytes())
          self.assertIn("digest", morning["ProgramArguments"])
          self.assertIn("morning", morning["ProgramArguments"])
          self.assertEqual(morning["StartCalendarInterval"], {"Hour": 8, "Minute": 0})
          evening = plistlib.loads(
              (self.tmp / "com.fulcra.coord.digest.evening.plist").read_bytes())
          self.assertEqual(evening["StartCalendarInterval"], {"Hour": 18, "Minute": 0})

      def test_dry_run_writes_nothing(self):
          plan = digest_schedule.install_digest(
              target_dir=self.tmp, logs_dir=self.tmp / "logs", dry_run=True)
          self.assertTrue(plan["writes"])
          self.assertFalse(any(Path(p).exists() for p in plan["writes"]))

      def test_cron_has_two_managed_lines(self):
          cron = self.tmp / "cron.txt"
          plan = digest_schedule.install_digest(crontab_path=cron, force_cron=True)
          text = cron.read_text()
          self.assertIn("0 8 * * *", text)
          self.assertIn("0 18 * * *", text)
          self.assertIn("--window morning", text)
          self.assertIn("--window evening", text)
          self.assertEqual(text.count(digest_schedule.CRON_MARKER), 2)

      def test_cron_uninstall_is_surgical(self):
          cron = self.tmp / "cron.txt"
          cron.write_text("# my own job\n*/5 * * * * echo hi\n")
          digest_schedule.install_digest(crontab_path=cron, force_cron=True)
          digest_schedule.install_digest(crontab_path=cron, force_cron=True, uninstall=True)
          text = cron.read_text()
          self.assertIn("echo hi", text)
          self.assertNotIn(digest_schedule.CRON_MARKER, text)
  ```
  > `force_cron=True` is a test-only seam (forces the crontab branch off-macOS-or-on) so the cron path is exercised on a macOS dev box. Mirrors how the other installers rely on `scheduler_env.is_macos()`; here we add an explicit override param so both branches are testable regardless of host.
- [ ] **Step 6.2 — run (expected FAIL):** `uv run --extra dev python -m pytest tests/test_operator_digest.py::TestInstallDigest -v` — `ModuleNotFoundError: No module named 'fulcra_coord.digest_schedule'`.
- [ ] **Step 6.3 — minimal implementation.** Create `fulcra_coord/digest_schedule.py`:
  ```python
  """Scheduled operator-digest installer (calendar-based, twice daily).

  WHY a NEW module (vs reusing heartbeat.py): the heartbeat is INTERVAL-scheduled
  (StartInterval / */N cron) — "every 20 min". The digest is CALENDAR-scheduled —
  "at 08:00 and at 18:00, local". launchd expresses that with StartCalendarInterval
  (not StartInterval), and cron with fixed ``M H * * *`` lines (not ``*/N``), so the
  plist/cron shapes genuinely differ. We DO reuse scheduler_env (the #25 PATH +
  log-paths hardening) and cli_invocation (resolve_cli_argv) verbatim, so the
  scheduled command resolves under uv-tool / source installs exactly like the
  heartbeat/listener jobs.

  Two windows -> two jobs: ``digest --window morning`` at 08:00 and
  ``digest --window evening`` at 18:00. Installable on any/all machines; the
  any-agent dedup marker (cli._claim_digest_marker) makes concurrent installs safe
  (first writer wins, others no-op). Contract mirrors the other installers:
  idempotent, dry-run writes nothing, surgical uninstall, fail-safe, stdlib-only.
  ``target_dir`` / ``crontab_path`` / ``logs_dir`` are overridable so tests never
  touch the real scheduler or ~/Library.
  """
  from __future__ import annotations

  import plistlib
  import shlex
  from pathlib import Path
  from typing import Any

  from . import cli_invocation
  from . import scheduler_env

  LABEL_PREFIX = "com.fulcra.coord.digest"
  LOG_STEM = "digest"
  CRON_MARKER = "# fulcra-coord-digest (managed; do not edit this line)"

  #: The two cadence windows and their wall-clock time-of-day (local). 08:00 / 18:00
  #: per the spec; the dedup marker is keyed by UTC date so a slightly different
  #: local fire time across machines still collapses to one digest per window.
  WINDOWS = (("morning", 8, 0), ("evening", 18, 0))


  def _label_for(window: str) -> str:
      return f"{LABEL_PREFIX}.{window}"


  def _plist_name_for(window: str) -> str:
      return f"{_label_for(window)}.plist"


  def _digest_args(window: str) -> list[str]:
      """The subcommand tail: write this window's digest. One call per job."""
      return ["digest", "--window", window]


  def _plist_body(argv: list[str], window: str, hour: int, minute: int,
                  logs_dir: Path) -> str:
      """A launchd plist running ``<argv...> digest --window <window>`` at a fixed
      time of day via StartCalendarInterval (NOT StartInterval — this is a
      calendar job, not an interval one). Built via plistlib so a spaced argv[0]
      survives and values are XML-escaped. #25 hardening: bakes the PATH + log
      paths so launchd's bare env can find the binary and a failure leaves a log."""
      out_path, err_path = scheduler_env.log_paths(logs_dir, f"{LOG_STEM}.{window}")
      body: dict[str, Any] = {
          "Label": _label_for(window),
          "ProgramArguments": list(argv) + _digest_args(window),
          "StartCalendarInterval": {"Hour": hour, "Minute": minute},
          "EnvironmentVariables": {"PATH": scheduler_env.scheduler_path(argv)},
          "StandardOutPath": out_path,
          "StandardErrorPath": err_path,
      }
      return plistlib.dumps(body).decode("utf-8")


  def _cron_line(argv: list[str], window: str, hour: int, minute: int) -> str:
      """A crontab entry running the window's digest at ``minute hour * * *``,
      tagged with the managed marker so uninstall is surgical. argv is shell-quoted
      token-by-token (a spaced path stays one word); #25 PATH prefix so cron's
      minimal PATH can find the binary."""
      schedule = f"{minute} {hour} * * *"
      path = shlex.quote(scheduler_env.scheduler_path(argv))
      cmd = " ".join(shlex.quote(t) for t in (list(argv) + _digest_args(window)))
      return f"{CRON_MARKER}\n{schedule} PATH={path} {cmd} >/dev/null 2>&1\n"


  def _is_managed_cron_command(line: str) -> bool:
      """True when a crontab line (after the marker) is one of OUR managed digest
      entries — a ``M H * * * <argv> digest --window <w>`` command with our
      redirection suffix. Lets uninstall drop only genuinely-ours lines and
      preserve an unrelated user job that happens to follow a stray marker."""
      s = line.rstrip("\n")
      return (" digest " in f" {s} "
              and " --window " in f" {s} "
              and s.rstrip().endswith(">/dev/null 2>&1"))


  def _strip_managed_cron(text: str) -> str:
      """Remove every managed marker line and the managed digest command that
      follows it (M2: only when that next line is genuinely ours). Every
      user-owned line is preserved — surgical uninstall."""
      out: list[str] = []
      lines = text.splitlines(keepends=True)
      i = 0
      while i < len(lines):
          if lines[i].rstrip("\n") == CRON_MARKER:
              if i + 1 < len(lines) and _is_managed_cron_command(lines[i + 1]):
                  i += 2
              else:
                  i += 1
              continue
          out.append(lines[i])
          i += 1
      return "".join(out)


  def install_digest(*, uninstall: bool = False, dry_run: bool = False,
                     target_dir: "str | Path | None" = None,
                     crontab_path: "str | Path | None" = None,
                     logs_dir: "str | Path | None" = None,
                     force_cron: bool = False) -> dict[str, Any]:
      """Install/uninstall the twice-daily scheduled ``fulcra-coord digest`` jobs.

      macOS -> two launchd plists (morning 08:00 / evening 18:00) in ``target_dir``
      (default ~/Library/LaunchAgents); other platforms (or ``force_cron=True``) ->
      two managed crontab lines in ``crontab_path``. Idempotent, surgical uninstall,
      dry-run writes nothing. ``force_cron`` is a test seam to exercise the cron
      branch on a macOS dev box. The any-agent dedup guard makes installing this on
      every machine safe (concurrent ticks collapse to one digest per window)."""
      argv = cli_invocation.resolve_cli_argv()
      use_cron = force_cron or not scheduler_env.is_macos()
      plan: dict[str, Any] = {
          "mechanism": "crontab" if use_cron else "launchd",
          "cli_command": cli_invocation.resolve_cli_command(),
          "windows": [w for w, _, _ in WINDOWS],
          "uninstall": uninstall,
          "dry_run": dry_run,
          "writes": [],
          "removes": [],
      }

      if not use_cron:
          base = Path(target_dir) if target_dir is not None else scheduler_env.launchagents_dir()
          logs = Path(logs_dir) if logs_dir is not None else scheduler_env.default_logs_dir()
          for window, hour, minute in WINDOWS:
              plist = base / _plist_name_for(window)
              if uninstall:
                  plan["removes"].append(str(plist))
                  if not dry_run and plist.exists():
                      plist.unlink()
                  continue
              plan["writes"].append(str(plist))
              if not dry_run:
                  base.mkdir(parents=True, exist_ok=True)
                  logs.mkdir(parents=True, exist_ok=True)
                  plist.write_text(_plist_body(argv, window, hour, minute, logs))
          return plan

      # --- crontab branch ----------------------------------------------------
      cron = Path(crontab_path) if crontab_path is not None else None
      if cron is None:
          base = Path(target_dir) if target_dir is not None else Path.home()
          cron = base / "fulcra-coord-digest-crontab.txt"
      existing = cron.read_text() if cron.is_file() else ""
      stripped = _strip_managed_cron(existing)
      if uninstall:
          if stripped != existing:
              plan["removes"].append(str(cron))
          if not dry_run:
              cron.parent.mkdir(parents=True, exist_ok=True)
              cron.write_text(stripped)
          return plan

      new_text = (stripped.rstrip("\n") + "\n" if stripped.strip() else "")
      for window, hour, minute in WINDOWS:
          new_text += _cron_line(argv, window, hour, minute)
      plan["writes"].append(str(cron))
      if not dry_run:
          cron.parent.mkdir(parents=True, exist_ok=True)
          cron.write_text(new_text)
      return plan
  ```
- [ ] **Step 6.4 — run (expected PASS):** `uv run --extra dev python -m pytest tests/test_operator_digest.py::TestInstallDigest -v`
- [ ] **Step 6.5 — failing test: the CLI handler reports the plan (dry-run) and wires into the map.**
  Add `class TestInstallDigestCommand`:
  ```python
  class TestInstallDigestCommand(unittest.TestCase):
      def test_command_is_wired(self):
          self.assertIs(entry.COMMAND_MAP["install-digest"], cli.cmd_install_digest)

      def test_dry_run_reports_plan(self):
          import io, contextlib
          tmp = Path(tempfile.mkdtemp())
          args = types.SimpleNamespace(uninstall=False, dry_run=True,
                                       target_dir=str(tmp), logs_dir=str(tmp / "l"))
          buf = io.StringIO()
          with contextlib.redirect_stdout(buf):
              rc = cli.cmd_install_digest(args, backend=["false"])
          self.assertEqual(rc, 0)
          self.assertIn("dry-run", buf.getvalue())
  ```
- [ ] **Step 6.6 — run (expected FAIL):** `uv run --extra dev python -m pytest tests/test_operator_digest.py::TestInstallDigestCommand -v` — `AttributeError: ... 'cmd_install_digest'`.
- [ ] **Step 6.7 — implementation: CLI handler + entry wiring.** Add to `fulcra_coord/cli.py` (import `digest_schedule` at top: extend the existing `from . import ...` line to include `digest_schedule`):
  ```python
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
  ```
  In `entry.py`, add the subparser (after the `digest` parser) and the map entry:
  ```python
      # ---- install-digest ----
      sp = sub.add_parser("install-digest",
                          help="Install the twice-daily scheduled `fulcra-coord digest` "
                               "jobs (launchd 08:00/18:00 on macOS, cron elsewhere) — "
                               "the push side of the operator digest")
      sp.add_argument("--target-dir", dest="target_dir", default=None, metavar="DIR",
                      help="Override the LaunchAgents/cron target dir (for testing)")
      sp.add_argument("--logs-dir", dest="logs_dir", default=None, metavar="DIR",
                      help="Override the directory for digest stdout/stderr logs")
      sp.add_argument("--uninstall", action="store_true", help="Remove the digest schedule")
      sp.add_argument("--dry-run", action="store_true", help="Print intended changes, write nothing")
  ```
  ```python
      "install-digest": _cli.cmd_install_digest,
  ```
- [ ] **Step 6.8 — run (expected PASS):** `uv run --extra dev python -m pytest tests/test_operator_digest.py -v`
- [ ] **Step 6.9 — commit:** `git add fulcra_coord/digest_schedule.py fulcra_coord/cli.py fulcra_coord/entry.py tests/test_operator_digest.py && git commit -m "feat(digest): install-digest scheduler (launchd 08:00/18:00 + cron)

Calendar-scheduled twice-daily digest jobs — distinct from the interval
heartbeat/listener (StartCalendarInterval / fixed cron, not StartInterval). Two
jobs (morning 08:00, evening 18:00) reusing the #25 PATH + log-path hardening and
resolve_cli_argv. Installable on every machine; the any-agent dedup marker makes
concurrent ticks collapse to one digest per window. Surgical uninstall, dry-run,
stdlib-only.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"`
- [ ] **Step 6.10 — parallel docs subagent (does NOT block).** Dispatch a subagent to update `packages/fulcra-coord/README.md`: add `digest` and `install-digest` to the command reference, describe the second "Agent Tasks — Digest" timeline track and the any-agent dedup guard, and note the 08:00/18:00 cadence. The subagent reads the new `cmd_digest`/`digest_schedule`/`emit_digest_annotation` code + the existing README voice and produces a clean diff. (Per the repo's docs-as-you-work rule.)

---

### Task 7: Companion — enrich per-event `build_annotation` note with work substance

Independent and backward-compatible (note text only; no schema change, no tag
change). Adds `workstream` + a fuller blurb (current_summary + next_action + kind)
to the per-event moment's `desc`, so a timeline entry conveys *what work*, not
just the lifecycle category.

- [ ] **Step 7.1 — failing test: build_annotation desc includes workstream + kind + summary/next.**
  Add to `tests/test_annotations.py` (a new `class TestBuildAnnotationEnrichment`):
  ```python
  class TestBuildAnnotationEnrichment(unittest.TestCase):
      def _task(self, **over):
          t = schema.make_task(
              title="Fix the widget", workstream="devops", agent="claude-code:mb:repo",
              kind="feature", summary="rewiring the pump", next_action="ship it")
          t["id"] = "20260604-fix-widget"
          t.update(over)
          return t

      def test_desc_carries_work_substance(self):
          p = annotations.build_annotation(
              lifecycle="update", task=self._task(), agent="claude-code:mb:repo")
          self.assertIn("devops", p["desc"])
          self.assertIn("feature", p["desc"])      # kind from tags
          self.assertIn("rewiring the pump", p["desc"])
          self.assertIn("ship it", p["desc"])

      def test_backward_compatible_when_sparse(self):
          # No summary/next_action -> still produces a non-empty desc, never raises.
          t = self._task(current_summary="", next_action="")
          p = annotations.build_annotation(lifecycle="create", task=t,
                                           agent="claude-code:mb:repo")
          self.assertTrue(p["desc"])
          self.assertIn("devops", p["desc"])
  ```
- [ ] **Step 7.2 — run (expected FAIL):** `uv run --extra dev python -m pytest tests/test_annotations.py::TestBuildAnnotationEnrichment -v` — fails because the current `desc` is just `next_action or current_summary or link` (no workstream/kind).
- [ ] **Step 7.3 — minimal implementation.** In `fulcra_coord/annotations.py`, replace the `detail`/`desc` lines inside `build_annotation` (currently `detail = (task.get("next_action") or ...) ...; desc = detail or link`) with a substance-bearing blurb. Reuse `schema._extract_kind_from_tags` (kind lives in tags, not a top-level field):
  ```python
      # COMPANION (operator-digest spec §7): carry the WORK SUBSTANCE in the note so
      # a per-event moment conveys *what work*, not just the lifecycle category.
      # Shape: "[<workstream>/<kind>] <title> — <summary> · next: <next_action>".
      # Backward-compatible: this only changes the human-readable note body (desc);
      # tags / name / link / payload shape are unchanged, so existing readers and
      # the idempotency/transport paths are untouched. Every part is optional —
      # a sparse task still yields a non-empty desc (at minimum the prefix + title).
      from .schema import _extract_kind_from_tags
      workstream = task.get("workstream", "") or ""
      kind = _extract_kind_from_tags(task.get("tags") or [])
      prefix = "/".join(p for p in (workstream, kind) if p)
      summary = (task.get("current_summary") or "").strip()
      nxt = (task.get("next_action") or "").strip()
      blurb_parts = []
      if prefix:
          blurb_parts.append(f"[{prefix}]")
      blurb_parts.append(title)
      tail = " · ".join(
          x for x in (summary, (f"next: {nxt}" if nxt else "")) if x)
      blurb = " ".join(blurb_parts) + (f" — {tail}" if tail else "")
      desc = blurb.strip() or link
  ```
  > Remove the now-superseded `detail = ...` / `desc = detail or link` lines this block replaces. `title` and `link` are already bound earlier in the function; `_extract_kind_from_tags` is a module-private helper in `schema` — import it locally to avoid widening `schema`'s public surface.
- [ ] **Step 7.4 — run (expected PASS):** `uv run --extra dev python -m pytest tests/test_annotations.py::TestBuildAnnotationEnrichment -v`
- [ ] **Step 7.5 — regression run (the existing annotation suite must stay green; a desc-format test there may need updating).** `uv run --extra dev python -m pytest tests/test_annotations.py -q`
  If an existing test asserted the OLD `desc == next_action`, update it to the new blurb shape (that's the intended behaviour change; keep the assertion meaningful, e.g. assert the substring `ship it` still appears).
- [ ] **Step 7.6 — commit:** `git add fulcra_coord/annotations.py tests/test_annotations.py && git commit -m "feat(annotations): enrich per-event moment note with work substance

A per-event timeline moment now reads '[devops/feature] <title> — <summary> ·
next: <action>' instead of just the bare next-action, so the human sees WHAT work
each lifecycle event was about. Note-body only: tags, name, link, transport, and
idempotency are unchanged, so it's fully backward-compatible (the operator-digest
spec's companion ask).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"`

---

### Task 8: version bump to 0.6.0 + CHANGELOG (final; rebase-aware)

> **Do this last, and after rebasing onto post-#39 `origin/main`** so 0.6.0 sits on top of 0.5.6.

- [ ] **Step 8.1 — failing test: __version__ is 0.6.0 and `--version` reports it.**
  Add `class TestVersion` to `tests/test_operator_digest.py`:
  ```python
  class TestVersion(unittest.TestCase):
      def test_version_is_060(self):
          from fulcra_coord import __version__
          self.assertEqual(__version__, "0.6.0")
  ```
- [ ] **Step 8.2 — run (expected FAIL):** `uv run --extra dev python -m pytest tests/test_operator_digest.py::TestVersion -v` — current value is `0.5.5` (or `0.5.6` post-rebase).
- [ ] **Step 8.3 — bump the version.** Edit `fulcra_coord/__init__.py`: `__version__ = "0.6.0"`.
- [ ] **Step 8.4 — run (expected PASS):** `uv run --extra dev python -m pytest tests/test_operator_digest.py::TestVersion -v`
- [ ] **Step 8.5 — add the CHANGELOG entry.** Prepend under the `---` in `packages/fulcra-coord/CHANGELOG.md`:
  ```markdown
  ## [0.6.0] — Operator Digest

  **Why:** the human surface was pull-only — you saw "what's blocked on me" / "what
  is everyone doing" only when you started a session or ran needs-me/agents/resume.
  Between sessions you were blind, and the granular per-event annotations were too
  fine-grained to read as a glance. The Operator Digest is the push side: a
  consolidated, human-paced situational-awareness summary delivered to your Fulcra
  timeline twice daily and on demand.

  - **`fulcra-coord digest [--window morning|evening] [--format json] [--dry-run]`** —
    builds a four-block digest from existing bus state + presence (blocked on you,
    upcoming, what each agent did since the last window, what's stale) and writes it
    to the timeline. `--dry-run` renders without writing; `--format json` prints the
    structured digest.
  - **New "Agent Tasks — Digest" timeline track** — a second moment definition,
    separate from the granular per-event "Agent Tasks" track (which is kept,
    untouched), so digests filter on their own.
  - **Any-agent, dedup-guarded** — any machine can run the digest; a first-writer-wins
    marker (`digest/markers/<date>-<window>.json`) collapses concurrent runs to one
    digest per window (a rare same-second double is accepted as harmless, since
    Fulcra Files has no compare-and-swap).
  - **`fulcra-coord install-digest`** — schedules the digest twice daily (launchd
    08:00/18:00 on macOS, cron elsewhere). Safe to install on every machine.
  - **Per-event annotations now carry work substance** — the note reads
    `[<workstream>/<kind>] <title> — <summary> · next: <action>` instead of just the
    lifecycle category. Note-body only; backward-compatible.

  All digest paths are best-effort: a failed read/marker/emit never raises into a
  scheduled tick. Datetime comparisons (the `since` window + due ranking) parse
  timestamps (consistent with the 0.5.x mixed-precision fix), never lexical compare.
  ```
- [ ] **Step 8.6 — full suite green.** `uv run --extra dev python -m pytest -q`
- [ ] **Step 8.7 — commit:** `git add fulcra_coord/__init__.py packages/fulcra-coord/CHANGELOG.md tests/test_operator_digest.py && git commit -m "chore(digest): bump to 0.6.0 + CHANGELOG for the Operator Digest

Marks the Operator Digest feature release: the digest command + 'Agent Tasks —
Digest' track, the any-agent dedup guard, install-digest scheduling, and the
per-event note enrichment. Lands on top of 0.5.6 (rebase post-#39).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"`

---

## Self-review

**(a) Spec coverage — every spec section maps to a task:**

| Spec component | Task |
|---|---|
| §1 `views.build_operator_digest` (4 blocks, due-then-age ranking, since-window, pure) | Task 1 |
| §2 `_render_digest` (name + note, skip empty, cap long, no-crash) | Task 2 |
| §3 New "Agent Tasks — Digest" track (own def + cache, reuse `_write_http`, tags `[agent-digest, <window>, agent:<kind>]`) | Task 3 |
| §4 `digest` command (resolve human, load summaries+presence, since/now, build+render+emit, `--format json`, `--dry-run`, COMMAND_MAP) | Task 4 |
| §5 Dedup guard (`digest/markers/<date>-<window>.json`, first-wins, race documented) | Task 5 |
| §6 `install-digest` (launchd/cron, 08:00/18:00, reuse `scheduler_env`) | Task 6 |
| §7 Companion per-event enrichment (workstream + summary/next/kind, backward-compatible) | Task 7 |
| Error handling (best-effort, never raises into a tick; marker-fail → skip; reuse writer robustness) | Built into Tasks 3, 4, 5, 6 (try/except + skip-on-uncertain-claim) |
| Testing mandate (each block present/empty, ranking, since filter, render, dedup, command, enrichment) | Tasks 1–7 tests |
| Version target 0.6.0 + CHANGELOG | Task 8 |

**(b) Placeholder scan:** No `TBD`, no "add error handling" (the error handling is written inline in each `try/except`), no "similar to Task N" (each task has its own full code), no "write tests for the above" (every test step shows actual test code). Every function uses real grounded names: `needs_human`, `upcoming_for_human`, `is_stale`, `presence_liveness`, `_parse_dt`, `_now`, `task_summary` field names (`owner_agent`, `done_at`, `updated_at`, `not_before`, `due`, `last_touched_by`, `blocked_on`, `next_action`, `current_summary`, `tags`), `remote.download_json`/`upload_json`/`presence_view_path`, `remote_root`, `_write_http`'s real endpoints (`/user/v1alpha1/tag`, `/user/v1alpha1/annotation`, `/ingest/v1/record/batch`), `_resolve_tag_id`/`_resolve_token`/`_request`/`_api_base`, `cache.annotations_dir`, `_definition_cache_path` pattern, `scheduler_env.scheduler_path`/`log_paths`/`launchagents_dir`/`is_macos`, `cli_invocation.resolve_cli_argv`/`resolve_cli_command`, `identity.resolve_human`/`resolve_agent`, `schema._extract_kind_from_tags`, the real `COMMAND_MAP` dict, and `_print_json`/`_info`.

**(c) Type / name consistency:** `build_operator_digest` returns a dict whose block keys are exactly `blocked_on_you`, `upcoming`, `per_agent`, `stale` (plus `schema`/`human`/`now`/`since` metadata). `_render_digest` reads those same four keys via `.get(...) or []`. `cmd_digest` passes the whole dict to `_render_digest` and prints it verbatim for `--format json` — the JSON test asserts `payload["blocked_on_you"]`, matching the producer. `per_agent` entries carry `agent`/`workstreams`/`summary`/`liveness`/`finished_since` in both the producer (Task 1) and the renderer (Task 2). `emit_digest_annotation(*, name, note, window, agent, backend=None)` is called by `cmd_digest` with exactly those kwargs (Task 4 test asserts `kw["window"]`, `kw["name"]`). `_claim_digest_marker(window, now, *, backend=None) -> bool` has the same signature in the Task-4 stub and the Task-5 real version, and `cmd_digest` calls it positionally `(window, now, backend=backend)`. `digest_schedule.install_digest(...)` is called by `cmd_install_digest` with the keyword args the function declares.

**Cross-cutting datetime discipline (PR #39 alignment):** every gate/threshold compare in the new code parses first — `_digest_due_key` (`_parse_dt(due)`, `_parse_dt(updated_at)`), the `finished_since` filter (`_parse_dt(done_at) < since_dt`), `_digest_window_since` (returns a `datetime`, subtracts a `timedelta`), and `_claim_digest_marker`'s date key (`now.astimezone(timezone.utc).strftime`). No lexical string `<`/`>` on timestamps anywhere. Sort *keys* that remain strings (`upcoming_for_human`'s internal ISO-Z sort, inherited unchanged) only order, never gate.
