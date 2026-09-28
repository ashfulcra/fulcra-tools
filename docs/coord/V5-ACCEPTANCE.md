# Coord v5 alpha acceptance

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
