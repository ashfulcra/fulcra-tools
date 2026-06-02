# Agent Tasks lifecycle annotations

fulcra-coord can drop a breadcrumb on the operator's **Fulcra timeline** every
time an agent moves a coordination task through its lifecycle. The goal: looking
at your own Life timeline in `library.fulcradynamics.com`, you can see *what the
agents were doing, and when* — interleaved with everything else Fulcra records.

All annotations land on a single **moment annotation track named `Agent Tasks`**.

## Status: deferred real write (gated, safe today)

The feature is **off by default** and currently a **no-op even when enabled**,
because the installed Fulcra surface does not yet expose an annotation *write*
path:

- The `fulcra-api` CLI has **no** `annotation` / `moment` / `event` write
  subcommand. Its command set is auth + `file` (library files) + data *reads*
  (`metric-time-series`, `get-records`, `sleep-*`, location, etc.).
- The `fulcra_api` Python core library exposes annotation **read** methods
  (`moment_annotations()`, `annotations_catalog()`) against
  `/data/v1alpha1/event/MomentAnnotation` and `/user/v1alpha1/annotation`, but
  **no create/upload** method.

So the writer is built end-to-end (tag derivation, payload, gating, idempotency,
CLI hook points) but the two transport functions (`_write_cli`, `_write_api`)
return `False` with a `TODO` until Fulcra ships a confirmed annotation-write
surface. When it does, only those two private helpers change.

## Enabling

Set `FULCRA_COORD_ANNOTATIONS`:

| Value | Behaviour |
|---|---|
| unset / `off` / anything unrecognized | No-op (default). Task ops behave exactly as before. |
| `cli` | Route writes through the resolved Fulcra CLI backend (the same backend file ops use). Currently a no-op pending a real CLI annotation subcommand. |
| `api` | POST to the Fulcra annotations HTTP endpoint. Currently a no-op pending a confirmed endpoint. |

The annotation write is **best-effort and never raises** into a task operation.
A missing, slow, or broken annotation backend can never break — or even change
the outcome of — a `start` / `update` / `done`. The hook fires only *after* the
task + views have fully written successfully.

## Lifecycle → tag mapping

Each annotation carries the lifecycle as a tag, derived from the command (and,
for the claim case, the resulting status):

| Command | Resulting state | Lifecycle tag |
|---|---|---|
| `start`, `tell`, `broadcast` | task created | `create` |
| `update --status active` | claimed / picked up | `pickup` |
| `update` (other), `assign`, `block`, `pause` | touched | `update` |
| `done` | finished | `complete` |

`abandon` and internal writes emit nothing.

## Tags

```
[ <lifecycle>, <agent_kind>, <session_tag> ]
```

- **lifecycle** — `create` | `pickup` | `update` | `complete`.
- **agent_kind** — from the first segment of the agent id (`<kind>:<host>:<repo>`):
  `claude-code → claude`, `openclaw → openclaw`, `codex → chatgpt`,
  `chatgpt → chatgpt`. Any other family is lowercased and passed through.
- **session_tag** — the 2nd agent-id segment (host/session), falling back to the
  3rd (repo/channel) when the 2nd is blank. Omitted when the id has no segments
  beyond the kind.

## Text and link

```
<lifecycle>: <title> (<task-id>) <library link>
```

The **library link** is a best-effort deep link to the task file in the Fulcra
library web app:

```
https://library.fulcradynamics.com/files/<remote-root>/tasks/<task-id>.json
```

This URL shape is **assumed** (the coordination tasks are Fulcra Files under
`<remote_root>/tasks/<id>.json`). If/when Fulcra exposes a canonical per-task
permalink, only `annotations.library_link()` changes.

## Idempotency

One annotation per *real* lifecycle transition — not per write-retry. A genuine
transition appends a task event with a unique timestamp; a retry (e.g. after a
transient view-upload failure) re-uploads the identical task. The writer records
a local marker keyed by `(task_id, lifecycle, latest-event-timestamp)` in the
per-root cache (`<cache>/roots/<root>/annotations/`). A retry collides with the
existing marker and is skipped; a new transition gets a fresh anchor and emits
again. The marker is deliberately **local** (not stored on the shared task JSON)
so it never pollutes the cross-agent payload or tangles with merge logic.

## Assumed API shape (deferred)

When wiring the `api` transport, the assumed (unconfirmed) create call is:

```
POST https://api.fulcradynamics.com/data/v1alpha1/event/MomentAnnotation
Authorization: Bearer <fulcra access token>
Content-Type: application/json

{
  "annotation": "<UUID of the 'Agent Tasks' moment-annotation type>",
  "time": "<ISO-8601>",
  "note": "<text>",
  "tags": ["create", "claude", "<session>"]
}
```

The `Agent Tasks` type would first be resolved/created via the user annotation
catalog (`/user/v1alpha1/annotation`). Confirm against `fulcra-api` source before
removing the TODO in `fulcra_coord/annotations.py::_write_api`.
