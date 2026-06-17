# Agent Tasks lifecycle annotations

fulcra-coord can drop a breadcrumb on the operator's **Fulcra timeline** every
time an agent moves a coordination task through its lifecycle. The goal: looking
at your own Life timeline in `library.fulcradynamics.com`, you can see *what the
agents were doing, and when* — interleaved with everything else Fulcra records.

All Agent-Tasks moments share a single **track tag `agent-tasks`** so they can
be filtered together on the timeline regardless of lifecycle or agent.

## Status: real write LIVE via the Fulcra HTTP API (gated, off by default)

There is one writer. When enabled, it writes annotations **directly over the
Fulcra HTTP API** using only the Python standard library (`urllib` + `json`) —
no `httpx`, no `fulcra-common` dependency — replicating the proven path
`fulcra-collect` uses. Enable it with `fulcra-coord annotations on`, or set
`FULCRA_COORD_ANNOTATIONS=on` for one shell.

For each lifecycle event the writer runs three Fulcra endpoints in order:

1. **Resolve/create each tag** — `GET /user/v1alpha1/tag/name/{name}` (200 → id),
   else `POST /user/v1alpha1/tag {"name": ...}` on a 404.
2. **Resolve/create the shared `Agent Tasks` moment definition** —
   `GET /user/v1alpha1/annotation` (adopt the existing def by name), else
   `POST /user/v1alpha1/annotation` with an `annotation_type: moment` body. The
   resolved definition id is **cached locally** (`<cache-root>/.../annotations/
   definition.json`) so this resolve/create happens once, not per annotation.
3. **Post the record** — `POST /ingest/v1/record/batch` with header
   `content-type: application/x-jsonl` and a one-line JSONL body whose
   `metadata.data_type` is `MomentAnnotation`, `metadata.tags` are the resolved
   tag ids, and `metadata.source` carries both a lifecycle-stamped
   `com.fulcradynamics.fulcra-coord.<lifecycle>.<uuid>` id and the
   `com.fulcradynamics.annotation.<definition_id>` definition source.

The bearer token comes from `FULCRA_ACCESS_TOKEN` if set, else the stdout of
`fulcra auth print-access-token`; with no token the write cleanly no-ops. The
API base is `FULCRA_API_BASE` (default `https://api.fulcradynamics.com`).

## Why not the CLI?

fulcra-coord is intentionally stdlib-only: it is a coordination bus, so it avoids
new runtime dependencies and uses the same proven HTTP ingest shape as
fulcra-collect.

Records are also ingest-only today. The Fulcra platform exposes CLI/library
commands for definitions and tags, but no first-class record-write verb for the
timeline occurrence itself. Splitting the path — tags/defs through a CLI but the
record over HTTP — would add a second transport without removing the HTTP write
surface. The whole writer can move to the Fulcra CLI once a record-write command
exists.

Legacy `FULCRA_COORD_ANNOTATIONS=http`, `api`, and `cli` values still normalize
to `on` for back-compat, so old machine config keeps emitting. They do not select
separate transports.

## Enabling

Set `FULCRA_COORD_ANNOTATIONS` or persist the setting:

| Value | Behaviour |
|---|---|
| unset / `off` / anything unrecognized | No-op (default). Task ops behave exactly as before. |
| `on` | Write via the single stdlib `urllib` path (tag resolve → moment-def resolve/create+cache → `POST /ingest/v1/record/batch`). Needs a token (`FULCRA_ACCESS_TOKEN` or `fulcra auth print-access-token`); base from `FULCRA_API_BASE`. |
| `http` / `api` / `cli` | Back-compat aliases for `on`. They all route to the same stdlib `urllib` writer. |

For durable machine-wide enablement:

```bash
fulcra-coord annotations on
```

That persists `on` at `<XDG_CONFIG_HOME>/fulcra-coord/annotations` so every agent
on the machine emits without exporting `FULCRA_COORD_ANNOTATIONS` in each shell.
`fulcra-coord annotations off` removes the persisted file. A non-empty
`FULCRA_COORD_ANNOTATIONS` env var always wins for the current shell, including
`off`.

The annotation write is **best-effort and never raises** into a task operation.
A missing token, slow API, or broken annotation backend can never break — or even
change the outcome of — a `start` / `update` / `done`. The hook fires only
*after* the task + views have fully written successfully.

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

The writer attaches four namespaced tags (the `cli_tags` field of the built
payload; the historical field name is retained for payload compatibility):

```text
[ agent-tasks, <lifecycle>, agent:<kind>, session:<sess> ]
```

- **agent-tasks** — the shared **track tag**, first, so every Agent-Tasks moment
  is filterable together on the timeline regardless of lifecycle/agent.
- **lifecycle** — `create` | `pickup` | `update` | `complete`.
- **agent:`<kind>`** — agent kind from the first segment of the agent id
  (`<kind>:<host>:<repo>`): `claude-code → claude`, `openclaw → openclaw`,
  `codex → chatgpt`, `chatgpt → chatgpt`. Any other family is lowercased and
  passed through. Namespaced with the `agent:` prefix so the flat tag space
  stays unambiguous.
- **session:`<sess>`** — the 2nd agent-id segment (host/session), falling back to
  the 3rd (repo/channel) when the 2nd is blank. Omitted when the id has no
  segments beyond the kind. Namespaced with the `session:` prefix.

The payload also keeps a bare `tags` list `[<lifecycle>, <kind>, <session>]` for
existing readers. The writer resolves the `cli_tags` names — the `agent-tasks`
track tag plus the prefixed forms — to tag ids and writes those in
`metadata.tags`.

## Name and description

The annotation **NAME** (the timeline label) is the concise, link-free form:

```text
<lifecycle>: <title> (<task-id>)
```

The **description** is a one-line detail — the task's `next_action`, falling back
to `current_summary`, falling back to the **library link**:

```text
https://library.fulcradynamics.com/files/<remote-root>/tasks/<task-id>.json
```

This library-link URL shape is **assumed** (the coordination tasks are Fulcra
Files under `<remote_root>/tasks/<id>.json`). If/when Fulcra exposes a canonical
per-task permalink, only `annotations.library_link()` changes.

## Idempotency

One annotation per *real* lifecycle transition — not per write-retry. A genuine
transition appends a task event with a unique timestamp; a retry (e.g. after a
transient view-upload failure) re-uploads the identical task. The writer records
a local marker keyed by `(task_id, lifecycle, latest-event-timestamp)` in the
per-root cache (`<cache>/roots/<root>/annotations/`). A retry collides with the
existing marker and is skipped; a new transition gets a fresh anchor and emits
again. The marker is deliberately **local** (not stored on the shared task JSON)
so it never pollutes the cross-agent payload or tangles with merge logic.

## HTTP wire shape

The writer (`fulcra_coord/annotations.py::_write_http`) posts a single JSONL
record:

```http
POST <FULCRA_API_BASE>/ingest/v1/record/batch
Authorization: Bearer <fulcra access token>
content-type: application/x-jsonl

{"specversion": 1,
 "data": "{\"note\": \"<desc>\", \"title\": \"<name>\"}",
 "metadata": {
   "data_type": "MomentAnnotation",
   "recorded_at": "<ISO-8601 Z>",
   "tags": [<resolved tag ids>],
   "source": ["com.fulcradynamics.fulcra-coord.<lifecycle>.<uuid>",
              "com.fulcradynamics.annotation.<definition_id>"],
   "content_type": "application/json"}}
```

The `<definition_id>` is the resolved/created `Agent Tasks` moment definition
(`/user/v1alpha1/annotation`), cached locally so it is resolved once. Tag ids are
resolved/created via `/user/v1alpha1/tag`. This mirrors the `fulcra-collect`
ingest path in shape, implemented with stdlib `urllib` so fulcra-coord stays
dependency-free.

## doctor

`fulcra-coord doctor` prints an `[Annotations]` section: the resolved mode
(`off`/`on`), and when enabled, the API base and whether a token is resolvable
(it never prints the token). An `off` mode says so plainly with the enable hint
— the fast answer to "why didn't anything appear on my timeline?".
