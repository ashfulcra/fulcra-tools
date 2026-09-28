# @fulcra/coord-v5 (opt-in alpha)

An independent Node package for durable coordination work: schema validation,
authorized causal replay, open-work digests, checkpoint/handoff packages, and a
recoverable local SQLite listener. These are unofficial, unsupported tools.
Existing `coord-engine` behavior and fleet schedules are unchanged.

## Install and check

Requires Node **22.16.0 or newer**, including built-in `node:sqlite`. The SQLite
constructor timeout sets this API floor. This extraction was verified on Node
26.5.0; older versions were not executed. There are zero runtime npm dependencies.

This alpha has not been published to npm. From this directory, run `npm pack`.
Then, in a separate installation directory:

```sh
npm install /absolute/path/to/fulcra-coord-v5-0.1.0-alpha.1.tgz
npx --no-install coord-v5 help
```

When a maintainer supplies a packed tarball, install that local file in your own
directory with `npm install /absolute/path/to/fulcra-coord-v5-0.1.0-alpha.1.tgz`.
No prototype checkout, Svelte, Collect, daemon, or scheduler is required.

## Commands delivered in this extraction

```text
coord-v5 help
coord-v5 transport read --config ABS --db ABS --start ZONED --end ZONED
coord-v5 transport publish --config ABS --db ABS --event ABS
coord-v5 transport inspect --config ABS --db ABS
coord-v5 transport replay --config ABS --db ABS
coord-v5 observation --config ABS --policy ABS --db ABS
coord-v5 listener configure|prepare|settle|ack|inspect --db ABS --scope JSON --holder ID
```

**Transport is pinned synthetic, not a usable general production configuration
yet.** Its reserved example principal is
`00000000-0000-4000-8000-000000000900`, annotation stream is
`00000000-0000-4000-8000-000000000901`, and base URL is
`https://api.fulcradynamics.com/`. A real bearer cannot satisfy these synthetic
ownership pins. Generic enrollment/configuration is a later release step; this
extraction does not add grants or create sources.

The config is exactly `{baseUrl,principalId,channel,workspaceId,workstreamId,
actorBinding}`; channel is `MomentAnnotation/<pinned stream>`, and actorBinding is
exactly `{principal_id,logical_agent_id,instance_id,session_id}`. Config/event/
policy files must be absolute, regular nonsymlink files, at most 64 KiB, mode
`0600`, in a private `0700` directory. Databases are also private local files;
protect their backups because retained notes contain request content.

Transport read/publish receive bearer credentials **only on stdin**, never argv
or config. Publishing journals one POST intent before I/O; an upload receipt is
pending, not verified. Reconcile through a later source-valid read; do not invent
replacement IDs or retry an ambiguous POST. Reads are bounded and partial, and
empty/failed reads never clear retained work. `transport replay` deliberately
withholds grants and cannot declare authorized work or global clearance.

`observation` reads an existing cache with explicit policy:
`{principal_id,workspace_id,stream_id,grants,work_jobs}`. Each grant has the four
actor fields plus an explicit `capabilities` array; each mapping is exactly
`{work_id,job_id}`. It prints `{observation,diagnostics}` or a blocked/unavailable
result without an observation. The successful `.observation` is input to listener
`prepare`; do not feed an unavailable result to it. It uses retained source read
time, not cache-open time. Unknown mappings never guess a target.

Listener scope is exactly `{principalId,workspaceId,environmentId,harness}`.
`configure` receives a JSON array of explicit routes on stdin; `prepare` receives
a version-1 observation; `settle` and `ack` receive exact wake/attempt/target
correlations. `inspect` needs no stdin. The listener journals and emits action
descriptors only: **it never invokes a harness**, creates a task, or installs a
schedule. Acknowledgment does not complete work. Partial journal obligations are
conservative cache state, not proof that a terminal task remains unfinished.

## Public APIs, not CLI verbs yet

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

Subpath exports also expose `work-contract`, `work-digest`, `checkpoint`,
`handoff`, `protocol`, `projection`, `listener`, `work-transport-config`,
`work-transport-read`, `work-transport-store`, `work-transport-publish`,
`listener-validation`, `listener-store`, `listener-runtime`, and `work-listener`.
Digest/checkpoint/package/readiness have APIs but no convenient CLI verbs in this
task. Replay requires separately verified source receipts, exact actor grants,
scope and observation evidence; an event or package cannot grant itself trust.
Handoff readiness additionally needs independently verified bytes/access checks.
No distributed lock, production rollout, or exactly-once external execution is
claimed. Wire formats remain `gatekeeper-work/1` and compatible `gatekeeper/1`.

## Contributor verification

With local development dependencies installed, run `npm test`. The extracted
Vitest tests use fixtures and real local SQLite, never live network. The separate
`node --test test/install.test.mjs` packs and installs offline into an empty
directory, verifies the bin and all runtime exports, and audits package contents.
Tests and fixtures stay in the repository but are excluded from the tarball.
See [PROVENANCE.md](PROVENANCE.md) for source files and mechanical transformations.
