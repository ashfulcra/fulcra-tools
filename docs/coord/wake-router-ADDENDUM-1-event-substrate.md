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
   Isolation is safe ONLY because §3.1's completeness contract detects and repairs the
   resulting gap — the two are one mechanism and ship together in E1.

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
- **Event identity (normative dedup rule):** every record carries the FULL digest
  `event_id = sha256("<subject_path>|<event>|<sha256 of the shard bytes>")` — no timestamp
  and no writer-local input participates in identity. Both writers derive it identically by
  construction: the immediate mirror hashes the exact bytes it just uploaded; back-fill
  (§3.1) downloads the shard at `subject_path` and hashes the same bytes; `event` is derived
  from the path class (`task/` ⇒ directive lifecycle, `_coord/responses/` ⇒ response,
  `review/**/verdicts/` ⇒ verdict-filed, `presence/` ⇒ presence-beat, …), reproducible from
  the path alone. The actor rides in the record body, derivable from the shard frontmatter —
  never in the identity. **Convergence rule for superseded intermediates (explicit):** if the
  shard was rewritten again before back-fill ran, back-fill necessarily produces the id of
  the LATEST bytes, which collides with (and dedupes into) the newest mutation's own event —
  the missed intermediate is subsumed by the state that replaced it, which is correct for
  folds that reconstruct current state; per-mutation history is explicitly NOT a goal of the
  mirror (the feed remains the complete change ledger). The record store is append-only, so
  duplicate physical records are possible by design; **readers dedupe on `event_id`** and
  duplicates are semantically invisible. Red-first pins: (a) double write/back-fill of one
  logical event folds identically to a single write; (b) immediate-mirror and back-fill
  writers given INTENTIONALLY different local clocks / feed timestamps still produce one
  logical event after back-fill (timestamps cannot influence identity).
- **Reader:** folds query `get-records CoordEvent <range>` and reconstruct deltas without
  listing or re-reading unchanged shards — subject to the §3.1 completeness contract.
  `payload` carries only fold-relevant fields already public in the shard (assignee, status,
  priority); **no secrets, no free-form content** — the relay contract's no-untrusted-fields
  rule applies.
- **Unknown `schema_version` ⇒ fallback** (principle 2). Bumps are additive-only.
- **Physical encoding (normative — from the 2026-07-23 live de-risk probe, bus shard
  `546b2445`):** the record service persists ONLY the sanctioned annotation fields
  `{id, tags, sources, recorded_at, note}` and **silently drops arbitrary top-level JSON
  fields** (confirmed live, including with `--no-validate`). The logical schema above is
  therefore an ENCODING CONTRACT, not a storage layout: the full logical event rides as
  compact JSON in `note`; `recorded_at` carries `ts`; `tags` carry the indexable dimensions
  (`coord-event`, `kind:<event>`, `actor:<agent>`, `team:<team>`) so range queries pre-filter
  without decoding; the client-supplied record `id` is the `event_id` (retries become upserts
  where the service honors client ids; reader-side `event_id` dedup remains the backstop where
  it does not). Writes go via JSONL stdin (field-options-only invocation fails in non-TTY
  sandboxes — probe finding 4). A record whose `note` fails to decode or lacks
  `schema_version` is unknown-version ⇒ fallback (principle 2). E1 re-verifies this encoding
  against the service before building on it (the probe is evidence, not a contract).

### 3.1 Completeness contract (normative — closes the missing-mirror hole)

Principle 4's isolation means a shard write can succeed while its event write fails; a healthy,
queryable record store may therefore be silently missing a mutation. Events-first folds MUST
be able to detect this, so:

- **The completeness oracle is the `data-updates` feed, not the record store.** The feed is
  written server-side by the store itself and cannot miss a shard write for a client-side
  reason — it is, structurally, the outbox this contract needs (nothing new to build or keep
  in sync).
- **Reconcile owns back-fill.** Each reconcile pass compares the feed's `file_changes` for
  coordination paths against `CoordEvent` records over the same window and back-fills any
  missing event (idempotent via `event_id`), then durably advances an
  `events_confirmed_through` watermark (stored with reconcile's other state, whole-file
  overwrite).
- **Reader rule:** an events-first fold may trust the record stream only up to
  `events_confirmed_through`. For any window beyond the watermark — or when the watermark is
  missing, stale beyond one reconcile interval, or unreadable — the fold consumes the FEED
  directly for that window, or takes the full-scan fallback (principle 2). A missing single
  event can therefore never produce a false-negative fold: unconfirmed windows are never
  served from records alone.
- **Red-first acceptance (E1):** (1) shard write succeeds, event write fails, record store
  stays healthy ⇒ the fold does NOT false-negative (unconfirmed window read from feed), and
  the next reconcile back-fills the event and advances the watermark; (2) retry/back-fill of
  the same logical event produces no duplicate after the reader's `event_id` dedupe.

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
| E1 | **CoordEvent substrate.** `data-type create` (versioned schema above), engine dual-write at the W1.5 chokepoint (failure-isolated both directions per principle 4), the §3.1 completeness contract (reconcile back-fill + `events_confirmed_through` watermark + reader rule), events-first fold seam with fail-closed fallback. Red-first tests: schema-version drift ⇒ fallback; feed/records unavailable ⇒ fallback; dual-write failure isolation both directions; **§3.1's two acceptance cases** (missing-mirror no-false-negative + back-fill; `event_id` dedupe under retry). | this addendum | coord-opus-worker |
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
