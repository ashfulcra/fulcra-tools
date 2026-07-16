# Fulcra primitives — a field guide for agents

Fulcra helps agents know their user, know what's happening in their user's
world, work with their user's other agents, and become more helpful over
time. This is what the platform actually provides for that and how to use
it, **by agent capability tier**. Written so you don't have to re-research the platform
surface. **Full surface re-verified 2026-07-16** (`fulcra-api` CLI/lib
**0.1.38** from PyPI, released 2026-07-16; api.fulcradynamics.com OpenAPI —
**53 paths**; a diffable paths/schemas baseline lives at
[`docs/specs/fulcra-openapi-digest.txt`](docs/specs/fulcra-openapi-digest.txt)
so future drift is a `diff`, not archaeology). Prior stamps: 2026-07-13 on
0.1.36, 2026-07-07/08 on 0.1.35.

**Drift since 2026-07-13 (0.1.36 → 0.1.38) — the rewrite trigger fired:**

- **`fulcra record` and `fulcra delete` shipped in 0.1.37** (2026-07-15).
  Annotation **records** — not just definitions — now have first-class CLI
  write and delete verbs, and the lib has `record_data_type`. This is the
  event the 07-13 stamp named as this doc's rewrite trigger; §Annotations is
  rewritten around it. **Tier 1 no longer hand-rolls ingest POSTs for
  records.**
- **`fulcra data-type schema`** — new subcommand, returns a data type's JSON
  schema (the discovery step before `record`). `data-type` is now
  `create|archive|restore|schema`.
- **`fulcra catalog --recordable-only`** — filters the catalog to types that
  `record`/`delete` accept. Catalog rows also carry `related_cli_commands`
  (which verbs work with that type).
- **`fulcra share create` is FIXED in 0.1.38.** 0.1.36/0.1.37 omitted the
  required `fulcra_user_name` body field and 422'd; 0.1.38 sends it. Upgrade
  instead of applying the REST workaround the 07-15 stamp carried — it is
  withdrawn.
- **Lib (0.1.37):** new `record_data_type`, `validate_records`,
  `v1_catalog_data_type`, `v1_catalog_schema`. There is **no** record-delete
  lib method — delete is composed client-side (see §Annotations).
- **Verb delta 0.1.36 → 0.1.38 is purely additive** (`record`, `delete`;
  nothing removed), and 0.1.38 is 0.1.37 plus the datashare fix alone. The
  0.1.36 `resolve_filepath` list-return contract is unchanged — see §File
  library. MCP tools and scopes unchanged (re-verified 2026-07-16).

> **Staleness warning:** the platform moves fast, and the CLI ships ahead of its
> git main on PyPI — **check the installed `fulcra-api` version, not just the
> repo**. Two releases landed inside 24 hours to produce this stamp. The
> published OpenAPI is **not** a complete route list (`/ingest/v1/record/batch`
> is live and unpublished), so absence from the spec is never evidence a route
> is gone — probe it (404 gone, 401 exists) before concluding anything.
> Next rewrite trigger: a **write** path via MCP, or record write/delete
> reaching tier 3 — that collapses the tier table's central asymmetry and is a
> rewrite, not a patch. Say so on the bus.

## Pick your tier

| Your capabilities | Tier | Use |
|---|---|---|
| Shell access (can run CLIs) | 1 | `fulcra` CLI + `fulcra_api` Python lib — **preferred over MCP** |
| Raw HTTP but no shell (e.g. GPT Actions) | 2 | Direct REST — device-flow auth + API calls below |
| MCP client only | 3 | `mcp.fulcradynamics.com` (read-side; see limits) |

---

## Auth (tiers 1 & 2)

Everything REST needs a Bearer JWT from Auth0, **audience
`https://api.fulcradynamics.com/`**, domain `fulcra.us.auth0.com`, public
client_id `48p3VbMnr5kMuJAUe9gJ9vjmdWLdnqZt` (public client — it ships in the
open-source CLI; not a secret).

- **Tier 1:** `fulcra auth login` → device flow → creds persisted to
  `~/.config/fulcra/credentials.json`, auto-refreshed. Token on demand:
  `fulcra auth print-access-token` (treat output as a credential — never log
  it). New-user onboarding = exactly this command; account auto-creates on
  first login via Portal/Context. **0.1.35:** the three prior login commands
  collapsed into one `auth login`; add `--get-auth-url` to run
  non-interactively (it prints the web-auth URL instead of opening a browser —
  useful for headless/remote hosts), with `--poll-timeout`/`--poll-interval`
  tuning the token wait.
- **Tier 2 (no shell):** the device flow is three plain HTTP calls:
  1. `POST https://fulcra.us.auth0.com/oauth/device/code`
     (form-encoded: `client_id`, `audience`, `scope=openid profile email offline_access`)
     → `{device_code, user_code, verification_uri_complete}`
  2. Show the user `verification_uri_complete`; they approve in any browser.
  3. Poll `POST https://fulcra.us.auth0.com/oauth/token`
     (form: `client_id`, `grant_type=urn:ietf:params:oauth:grant-type:device_code`,
     `device_code`) → `{access_token, expires_in, refresh_token}`.
     Refresh later with `grant_type=refresh_token`.
- Your `fulcra_userid` is the `fulcradynamics.com/userid` claim in the JWT
  (needed by several data endpoints; not a secret).

## File library (tiers 1 & 2) — read AND write

Versioned, path-addressed user file store. This is what the **coord** bus
(`coord-engine`, and the first-generation `fulcra-coord` before it) runs on, so it
is battle-tested at load.

- **Tier 1:** `fulcra file list|stat|download|upload|delete|restore <path>`
  (`stat` shows version history; **0.1.36 added `restore` as a CLI verb** —
  previously lib-only `restore_file`).
- **Lib contract (0.1.36):** `resolve_filepath(path)` returns a **list** of
  match dicts, not one dict — `matches[0]["id"]`. A path with no match
  **raises** `Exception("File not found in Fulcra: <path>")` rather than
  returning `[]`, so absent-handling means catching that exception. Code
  written against 0.1.35's single-dict return breaks silently.
- **Tier 2 (REST, all on `https://api.fulcradynamics.com`):** the file API is
  now **published in the OpenAPI spec** (it wasn't before — don't be surprised
  it's there). Two identical path prefixes exist: **`/input/v1/file`** (the
  newer name) and **`/input/v1/file_upload`** (its alias; what the shipped CLI
  still calls). Either works; prefer `/input/v1/file`.
  - List: `GET /input/v1/file?path=<dir>&state=uploaded`
  - Stat/versions: `GET /input/v1/file/{input_id}`
  - Download: `GET /input/v1/file/{input_id}/download`
  - **Upload (two-step):** `POST /input/v1/file` with JSON
    `{name, path, content_type, content_length}` → response contains signed
    `url` → `POST` the raw bytes to that URL with matching headers.
  - Delete: `DELETE /input/v1/file/{input_id}`; restore:
    `POST /input/v1/file/{input_id}/restore`
  - What-changed: `GET /input/v1/file/recent_changes` (published in the spec
    as of 07-13; the file-side counterpart of `/data/v1/updates`)

## Annotations (definitions vs records — they differ!)

**Definitions** (the user's custom data types) have full CRUD today:

- Tier 1: `fulcra data-type create|archive|restore|schema` (types: moment,
  duration, boolean, numeric, scale; options for tags/units/scale
  labels/`--add-to-timeline`). `data-type schema <TYPE> [--api-version <V>]`
  (**new in 0.1.37**) returns the JSON schema for a type — the fields a record
  of it may carry. `--api-version` is required only when a type has several;
  `MomentAnnotation` is **`v1alpha1`**, not `v1`. The Python lib exposes the
  same: `create_annotation`, `delete_annotation`, `restore_annotation`,
  `annotations_catalog`, plus `v1_catalog_schema` for the schema. Prefer the
  CLI/lib over raw REST when you have a shell.
- Tier 2: `POST|GET|PUT|DELETE /user/v1alpha1/annotation[/{id}]`, soft-delete
  with `POST /{id}/cancel_deletion` to restore. JSON-schema discovery:
  `GET /user/v1alpha1/schema/annotation`.

**Records** (instances on the timeline) are **CLI-writable and CLI-deletable as
of 0.1.37** — this reversed the doc's long-standing "records are ingest-only"
guidance. If you have a shell, do **not** hand-roll ingest POSTs any more; the
raw endpoints below are tier-2 material and background for reading old code.

**Tier 1 — records:**

- **Discover what you can write:** `fulcra catalog --recordable-only` lists the
  types `record` and `delete` accept; each row's `related_cli_commands` says
  which verbs apply. Then `fulcra data-type schema <TYPE> --api-version <V>`
  for the fields.
- **Write:** `fulcra record DATA_TYPE [VALUE]`. `DATA_TYPE` takes the
  **`Base/<definition-uuid>` shorthand** (e.g.
  `fulcra record MomentAnnotation/<UUID> --note="Felt energized"`); the CLI
  splits it, POSTs to the base type, and appends
  `com.fulcradynamics.annotation.<UUID>` to `sources` for you — the tier-2
  dance described below, done automatically. `VALUE` is the shorthand for a
  single metric value (`fulcra record NumericAnnotation/<UUID> 75.5`).
  Arbitrary fields go as `--<name>=<value>`, parsed as JSON first and falling
  back to string; they override fields from input data.
- **Batch:** pipe JSON or JSONL on stdin, or `-f/--file <path>` — one record
  per line, each validated independently.
- **Other flags:** `--tag` and `--source` (repeatable; merged with whatever is
  in the input data — `com.fulcradynamics.cli` is always added),
  `--api-version` for ambiguous types, `--no-validate` to skip the client-side
  schema check. Validation is **on by default**: the CLI fetches the type's
  schema and refuses bad records before they reach the server.
- **Delete:** `fulcra delete DATA_TYPE [RECORD_ID]`, only for recordable types.
  Batch the same way, with `{"record_id": "<UUID>"}` per line.
- **Delete is a tombstone, not an erasure** — worth knowing because it leaks
  through. The CLI implements `delete` by *recording* a **`DeletedRecord`**
  (`{"record_id": …, "data_type": <base type>}`) through the same ingest path.
  So deletion is itself an append, `DeletedRecord` is a real queryable type,
  and `--no-validate` on `delete` skips the `DeletedRecord` schema check. There
  is still **no update/replace verb**: correct a record by deleting and
  re-recording, or by writing a superseding one.
- **Lib:** `record_data_type(data_type, records, api_version="v1alpha1")` takes
  a list of dicts and returns `{"upload_id": …}`; `validate_records(...)`
  pre-flights them against the schema, returning
  `(index, message, ValidationError)` tuples. **There is no delete-record lib
  method** — the CLI composes it, so lib callers write the `DeletedRecord`
  themselves via `record_data_type`, and the `Base/<uuid>` shorthand is CLI-only
  (the lib wants a base type plus an explicit `sources` entry).

**Tier 2 — records (and what the CLI does under the hood).** Two ingest write
paths. The typed endpoint is **live round-trip verified 2026-07-08** and is what
0.1.37's `record`/`delete` call; the legacy single-record path is published in
the OpenAPI (spec-confirmed, not re-round-tripped):

- **Typed endpoint (preferred):** `POST /ingest/v1/record/{data_type}` —
  `data_type` is a path segment (e.g. `MomentAnnotation`). The body is the
  **unwrapped** record for that type — NOT the legacy `DataRecordV1` envelope.
  For `MomentAnnotation` the schema is flat:
  `{"note": <str>, "recorded_at": <iso8601>, "tags": [<tag uuid>],
  "sources": [<source id>], "id": <optional uuid, generated if omitted>}`.
  Send `Content-Type: application/json` for a single record, or
  `application/x-jsonl` with **one record per line** for a batch (each line is
  validated independently). A `content-length` header is required. 201 →
  `{"upload_id": <uuid>}`. Takes an **`?api_version=`** query param (the lib
  defaults it to `v1alpha1`; `record`/`delete` resolve it off the catalog row
  unless `--api-version` says otherwise).
- **Deletion at tier 2** is this same endpoint: `POST
  /ingest/v1/record/DeletedRecord` with `{"record_id": <uuid>, "data_type":
  <base type>}`. Live (401 unauth, probed 2026-07-16), and unpublished in the
  spec like the rest of `/ingest`.
- **Schema discovery (stable v1 catalog):** `GET /data/v1/catalog` lists every
  type with `recordable` + `api_version` fields;
  `GET /data/v1/catalog/{data_type}/{api_version}/schema` returns the JSON Schema
  for that type's record body (e.g. `…/MomentAnnotation/v1alpha1/schema`);
  `GET /data/v1/catalog/{data_type}/{api_version}` returns type metadata incl.
  `record_spec.schema`. Note `MomentAnnotation`'s `api_version` is **`v1alpha1`**,
  not `v1` — read it off the catalog row, don't assume.
- **Custom types (caveat — verified, does NOT work as a path segment; tier 1
  is exempt, `fulcra record` handles this for you):** the
  typed `{data_type}` accepts only **base** types. A custom definition's
  `MomentAnnotation/<definition-uuid>` is **not** a valid path segment —
  `POST /ingest/v1/record/MomentAnnotation/<uuid>` (raw slash or `%2F`-encoded)
  returns `404 {"detail":"Data type '…' not found"}`. To write a record against a
  custom definition you still POST to the **base** type endpoint
  (`/ingest/v1/record/MomentAnnotation`) and reference the definition in the
  record's **`sources`** array as `"com.fulcradynamics.annotation.<definition-uuid>"`.
  Verified 2026-07-08: such a record then reads back under
  `fulcra get-records MomentAnnotation/<definition-uuid>`. (This is the same
  definition-by-source mechanism the legacy writer already uses.)
- **Legacy endpoint (still valid):** `POST /ingest/v1/record` with `DataRecordV1`:
  `{"data": "<string payload>", "metadata": {"data_type": <type>,
  "recorded_at": <iso8601 | {start,end} range>, "source": [<source ids>],
  "tags": [<tag uuid>], "content_type": <optional>}, "specversion": 1}`.
  Batch: `POST /ingest/v1/record/batch`, content-type `application/x-jsonl`,
  one record per line. **Caveat: NOT in the published OpenAPI (53 paths,
  re-checked 2026-07-16)** — but it is **live** (401 unauth on probe 2026-07-16,
  i.e. present and requiring auth; a 404 would mean gone). It works in
  production (the Attention extension and the legacy coord writer POST to it
  daily); it is unpublished, like `/data/v1/updates` once was. **Spec-absence is
  not removal** — a drift check has already misread it that way once. Treat it
  as retirement-eligible and prefer the typed
  endpoint's jsonlines mode for new code. Mind the
  envelope differences vs the typed body: legacy uses `source` (singular key)
  not `sources`, carries the payload as a JSON **string** in `data` rather than a
  top-level `note`, and rides `data_type` inside `metadata`.
- **Still no replace/update at any tier** — a record is appended, then
  tombstoned by a `DeletedRecord`. Corrections are a delete plus a re-record, or
  a superseding record; nothing edits in place.
- The **fulcra-coord** Agent-Tasks annotation writer resolves its **tags** and
  annotation-**definitions** through the public `fulcra` CLI (`fulcra tag
  get/create`, `fulcra catalog`, `fulcra data-type create`) — coord is a
  dependency-light bus that shells out to the CLI rather than importing a client.
  Its **records** still go out over stdlib `urllib` to
  `POST /ingest/v1/record/batch` (the legacy path) — the one remaining raw-REST
  path in the writer, and a holdover: it predates 0.1.37. **The verb it was
  waiting on now exists**, so that POST can migrate to `fulcra record` on the
  same shell-out pattern as its tags and definitions. Unblocked, not yet done.
- **Reads:** tier 1 `fulcra get-records <DataType> "<range>"` — the same
  `Base/<definition-uuid>` shorthand `record` and `delete` take, so a
  write/read-back round trip is three commands with one identifier. `<range>`
  is two ISO8601 timestamps or one relative interval (`"1 day"`, `"3h"`).
  Tier 2 via `/data/v1alpha1/event/{data_type}`.

## Tags (tiers 1 & 2)

Group/label annotations. Tier 1 (CLI 0.1.34): `fulcra tag create|delete|get|list`
(lib: `create_tag`/`create_tags`/`delete_tag`/`get_tag_by_name`/`get_tag_by_id`/
`tags`). Tier 2: `GET|POST /user/v1alpha1/tag`; lookup
`GET /user/v1alpha1/tag/id/{id}` or `GET /user/v1alpha1/tag/name/{name}`; delete
`DELETE /user/v1alpha1/tag/id/{id}`.

## Data queries (read-side, tiers 1 & 2)

- Catalog of everything queryable: `fulcra catalog` /
  `GET /data/v1/catalog` (stable) or `GET /data/v1alpha1/data_types`; metrics
  catalog at `/data/v0/metrics_catalog`. Filters:
  `-c/--category`, `-d/--data-type`, `-n/--name` (partial), `--api-version`,
  `--base-types-only`, and **`--recordable-only`** (0.1.37 — the writable
  subset; see §Annotations). Rows carry `related_cli_commands`: the verbs that
  work with that type. Start here rather than guessing an identifier.
- Raw records for any type: `fulcra get-records <DataType> "<range>"` — records
  may carry multiple sources and need filtering/prioritizing before you treat
  them as an answer.
- Discovery: `GET /data/v1alpha1/data_available` (what data exists for a time
  range) and `/data/v1alpha1/data_sources` (which sources are connected).
- What-changed: `fulcra data-updates "<range>"` (0.1.35) — summary of which
  data types had records processed (with counts) and which uploaded files
  changed over a time range; good for incremental "what's new since I last
  looked" syncs. Backed by `/data/v1/updates` — **now published in the
  OpenAPI** (it was live-but-unpublished at the 07-08 stamp).
- Time series: `/data/v0/time_series_grouped` (arbitrary metrics × time,
  `samprate` resolution); per-metric `/data/v1alpha1/metric/{type}` and
  events `/data/v1alpha1/event/{type}` (both with `/agg/{resolution}` variants).
- Insights: `GET /data/v1alpha1/insight` — derived/insights data (new surface;
  shape still alpha — inspect the OpenAPI response schema before relying on it).
- Schemas: `GET /user/v1alpha1/schema/annotation` and `…/schema/measurement`.
- Domain helpers in lib/CLI: sleep cycles/stages, calendars + events,
  workouts, location time series / at-time / visits.

## Data sharing (tiers 1 & 2) — CLI 0.1.36, fixed in 0.1.38

Share slices of your Fulcra data with another Fulcra user, and read data
shared with you.

- **Tier 1:** `fulcra share create|update|delete|leave|list-incoming|list-outgoing`.
  Reading shared data: `fulcra get-records <DataType> "<range>" --user-id
  <their fulcra_userid>` (requires an active incoming share).
  **Fixed in 0.1.38 — upgrade, don't work around.** 0.1.36/0.1.37 omitted the
  `fulcra_user_name` body field the server had grown a requirement for, so every
  `fulcra share create` 422'd
  (`{'type':'missing','loc':['body','fulcra_user_name']}`); 0.1.38 sends it and
  the REST workaround the 07-15 stamp carried is withdrawn. On **0.1.36/0.1.37
  the 422 still happens** — check your installed version before debugging it.
  Note what 0.1.38 sends: the client fills `fulcra_user_name` with your
  **fulcra_userid**, not a display name, marked temporary in the source until
  the name is available from the identity token. Cosmetic today; don't build on
  that field's contents.
- **Tier 2:** the CLI hits `GET|POST /user/v1alpha1/datashares` and
  `GET|PUT|DELETE /user/v1alpha1/datashare/{datashare_id}` — **live but NOT in
  the published OpenAPI as of 2026-07-13** (the same
  published-later pattern `/data/v1/updates` followed); read shapes off the
  CLI source (`fulcra_api/core.py`) until they publish, and treat them as
  changeable.
- Coordination note: this is the platform's first cross-**principal** surface —
  relevant to the multi-party layer sketched in `COORDINATION-PROTOCOL.md` §6
  (each party owns their store; disclosure crosses an explicit boundary). Not
  yet used by anything in this repo.

## User preferences endpoint — NOT a general store

`GET|POST /user/v1alpha1/preferences` is the **portal/Context UI-state doc**
(timezone, pinned metrics, calendar selections). Flat JSON, whole-doc replace,
no provenance. Don't park agent/preference data here; use files + annotations
(see `packages/fulcra-prefs`).

## MCP server (tier 3) — know its limits

`https://mcp.fulcradynamics.com/mcp` (streamable HTTP, auth required).

- It runs its **own OAuth authorization server** (issuer
  `mcp.fulcradynamics.com`, dynamic client registration, scopes
  `openid/profile/name/email`). **MCP tokens are NOT API tokens** — different
  issuer/audience; they will not authenticate against
  `api.fulcradynamics.com`. Don't try.
- Tool list (verified from source, `fulcradynamics/fulcra-context-mcp`,
  2026-07-06): 11 READ-ONLY tools — get_annotations, get_workouts,
  annotations_catalog, get_metrics_catalog, get_metric_time_series,
  get_metric_samples, get_sleep_cycles, get_location_at_time,
  get_location_time_series, debug_token_info, get_user_info. Run locally
  with `uvx fulcra-context-mcp@latest` (stdio) or use the hosted endpoint.
  Server docs: https://fulcradynamics.github.io/developer-docs/mcp-server/
- **No file or annotation write path via MCP today.** MCP-only agents are
  read-side; write requires tier 1/2 (a gap filed with the platform team).
  fulcra-collect is the write/ingest side of that split: MCP reads what
  collect (and the other tier-1/2 writers) put in.

## Pointers

- Developer docs: https://docs.fulcradynamics.com (API reference + concepts)
- Developer portal (guides incl. MCP server): https://fulcradynamics.github.io/developer-docs/
- OpenAPI: https://api.fulcradynamics.com/openapi.json (public, no auth)
- Python lib/CLI: https://github.com/fulcradynamics/fulcra-api-python
- Agent skills (incl. fulcra-onboarding): https://github.com/fulcradynamics/agent-skills
- Coordination bus conventions: `AGENTS.md` in this repo
