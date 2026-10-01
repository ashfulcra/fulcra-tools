# @fulcra/coord-v5 (opt-in alpha)

Independent Node tools for durable coordination: validation, authorized causal
replay, open-work digests, checkpoints/handoffs and a recoverable SQLite listener.
Unofficial and unsupported; existing coord-engine and fleet schedules are unchanged.
Wire formats remain `gatekeeper-work/1` and `gatekeeper/1`.

See [current release evidence and remaining gates](../../docs/coord/V5-RELEASE-STATUS.md)
and the [acceptance ledger](../../docs/coord/V5-ACCEPTANCE.md). Bounded native
delivery/lifecycle evidence is separate from complete-history and full-handoff proof.

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
coord-v5 checkpoint prepare < checkpoint.json
coord-v5 checkpoint package < package-input.json
coord-v5 checkpoint verify --artifact ABS --publication ABS
coord-v5 handoff readiness < readiness-input.json
coord-v5 handoff verification import --config ABS --db ABS --receipt ABS
coord-v5 enrollment plan < assessment.json
coord-v5 enrollment instructions < instructions-input.json
coord-v5 observation --config ABS --policy ABS --db ABS
coord-v5 listener configure|prepare|settle|ack|inspect --db ABS --scope JSON --holder ID
coord-v5 listener dispatch-claude --db ABS --scope JSON --holder ID --executable ABS [--timeout-ms N]
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
64 KiB, private (`0600`) in a private (`0700`) directory. Handoff receipt input
has the same private-file rules and a 2 MiB limit. Databases/backups contain
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

`handoff verification import` accepts one operator-trusted local attestation and
retains it in the private SQLite database across restarts. Its exact envelope is
`{schema:"handoff-verification/1",principal_id,workspace_id,workstream_id,
stream_id,package,verification}`. `package` is the validated handoff package whose
digest the offer and ready events name; `verification` is the existing replay
verification shape with ready/offer IDs, digests, and `checks`. The importer
requires matching authenticated retained ready, offer, and checkpoint-publication
events, exact receiver/publication bindings, and a verified check for every package
access requirement. Identical imports return `same`; conflicts, stale checks and
invalid input are blocked. A damaged stored receipt makes the view unavailable.
Identical event content in multiple authenticated source records counts as one
referenced event; differing content under the same event ID still fails closed.
With multiple valid ready events for an offer, expired proofs survive only when
required by a replay-accepted transfer's causal history. Unrelated expired
pending readiness remains inactive.
Every handoff row additionally carries `verification`, the current validity of the
proof behind that handoff, reported separately from `state`:
`{state:"valid"|"expired"|"absent"|"unknown",valid_until,ready_event_id}`. For accepted
handoffs, this is the proof named by the accepted event's `ready_event_id`, even
when an earlier retained ready event is displayed on the row. Causal acceptance
and current proof validity are orthogonal: a lapsed proof leaves an accepted
handoff and its ownership unchanged, but reports `verification.state:"expired"`
so consumers can re-verify before acting. `absent` means no retained receipt
resolved for the row and only occurs before readiness. Pure replay defaults the
verification clock to `asOf`; an explicitly supplied malformed
`verificationAsOf` reports `unknown` without changing causal state or ownership.
Replay returns early without an `asOf`, so no row claims validity with no causal
clock.
Authorized views consume retained proofs automatically; expired checks cannot
make a pending handoff currently ready, but a valid historical acceptance keeps
its ownership after expiry. This command trusts the local operator's assertion:
it does not fetch an artifact, test remote access, issue a native wake, or prove
source completeness. Partial history stays partial and cannot authorize an
exclusive takeover or all-clear.

Make private config/policy/event files. Validate the event, publish with bearer stdin,
then read a fresh bounded window with bearer stdin. Run `work view`/`work digest`.
Validate a checkpoint and package it with explicit replay/trust inputs. For listeners,
run `observation`, pass only its successful `.observation` to `listener prepare`, and
correlate `settle`/`ack` with the exact emitted action. A harness must execute actions.

`observation` returns `{observation,diagnostics}` or blocked/unavailable without an
observation. Unknown job mappings never guess targets. Listener scope is
`{principalId,workspaceId,environmentId,harness}`. `configure` takes a route array on
stdin; `prepare` takes a version-1 observation; `settle`/`ack` take exact
wake/attempt/target correlations. `inspect` needs no stdin. `prepare` never invokes
a harness; only explicit `dispatch-claude` invokes a bound Claude CLI session.
The listener never creates a session/task or installs a schedule. Ack does not complete work. Partial
retained obligations are conservative cache state, not proof of unfinished terminal work.

### Explicit Claude CLI delivery

Use scope harness `claude-code` (not Claude desktop/cloud/web). Private routes
bind an existing session and trusted working directory, separate from Codex
`threadId`/`hostId` routes:

```json
[{"jobId":"synthetic-job","logicalIdentity":"synthetic-agent","lifecycle":"active","target":{"kind":"claude-code","sessionId":"11111111-1111-4111-8111-111111111111","cwd":"/absolute/trusted/workspace"}}]
```

The UUID is deliberately invented, not a usable native session. Keep real route
inputs, session IDs, CWD mappings and the SQLite journal private. A Claude route
cannot also contain a Codex thread/host. Retired routes may resolve to an active
Claude coordinator; rebinding a session or CWD supersedes old attempts.

`prepare` emits `{wakeId,attemptId,tool:"coord-v5 listener dispatch-claude",
arguments:{wakeId,attemptId,target}}`. Pass only `.arguments` on stdin to the
explicit dispatcher; it reconstructs the bounded prompt from journaled items.
The trusted operator supplies the absolute executable via `--executable`, never
an event, route, prompt, arbitrary argv or shell command. CLI authentication must
already work; this command neither signs in nor changes credentials.

Dispatch fixes `--print --resume UUID --model claude-haiku-4-5-20251001 --max-budget-usd 0.25
--output-format json --safe-mode --tools '' --disable-slash-commands
--strict-mcp-config --mcp-config '{"mcpServers":{}}' --permission-mode plan
--no-chrome`. The prompt goes on stdin. Safe mode, disabled skills, zero built-in
tools and an empty strict MCP configuration exclude ambient execution tools.
Unsupported flags fail closed, with no fallback invocation. These flags request
the full model ID `claude-haiku-4-5-20251001` and a $0.25 provider budget; the
adapter enforces exact argv, not provider model routing or billing. The served
conversational model may differ despite the requested model ID: inspect actual
assistant-model telemetry. A controlled
fresh-print comparison in one environment motivated the full-ID request; it is
not universal routing evidence or resumed-session proof. Auxiliary `modelUsage`
entries do not identify the conversational model. A successful run below the
requested budget does not prove cap enforcement or a hard spend ceiling.
The adapter bounds runtime to at most 60 seconds (lower with `--timeout-ms`) and
combined stdout/stderr to at most 64 KiB. Timeout/output overflow kills only the
owned child; this is not descendant cleanup or a provider cancellation guarantee.
Raw process output and secret-bearing diagnostics are not printed or journaled.

A SQLite transaction commits `dispatching`/`claimed` before spawning. Only exit
0 JSON with `type:"result"`, `subtype:"success"`, `is_error:false` and the exact
`session_id` yields `{wakeId,attemptId,state:"accepted",code:"NATIVE_ACCEPTED"}`
and exit 0. Malformed, mismatched, timed-out, oversized or failed output yields
`uncertain` and a safe code at exit 2. Repeat/stale/busy dispatch is refused at
exit 2; invalid input exits 1. Accepted is native invocation acceptance, not a
receiver acknowledgment, work completion, schedule/lifecycle proof, or complete
provider history. `listener ack` remains a separate exact-target action.

Claims fence duplicate attempts and other attempts for the same native session
only within this journal/scope. The operator must prevent concurrent resume by
other processes, journals, scopes or interactive sessions. A native CLI refusal
is uncertain, never proof that a message was not delivered. Natural process exit
is tested independently of crash recovery: a crash or failed settlement leaves
a claim requiring reconciliation, and no claim/uncertain invocation retries
automatically. Before explicitly attesting a later acceptance with `settle`,
establish that no dispatcher is still executing and independently verify the
exact attempt/session. An acknowledgment alone does not release an ambiguous
dispatch claim. Never manufacture a replacement session to retry ambiguity.

Synthetic executable fixtures and the packed-install tests verify these local
contracts without contacting Claude. Authenticated native acceptance, busy-session
behavior, restart/host outage, scheduled wakes and provider-version compatibility
require separate live evidence; fixture success proves none of them.

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

These stdin commands check serialized caller evidence, not network, artifact bytes or access
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

For a locally obtained artifact and its `checkpoint.published` event, run
`coord-v5 checkpoint verify --artifact /private/path/checkpoint.json --publication
/private/path/publication.json`. Both paths must be absolute, regular, nonsymlink
files with mode `0600` inside a `0700` directory. Artifact bytes are capped at
256 KiB and publication JSON at 64 KiB; validated checkpoint content retains the
128 KiB canonical limit. The command hashes the exact artifact bytes before
strict UTF-8/JSON decoding, then checks normalized content digest, scope,
checkpoint/work/assignment bindings, and full actor identity. A successful
`{ok:true,checkpoint,verification}` records local byte/content measurements only:
it does not authenticate remote retrieval, access, a native wake, or source
completeness. Rejections return stable codes without echoing paths or content and
exit nonzero. The library exposes `verifyCheckpointArtifact(bytes,
publicationEvent)` for the same bounded check.

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
