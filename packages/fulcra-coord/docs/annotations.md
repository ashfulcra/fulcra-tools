# Agent Tasks lifecycle annotations

fulcra-coord can drop a breadcrumb on the operator's **Fulcra timeline** every
time an agent moves a coordination task through its lifecycle. The goal: looking
at your own Life timeline in `library.fulcradynamics.com`, you can see *what the
agents were doing, and when* — interleaved with everything else Fulcra records.

All Agent-Tasks moments share a single **track tag `agent-tasks`** so they can
be filtered together on the timeline regardless of lifecycle or agent.

## Status: real write LIVE via the Fulcra HTTP API (gated, off by default)

The recommended transport is **`http`** (alias `api`): it writes annotations
**directly over the Fulcra HTTP API** using only the Python standard library
(`urllib` + `json`) — no `httpx`, no `fulcra-common` dependency — replicating the
exact proven path `fulcra-collect` uses. Set `FULCRA_COORD_ANNOTATIONS=http`.

For each lifecycle event the `http` writer runs three Fulcra endpoints in order:

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

### Legacy `cli` transport

The older `cli` transport instead creates each *moment annotation* by shelling
out to the Fulcra CLI:

```
fulcra create-data-type MomentAnnotation "<NAME>" \
    --description "<desc>" --add-to-timeline \
    --tag agent-tasks --tag <lifecycle> --tag agent:<kind> --tag session:<sess>
```

- `--add-to-timeline` makes it a real occurrence on the Life timeline.
- Tags passed by name are **auto-created** by Fulcra.
- The create returns JSON including the annotation `id` and `fulcra_source_id`
  (`com.fulcradynamics.annotation.<id>`); it is deletable via
  `fulcra delete-data-type <id>` (used only by the live smoke test).

### CLI build dependency

This `create-data-type` support currently lives on the Fulcra CLI's
**`create-annotations-commands`** branch — **not yet on `fulcra-api` main**.

**Important — use a SEPARATE pointer, not `FULCRA_CLI_COMMAND`.** That branch
carries `create-data-type` but **not** the `file` command group, and the
Files-capable build (`file-management`) lacks `create-data-type` — **no single
fulcra-api build has both yet.** Since the core coordination file-ops and the
annotation write would otherwise resolve from the same base, pointing
`FULCRA_CLI_COMMAND` at the annotations build **breaks task I/O**. Instead, point
only the annotation writer at it via the dedicated **`FULCRA_COORD_ANNOTATION_CLI`**,
leaving `FULCRA_CLI_COMMAND` on the Files build:

```
# file-ops stay on the Files-capable build:
export FULCRA_CLI_COMMAND="uv run --project /path/to/fulcra-api-python-files fulcra"
# annotations use the annotations build:
export FULCRA_COORD_ANNOTATION_CLI="uv run --project /path/to/fulcra-api-python-annotations fulcra"
```

Once both command sets land on `fulcra-api` main and the installed CLI has
`create-data-type` AND `file`, neither pointer is needed — `FULCRA_COORD_ANNOTATION_CLI`
falls back to the shared `FULCRA_CLI_COMMAND` → `fulcra-api` on PATH resolution.

The `cli` transport's `create-data-type` support lives only on a CLI build that
the everyday installed CLI lacks (see below), which is exactly why `http` is the
default-recommended path — it needs nothing beyond a Fulcra token.

## Enabling

Set `FULCRA_COORD_ANNOTATIONS`:

| Value | Behaviour |
|---|---|
| unset / `off` / anything unrecognized | No-op (default). Task ops behave exactly as before. |
| `http` (alias `api`) | **LIVE, recommended.** Write directly over the Fulcra HTTP API via stdlib `urllib` (tag resolve → moment-def resolve/create+cache → `POST /ingest/v1/record/batch`). Needs a token (`FULCRA_ACCESS_TOKEN` or `fulcra auth print-access-token`); base from `FULCRA_API_BASE`. See `_write_http`. |
| `cli` | **Legacy.** Route writes through `create-data-type MomentAnnotation ... --add-to-timeline` on the annotation CLI base — `FULCRA_COORD_ANNOTATION_CLI` if set, else the shared `FULCRA_CLI_COMMAND` → `fulcra-api` on PATH → `uv tool run fulcra-api`. Requires a CLI build with annotation support (see above). |

The CLI base for annotation writes is resolved by `remote.cli_base_cmd()` — the
**same** resolution file ops use, minus the `file` subcommand — so the binary is
never hardcoded. (The `FULCRA_COORD_BACKEND` file-ops/test override is **not**
consulted for annotations: it speaks the file-protocol of the test emulator, not
the CLI's top-level command surface.)

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

The CLI write attaches four tags (the `cli_tags` field of the built payload):

```
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

(The payload also keeps a bare `tags` list `[<lifecycle>, <kind>, <session>]`
for existing readers. The `http` writer resolves the `cli_tags` names — the
`agent-tasks` track tag plus the prefixed forms — to tag ids and rides those in
`metadata.tags`.)

## Name and description

The annotation **NAME** (the timeline label) is the concise, link-free form:

```
<lifecycle>: <title> (<task-id>)
```

The **description** is a one-line detail — the task's `next_action`, falling back
to `current_summary`, falling back to the **library link**:

```
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

## HTTP wire shape (`http` transport)

The `http` writer (`fulcra_coord/annotations.py::_write_http`) posts a single
JSONL record:

```
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
ingest path byte-for-byte in shape, implemented with stdlib `urllib` so
fulcra-coord stays dependency-free.

## doctor

`fulcra-coord doctor` prints an `[Annotations]` section: the resolved mode
(`off`/`cli`/`http`), and when enabled, the API base and whether a token is
resolvable (it never prints the token). An `off` mode says so plainly with the
enable hint — the fast answer to "why didn't anything appear on my timeline?".
