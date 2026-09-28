# @fulcra/coord-v5 (opt-in alpha)

Independent Node tools for durable coordination: validation, authorized causal
replay, open-work digests, checkpoints/handoffs and a recoverable SQLite listener.
Unofficial and unsupported; existing coord-engine and fleet schedules are unchanged.
Wire formats remain `gatekeeper-work/1` and `gatekeeper/1`.

## Install

Requires Node 22.16.0 or newer (built-in SQLite constructor timeout API).
Verified on Node 26.5.0, with SQLite/import smoke on 22.16.0. Zero runtime
dependencies; no Svelte, prototype checkout, daemon or scheduler. This alpha is
not published to npm. Run `npm pack` here, then in another directory:

```sh
npm install /absolute/path/to/fulcra-coord-v5-0.1.0-alpha.1.tgz
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
coord-v5 observation --config ABS --policy ABS --db ABS
coord-v5 listener configure|prepare|settle|ack|inspect --db ABS --scope JSON --holder ID
```

## Explicit enrollment

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
work. `transport replay` deliberately withholds grants.

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

## Pure stdin commands

Each accepts one UTF-8 JSON value, at most 1 MiB:

- `event validate`: event body; returns `{ok,event}` or `{ok:false,errors}`.
- `checkpoint validate`: checkpoint body; returns `{ok,checkpoint}` or errors.
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
