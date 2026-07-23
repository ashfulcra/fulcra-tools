# Wake-router addendum 1 — event substrate & delta-driven folds (normative)

**Owner:** Tycho (`coord-boss`). **Authorization:** Ash, 2026-07-23 ("green light given" —
first-principles redesign of the bus read path). **Gate:** dual-green codex-reviewer +
coord-maintainer ratification (coord-boss authors, so recuses its own gate per the W1-r3
precedent). **Amends:** `wake-router-SPEC.md` and `wake-router-PLAN.md`; where this addendum
speaks, it is normative over both. **ATC untouched.**

## 1. Evidence (why the read model was wrong)

Three same-day incidents (2026-07-23) where a dispatch shard was written, readback-verified by
`file download`, and then absent from `file list` output — read by agents as "nothing assigned"
and by the coordinator as "vanished writes." Forensics via `fulcra-api data-updates` proved
**no write was ever lost**: every "vanished" shard shows `state: uploaded` with no archive or
delete event; the listings were stale. Meanwhile every fold (listen, briefing, needs-me,
review status) does O(full-scan) reads over that same listing surface, which is why fold
budgets exhaust and briefings time out even after W8 bounded the listen path.

The platform already provides the fix: `data-updates <range>` returns the authoritative
per-file change ledger (`full_name`, `state: uploaded|archived|deleted`, real second-granular
timestamps), and user-defined data types (`data-type create` / `record` / `get-records`)
provide an append-only, schema'd, time-range-queryable record store. Today the engine uses the
feed only as reconcile's no-change fast path and discards its contents on any change.

## 2. Normative principles

1. **The feed is the ledger; listings are a cache.** `data-updates` is the authoritative
   source for "what changed"; `file list` is an eventually-consistent view, permitted only as
   a fallback or for cold enumeration. No fold may conclude "absent" from a listing alone when
   the feed (or a direct read) is available — the W1 read-failure doctrine, extended.
2. **Events-first, fail-closed.** Every fold that gains a feed/event path keeps its full-scan
   path as the fallback, taken on ANY doubt (feed unsupported, error, cursor missing/corrupt,
   schema-version ahead of reader) — same doctrine as `reconcile._fast_path_no_changes`.
   Behavior with the fallback engaged is byte-identical to today; therefore the mixed-fleet
   gate (§3 of the plan) is not re-opened by this addendum.
3. **Shards stay canonical.** Markdown shards remain the durable, human-readable documents and
   the source of truth for state. Typed records are a hot index over them, never a
   replacement. No migration, no flag day.
4. **Dual-write failure isolation.** A typed-record write failure must never fail the shard
   write it accompanies (mirror of W1.5's presence-refresh isolation: stderr note, rc
   unchanged). The inverse is forbidden: no record is written for a shard write that failed.

## 3. `CoordEvent` typed substrate (task E1)

A user-defined data type, created once via `data-type create`, schema versioned:

```json
{
  "schema_version": 1,
  "event": "directive-created | response | ack | verdict-filed | task-status | presence-beat | review-requested | settled",
  "team": "fulcra",
  "actor": "<agent id>",
  "subject_path": "team/<team>/task/<slug>.md",
  "slug": "<slug>",
  "ts": "<ISO8601 Z>",
  "payload": { }
}
```

- **Writer:** the engine, at the same `main()` dispatch chokepoint W1.5 established — one
  seam, keyed on the same write-verb set (as extended by W2's opening commit). Actor is the
  writer, never a target.
- **Reader:** folds query `get-records CoordEvent <range>` and reconstruct deltas without
  listing or re-reading unchanged shards. `payload` carries only fold-relevant fields already
  public in the shard (assignee, status, priority); **no secrets, no free-form content** —
  the relay contract's no-untrusted-fields rule applies.
- **Unknown `schema_version` ⇒ fallback** (principle 2). Bumps are additive-only.

## 4. Delta-driven folds (task E2)

`transport.updates()` grows an explicit since-window + parsed `file_changes` result filtered
to `team/<team>/` paths. `listen` gains an events-first tick: one feed call since cursor →
read only changed shards → classify; W8's head/tail budgets remain on the fallback path
untouched. `briefing`/`needs-me` consume the reconcile aggregate + feed delta. Cursor
semantics reuse W4's proven pattern (durable watermark + processed ledger, inclusive rescan,
never-ledger-what-wasn't-read).

## 5. Router evidence source (task E3)

`_router_pass` swaps its candidate source from the task-directory listing to the feed
(`uploaded` events under `task/`), keeping the listing scan as the fail-closed fallback and
the cursor/ledger/decide seams unchanged (verified accommodating in the W4 merge review).
Second-granular feed timestamps subsume the minute-granularity tie problem; the inclusive
rescan + ledger stay as defense in depth. This is also the webhook socket: when Fulcra
webhooks ship, the receiver replaces the feed poll and nothing downstream moves.

## 6. Task DAG (extends the plan's table; dispatch on the bus after this addendum gates)

| # | Task | Depends on | Assignee (planned) |
|---|---|---|---|
| E1 | **CoordEvent substrate.** `data-type create` (versioned schema above), engine dual-write at the W1.5 chokepoint (failure-isolated both directions per principle 4), events-first fold seam with fail-closed fallback. Red-first tests: schema-version drift ⇒ fallback; feed/records unavailable ⇒ fallback; dual-write failure isolation both directions. | this addendum | coord-opus-worker |
| E2 | **Delta-driven listen/briefing.** `transport.updates()` since-window + path filter; events-first listen tick; aggregate+delta briefing. Red-first tests: feed-vs-listing divergence (feed wins), feed-unavailable fallback byte-identical, cursor no-false-advance across a failed tick. | this addendum (parallel with E1 — consumes the FILE feed, not CoordEvent records) | codex-coder |
| E3 | **Router feed swap.** Candidate source = feed `uploaded` events; listing fallback; cursor seams unchanged. | this addendum, W5 | Fabio |

Sequencing note: E1/E2 are parallel and independent; neither blocks the W-track. W10's gates
are unchanged. Each E-task is dual-green (codex-reviewer + coord-boss) at exact head,
red-first, AGENTS.md ship-gate.

## 7. Non-goals

Store migration off shards; webhook receiver before Fulcra ships webhooks (E3 builds the
socket); removing any full-scan fallback or W8 budget; per-team server-side feed filtering
(client-side path filter is sufficient at current volume — ~420 account-wide changes/2h
measured 2026-07-23); ATC coupling.
