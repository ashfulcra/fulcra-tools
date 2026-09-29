# @fulcra/coord-v5 (opt-in alpha)

Independent Node tools for durable coordination: validation, authorized causal
replay, open-work digests, checkpoints/handoffs and a recoverable SQLite listener.
Unofficial and unsupported; existing coord-engine and fleet schedules are unchanged.
Wire formats remain `gatekeeper-work/1` and `gatekeeper/1`.

## Install

Requires Node 22.16.0 or newer (built-in SQLite constructor timeout API).
Node22.16 prints its built-in SQLite ExperimentalWarning on stderr; this is
not a CLI error, and the package does not suppress runtime warnings.
Verified on Node 26.5.0, with SQLite/import smoke on 22.16.0. Zero runtime
dependencies; no Svelte, prototype checkout, daemon or scheduler. This alpha is
not published to npm. Run `npm pack` here, then in another directory:

```sh
npm install /absolute/path/to/fulcra-coord-v5-0.1.0-alpha.3.tgz
npx --no-install coord-v5 --help
```

## Commands

```text
coord-v5 transport read --config ABS --db ABS --start ZONED --end ZONED
coord-v5 transport publish --config ABS --db ABS --event ABS
coord-v5 transport inspect|replay --config ABS --db ABS
coord-v5 work view --config ABS --db ABS --policy ABS
coord-v5 work digest --config ABS --db ABS --policy ABS --viewer ID --role ROLE --query QUERY
coord-v5 event validate < event.json
coord-v5 checkpoint validate < checkpoint.json
coord-v5 checkpoint package < package-input.json
coord-v5 handoff readiness < readiness-input.json
coord-v5 enrollment plan < assessment.json
coord-v5 enrollment instructions < instructions-input.json
coord-v5 observation --config ABS --policy ABS --db ABS
coord-v5 listener configure|prepare|settle|ack|inspect --db ABS --scope JSON --holder ID
```

## Explicit enrollment

Start with [Join a workspace](docs/onboarding.md) and the [harness guide](docs/harnesses.md).
The enrollment planner selects an interactive or native-listener path from dated
local capability evidence, reuses exact native registrations, and preserves faster
operator intervals. It neither installs a schedule nor grants membership. The
instructions command returns a reversible local instruction block without editing files.

Config is exactly `{baseUrl,principalId,channel,workspaceId,workstreamId,
actorBinding}`. Base URL remains `https://api.fulcradynamics.com/`; channel is
`MomentAnnotation/<stream UUID>`. Actor binding is exactly
`{principal_id,logical_agent_id,instance_id,session_id}`, with principal matching
configuration. Supply your existing owned source and explicit actor binding.
Synthetic examples use principal `00000000-0000-4000-8000-000000000900` and stream
`00000000-0000-4000-8000-000000000901`; these are not usable credentials.
Every live read/publish preflights authenticated principal, catalog channel and
owned annotation metadata. This package does not create sources or grant authority.
SQLite scope cannot be rebound to another source.

Config/event/policy files must be absolute, regular nonsymlink files, at most
64 KiB, private (`0600`) in a private (`0700`) directory. Databases/backups contain
request content and must stay private. Bearers go only on stdin, never argv/config.

Policy is exactly `{principal_id,workspace_id,stream_id,grants,work_jobs}` and must
match cache scope. Grants contain the four exact actor fields plus explicit
`capabilities`; job mappings are `{work_id,job_id}`. Empty arrays are valid and
grant nothing. Configuration is not an authorization grant.

Publishing journals a POST intent before I/O. Acceptance is pending, not verified:
reconcile through a later source-valid read. Never invent replacement IDs or retry
an ambiguous POST. Reads are bounded/partial; failed/empty reads never clear retained
work. An unavailable read exits nonzero even though its failed observation is journaled;
clean partial reads remain usable and exit zero. `transport replay` deliberately
withholds grants.

## Retained views and workflow

`work view` returns `{status:"ready",projection}` using the same policy/replay seam
as the listener bridge and actual accumulated events/source receipts. `work digest`
returns the digest for `--role member|owner|coordinator` and
`--query everything_owed|needs_me|completed_history|lost_track`. Both read an existing
SQLite cache offline. As-of is actual source observation time, not execution time.
Cold/failed-only caches return unavailable and exit nonzero. Partial data or withheld
grants never means global all-clear. Historical-range selection and presence filtering
are API inputs, not CLI features in this slice.

Make private config/policy/event files. Validate the event, publish with bearer stdin,
then read a fresh bounded window with bearer stdin. Run `work view`/`work digest`.
Validate a checkpoint and package it with explicit replay/trust inputs. For listeners,
run `observation`, pass only its successful `.observation` to `listener prepare`, and
correlate `settle`/`ack` with the exact emitted action. A harness must execute actions.

`observation` returns `{observation,diagnostics}` or blocked/unavailable without an
observation. Unknown job mappings never guess targets. Listener scope is
`{principalId,workspaceId,environmentId,harness}`. `configure` takes a route array on
stdin; `prepare` takes a version-1 observation; `settle`/`ack` take exact
wake/attempt/target correlations. `inspect` needs no stdin. The listener never invokes
a harness, creates a task or installs a schedule. Ack does not complete work. Partial
retained obligations are conservative cache state, not proof of unfinished terminal work.

## Source-backed presence and durable roles

Presence/role events use the same `gatekeeper-work/1` annotation source, exact
actor grants and retained source receipts as work. They are not local-only state.
New capabilities are `presence.publish`, `role.manage`, `role.claim` and
`role.checkpoint`; configuration/enrollment grants none automatically.

`presence.observed` has exact payload `{actor,contact_at,inbox_observed_at,
inbox_coverage,progress_at,checkpoint_event_id,contact_due_at,progress_due_at,
checkpoint_due_at,engagement,work_ids}`. Actor must equal the event author in all
four identity fields. Engagement is `{mode:resident|session|occasional,until}`;
only session requires a nonnull zoned `until`. Nullable clocks mean unknown.
Inbox polling/contact never becomes progress. A checkpoint clock derives from an
authorized publication, not a claimed heartbeat time. Incomparable presence heads
remain conflicted; timestamp sorting does not choose one.

Role subjects use stable UUIDs across sessions. Event payloads are:

- `role.defined`: `{name,policy:shared|exclusive}` (`role.manage`), immutable.
- `role.claimed`: `{expected_role_event_id,previous_claim_event_id,expires_at}`
  (`role.claim`); nullable previous claim, exact-session renewal only.
- `role.released`: `{claim_event_id,reason}`; exact claimant or `role.manage`.
- `role.checkpoint`: `{claim_event_id,checkpoint_event_id,body_digest}`;
  `role.checkpoint` plus a current unexpired exact claim and authorized same-actor
  publication with matching digest. Artifact access is independently verified.
- `role.resolved`: `{expected_role_event_id,claim_event_ids,retained_claim_event_ids}`
  (`role.manage`); explicitly references all competing claim heads. No timestamp
  winner. Concurrent renewal/release stays contested until causally resolved.

Every nonnull event reference must be a direct parent, including resolution claim
IDs. A role checkpoint reference survives release/expiry and successor claims,
but does not transfer credentials, artifact access, assignment or execution authority.
Exclusive concurrent claims are contested; shared claims coexist. Lease expiry is
lapsed, not proof nobody works. Claims are coordination evidence, not distributed locks.

`replayWorkEvents` adds authorized `projection.presence` and `projection.roles`.
Subpaths `work-presence` / `work-roles` export
`evaluateWorkPresence({projection,evaluated_at,max_source_age_ms})` and
`evaluateWorkRoles` with the same input. Both return `{status,evaluated_at,
source_as_of,last_successful_observation_at,max_source_age_ms,source_freshness,rows}`.
Presence rows expose separate `clocks` and engagement state. Role rows expose
`state`, `live_claims`, `routing_allowed`, and retained `checkpoint_reference`.

`buildAuthorizedWorkView` accepts those optional evaluation inputs and adds
evaluated `presence`/`roles` arrays beside its projection. Default maximum source
age is **300000 ms (5 minutes)**; acceptance should supply an explicit age. Source
`as_of` stays unchanged. Stale/failed source observations retain evidence but mark
resolution unknown. A strictly newer successful read restores current source
health without deleting earlier diagnostic windows, gaps, or retained work.
Equal-time failures remain unknown. Partial history cannot prove vacancy or enable automatic role
routing; only fresh, complete, held roles permit it. Existing explicitly configured
native routes are separate. CLI digest now consumes authorized retained presence,
with a separate evaluation time for clocks, and never clears partial coverage.

## Pure stdin commands

Each accepts one UTF-8 JSON value, at most 1 MiB:

- `event validate`: event body; returns `{ok,event}` or `{ok:false,errors}`.
- `checkpoint validate`: checkpoint body; returns `{ok,checkpoint}` or errors.
- `checkpoint prepare`: checkpoint body; returns `{ok,checkpoint,canonical_body,
  body_digest,artifact_sha256}` or errors. `canonical_body` is the exact normalized,
  key-sorted UTF-8 upload content, with no trailing newline. Existing validation
  API behavior is unchanged.
- `checkpoint package`: `{checkpoint,publicationEvent,recipient,accessRequirements,
events,trust,observation,asOf}` from `buildHandoffPackage`; returns `{ok,package}`
  or errors. Trust includes scope, exact grants and event evidence.
- `handoff readiness`: `{package,projection,receiver,checks,asOf}` from
  `assessHandoffReadiness`; returns ready/unknown/blocked.

These check serialized caller evidence, not network, artifact bytes or access
independently. Readiness requires separately verified publication/resource receipts
and external-operation checks bound to recipient/package digest with validity times.
Missing checks never become success. Invalid input, refused validation and
unknown/blocked assessments exit nonzero. Parser exceptions return safe codes,
never input or bearer.

Prepare before publishing a checkpoint so timestamp normalization (for example
`Z` to `.000Z`) cannot silently change the body you hash:

```sh
coord-v5 checkpoint prepare < checkpoint.json > prepared.json
jq -j '.canonical_body' prepared.json > checkpoint.canonical.json
```

Upload `checkpoint.canonical.json` unchanged. In `checkpoint.published`, use
`.body_digest` for canonical **normalized checkpoint content** and
`.artifact_sha256` for these exact uploaded **raw bytes**. `jq -j` matters: adding
a newline or pretty-printing changes the artifact byte hash. If you intentionally
upload another JSON serialization, compute its raw SHA256 separately; do not
replace the prepared body_digest. A receiver verifies downloaded raw bytes against
artifact.sha256, then validates/normalizes the parsed checkpoint and compares its
prepared body_digest against publication.body_digest. Both checks are required;
matching bytes alone is not canonical body integrity, and matching normalized
content alone is not proof of exact artifact bytes. Preparing JSON does not upload,
publish, grant access, or independently verify a remote artifact.

When raw and canonical hashes differ, handoff verification receipts must include
`checks.publication.artifact_sha256` matching the publication's raw artifact hash,
in addition to its existing `body_digest` binding. Omitting this additive field is
supported only for legacy publications whose raw hash equals the body digest (or
whose artifact pointer has no SHA256); it cannot attest a distinct upload hash.

## APIs and verification

```js
import {
  contract,
  replay,
  digest,
  checkpoint,
  handoff,
} from "@fulcra/coord-v5";
import { replayWorkEvents } from "@fulcra/coord-v5/work-projection";
```

Subpath exports expose core, transport/listener modules and `work-view`. Replay
requires verified source receipts, exact actor grants, scope and observation evidence;
an event/package cannot grant itself trust. No distributed lock, rollout or exactly-once
external execution is claimed.

Run `npm ci --ignore-scripts` and `npm test`. Tests use synthetic fixtures and real
local SQLite, never live network. `node --test test/*.test.mjs` packs/installs offline
into empty directories, exercises installed commands and audits tarball contents.
Tests/fixtures are excluded from the tarball. See [PROVENANCE.md](PROVENANCE.md).
