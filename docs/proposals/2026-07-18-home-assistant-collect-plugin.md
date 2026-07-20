# Home Assistant data source for Fulcra Collect

**Status:** proposed — implementation is blocked on `collect-maintainer` approval

**Date:** 2026-07-18 (redirected 2026-07-20)

**Owner:** `codex-coder`

**Reviewer:** `collect-maintainer`

## Decision

Build a deliberately read-only **Home Assistant** data-source plugin for Fulcra
Collect. The plugin connects to a user-supplied local Home Assistant URL, uses a
long-lived access token stored in the OS keychain, imports recorder history for
explicitly selected entities, subscribes to `state_changed` events over Home
Assistant's WebSocket API, and reconciles missed events through its REST API.

This replaces the earlier direct HomeKit Accessory Protocol (HAP) controller
proposal. Home Assistant is already the operator's controller, exposes the
selected accessories without re-pairing or removing them from Apple Home, and
keeps the collection path outside Apple's HomeKit APIs and database. Nabu Casa
is neither required nor used in v1.

The plugin is not a remote-control integration and does not write Home Assistant
state. It collects only entities the operator selects, maps known entity domains
and metadata to existing Fulcra annotation types, and reports unknown mappings
as diagnostics rather than guessing.

## Why not the HomeKit APIs

Apple's current Developer Program License Agreement restricts HomeKit API use
to applications primarily designed for home configuration or automation, and
restricts HomeKit API/database information to that purpose and to the Apple
product. The agreement's example specifically prohibits exporting that data to
an external non-Apple database. Fulcra Collect is a personal-data collector and
cloud ingest is its purpose, so a signed companion, read-only behavior, or an
entitlement does not make the HomeKit-framework route acceptable.

A third-party HAP controller such as `aiohomekit` avoids Apple's framework and
database, but usually requires the user to remove an accessory from Apple Home
before pairing it to the new controller. That is unnecessary when Home
Assistant already has the entity. Direct HAP remains a technically distinct
future source, not the default or fallback for this project.

Primary references checked 2026-07-20:

- [Home Assistant WebSocket API](https://developers.home-assistant.io/docs/api/websocket/)
- [Home Assistant REST API](https://developers.home-assistant.io/docs/api/rest/)
- [Home Assistant authentication](https://www.home-assistant.io/docs/authentication/)
- [Home Assistant authentication API](https://developers.home-assistant.io/docs/auth_api/)
- [Home Assistant entity model](https://developers.home-assistant.io/docs/core/entity/)
- [Home Assistant entity registry](https://developers.home-assistant.io/docs/entity_registry_index/)
- [Apple Developer Program License Agreement, HomeKit API terms](https://developer.apple.com/support/terms/apple-developer-program-license-agreement/)

## Source contract

### Connection and authentication

- The user supplies or confirms a local base URL such as
  `http://homeassistant.local:8123`; accepting a private IP address is also
  supported. No remote URL is synthesized and no Nabu Casa account is needed.
- The user creates a Home Assistant long-lived access token. Collect stores it
  as a versioned secret in the plugin's OS-keychain namespace. It never appears
  in `config.toml`, logs, previews, progress events, records, or source IDs.
- REST calls send `Authorization: Bearer <token>` and JSON content negotiation.
- The WebSocket client connects to `/api/websocket`, waits for
  `auth_required`, sends the token in an `auth` message, and proceeds only after
  `auth_ok`. `auth_invalid` is a hard credential failure, not a retry loop.
- v1 targets a local trusted network. HTTPS is supported when configured; the
  plugin never disables certificate verification.

### Entity discovery and selection

Setup fetches the current Home Assistant states and presents an operator-facing
list grouped by domain/device. **Nothing is selected by default.** The operator
chooses the exact entities that may be read and uploaded.

Each selection stores:

- `entity_id` (required);
- entity-registry `unique_id` when Home Assistant exposes one;
- domain, `device_class`, `unit_of_measurement`, and friendly label;
- chosen Fulcra definition and sampling override, if any.

`entity_id` is the API routing identity and can be renamed by the user.
`unique_id`, when available, is the stable registry identity and is preferred
for local state and dedup keys. When no `unique_id` exists, the plugin uses
`entity_id`, detects disappearance/rename as a diagnostic requiring explicit
reselection, and never silently aliases a different entity.

Selection policy is stored under `plugin_settings.home_assistant`; the token is
not. Entity and room names remain local until the user explicitly selects a
stream, after which the selected label may be used in the Fulcra definition.

### Live stream and reconciliation

The plugin is a supervised `service` with
`collect_mode="live_continuous"`:

1. load selected entities and their persistent observation state;
2. connect and authenticate to the WebSocket API;
3. subscribe to `state_changed` events;
4. retain only selected entity IDs and supported mappings;
5. reconnect with bounded exponential backoff and jitter;
6. after every connection or reconnect, fetch current states through REST to
   reconcile events missed while disconnected;
7. perform a periodic REST reconciliation (default five minutes, configurable)
   so a healthy-looking socket cannot hide a missed event.

No Home Assistant service call or state-changing command is implemented. The
WebSocket protocol is used only for authentication, subscription, and
read-only registry/state discovery needed by setup.

### Recorder-history backfill

On first run, import recorder history for selected entities through
`/api/history/period`, using `filter_entity_id`, an explicit start/end window,
and the minimal response form where appropriate.

- Default requested window: ten days.
- The window is configurable and capped by a documented safety maximum.
- Home Assistant installations may retain less or more history. An empty or
  truncated source window is valid and visible in diagnostics, not an error
  that triggers fabricated coverage.
- Fetch in bounded time slices and entity batches to avoid large responses.
- Persist a per-entity backfill cursor only after the corresponding Fulcra
  records are accepted.
- Re-running setup or restarting during backfill is idempotent through the same
  durable source IDs and claim/unclaim flow used for live records.
- Recorder history contains state changes, not a uniformly sampled series; the
  plugin preserves source timestamps and applies the same normalization and
  sampling rules as live observations.

Backfill can be disabled. Live collection never waits indefinitely for a large
history import; the worker interleaves bounded backfill batches with current
events after the initial state reconciliation.

## Sampling and deduplication

### Numeric sampling

Emit the first accepted observation, then emit when either the normalized value
crosses its deadband or the maximum-silence interval expires. Coalesce rapid
updates to at most one accepted record per entity per minute by default.

| Device class / semantic | Canonical unit | Deadband | Max silence |
|---|---:|---:|---:|
| temperature | `degC` | 0.1 °C | 30 min |
| humidity | `%` | 1 percentage point | 30 min |
| carbon dioxide | `ppm` | 25 ppm | 15 min |
| illuminance | `lux` | max(1 lux, 5%) | 15 min |
| power | `W` | max(1 W, 5%) | 5 min |
| energy | `kWh` | 0.01 kWh | 30 min |

Home Assistant may display a user-selected unit. The mapping layer converts a
recognized source unit to the canonical unit before deadband evaluation and
ingest. An incompatible or unknown unit blocks that entity and produces a
diagnostic; it is never treated as unitless.

### Discrete state

Emit a `MomentAnnotation` only when the normalized value differs from the last
persisted value. Startup establishes a baseline and may emit a clearly labeled
`baseline` moment, but it must not claim startup as the transition time. The v1
set includes contact/door/window, motion, occupancy, leak, smoke, lock state,
and on/off entities. Indefinitely open durations and record rewrites are out of
scope.

Home Assistant states such as `unknown`, `unavailable`, `none`, and malformed
numeric strings are availability diagnostics, not measurements or discrete
transitions.

### Persistent state and durable dedup

`PluginState.watermark` is one string and cannot hold independent state for
many entities. Add generic plugin-scoped JSON/KV state to `RunContext`, backed
by Collect's `state.db`, before implementing this source. It must support atomic
read/update per key and long-lived service workers.

Use this stable local identity:

`home-assistant:<instance-id-hash>:<unique-id-or-entity-id>`

Persist normalized value, source timestamp, last observed time, last emitted
time, backfill cursor, and the last known entity ID. The instance hash is
derived from a stable non-secret instance identity established during setup,
not the raw URL or token.

Build a deterministic record source ID from the stable identity, normalized
value, and source observation timestamp. Atomically claim it with
`ctx.claim_dedup_keys` before upload and unclaim it on failed ingest. Never put
the token, raw instance URL, device identifiers, entity names, room names, or
other topology in source IDs.

## Fulcra data model

Use the typed endpoint for every new record. Record construction uses
`fulcra_common.wire.build_typed_record`; upload uses
`IngestPipeline.ingest_typed`. No direct `httpx` ingest and no second Fulcra
authentication path.

| Home Assistant entity | Fulcra record | Rule |
|---|---|---|
| numeric `sensor` with supported `device_class` and unit | `NumericAnnotation` | normalized value, canonical unit, source timestamp |
| supported `binary_sensor` | `MomentAnnotation` | normalized state on change |
| lock state | `MomentAnnotation` | read-only state; never authorization data |
| light/switch/input boolean state | `MomentAnnotation` | on/off change only |
| unsupported domain/device class/unit | none | visible diagnostic; never guessed |

Create or adopt one definition per selected semantic stream through
`RunContext.resolved_definition_id`. Resolve tags through the existing
definition adapter. Coarse tags may include `home`, `environment`, `security`,
and `energy`.

The implementation must include a checked mapping table from Home Assistant
domain + `device_class` + unit to:

- supported/unsupported;
- normalized semantic name;
- Fulcra base type;
- canonical unit and conversion;
- deadband/max-silence policy;
- allowed discrete values;
- privacy classification.

Frequently changing non-state attributes are not ingested in v1. Unknown
attributes and domains are ignored and surfaced in diagnostics, never stored as
raw blobs.

## Fit with Collect's contract

The package should be `packages/home-assistant` / `fulcra-home-assistant` and
expose:

```toml
[project.entry-points."fulcra_collect.plugins"]
home-assistant = "fulcra_home_assistant.collect_plugin:PLUGIN"
```

The `Plugin` declaration uses:

- `id="home-assistant"`;
- `kind="service"`;
- `collect_mode="live_continuous"`;
- `requires_network=True`, because a reachable LAN endpoint is required. This
  is also only descriptive for this service today: Collect's scheduler checks
  it for scheduled runs, while supervised services need their own reconnect and
  failure behavior;
- a REST/WebSocket connection health check;
- setup steps and authenticated loopback routes for URL/token validation,
  entity discovery, selection, mapping preview, and backfill policy.

Collect currently wires plugin-specific routes through hardcoded optional
imports in
[`packages/collect/fulcra_collect/web.py`](../../packages/collect/fulcra_collect/web.py),
so the onboarding PR must add the Home Assistant registration there rather
than assuming a generic route hook.

The worker consumes only `RunContext.config`, `RunContext.credentials`, the
plugin-scoped state/KV API added by PR 1, `RunContext.fulcra_token()`, definition
resolution, dedup claims, and progress/annotation receipts.

The frozen app manifest, menubar dependencies, top-level data-source docs, and
`docs/how-do-i-get-my-data.md` must be updated when the plugin ships. The
WebSocket dependency must be tested in the py2app build.

## User experience

1. Enter or confirm the local Home Assistant URL.
2. Paste a newly created long-lived access token into the authenticated
   loopback UI. The UI sends it directly to keychain storage and never echoes
   it after validation.
3. Test Connection authenticates REST and WebSocket access and reports the Home
   Assistant version without ingesting.
4. Select entities explicitly. The preview shows domain, device class, source
   and canonical units, Fulcra type, definition, and sampling policy.
5. Choose a recorder-history window or disable backfill.
6. Confirm the exact selected streams before enabling the service.
7. Disable stops collection but preserves selection, cursors, and the token.
   “Forget connection” removes the token and local instance state only after
   explicit confirmation.

## Security and privacy requirements

- Read only: no service calls, state writes, automation triggers, or control UI.
- Nothing is selected by default; importing every entity is prohibited.
- Never ingest alarm codes, lock PINs, access-control credentials, camera/audio
  streams, media URLs, precise person/device locations, or raw attribute blobs.
- Person, device-tracker, camera, microphone, alarm-control, and media-player
  domains are out of scope for v1 even if technically readable.
- The long-lived token remains in the OS keychain and is redacted from every
  output and exception path.
- Setup routes remain authenticated loopback routes and reject cross-origin
  requests using the daemon's existing protections.
- Instance, device, entity, and room identifiers stay local unless needed for
  an explicitly selected definition; source IDs use hashes rather than raw
  identity.
- Backfill and live events pass through the same allowlist and privacy mapping.
- Diagnostics expose counts and safe identifiers, never token values or raw
  Home Assistant payloads.

## Implementation plan

No implementation PR starts until this proposal's bus review is `APPROVED`.
After approval, use small PRs built by `codex-coder`, reviewed by
`collect-maintainer`, and merged only with green CI and the exact required bus
verdict.

### PR 1 — generic persistent plugin KV state

- Add a plugin-scoped KV table and migration to Collect's `state.db`.
- Add atomic get/set/update/delete methods to `RunContext` and worker wiring.
- Specify size/type limits and concurrency semantics.
- Test subprocess persistence, atomic updates, and plugin-ID isolation.
- No Home Assistant dependency yet.

### PR 2 — package skeleton, mapping, and pure sampling engine

- Add `packages/home-assistant`, plugin entry point, and frozen manifest entry.
- Implement domain/device-class/unit mapping and normalization as pure code.
- Implement deadband, max-silence, baseline, transition, source-ID, cursor, and
  dedup decisions as pure code.
- Add fixtures for numeric/discrete states, unit conversion, unknown mappings,
  unavailable states, restart recovery, and secret redaction.
- No live Home Assistant connection or ingest yet.

### PR 3 — connection, keychain, entity selection, and onboarding

- Add REST and WebSocket authentication clients with bounded timeouts.
- Add authenticated loopback routes for connection validation, entity
  discovery, explicit selection, mapping preview, and backfill policy.
- Register those optional routes explicitly in Collect's `web.py`.
- Store the token in the OS keychain; persist only non-secret settings in
  `config.toml`.
- Test auth failure, TLS behavior, CSRF/origin protections, redaction, keychain
  round trips, entity rename/disappearance, and unknown mappings.

### PR 4 — recorder backfill, live service, and typed ingest

- Implement bounded recorder-history import for selected entities.
- Add the supervised worker: subscribe to `state_changed`, filter selections,
  reconcile through REST, reconnect/backoff, and shut down cleanly.
- Resolve definitions/tags through `RunContext`.
- Build `NumericAnnotation`/`MomentAnnotation` records with `fulcra-common` and
  ingest through `IngestPipeline`.
- Persist observation/backfill state and use claim/unclaim around every upload.
- Test short retention, empty history, history/live overlap, missed events,
  event storms/coalescing, restart dedup, partial failure, and HA restarts.

### PR 5 — packaging, diagnostics, docs, and limited beta

- Add and freeze the WebSocket dependency in the macOS app.
- Add safe health/diagnostic output and a mapping preview command.
- Update Collect/data-source docs and manual QA checklist.
- Run a limited beta against at least two Home Assistant installations and
  selected numeric, binary, and energy entities.
- Publish only mappings and compatibility verified by fixtures or beta evidence.

## Acceptance criteria

- Setup reads a local Home Assistant instance without Nabu Casa or accessory
  re-pairing.
- The operator must explicitly select every collected entity.
- First-run recorder history and live `state_changed` events converge without
  duplicate Fulcra records, including after restart or missed events.
- Supported numeric values have checked canonical-unit conversion; unsupported
  domains/classes/units fail visibly and create no guessed data.
- The plugin performs no Home Assistant write or control action.
- Tokens, prohibited domains, raw attributes, and topology identifiers never
  leak into config, logs, diagnostics, records, or source IDs.
- All new records use the existing typed-ingest and definition contracts.
- No implementation code lands before this proposal is approved.
- Every implementation PR receives independent `collect-maintainer` review and
  green tests before merge.

## Open review questions

1. Should startup emit a labeled baseline moment for discrete state, or persist
   the baseline and emit only the first later transition?
2. Is ten days the right default requested history window, and what safety cap
   should setup enforce?
3. Are the proposed deadbands/max-silence intervals acceptable for Fulcra query
   quality and ingest volume?
4. Does `collect-maintainer` agree that generic plugin KV state remains PR 1,
   rather than giving this source a private SQLite store?
5. Should v1 treat missing entity-registry `unique_id` as supported with the
   explicit rename/reselection behavior above, or exclude such entities?
