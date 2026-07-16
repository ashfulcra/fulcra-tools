# v1.6.8 lead — bound the acks fold with recent_changes (remote-feasibility)

Worktree `/Users/ashkalb/Developer/ft-v168`, branch `qa-v168-readtail` (off origin/main). Package `packages/coord-engine`. stdlib-only. TDD, red first. Commit author `ashfulcra <114089064+ashfulcra@users.noreply.github.com>` + trailer `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. Senior-dev voice, no LLM tics.

## Problem (measured, do not re-derive)
`reconcile` takes ~785s from a remote host (1.2s/op transport) even on a pass with **734 reused / 2 parsed** — so it is NOT the summaries rows. The cost is `_fold_and_gc_acks` (`reconcile.py:173`): it does **one `list_dir` per ack directory — 280 measured — plus one `read` per shard, every pass, unconditionally**. That is ~336s in listings alone. The task path is already cheap (one `list_dir` + in-memory reuse). Fix the acks fold ONLY; do not restructure reconcile's task/reuse model.

Two designs already measured and REJECTED — do not revisit:
- mtime-gate the ack dirs: impossible. `transport.py:135-142` gives a bare `name/` directory line `mtime=None`; the store carries no dir mtime.
- fold only open/reparsed slugs: of 280 ack dirs, 185 are OPEN → only ~34% off. Not a fix.

## The mechanism (verified live)
`GET /input/v1/file/recent_changes?start_time=<iso>&end_time=<iso>` — a **published** spec endpoint (`docs/specs/fulcra-openapi-digest.txt`; primitives doc §"Spec composition"). Returns one flat JSON `{"files":[...]}`, tree-wide, each entry: `full_name` (full path, e.g. `/team/fulcra/_coord/acks/<slug>/<agent>.md`), `size`, `state`, `uploaded_at` (SECOND precision), `archived_at`, `deleted_at`, `id`.

Verified properties (established empirically — trust these):
- **Complete**: for a 7h window, the expected set computed independently from listing mtimes (31 task docs) exactly matched what recent_changes reported (31). Zero missing.
- **No silent cap**: counts scale linearly — 7h=2963, 24h=9532, 7d=30674. No round ceiling, no pagination cursor (single `files` key).
- **Fails LOUD, never truncates**: a 30-day window returns HTTP 500 after ~10s.
- **Volume at our cadence**: only 13 ack shards changed in a 7h window; at a 20-min heartbeat it is ~0-2.

## Task
Make `_fold_and_gc_acks` change-driven, fail-closed, with the current behavior as fallback.

1. **Add a transport capability** for the change query. Put it beside the existing transport methods (`transport.py`) — e.g. `recent_changes(start_iso, end_iso) -> list[dict] | None`. Use the same auth/CLI-or-REST path the transport already uses; return `None` on ANY failure (500, timeout, unparseable) — the caller treats `None` as UNKNOWN, never as "nothing changed". Keep it stdlib-only.

2. **Rework `_fold_and_gc_acks(transport, team, live_slugs, *, now, ...)`** to accept the prior acks (from the prior aggregate rows' `acked_by`) and a `since` anchor (the prior aggregate's `generated_at` — same anchor the summaries reuse uses):
   - Query `recent_changes(since, now)`. Filter to paths under `_coord/acks/`. Derive the affected slug set from `full_name`.
   - **Re-fold only affected slugs** (list that slug's ack dir + read its shards — existing logic, unchanged, per-slug).
   - **All other live slugs**: reuse their prior `acked_by` — zero ops.
   - **Fallback (fail-closed)**: if the query returns `None`/unknown, OR there is no usable `since` anchor (legacy aggregate without `generated_at`), OR the caller requests a full pass → do TODAY's full fold (list every ack dir). Emit a visible degraded/info line naming why — never silently reuse on an unknown change-query.
   - **Periodic full fold backstop**: force a full fold every Nth pass (add a small env knob via `config.env_int`, e.g. `COORD_ACKS_FULL_EVERY`, sane default) so a missed change can never persist indefinitely. Follow the repo's config policy (positive-finite, bad value → default).
   - **GC**: the orphan-shard GC currently rides this loop. Keep its data-loss guards EXACTLY as-is (never GC when live set empty; only datable shards older than `GC_GRACE_HOURS`). It is cleanup, not correctness — run it only on the full-fold passes, so it does not force per-dir listings on the incremental path. Say so in the docstring.

3. **Docstring/comments**: state the invariant plainly — the incremental path is an OPTIMIZATION whose failure mode is always "fall back to the full fold", never "assume unchanged".

## Tests (TDD, red first) — extend the reconcile test module; use the FakeTransport pattern
1. Incremental: prior aggregate with acked_by for many slugs + a change-query reporting ONE ack shard → only that slug's ack dir is listed/read (assert call counts on the fake), all other slugs keep their prior acked_by. (Red today: all dirs listed.)
2. Fail-closed: change-query returns None (simulate 500) → full fold runs (every dir listed), a degraded/info line is emitted, and acked_by is correct. Never silently reuses.
3. No anchor: prior aggregate lacking `generated_at` → full fold (no crash, no silent reuse).
4. Periodic backstop: on the Nth pass the full fold runs even when the query is healthy.
5. GC unchanged: orphan shard older than grace is still GC'd on a full-fold pass; the live-set-empty and within-grace guards still hold.
6. Full suite stays green (`pytest packages/coord-engine -q`; establish the baseline first — v1.6.7 was 818 passed / 1 skipped, main may differ).

## Report
Return ONLY: status, commit sha(s), one-line test summary (command + counts), confirmation of the three behaviors (incremental folds only changed slugs; unknown-query falls back to full fold + degrades loud; periodic backstop fires), and any concern. Do NOT push, do NOT bump the version — I handle push + review + release + the remote A/B with coord-boss.
