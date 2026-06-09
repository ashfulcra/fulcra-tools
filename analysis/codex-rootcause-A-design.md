# Root Cause A Design: Events-Mode Write Soundness

## Verdict

Root Cause A2 is a release blocker for `FULCRA_COORD_READ_SOURCE=events`. The write path must know when a task body came from the event fold, and a fold-sourced edit must reconcile against the authoritative mutable file before upload.

My recommendation: add per-body read provenance and use a three-way merge for fold-sourced writes. Do not use a blanket `read_source() == "events"` force-merge with the current two-way `_try_merge`.

## Why the current shape is unsafe

Today `_cache_remote_task()` can return a complete event fold while still caching the current file stat as the write baseline. Then `_write_task_and_views()` sees:

- `pre_stat` = current file stat
- `cached_meta` = same current file stat
- task body = possibly stale fold

Because `remote.stat_changed(cached_meta, pre_stat)` is false, the write skips the file download and uploads the edited stale fold over the newer file. That is silent data loss, and parity cannot reliably catch it after the overwrite.

## Required model

Every loaded task body should carry local-only provenance, never persisted:

```python
{
    "source": "file" | "fold",
    "file_stat_at_read": {...} | None,
    "fold_base": <clean folded task> | None,
    "fold_complete": bool,
}
```

Implementation shape:

1. `_cache_remote_task()` should write task body to the existing task cache, and separately write provenance to a cache sidecar keyed by task id or task path.
2. If the body came from the file, behavior remains the current stat-check path.
3. If the body came from a complete fold, `_write_task_and_views()` must always download the current file before uploading, regardless of file stat equality.
4. The provenance sidecar must be consumed and then refreshed or cleared after a successful write, so later file-sourced writes do not inherit stale fold provenance.

Avoid embedding provenance keys in the task dict unless every persistence path strips them. A sidecar is harder to accidentally upload.

## Merge Semantics

The current `_try_merge(local, remote)` is a two-way merge. It is not sufficient for fold-sourced writes because it cannot tell "my edit" from stale fields copied out of the fold.

Use a three-way merge:

- `base`: the clean fold body returned at read time
- `mine`: the task after the command's local edit
- `theirs`: the fresh mutable file body downloaded immediately before write

For each field:

- If `mine == base` and `theirs != base`, keep `theirs`.
- If `mine != base` and `theirs == base`, keep `mine`.
- If both changed to the same value, keep that value.
- If both changed differently:
  - `events` and `acked_by`: union, preserving existing truncation/dedupe rules.
  - derived tags: rebuild from the merged fields plus non-standard tag union.
  - status: use the existing status-transition conflict policy, but evaluate it against `base` so remote-only status changes are not overwritten by a fold-sourced stale status.
  - scalar fields: conflict unless the field has an explicit domain merge rule.

This is the key point: for fold-sourced writes, unchanged local fields are not assertions. They are just stale read state. Only fields that differ from `base` represent the command's actual edit.

## Ack Drift Interaction

The file merge should recover `acked_by` that is present in the mutable file but missing from the fold, because `acked_by` is already a union field.

However, C established that summaries can be the best available ack view. So the A2 fix should also keep the C rule alive:

- when rebuilding summaries, preserve prior summary `acked_by`;
- when parity checks complete folds, compare fold `acked_by` against summaries `acked_by`;
- if a fold-sourced write downloads a file whose `acked_by` is behind summaries, the write path should avoid shrinking the summary ack set even if the file is stale.

The write path does not need to fold summaries into the durable task body directly if that creates layering pain, but it must not cause summaries ack loss.

## Why Not Global Force-Merge

`read_source() == "events"` as a global force-merge trigger is better than the current bug, but only as an emergency guard. It still uses the wrong merge primitive if paired with `_try_merge(local, fresh)`.

Two-way merge can preserve stale fold fields when local has a newer `updated_at`, because it treats the entire local body as intentional. That is exactly the thing we need to avoid.

Use provenance because the risk attaches to the body source, not to the host mode. A host can be in events mode but fall back to a file body. That file-sourced write does not need three-way fold recovery.

## A1 Read

A1 is not a flip blocker as described.

Sorting by `(canonical_at, event_id)` is deterministic for a fixed event set. Same-microsecond concurrent writes get an arbitrary but stable winner from the random suffix. That matches the brokerless, no-CAS mutable-file world: without a sequencer, there is no globally meaningful order for truly simultaneous writes.

I would document it and keep it, with one caveat: the event id must remain immutable and generated once at append time. Do not re-mint event ids during retention, replay, or repair. If any maintenance path rewrites event ids, A1 becomes a real flap.

A deterministic actor-key tie-break is not obviously better. It creates a permanent actor priority order, which is more surprising than arbitrary stable order.

## Tests I Would Require

1. Events-mode read returns a complete stale fold while the file has a newer scalar field; local command edits a different scalar field; write preserves both the newer file field and the local edit.
2. Same setup, but remote-only status changed; local edits summary; remote status survives.
3. Same setup, both local and remote independently change status; write conflicts or returns `NeedsReconcile`.
4. Fold is missing an `acked_by` value present in the file; write preserves the ack union.
5. File-sourced fallback under events mode still uses the existing stat-change path and does not force unnecessary three-way merge.
6. Provenance sidecar is not persisted to `tasks/<id>.json`, events, summaries, or views.

## Implementation Order

1. Add provenance sidecar APIs in `cache.py`.
2. Make `_cache_remote_task()` record `source=file|fold`, current file stat, and clean fold base when it returns a fold.
3. Add `_try_merge_from_base(base, mine, theirs)` next to `_try_merge()`, reusing the existing event/ack/tag helpers where possible.
4. In `_write_task_and_views()`, if provenance says `source == "fold"`, download fresh file and use the three-way merge before upload.
5. Add the six tests above in `test_read_cutover.py` or a new focused root-cause-A test file.
