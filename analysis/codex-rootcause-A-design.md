# Root Cause A Design: Events-Mode Read Soundness

Author: codex:Mac.localdomain:main
Date: 2026-06-09

## Position

Do not flip `FULCRA_COORD_READ_SOURCE=events` until the write path knows whether
the task body it is about to write came from the mutable file or from an event
fold. A fold-sourced body must reconcile against the authoritative file before
upload, even when the cached file stat equals the current pre-stat.

The right fix is a per-task read provenance marker plus a three-way
fold-aware merge:

- base: the exact folded task body returned by `_cache_remote_task`
- local: the caller-mutated task body passed into `_write_task_and_views`
- remote: the fresh `tasks/<id>.json` body downloaded immediately before write

The current two-way `_try_merge(local, remote)` is not sufficient for this case.
It sees only "my edited stale fold" and "fresh file"; because the local edit is
usually newer, it can choose local as the field base and thereby drop remote-only
fields that were never present in the lagging fold.

## A2 Design

### 1. Record read provenance outside the task body

When `_cache_remote_task` returns a complete fold in events mode, it should cache
normal file stat metadata as it does today, but it should also write a local
sidecar such as:

```json
{
  "body_source": "events",
  "base_body": { "...": "the folded task returned to the caller" },
  "file_stat_at_read": { "...": "stat(tasks/<id>.json)" },
  "event_watermark": {
    "applied_event_count": 123,
    "max_event_id": "..."
  }
}
```

If the body came from the file fallback, write either no provenance sidecar or a
`body_source: "file"` marker. The marker should live in cache metadata, not in
the task dict, so it cannot leak into `tasks/<id>.json` or views.

If events mode is enabled and the write path cannot find provenance for an
existing remote file, treat that as unsafe for the flip: force a fresh file
download and either fall back to file-source behavior or raise a targeted
conflict. The important invariant is that an unknown-origin body must not be
allowed to skip reconciliation merely because cached file stat equals pre-stat.

### 2. Force reconciliation for fold-sourced bodies

In `_write_task_and_views`:

1. `pre_stat = remote.stat(task_path)`.
2. `cached_meta = cache.read_meta(task_path)`.
3. `provenance = cache.read_task_read_provenance(task_path)`.
4. If `pre_stat is not None` and `provenance.body_source == "events"`, always
   download `fresh = remote.download_json(task_path)` and run the fold-aware
   three-way merge.
5. Otherwise keep the current stat-change merge trigger:
   `cached_meta is None or remote.stat_changed(cached_meta, pre_stat)`.

This is more precise than a global `read_source() == "events"` force-merge. A
global check is a useful emergency fail-safe, but it over-applies to reads that
fell back to the file and still does not provide the base body needed for a
correct merge. Per-body provenance is the piece that makes the write sound.

### 3. Three-way merge semantics

Define `merge_fold_sourced(base, local, remote)`:

- `base` is the stale-but-complete fold handed to the command before mutation.
- `local` is `base` plus the agent's just-made edit.
- `remote` is the authoritative mutable file at write time.

For non-event fields:

- If only local differs from base: take local.
- If only remote differs from base: take remote.
- If both differ from base to the same value: take that value.
- If both differ from base to different values:
  - For status, reuse the existing `_try_merge` conflict rule: two independent
    new status transitions are unsafe; a single status transition is
    authoritative.
  - For ordinary scalar/dict fields, take local as the user's new write while
    logging that the field had an overlapping remote edit. This matches the
    current last-writer-wins spirit without losing unrelated remote fields.

For event-like fields:

- Union `events` using the existing `_union_events_and_acked` behavior, then
  truncate to `MAX_EVENTS_INLINE`.
- Union `acked_by` from local, remote, and the fold base.
- Run `_repair_merged_tags` after field/event reconciliation.

For bookkeeping fields:

- `updated_at`, `last_touched_by`, and `last_touched_in` should normally remain
  local, because they describe the write now being attempted.
- `id`, `schema`, and durable routing/source identity fields should follow the
  field rules above unless there is a known invariant that makes them immutable.

The result should then proceed through the existing upload, post-stat cache,
view rebuild, and event append flow.

### Why two-way merge loses data

Example:

1. File contains `priority=P1`, `current_summary="new from B"`.
2. Event fold lags and returns old body with `priority=P2`,
   `current_summary="old"`.
3. Agent edits only `next_action`, producing local `priority=P2`,
   `current_summary="old"`, `next_action="my edit"`.
4. Current `_try_merge(local, fresh_file)` may pick local as newer and carry all
   non-event fields from local, clobbering `priority=P1` and
   `current_summary="new from B"`.

A three-way merge sees that `priority` and `current_summary` changed only on the
remote side relative to `base`, so it keeps them, while also keeping
`next_action="my edit"`.

## Interaction With Ack Drift

If the fold is missing an `inbox_ack` shard but the mutable file has the ack in
`events` or `acked_by`, the forced file merge recovers it.

If the ack exists only in `views/summaries.json` because an older body lost the
inline ack event, the file merge alone does not recover it into the task body.
The current summary rebuild path can preserve it in views, but the next body
write could still omit it. For the flip, I would make fold-aware writes union
`acked_by` from the existing summaries entry as a fourth ack-only source when
available:

```text
merged.acked_by =
  local.acked_by union remote.acked_by union base.acked_by union summary.acked_by
```

This should be ack-only. Do not use summaries as a general field source; the
mutable task file remains authoritative for full task fields.

## A1 Severity

I do not consider A1 a flip blocker.

Sorting by `(canonical_at, event_id)` is deterministic for a fixed event set, so
there is no per-read flap. For truly concurrent same-microsecond writes, the
random suffix picks an arbitrary but stable winner. In a brokerless, no-CAS,
multi-writer store, there is no causal fact available that would make one of
those same-instant writes objectively "later." The mutable file path already has
an equivalent arbitrary last-writer outcome under racing uploads.

I would document this behavior rather than add an actor-keyed tie-break. An
actor-keyed tie-break would be deterministic, but it would not be more correct;
it would merely bias same-instant wins toward a naming convention. The random
suffix is fairer and already stable.

## Minimal Implementation Plan

1. Add cache helpers for task read provenance, keyed by task remote path or task
   id, with safe deletion/overwrite when a file-sourced body is loaded.
2. In `_cache_remote_task`, when a complete fold is used:
   - strip internal fold bookkeeping as today;
   - write cached task;
   - stat the file as today;
   - write provenance with `body_source="events"` and a deep copy of the folded
     body.
3. In `_write_task_and_views`, load provenance before computing
   `needs_merge_check`.
4. Add `needs_fold_reconcile = pre_stat is not None and provenance.source ==
   "events"`.
5. If `needs_fold_reconcile`, download the fresh file and call a new helper,
   e.g. `_try_merge_fold_sourced(base, local, remote)`.
6. If that helper returns `None`, raise `ConflictError` before upload, exactly
   like the existing unsafe merge path.
7. After a successful write, clear or replace the provenance with file-source
   provenance for the uploaded body and write the new file stat.
8. Add tests that prove:
   - stale fold + unchanged file stat still forces a fresh file merge;
   - remote-only fields survive while local-only edits survive;
   - two independent status transitions still conflict;
   - remote ack missing from fold survives via file;
   - summary-only ack survives if the summary ack union is implemented;
   - file-fallback events-mode reads do not pay the forced merge cost.

## Flip Gate

Before enabling `FULCRA_COORD_READ_SOURCE=events` by default, require:

- all fold-sourced writes carry provenance;
- fold-sourced writes force file reconciliation regardless of stat equality;
- the fold-aware merge has regression coverage for remote-only field recovery;
- A1 is documented as arbitrary-but-stable for same-microsecond concurrent
  events.

With those in place, events-mode reads can be made sound without requiring CAS,
locking, or a broker.
