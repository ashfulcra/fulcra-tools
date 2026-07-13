# Fulcra primitives — a field guide for agents

What the Fulcra platform actually provides and how to use it, **by agent
capability tier**. Written so you don't have to re-research the platform
surface. **Full surface re-verified 2026-07-13** (`fulcra-api` CLI/lib
**0.1.36** from PyPI, released 2026-07-10; api.fulcradynamics.com OpenAPI —
**53 paths**, same count as 07-08 but the composition changed: see below; a
diffable paths/schemas baseline now lives at
[`docs/specs/fulcra-openapi-digest.txt`](docs/specs/fulcra-openapi-digest.txt)
so future drift is a `diff`, not archaeology). Prior stamps: 2026-07-07/08 on
0.1.35.

**Drift since 2026-07-08, all shipped in CLI 0.1.36:**

- **NEW `fulcra share` command group** — data sharing between Fulcra users:
  `create|update|delete|leave|list-incoming|list-outgoing`. See §Data sharing.
- **`get-records --user-id`** shipped (query another user's data via an
  active datashare), plus shorthand data-type identifiers — the two items the
  07-08 stamp tracked as unreleased main work.
- **`fulcra file restore`** is now a released CLI verb (was lib-only).
- **Lib BREAKING change:** `resolve_filepath` now returns **`list[dict]`**
  (one element per match/version) where 0.1.35 returned a single dict. This
  silently broke a downstream consumer (fulcra-prefs, fixed 2026-07-13); any
  lib code doing `resolve_filepath(...)["id"]` breaks on 0.1.36 — take
  `[0]["id"]` and handle the empty list.
- **Spec composition:** `/data/v1/updates` and
  `/input/v1/file[_upload]/recent_changes` are now **published** in the
  OpenAPI (both were live-but-unpublished at the 07-08 stamp). The datashare
  endpoints the new `share` CLI hits are today's live-but-unpublished set;
  `/ingest/v1/record/batch` remains unpublished. Records remain CLI-less
  (`data-type` still exposes only `create`/`archive`/`restore`), and the MCP
  server tool list is unchanged (last source-verified 2026-07-06).

> **Staleness warning:** the platform moves fast, and the CLI ships ahead of its
> git main on PyPI — **check the installed `fulcra-api` version, not just the
> repo**. As of 0.1.34 the annotation-**definition** and **tag** commands are in
> the released CLI (`fulcra data-type …`, `fulcra tag …`); annotation **record**
> write/delete/replace are still NOT (records are ingest-only — see below). The
> day record-level commands land (`fulcra data-type --help` shows a `record`/
> `delete-record`/`append`-style verb, or a `fulcra record …` group appears),
> the tier-2 API-direct guidance shifts with them and this doc gets a full
> re-verification + rewrite, not a patch — say so on the bus.

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
  match dicts, not one dict — `matches[0]["id"]`, and treat `[]` as absent.
  Code written against 0.1.35's single-dict return breaks silently.
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

- Tier 1: `fulcra data-type create|archive|restore` (types: moment, duration,
  boolean, numeric, scale; options for tags/units/scale labels/`--add-to-timeline`).
  **New in CLI 0.1.34** — these (and the `tag` group) are now in the released
  CLI; earlier releases (0.1.33) lacked them, so older agents hand-rolled
  definition creation via the tier-2 POST below. The Python lib exposes the
  same: `create_annotation`, `delete_annotation`, `restore_annotation`,
  `annotations_catalog`. Prefer the CLI/lib over raw REST when you have a shell.
- Tier 2: `POST|GET|PUT|DELETE /user/v1alpha1/annotation[/{id}]`, soft-delete
  with `POST /{id}/cancel_deletion` to restore. JSON-schema discovery:
  `GET /user/v1alpha1/schema/annotation`.

**Records** (instances on the timeline) are **write-via-ingest only** — still
true as of CLI 0.1.36; there is no `fulcra` record-write/delete command and no
record-write/delete lib method, only definition + tag management. There are now
**two ingest write paths**. The typed endpoint is **live round-trip verified
2026-07-08**; the legacy single-record path is published in the OpenAPI
(spec-confirmed, not re-round-tripped):

- **Typed endpoint (preferred, new):** `POST /ingest/v1/record/{data_type}` —
  `data_type` is a path segment (e.g. `MomentAnnotation`). The body is the
  **unwrapped** record for that type — NOT the legacy `DataRecordV1` envelope.
  For `MomentAnnotation` the schema is flat:
  `{"note": <str>, "recorded_at": <iso8601>, "tags": [<tag uuid>],
  "sources": [<source id>], "id": <optional uuid, generated if omitted>}`.
  Send `Content-Type: application/json` for a single record, or
  `application/x-jsonl` with **one record per line** for a batch (each line is
  validated independently). A `content-length` header is required. 201 →
  `{"upload_id": <uuid>}`.
- **Schema discovery (stable v1 catalog):** `GET /data/v1/catalog` lists every
  type with `recordable` + `api_version` fields;
  `GET /data/v1/catalog/{data_type}/{api_version}/schema` returns the JSON Schema
  for that type's record body (e.g. `…/MomentAnnotation/v1alpha1/schema`);
  `GET /data/v1/catalog/{data_type}/{api_version}` returns type metadata incl.
  `record_spec.schema`. Note `MomentAnnotation`'s `api_version` is **`v1alpha1`**,
  not `v1` — read it off the catalog row, don't assume.
- **Custom types (caveat — verified, does NOT work as a path segment):** the
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
  checked 2026-07-08)** — it works in production (the Attention extension and
  the legacy coord writer POST to it daily) but is unpublished, like
  `/data/v1/updates`; treat it as retirement-eligible and prefer the typed
  endpoint's jsonlines mode for new code. Mind the
  envelope differences vs the typed body: legacy uses `source` (singular key)
  not `sources`, carries the payload as a JSON **string** in `data` rather than a
  top-level `note`, and rides `data_type` inside `metadata`.
- **No record-level delete/replace yet** — corrections are modeled as new
  records (e.g. a superseding signal), not edits. Record-write/delete **CLI
  verbs** (expected to be built on these typed endpoints) are still NOT shipped;
  **their arrival — not the typed endpoint's — is what triggers this doc's full
  re-verification + rewrite** (a patch won't do; announce it on the bus), and the
  tier-2 guidance shifts to the CLI then.
- The **fulcra-coord** Agent-Tasks annotation writer resolves its **tags** and
  annotation-**definitions** through the public `fulcra` CLI (`fulcra tag
  get/create`, `fulcra catalog`, `fulcra data-type create`) — coord is a
  dependency-light bus that shells out to the CLI rather than importing a client.
  Its **records** remain ingest-only over stdlib `urllib`
  (`POST /ingest/v1/record/batch`, the legacy path) because the platform exposes
  no record-write CLI/lib verb yet. That record POST is the one remaining raw-REST
  path in the writer; it migrates to the CLI once a first-class record-write verb
  ships (and could adopt the typed endpoint meanwhile).
- **Reads:** tier 1 `fulcra get-records <DataType> "<range>"` (user-defined:
  `MomentAnnotation/<definition-uuid>`); tier 2 via
  `/data/v1alpha1/event/{data_type}`.

## Tags (tiers 1 & 2)

Group/label annotations. Tier 1 (CLI 0.1.34): `fulcra tag create|delete|get|list`
(lib: `create_tag`/`create_tags`/`delete_tag`/`get_tag_by_name`/`get_tag_by_id`/
`tags`). Tier 2: `GET|POST /user/v1alpha1/tag`; lookup
`GET /user/v1alpha1/tag/id/{id}` or `GET /user/v1alpha1/tag/name/{name}`; delete
`DELETE /user/v1alpha1/tag/id/{id}`.

## Data queries (read-side, tiers 1 & 2)

- Catalog of everything queryable: `fulcra catalog [--category <c>]` /
  `GET /data/v1/catalog` (stable) or `GET /data/v1alpha1/data_types`; metrics
  catalog at `/data/v0/metrics_catalog`.
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

## Data sharing (tiers 1 & 2) — NEW in CLI 0.1.36

Share slices of your Fulcra data with another Fulcra user, and read data
shared with you.

- **Tier 1:** `fulcra share create|update|delete|leave|list-incoming|list-outgoing`.
  Reading shared data: `fulcra get-records <DataType> "<range>" --user-id
  <their fulcra_userid>` (requires an active incoming share).
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
