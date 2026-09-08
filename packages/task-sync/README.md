# Fulcra task sync

The engine behind the Reminders and Todoist plugins in the
[Collect 0.1.2 candidate](https://github.com/ashfulcra/fulcra-tools/pull/757).
It is not required by the coordination or continuity tools, and is not part of
the current Mac download.

Shared completion engine for Collect task providers. Source lists must be selected
explicitly. Source titles, notes, list membership and due dates flow into
`vault/tasks/<provider>/<sha256-source-id>.md`. Completing the source publishes
`status: resolved`; changing an imported Fulcra file's status to `resolved` requests
completion of the same source task. Keep its identity and generation unchanged.
Only completion writes back: editing other fields never edits or reopens the source.
Missing tasks and failed or partial snapshots never imply completion or deletion.

## Integration

`fulcra_task_sync.models` provides frozen `Task` and `Collection` dataclasses and a
`Provider` protocol. Providers expose `name`, `namespace` (stable source-account
identity), `collections()`, `tasks(selected_ids)`, `get_task(id)` and
`complete(id, expected_revision, collection_id)`. Discovery and task snapshots must
raise on partial reads. Source methods must bound their own blocking I/O. Completion
must freshly verify identity, selected collection and revision and return the saved
Task; the engine independently reads back completion. The initial engine blocks all
automatic recurring writeback because the providers cannot atomically protect an
occurrence. Source recurrence changes still import and rotate the generation.

```python
from fulcra_task_sync.engine import run_sync
from fulcra_task_sync.vault import FulcraVault
from fulcra_collect.config_leases import task_mutation_scope

with FulcraVault(token) as vault:
    result = run_sync(
        provider, selected_ids, vault,
        load_state, save_state,
        dry_run=preview,
        still_selected=lambda: current_selected_ids(),
        deadline_s=600,
        selection_epoch=saved_configuration_epoch,
        mutation_scope=task_mutation_scope,
    )
```

`load_state(key)` returns a JSON value or `None`; `save_state(key, value)` must
durably persist or raise (an explicit `False` also fails). Both dictionaries and
list chunks are stored. Keep callbacks in Collect's private plugin KV store; do
not export state or source data into the checkout. Account keys hash Fulcra account
identity, provider account identity and provider name. The Apple provider uses its
local EventKit namespace and opaque list/task IDs; account-based providers must
supply their verified account ID. Serialize runs for the same provider account
across **all** selections; this API does not provide cross-process locking.

An account-level active record stores the current selection fingerprint and a
fresh epoch on each observed selection transition. Per-task records carry that
epoch. Returning from A to B to A establishes a new baseline, including when B
excluded every task from A; it never revives an earlier completion journal. A
missing baseline never authorizes an old resolved file. A local journal is also
invalidated when the newest managed Fulcra file carries another generation.

Pass `selection_epoch` as the saved configuration lifecycle revision. The settings
owner must rotate it on list, credential, enable/disable and preview changes,
including changes while no worker is running. The engine cannot observe a disable
or empty-selection interval during which it never runs. Collect's app/CLI settings
flow supplies that revision; direct configuration file edits bypass its lifecycle
tracking. `selection_epoch` defaults to an empty string for integrations that only
need transitions observed by consecutive sync runs.

`still_selected()` returns the current enabled selection, or an empty collection if
disabled, preview was enabled, or the captured configuration epoch changed mid-run.
The whole selection must still match. It is checked before source writes, vault
writes and every KV write, including immutable chunks. An already-started network
or EventKit operation cannot be recalled. Preview performs reads and planning
without source/file writes, checkpoint changes or advancing the active epoch.
No selection performs no I/O, including no checkpoint or epoch writes.

`mutation_scope` is a no-argument context-manager factory covering each source
completion or vault upload. The engine checks consent again **inside** that scope
and holds it until the external call returns. Collect passes
`fulcra_collect.config_leases.task_mutation_scope`, which acquires shared locks on
the account-transition lock and configuration lock, in that order. Account and
settings changes acquire the corresponding exclusive locks: a transition that
wins first makes the inner guard reject the write; a write that wins first finishes
its call before the transition can proceed. This closes the gap between checking
consent and initiating the external mutation. Lease acquisition waits at most 30
seconds and fails without writing if the locks remain unavailable.

Local KV checkpoints remain outside the external-mutation lease: an epoch change
can leave a stale local journal, but the protected final guard rejects its external
write and the next active epoch invalidates that journal. With `mutation_scope`
omitted, the engine uses a no-op context and offers only the earlier callback
checks; other integrations must supply a scope coordinated with their consent
changes to obtain the same guarantee. A remote operation with an uncertain timeout
may still finish remotely after its client call returns; a local lease cannot
cancel an already-submitted request.

Schema 2 stores each task separately using compact source hashes/booleans; titles
and note bodies never enter checkpoints. Explicit seen version IDs and hashes of
trusted published baselines live in immutable bounded chunks. The tracking index
uses the same chunks, and mutable task/account manifests publish their references
only after all chunks persist. A failed manifest write leaves the previous record
usable; unreferenced chunks are harmless and currently retained in the local KV
store. Every key is checked against Collect's 256-byte limit and every compact JSON
value against its 64-KiB limit. Schema-1 whole-selection records are ignored and
establish a new baseline rather than importing their old journals. Existing old
records are not deleted automatically.

The result exposes `counts` (`imported`, `updated`, `completed`, `unchanged`,
`missing`, `skipped`, `blocked_recurring`, `planned`), `errors`, `remaining` and `partial`.
Errors contain an opaque task fingerprint and exception class only, never source
text, tokens, URLs or provider error bodies. `completed` counts verified source
completions; a later file upload can still fail, producing a partial result and a
retryable journal. `remaining` counts failed/unvisited items (one when initial
snapshot work is unknown). Recurring requests are counted as blocked, and their
resolved Fulcra status remains visible until source state changes.

## Safety and history

Strict YAML frontmatter requires `type: Task`, `sync_schema: collect-task/v1`,
`source`, `source_id`, `list_id`, `generation`, `status`, `source_completed`,
`source_due`, and `source_revision`. Duplicate keys, aliases, malformed fields,
identity mismatch and missing/duplicate body fences fail closed. A source-owned
body region uses `<!-- collect-task:source:begin -->` and
`<!-- collect-task:source:end -->`. Source text is HTML-escaped to prevent fence
injection. Unknown frontmatter values and text outside the owned region survive
normal updates; YAML formatting/comments are not preserved.

The engine reads every unseen version ID, including resolutions that raced with a
later source upload. It never advances a timestamp or own-upload cursor over
unseen versions. Reopening, collection changes and recurring due changes rotate
the generation. Old generations cannot complete the current task. Trusted published
source baselines allow a valid older revision in the same generation to request
completion while rejecting modified source metadata. Planned upload baselines are
checkpointed before uploads so a lost upload reply can be recovered. Journals persist
before completion and retries freshly inspect the source, so a lost completion
reply does not cause a repeated completion. If an uncertain completion is followed
by a changed open source revision, its generation rotates conservatively to avoid
completing a possible reopen. A rejected write or malformed file
remains a reported conflict. Source revision checks narrow races but cannot offer
a cross-system transaction; providers without conditional writes have a residual
race between their final read and source save.

Fulcra Files has whole-file versioned uploads, without conditional updates.
Concurrent completion intents survive through version history; unrelated concurrent
text edits may require recovery from history if an upload races with the edit.
The engine cannot guarantee merging arbitrary concurrent prose edits.

## Bounds and transport

`FulcraVault(token, *, base_url, namespace, timeout_s=20, max_versions=500,
transport=None)` provides `versions(path)` (newest first), `read_version(id)`,
`write(path,text)` (confirmed upload version ID), `namespace`, `close()` and context
manager cleanup. Without an explicit namespace it reads `/user/v1alpha1/info` for
the authenticated `userid`. Routes and upload shapes follow installed
`fulcra-api` **0.1.41**: `GET/POST /input/v1/file_upload` and
`GET /input/v1/file_upload/{id}/download`. History requests include both uploaded
and archived versions. Signed storage uploads use POST, as the installed client
does, with no Fulcra Authorization header. Downloads follow at most one secure
redirect using a separate anonymous client. TLS and bounded response bodies are
required; raw upstream errors are not included in sync reports.

Runs stop scheduling I/O at 600 seconds (or an earlier requested deadline).
Snapshots and the complete tracking index each cap at 10,000 tasks; collections
cap at 10,000. Source task/list IDs cap at 1,024 UTF-8 bytes, version IDs at 512
bytes, and each response/document at 1 MB. Each task retains at most 500 explicit
seen version IDs and 500 trusted baseline hashes. List chunks target 24 KB, so even
a maximum-sized tracking index and 500 long version IDs fit the KV record limits.
HTTP timeouts are bounded by the remaining run budget; a blocking provider call
may overrun by its own timeout, and lease acquisition by its bounded wait.
Exceeding a cap fails closed and is reported,
never treated as complete history. A completion/upload requiring another version
is blocked before source mutation when the history budget is full. Explicit seen
IDs are retained rather than compacted by timestamp. Long-lived files at the cap
require a deliberate migration/archive policy before writes resume. Successful
tasks and confirmed missing/moved items checkpoint a continuation pointer for
bounded-run resumption, so repeated lookups cannot starve later tasks. Continuation
writes recheck the live selection and deadline; if either changed during a lookup,
that item remains pending for the next eligible run.

## Verification and privacy

Run `PYTHONPATH=packages/task-sync:packages/collect .venv/bin/python -m pytest
packages/task-sync/tests -q` from the repository root. Tests use deliberately
synthetic providers, task bodies, account namespaces and HTTP transports. They
exercise completion in both directions, history races, reopen, recurring blocks,
lost responses, upload failure, deselection, preview, malformed documents,
incomplete snapshots, deadlines and account changes. Production Collect KV
validators check large notes, 200-task imports, maximum-sized indexes/history, and
failed manifest recovery. Selection/epoch round trips and superseded generations
cannot revive journals. Slow missing/moved lookups under a short run budget still
allow later tasks to progress, without checkpoints after deadline or deselection.
Real configuration locks verify source/vault call ordering against account
transitions, settings-write exclusion and bounded lease cleanup. No live data is needed.
This package has not performed live reminder completion or production rollout.
