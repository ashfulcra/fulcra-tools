# Fulcra primitives — a field guide for agents

What the Fulcra platform actually provides and how to use it, **by agent
capability tier**. Written so you don't have to re-research the platform
surface. Verified against live services on 2026-07-03 (`fulcra-api` CLI/lib
**0.1.35** from PyPI, fulcra-api-python main @ `62f580b`,
api.fulcradynamics.com OpenAPI — **48 paths**; the file API + a v1 catalog +
an insights endpoint are published in the spec — docs.fulcradynamics.com,
mcp.fulcradynamics.com discovery docs). 0.1.35 added the `data-updates`
command and refined `auth login` (see below); still no record-level commands.

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

Versioned, path-addressed user file store. This is what the fulcra-coord bus
runs on, so it is battle-tested at load.

- **Tier 1:** `fulcra file list|stat|download|upload|delete <path>`
  (`stat` shows version history; deleted files restorable via lib
  `restore_file`).
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
true as of CLI 0.1.34; there is no `fulcra` record-write/delete command and no
record-write/delete lib method, only definition + tag management:

- `POST /ingest/v1/record` with `DataRecordV1`:
  `{"data": "<string payload>", "metadata": {"data_type": <type>,
  "recorded_at": <iso8601 | {start,end} range>, "source": [<source ids>],
  "content_type": <optional>}, "specversion": 1}`
- Batch: `POST /ingest/v1/record/batch`, content-type `application/x-jsonl`,
  one record per line. (This is the Attention extension's write path.)
- **No record-level delete/replace yet** — corrections are modeled as new
  records (e.g. a superseding signal), not edits. CLI record commands are the
  next thing expected to land; that arrival triggers this doc's full rewrite.
- The **fulcra-coord** Agent-Tasks annotation writer resolves its **tags** and
  annotation-**definitions** through the public `fulcra` CLI (`fulcra tag
  get/create`, `fulcra catalog`, `fulcra data-type create`) — coord is a
  dependency-light bus that shells out to the CLI rather than importing a client.
  Its **records** remain ingest-only over stdlib `urllib`
  (`POST /ingest/v1/record/batch`) because the platform exposes no record-write
  CLI/lib verb yet. That record POST is the one remaining raw-REST path in the
  writer; it migrates to the CLI once a first-class record-write verb ships.
- **Reads:** tier 1 `fulcra get-records <DataType> "<range>"` (user-defined:
  `MomentAnnotation/<definition-uuid>`); tier 2 via
  `/data/v1alpha1/event/{data_type}`.

## Tags (tiers 1 & 2)

Group/label annotations. Tier 1 (CLI 0.1.34): `fulcra tag create|delete|get|list`
(lib: `create_tag`/`create_tags`/`delete_tag`/`get_tag_by_name`/`get_tag_by_id`/
`tags`). Tier 2: `GET|POST /user/v1alpha1/tag`; lookup `GET /tag/id/{id}` or
`GET /tag/name/{name}`; delete `DELETE /tag/id/{id}`.

## Data queries (read-side, tiers 1 & 2)

- Catalog of everything queryable: `fulcra catalog [--category <c>]` /
  `GET /data/v1/catalog` (stable) or `GET /data/v1alpha1/data_types`; metrics
  catalog at `/data/v0/metrics_catalog`.
- Discovery: `GET /data/v1alpha1/data_available` (what data exists for a time
  range) and `/data/v1alpha1/data_sources` (which sources are connected).
- What-changed: `fulcra data-updates "<range>"` (0.1.35) — summary of which
  data types had records processed (with counts) and which uploaded files
  changed over a time range; good for incremental "what's new since I last
  looked" syncs. Backed by `/data/v1/updates` (CLI-exposed; not yet in the
  public OpenAPI).
- Time series: `/data/v0/time_series_grouped` (arbitrary metrics × time,
  `samprate` resolution); per-metric `/data/v1alpha1/metric/{type}` and
  events `/data/v1alpha1/event/{type}` (both with `/agg/{resolution}` variants).
- Insights: `GET /data/v1alpha1/insight` — derived/insights data (new surface;
  shape still alpha — inspect the OpenAPI response schema before relying on it).
- Schemas: `GET /user/v1alpha1/schema/annotation` and `…/schema/measurement`.
- Domain helpers in lib/CLI: sleep cycles/stages, calendars + events,
  workouts, location time series / at-time / visits.

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
- Published tool list: not yet documented (docs page is a stub). Evidence in
  docs suggests data-read tools (metrics, calendar, workouts, location).
- **No file or annotation write path via MCP today.** MCP-only agents are
  read-side; write requires tier 1/2 (a gap filed with the platform team).

## Pointers

- Developer docs: https://docs.fulcradynamics.com (API reference + concepts)
- OpenAPI: https://api.fulcradynamics.com/openapi.json (public, no auth)
- Python lib/CLI: https://github.com/fulcradynamics/fulcra-api-python
- Agent skills (incl. fulcra-onboarding): https://github.com/fulcradynamics/agent-skills
- Coordination bus conventions: `AGENTS.md` in this repo
