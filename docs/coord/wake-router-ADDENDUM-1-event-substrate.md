# Wake-router addendum 1 — feed-driven folds (normative)

**Owner:** Tycho (`coord-boss`). **Authorization:** Ash, 2026-07-23 ("green light given" —
first-principles redesign of the bus read path; simplified same day by operator direction:
"there has to be a better way … than bloating and reconciling by combining a time series you
define, data-updates, and files"). **Gate:** dual-green codex-reviewer + coord-maintainer
ratification (coord-boss authors, so recuses its own gate per the W1-r3 precedent).
**Amends:** `wake-router-SPEC.md` and `wake-router-PLAN.md`; where this addendum speaks, it is
normative over both. **ATC untouched.**

## 1. Evidence (why the read model was wrong)

Three same-day incidents (2026-07-23) where a dispatch shard was written, readback-verified by
`file download`, and then absent from `file list` output — read by agents as "nothing assigned"
and by the coordinator as "vanished writes." Forensics via `fulcra-api data-updates` proved
**no write was ever lost**: every "vanished" shard shows `state: uploaded` with no archive or
delete event; the listings were stale. The same afternoon, a stale summaries index left one
agent's listen fold trying to overlay **129 unsummarized docs inside a seconds-scale budget**
(served 3), effectively deafening it — the full-scan read model failing at both ends.

The platform already provides the fix: `data-updates <range>` returns the authoritative
per-file change ledger (`full_name`, `state: uploaded|archived|deleted`, real second-granular
timestamps). Today the engine uses it only as reconcile's no-change fast path and discards its
contents on any change.

## 2. Normative principles

1. **The feed is the ledger; listings are a cache.** `data-updates` is the authoritative
   source for "what changed"; `file list` is an eventually-consistent view, permitted only as
   a fallback or for cold enumeration. No fold may conclude "absent" from a listing alone when
   the feed (or a direct read) is available — the W1 read-failure doctrine, extended.
2. **Feed-first, fail-closed.** Every fold that gains a feed path keeps its full-scan path as
   the fallback, taken on ANY doubt (feed unsupported, error, cursor missing/corrupt) — same
   doctrine as `reconcile._fast_path_no_changes`. Behavior with the fallback engaged is
   byte-identical to today; therefore the mixed-fleet gate (§3 of the plan) is not re-opened
   by this addendum.
3. **Shards stay canonical; derived views are rebuilt, never trusted.** Markdown shards remain
   the durable, human-readable state. Everything else (`summaries.json`, the settled index,
   router `delivered.json`) is a derived view: consumers may use it for speed, but the feed +
   targeted shard reads are the recovery path for any doubt about it.
4. **No second ledger.** A coordination-owned typed event mirror (a `CoordEvent` data type,
   dual-written beside each shard) was designed, review-hardened through three rounds, and
   **CUT by operator direction (2026-07-23)** — recorded here so it is not re-invented: a
   client-written mirror duplicates the feed's job with strictly weaker guarantees (it can
   miss a write for any client-side reason), and the machinery those rounds kept demanding —
   content-addressed identity, reconcile back-fill, a confirmed-through watermark — was all
   consistency tax on the second ledger, not fold value. The feed is server-written and
   complete by construction; combined with targeted shard reads it answers everything the
   mirror would have. *Revisit trigger (measure first):* only if feed retention is ever shown
   to be shorter than a fold horizon that derived snapshots cannot cover.

## 3. Delta-driven folds

### 3.1 Incremental reconcile (task E1)

Reconcile becomes a **feed-cursor incremental folder**: each pass consumes `data-updates`
since its durable cursor (reusing W4's proven watermark + processed-ledger pattern, inclusive
rescan, never-ledger-what-wasn't-folded), reads ONLY the changed coordination shards, and
updates `summaries.json` + the settled index in place. The periodic full scan remains as (a)
the fail-closed fallback on any cursor/feed doubt and (b) a scheduled self-check that the
incremental view has not drifted (divergence is loud and triggers a rebuild, never silently
absorbed). This kills the fresh-overlay bloat class at the root: "fresh" stops meaning
"everything the index hasn't met" (129 docs today) and starts meaning "feed entries since the
last pass" (typically zero to a handful).

### 3.2 Delta-driven listen/briefing (task E2)

`transport.updates()` grows an explicit since-window + parsed `file_changes` result filtered
to `team/<team>/` paths. `listen` gains a feed-first tick: one feed call since cursor → read
only changed shards → classify; W8's head/tail budgets remain on the fallback path untouched.
`briefing`/`needs-me` consume the reconcile aggregate (§3.1 keeps it current) + the feed
delta since the aggregate's cursor. Red-first tests: feed-vs-listing divergence (feed wins),
feed-unavailable fallback byte-identical, cursor no-false-advance across a failed tick.

### 3.3 Router evidence source (task E3)

`_router_pass` swaps its candidate source from the task-directory listing to the feed
(`uploaded` events under `task/`), keeping the listing scan as the fail-closed fallback and
the cursor/ledger/decide seams unchanged (verified accommodating in the W4 merge review).
Second-granular feed timestamps subsume the minute-granularity tie problem; the inclusive
rescan + ledger stay as defense in depth. This is also the webhook socket: when Fulcra
webhooks ship, the receiver replaces the feed poll and nothing downstream moves.

## 4. Task DAG (extends the plan's table; dispatch on the bus after this addendum gates)

| # | Task | Depends on | Assignee (planned) |
|---|---|---|---|
| E1 | **Incremental reconcile.** Feed-cursor incremental fold maintaining `summaries.json` + settled index; full scan as fail-closed fallback AND scheduled drift self-check (loud divergence ⇒ rebuild). Red-first tests: feed-unavailable ⇒ full pass; corrupt cursor ⇒ full pass; incremental result equals full-scan result on a fixture window; drift detection triggers rebuild loudly. | this addendum | coord-opus-worker |
| E2 | **Delta-driven listen/briefing.** Per §3.2. | this addendum (parallel with E1) | codex-coder |
| E3 | **Router feed swap.** Per §3.3. | this addendum, W5 | Fabio |

E1/E2 are parallel and independent; neither blocks the W-track. W10's gates are unchanged.
Each E-task is dual-green (codex-reviewer + coord-boss) at exact head, red-first, AGENTS.md
ship-gate.

## 5. Non-goals

Store migration off shards; a coordination-owned typed data type (cut — §2.4; the record
service's sanctioned-fields probe evidence lives in bus shard `546b2445` for whoever revisits);
webhook receiver before Fulcra ships webhooks (E3 builds the socket); removing any full-scan
fallback or W8 budget; per-team server-side feed filtering (client-side path filter is
sufficient at current volume — ~420 account-wide changes/2h measured 2026-07-23); ATC coupling.
