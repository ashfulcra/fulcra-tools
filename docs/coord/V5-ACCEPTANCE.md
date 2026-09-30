# Coord v5 alpha acceptance

## Alpha.3 targeted acceptance — 2026-09-30

Runtime acceptance pin: `e3985e2bb18f5148088adc001d7b77c32bf15aef`.
Fresh local verification passed 374 unit tests across 20 suites and four
packed-install tests. Package CI passed on Node 22.16.0 and 26.5.0.

An independent Linux Claude Code cloud tester reports passing the targeted
failed-read/recovery and fresh remote-checkpoint tests against this pin.
Equivalent JSON with different raw bytes passed both raw-artifact and
canonical-body checks. Changed checkpoint content with an accurate raw hash
was rejected by the canonical-body check. These are tester-reported results,
not a claim that the maintainer reran the remote sessions.

The maintainer separately exercised live annotation read, listener-generated
action, actual Codex Desktop receiver acknowledgment, and journal reopen without
duplicate delivery. This bounded existing-session test is not new-session
creation, restart/outage recovery, or sustained-operation acceptance.

Native scheduled reporting-channel reads have also been observed on Codex and
reported by the cloud tester. Reporting-channel polling does not implement the
package's missing Claude dispatcher. Full handoff readiness remains unproven:
bounded transport reads provide no source-completeness attestation, and accepted
successor ownership must be demonstrated separately. No fleet cutover or
cross-account acceptance is claimed. Earlier sections below are historical;
their pending targeted retests are superseded by this section.

## Independent multi-harness report — 2026-09-28

The harness tester reports 18 bounded real reasoning sessions against commit
`de1005e7d8fc3febd8950da6b9d89ea367d59079`: Claude Code with Haiku 4.5,
Codex CLI with GPT-5.4-mini, Hermes with Haiku 4.5, and OpenClaw with Haiku 4.5.
These are tester-reported results; the raw evidence remains in the private test
environment and has not been independently replayed by the maintainer.

- Live publication/readback passed in all four harnesses. Fresh-cache retained
  work, failed-read refusal, source/presence clocks, contested role claims,
  lapse and authorized resolution were exercised by real model sessions.
- A Hermes writer uploaded checkpoint bytes through the separate file adjunct.
  A fresh Claude successor downloaded and verified them, read the event tail,
  built a package and continued work. The tester normalized checkpoint bytes
  as a workaround. Independent handoff readiness receipts were not tested.
- Listener uncertain-send/dedup used a real Codex turn, but locally generated
  receipts do not prove native receiver delivery. The tester's host lacks the
  Codex desktop tool and other harness adapters. Earlier idle Codex heartbeat
  evidence below does not fill those gaps.
- Reported cost was about $1.18, only $0.51 directly measured; the rest was
  estimated. This is not a measured fleet operating cost.

The run exposed sticky historical read errors and checkpoint digest normalization
mismatches. Alpha.3 addresses these with regression coverage and a checkpoint
preparation command. Targeted external retests remain required. Raw file SHA-256
and canonical checkpoint body digest are separate integrity checks. Historical
observation windows stay in the journal when current source health recovers.

Local alpha.3 verification: 373 unit tests across 20 suites and four packed-install
tests pass on Node 22.16.0 and 26.5.0. The installed-command test also exercises
checkpoint preparation and independently hashes its exact output bytes.

Alpha.3 packed artifact: `fulcra-coord-v5-0.1.0-alpha.3.tgz`, 33 allowlisted files,
zero runtime dependencies; SHA-256
`4f76cc5cbd2d414afe65ca7aad8cca9de3901c97f57345b239c786bdf0724972`.

Cross-account, native delivery across these four harnesses, independently checked
handoff readiness, harness restart and sustained operation remain unproven. No
production authority, schedules or fleet enrollment changed.

## Alpha.2 integration — 2026-09-28

The package now includes source-backed presence, durable role claims and
capability-based enrollment. The earlier alpha.1 record below remains historical.

- 366 unit tests across 20 suites and four installed-command tests pass on
  Node 22.16.0 and 26.5.0, including enrollment and presence/role regressions.
- A real owned annotation source accepted definition, claim, checkpoint reference,
  presence and release events. Fresh-process replay retained them. Current contact
  and overdue work progress stayed distinct; partial coverage prohibited automatic
  exclusive-role routing.
- A checkpoint was uploaded to Fulcra, downloaded and SHA-256 verified. A second
  native reasoning session independently retrieved it and the fresh event tail,
  observed release, published a successor role claim and retained checkpoint
  lineage after reopening its cache. This did not transfer task assignment or
  authorize exclusive execution from a partial read.
- A native scheduled Codex heartbeat executed at 12:27 UTC with working installed
  tools/source access. The trial was paused after its receipt. This proves an
  idle scheduled wake, not app restart, host outage or an ongoing deployment.
- The independent session followed the bundled onboarding guide. It correctly
  selected interactive mode and treated the paused registration as reported,
  not active. Its feedback produced a regression fix: interactive plans no longer
  instruct users to keep a schedule alive. Evidence-reference and harness-key
  instructions were clarified without adding warnings to the entry flow.

Final alpha.2 tarball: `fulcra-coord-v5-0.1.0-alpha.2.tgz`, 33 allowlisted files,
zero runtime dependencies, SHA-256
`0af34019a3ca7dbeabbd1cf4b951db09650ab2c78b6502b40b088f982754920d`.
Private receipts, descriptors and uploaded test identifiers stay outside this repository.

Remaining acceptance is explicit: other harnesses, busy delivery, app/container
restart, host outage, credentials, permission prompts, upgrades and sustained
cost/latency. These sessions share an authenticated principal; cross-account
is not proved. No fleet schedule, team authority or existing v4 workspace was
switched. The separate upstream Workspaces annotation skill remains under review;
it is not automatically wire-compatible with this package.

## Result

The standalone bus core passed an installed-package exchange between two real
native Codex sessions on 2026-09-28 UTC. This is an opt-in core release, not fleet
adoption or completion of the full coordination rebuild.

The sender installed a packed tarball outside the source checkout, read existing
work from its owned MomentAnnotation source, published a question, and verified
source-backed readback. A second reasoning session, using the installed command
and a separate SQLite cache, independently read that question, published an
answer, and reconciled the answer through a fresh source read. The sender then
independently read the exact answer.

Both sides reopened their persisted views in new processes. The answer remained
visible, the unfinished work remained in the backlog, and a zero-item recipient
digest retained `clear: false` because coverage was partial. A local checkpoint
capturing the earlier unfinished state validated through the installed command;
the resumed read included the subsequent answer instead of treating the old
checkpoint as current truth.

## Exact artifact

- Package: `@fulcra/coord-v5@0.1.0-alpha.1`
- Package source commit: `2c22cd73`
- Filename: `fulcra-coord-v5-0.1.0-alpha.1.tgz`
- SHA-256: `4939fcd007d8327081c695c7c1d77825b4f9c0a16f3793a02730b2088b924f04`
- 27 allowlisted files; zero runtime dependencies.

The receiver used the immediately preceding tarball. Comparing its installed tree
with the final artifact showed only the README's Node warning note differed; all
runtime bytes were identical. The final artifact was separately installed offline
and used to repeat the sender's read/replay/checkpoint checks.

Build the artifact from the package directory with `npm pack`. Install the local
tarball in a separate directory with `npm install --offline --ignore-scripts
--no-audit --no-fund /absolute/path/to/fulcra-coord-v5-0.1.0-alpha.1.tgz`.
The [package README](../../packages/coord-v5/README.md) defines enrollment and
commands. The package is not published to the npm registry.

## Automated checks

- Node 22.16.0 and 26.5.0: 331 unit tests across 18 suites and two packed-install
  integration tests pass.
- Installed integration tests exercise actual local SQLite, authorized replay,
  partial/unavailable coverage, wrong-scope refusals, checkpoint packaging and
  bounded stdin input. Their HTTP/source/access evidence is synthetic.
- Node 22 emits its built-in experimental SQLite warning. The test harness
  recognizes only that exact warning; a regression preserves other stderr.
  Application runtime warnings are not suppressed.
- Public-tree privacy guard, credential scan, scoped lint, syntax and diff checks
  passed. Private configuration, actor/source identifiers, caches and live test
  receipts are intentionally outside the public repository.
- CI runs the package tests on both tested Node versions. Local results do not
  imply a remote CI run has completed.

## Boundaries

The two sessions used the same authenticated Fulcra account with explicit local
actor grants. They are distinct reasoning sessions, not independently authenticated
principals. The second session was directly dispatched through the native harness;
this was not an autonomous listener wake test. The live read remains bounded and
partial, not a completeness proof.

The question was answered; the underlying work was not marked completed or
assigned. The local checkpoint was not uploaded or published. Distributed
handoff, independently checked artifact access, role takeover, cloud/container
restart, cross-account sharing and other harnesses still require their own
integration tests. Existing authority and schedules were not changed.
