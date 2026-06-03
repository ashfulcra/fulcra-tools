# Completing cross-source media dedup

**Status:** design (approved direction; pending spec review)
**Date:** 2026-06-03
**Author:** Claude (with Ash)

## Problem

The "Listened"/"Watched" tracks accrue **duplicate annotations** when the same
real event is reported by more than one source (e.g. a song scrobbled by both
Spotify-IFTTT and Last.fm; a podcast in both Apple Podcasts and Spotify). The
user observed ~40 cross-source-duplicated titles in Listened (Watched is mostly
single-source/legit).

Cross-source dedup is a **known, half-built feature** — `since_filter.py` says
the `until` knob exists "sidestepping the duplicate-write problem **until
cross-source dedup ships**." An architecture map (2026-06-03) found the precise
gap:

- Every realtime importer **emits** a cross-source key
  `com.fulcra.content.{listened,podcast,watched}.v1.<hash>` (hash = 5-minute
  time bucket + normalized identity) into `extra_source_ids`, and the wire/
  ingest plumbing carries it into the record's `source` array.
- `client.fetch_existing_source_ids` **reads those keys back** into the
  `existing` set.
- **But `run_import` skips an event only when its `deterministic_id` (the
  per-source id) is already present (`fulcra.py:166`) — it never consults the
  cross-source key.** Two sources for one listen have different
  `deterministic_id`s, so both get posted, and Fulcra does **not** dedupe by
  `source_id` at write time, so both persist forever. That "emit-but-never-
  skip-on-cross-key" gap is the unbuilt piece.

Three secondary findings:
- **Coverage gaps:** `deezer` (listened), `youtube` (watched), and
  `generic_csv` emit no cross key, so they can never cross-dedupe.
- **No write-time guarantee:** the readback-skip is racy (two sources importing
  the same window before either's write lands both post) and depends on
  readback-window coverage.
- **Twin cache is half-wired:** `record_imported_events` (which populates the
  local high-confidence fingerprint cache that `find_low_conf_twins` reads) is
  called **only** on the CLI import path (`cli_common.py:141`), never on the
  scheduled-plugin path — so scheduled-only users build an empty cache and the
  low/high-confidence "twin" matcher is inert.

The user's *existing* spotify-ifttt records lack the cross key because they are
**historical** (imported before the fingerprint line was added); the current
importer emits it correctly. Retroactively removing existing duplicates is out
of scope (needs per-record soft delete — see Non-goals).

## Goals

1. The same real listen/watch reported by N sources produces **one** annotation
   ("merge" policy, 5-minute tolerance — reusing the existing bucket).
2. Re-emits / re-imports / concurrent runs never create duplicates
   (race-free, write-time guarantee).
3. Every relevant importer participates (close the coverage gaps).
4. Cross-confidence twin dedup (e.g. slim Netflix row vs rich Trakt row) works
   for scheduled-only users, not just CLI imports.

## Non-goals

- **Retroactive cleanup** of the existing ~147k duplicate annotations — Fulcra
  has no per-record delete today; this is unblocked later by per-record soft
  delete (PM task `53d8df8c`).
- Removing the `until` workaround — keep it until the skip is verified in the
  wild.
- Changing the fingerprint algorithm (5-min bucket + normalized identity is
  accepted as the "same listen" definition).

## Design

Four components. (1) is the core "ship it"; (2) closes coverage; (3) is the
race-free guarantee; (4) completes the cross-confidence path.

### Unified dedup-key model (underpins 1 and 3)

Every event already exposes its dedup keys: `deterministic_id` (per-source) and
`extra_source_ids` (the `.content.*` cross-source key(s)). Define an event's
**dedup key set** = `{deterministic_id} ∪ set(extra_source_ids)`. An event is a
duplicate if **any** key in its set is already known. This single rule covers
both re-emits (deterministic_id) and cross-source (content key).

### 1. Client-side cross-source skip (core — "ships the feature")

In `fulcra.py run_import`, change the per-event skip from
`deterministic_id not in existing` to: **skip if the event's dedup key set
intersects `existing`** (which already contains both per-source and `.content.*`
ids from `fetch_existing_source_ids`). All paths (CLI + scheduled) share
`run_import`, so this dedupes everywhere at once.

- The `only_for_defs` readback scoping is unchanged; note (documented) that
  events orphaned by a soft-deleted def are filtered out of `existing` and thus
  not deduped against — acceptable (they're orphaned).
- Update the "skipped_existing"/"verified" accounting to reflect cross-key
  skips.

### 2. Coverage (every source participates)

Emit the appropriate `.content.*` cross key from the importers missing it:
`deezer` (listened), `youtube` (watched_*), and `generic_csv` (kind from the
mapping). Follow the existing pattern (`cross = <kind>_fingerprint(...)`;
`extra_source_ids=(cross,) if cross else ()`). For `generic_csv`, wire the
optional `extract_extra_source_ids` callback the way `letterboxd` does for
`generic_rss`.

### 3. Server-side / local persistent write-dedup (race-free guarantee)

Generalize the attention daemon dedup (PR #20's `forwarded_attention` table +
`claim_attention_source_id`) into a **general persistent claim** keyed on dedup
keys, shared by the attention route and the media import path:

- New table `forwarded_events(dedup_key TEXT PRIMARY KEY, forwarded_at TEXT)`
  in `state.db` (migration), superseding/extending `forwarded_attention`.
- `db.claim_dedup_keys(conn, keys: set[str]) -> bool`: atomically `INSERT OR
  IGNORE` every key; return True iff **none** of them already existed (i.e. this
  event is new). Storm-safe — SQLite serializes; claim-then-forward, no rollback
  on forward failure (matches PR #20's decision: never-duplicate over
  never-lose, fail-closed).
- In `run_import`, before POSTing each (post-readback-surviving) event, call
  the claim; POST only if it returns True. This catches concurrent runs and
  same-run cross-source twins that the readback (a point-in-time snapshot)
  misses.
- The attention extension route reuses the same helper (its keys = the single
  attention source_id), replacing the attention-specific claim.

**Dependency-safe seam (important):** `run_import` lives in `media-helpers`,
but `state.db` and `db.claim_*` live in `collect`, and **`media-helpers` must
not import `collect`** (it depends only on `fulcra-common`). So the claim is
**injected**, not imported: expose it on the `RunContext` the daemon already
passes into plugin runs (e.g. `ctx.claim_dedup_keys(keys) -> bool`), backed by
the daemon's `state.db`. `run_import` calls `ctx.claim_dedup_keys` through that
interface; a `None`/absent claimer (e.g. standalone CLI imports run outside the
daemon) degrades to readback-skip-only (component 1 still applies). This keeps
the package dependency direction intact and lets the attention route and media
path share one implementation behind one interface.

The two layers compose: readback-skip catches "already in Fulcra from a prior
run"; the local claim catches "already forwarded by this daemon / concurrent /
same-run". Together: race-free, plugin-agnostic.

### 4. Twin-cache wiring for scheduled plugins (cross-confidence)

Call `record_imported_events` (populate the high-confidence twin cache) on the
scheduled-plugin path, not just the CLI path — so a scheduled-only user's cache
fills and `find_low_conf_twins` can defer a low-confidence incoming event (e.g.
a slim Netflix row) to a cached high-confidence twin (the rich Trakt row). Apply
the same twin policy plumbing the CLI uses; default policy stays `keep` unless
configured. (This addresses the cross-*confidence* case the exact-bucket key
can't: same content, different bucket/confidence.)

## Data flow (after)

importer → `NormalizedEvent{deterministic_id, extra_source_ids(.content.*),
content_fingerprint(string), confidence}` → `run_import`:
  1. readback `existing` (per-source + `.content.*`, scoped to current defs)
  2. **skip if dedup-key-set ∩ existing** (component 1)
  3. **claim_dedup_keys** in state.db; skip if not newly claimed (component 3)
  4. POST survivors; on success `record_imported_events` → twin cache
     (component 4)
  5. `find_low_conf_twins` (batch + twin cache) applies the twin policy

## Testing

- **Component 1:** integration test — two sources, same listen, same bucket →
  the second is **skipped** in `run_import` (not just "fingerprints match"; the
  existing tests only assert matching). Negative: different buckets → both kept.
- **Component 2:** per-importer test that deezer/youtube/generic_csv now emit
  the expected `.content.*` key (extend `test_cross_source_dedup.py`).
- **Component 3:** `claim_dedup_keys` unit tests (new key → True; any-key-seen →
  False; N concurrent identical → exactly one True; persistence across reopen).
  Integration: a re-run / concurrent run posts each unique event once.
  Attention route still dedupes (regression of PR #20 via the shared helper).
- **Component 4:** scheduled-plugin path populates the twin cache; a low-conf
  scheduled event defers to a cached high-conf twin.
- Full suites green, no network.

## Risks / edge cases

- **`normalize_title(track)` → "" → no cross key** (pathological titles like
  "(Remastered)"): such events can't cross-dedupe. Accepted (rare); they still
  dedupe on `deterministic_id`.
- **`parse_ifttt_timestamp` raises on a bad row and aborts the whole import**
  (`spotify_ifttt.py`) — pre-existing robustness bug; fix opportunistically
  (skip the row) but not core to this spec.
- **Bucket-boundary misses:** two sources straddling a 5-min boundary won't
  merge. Inherent to bucketing; accepted.
- **`forwarded_events` unbounded growth** — same follow-up as PR #20 (prune by
  `forwarded_at`); PM-tracked.
- **Migration of `forwarded_attention` → `forwarded_events`:** preserve existing
  attention claims (rename/migrate, don't drop).

## Rollout

Components 1+2 ship the feature for the common case and are independently
valuable. 3 adds the race-free guarantee. 4 is separable but in-scope per
decision. Implement in that order behind one branch/PR (or a small stack).
Keep `until` until verified.
