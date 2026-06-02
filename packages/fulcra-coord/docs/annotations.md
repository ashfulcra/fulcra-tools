# Agent Tasks lifecycle annotations

fulcra-coord can drop a breadcrumb on the operator's **Fulcra timeline** every
time an agent moves a coordination task through its lifecycle. The goal: looking
at your own Life timeline in `library.fulcradynamics.com`, you can see *what the
agents were doing, and when* — interleaved with everything else Fulcra records.

All Agent-Tasks moments share a single **track tag `agent-tasks`** so they can
be filtered together on the timeline regardless of lifecycle or agent.

## Status: real write LIVE via the Fulcra CLI (gated, off by default)

The write is **confirmed working**. Each lifecycle event creates a *moment
annotation* on the operator's timeline by shelling out to the Fulcra CLI:

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

The **`api` transport remains deferred**: the `fulcra_api` Python core library
still exposes only annotation *read* methods (`moment_annotations()`,
`annotations_catalog()` against `/data/v1alpha1/event/MomentAnnotation` and
`/user/v1alpha1/annotation`) and **no create/upload** method, so `_write_api`
returns `False` with a `TODO`. When that endpoint is confirmed, only `_write_api`
changes.

## Enabling

Set `FULCRA_COORD_ANNOTATIONS`:

| Value | Behaviour |
|---|---|
| unset / `off` / anything unrecognized | No-op (default). Task ops behave exactly as before. |
| `cli` | **LIVE.** Route writes through `create-data-type MomentAnnotation ... --add-to-timeline` on the annotation CLI base — `FULCRA_COORD_ANNOTATION_CLI` if set, else the shared `FULCRA_CLI_COMMAND` → `fulcra-api` on PATH → `uv tool run fulcra-api`. Requires a CLI build with annotation support (see above). |
| `api` | POST to the Fulcra annotations HTTP endpoint. Still a no-op pending a confirmed create endpoint. |

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
for the deferred `api` transport / existing readers.)

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
