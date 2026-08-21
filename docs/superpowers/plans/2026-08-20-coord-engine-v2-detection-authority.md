# Coord Engine v2 Detection Authority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:executing-plans` or `superpowers:subagent-driven-development` to
> implement this plan task-by-task. Use `superpowers:test-driven-development`
> for every behavior change. Steps use checkbox syntax (`- [ ]`) for tracking.

**Goal:** Ship coord-engine `2.0.0` with one truthful change-detection authority,
typed command outcomes, immutable projection generations, bounded-staleness
overlays, and a fleet-verified release gate.

**Architecture:** Follow the approved design in
`docs/superpowers/specs/2026-08-20-coord-engine-v2-detection-authority-design.md`.
Introduce pure typed outcome and detection modules, adapt existing CLI surfaces
without weakening their stronger domain envelopes, build immutable generations
behind a publish-last manifest, and make every public fold validate and overlay
the current generation before rendering. Canonical documents remain authority;
projections are deterministic, replaceable views.

**Tech Stack:** Python 3.10+ stdlib-only package, pytest, existing
`FulcraTransport`, OKF documents, compact JSON through `jsonutil`, and
`budget.Deadline` for every bounded operation.

**Review protocol:** Implement units in order. Each unit ends at a pushed exact
head and opens its own `coord-boss` review register naming the invariant. Do not
start the next unit before the prior exact head is approved. Through 2026-08-23,
`coord-boss` is required and `codex-reviewer` is optional.

**Global constraints:**

- Preserve contract-2 shapes and stronger domain rc semantics while migrating
  their implementation onto shared typed outcomes.
- `NOT_RUN`, `CLEAR`, `DATA`, and `UNKNOWN` are distinct facts. Any required
  `UNKNOWN` makes the process rc nonzero.
- Human and JSON output are projections of the same sealed `CommandOutcome`.
- Never infer record identities from `data-updates` counts.
- Never publish a partial generation or advance a watermark on doubt.
- Every new aggregate-backed read uses the shared degraded marker and one-value
  JSON rules in `AGENTS.md` and `docs/coord/OUTPUT-CONTRACT.md`.
- Update `AGENTS.md`, the coord-engine README, and output-contract docs in the
  same unit whenever agent-facing behavior changes.
- Every unit runs focused tests and the full coord-engine suite before review.

---

## Unit 1: Outcome Spine and Continuity Save Truthfulness

**Invariant:** One typed outcome determines coverage, both renderers, and rc;
continuity save failures can never render as a successful no-op.

**Files:**

- Create: `packages/coord-engine/coord_engine/outcome.py`
- Create: `packages/coord-engine/tests/test_v2_outcome.py`
- Modify: `packages/coord-engine/coord_engine/cli.py`
- Modify: `packages/coord-engine/tests/test_output_contract.py`
- Modify: `packages/coord-engine/tests/test_cli.py`
- Modify: `packages/coord-engine/README.md`
- Modify: `docs/coord/OUTPUT-CONTRACT.md`
- Modify: `AGENTS.md`

- [x] **Step 1: Write red tests for the pure typed outcome model**

Cover the four states, required versus optional surface coverage, deterministic
coverage ordering, unknown-reason preservation, `rc` derivation, compact JSON,
and text/JSON parity from the same sealed object.

```python
def test_required_unknown_makes_rc_nonzero():
    result = CommandOutcome.from_surfaces(
        rows=[],
        coverage=[SurfaceCoverage("tasks", CoverageState.UNKNOWN,
                                  required=True, reason="budget-cut")],
    )
    assert result.state is OutcomeState.UNKNOWN
    assert result.rc == 3
    assert result.to_json()["state"] == "UNKNOWN"
    assert "tasks" in result.to_text()
```

- [x] **Step 2: Run the new outcome tests and confirm import failure**

Run: `uv run pytest packages/coord-engine/tests/test_v2_outcome.py -q`

Expected: FAIL because `coord_engine.outcome` does not exist.

- [x] **Step 3: Implement the minimal outcome model**

Add `OutcomeState`, `CoverageState`, `SurfaceCoverage`, and `CommandOutcome`.
Validate impossible combinations at construction time. Seal `rc` before
serialization. Keep renderer functions pure and deterministic; do not print
inside the model.

- [x] **Step 4: Adapt Class A envelopes through the typed spine**

Write red compatibility tests around `class_a_envelope`, then replace its local
health/rc derivation with an adapter that builds `CommandOutcome`. Preserve the
existing contract-2 envelope byte shape, source validation, basis vocabulary,
and stronger Class B domain envelopes.

Run: `uv run pytest packages/coord-engine/tests/test_v2_outcome.py packages/coord-engine/tests/test_output_contract.py -q`

Expected: PASS, including exact health-to-rc assertions.

- [x] **Step 5: Fix `continuity park` role-doc/lease disagreement red-first**

Add a fixture where `roles/<name>/leases/` exists and contains the caller's
fresh lease but `roles/<name>.md` is absent. Assert:

- park exits nonzero;
- stderr says `could not write` / `CHECKPOINT NOT WRITTEN`, never
  `nothing to park`;
- no snapshot or checkpoint pointer is written;
- role status and park classify the same holder evidence through one helper.

Change `_held_roles` to enumerate both role documents and lease directories,
then route every candidate through `_role_fresh_holders`. Missing metadata for a
leased role is `UNKNOWN`, not negative membership. Track snapshot and
checkpoint-ref write failures and return nonzero if any held role failed to
save; rc 0 certifies every selected save completed.

- [x] **Step 6: Update contracts and run Unit 1 verification**

Document the typed spine and continuity save-path semantics in all three
ship-gate docs.

Run:

```bash
uv run pytest packages/coord-engine/tests/test_v2_outcome.py \
  packages/coord-engine/tests/test_output_contract.py \
  packages/coord-engine/tests/test_cli.py -q
uv run pytest packages/coord-engine/tests -q
git diff --check
```

Expected: all tests PASS; no diff errors.

- [x] **Step 7: Commit, push, verify, and request exact-head review**

Commit with the repository-required co-author trailer. Verify the pushed hash
with `git ls-remote`, then open review slug
`engine-v2-unit-1-outcome-spine` for `coord-boss` at the exact head.

## Unit 2: Identity and Canonical Classifier

**Invariant:** Identity precedence is env-over-host, and empty, tombstoned,
unreadable, and unsupported inputs never collapse into one another.

**Files:**

- Create: `packages/coord-engine/coord_engine/classifier.py`
- Create: `packages/coord-engine/tests/test_v2_classifier.py`
- Modify: `packages/coord-engine/coord_engine/cli.py`
- Modify: `packages/coord-engine/coord_engine/config.py`
- Modify: `packages/coord-engine/coord_engine/transport.py`
- Modify: `packages/coord-engine/tests/test_identity_hostname.py`
- Modify: `packages/coord-engine/tests/test_env_hermeticity.py`
- Modify: `AGENTS.md`

- [ ] Write red table tests for explicit argument, environment, persisted
  identity, and sanitized-host precedence, including two sessions on one host.
- [ ] Write red classifier tests for positive empty, lifecycle-proven tombstone,
  unreadable directory, unreadable listed document, and unsupported shape.
- [ ] Implement one identity resolver and one classified canonical-read seam.
- [ ] Replace direct host fallbacks and ad hoc `None` interpretation in the
  migrated surfaces; preserve domain-specific stronger states.
- [ ] Run focused tests, the full coord-engine suite, and `git diff --check`.
- [ ] Commit, push/verify, and request `engine-v2-unit-2-identity-classifier`.

## Unit 3: Normalized Change Detector

**Invariant:** Ordinary detection consumes one normalized feed batch; record
counts are only zero/nonzero detector signals, never magnitude or identity.

**Files:**

- Create: `packages/coord-engine/coord_engine/change_detection.py`
- Create: `packages/coord-engine/tests/test_v2_change_detection.py`
- Modify: `packages/coord-engine/coord_engine/transport.py`
- Modify: `packages/coord-engine/coord_engine/records.py`
- Modify: `packages/coord-engine/coord_engine/reconcile.py`
- Modify: `packages/coord-engine/tests/test_reconcile_incremental.py`
- Modify: `packages/coord-engine/README.md`

- [ ] Write red tests for envelope validation, namespace coverage, immutable
  identity dedup, deterministic sorting, budget expiry, unknown namespaces, and
  no-envelope transport failures.
- [ ] Write the record-count tests: measured nonzero/enumeration disagreements
  (`2721 -> 9`, `2737 -> 25`) trust the attested immutable identities rather
  than numeric equality; positive-with-none (`1444 -> 0`) and an unproven
  boundary yield record coverage `UNKNOWN`. A zero count is not proof of CLEAR:
  an attested empty cursor window or the named bootstrap recovery is required.
- [ ] Implement `ChangeDetector.poll(...) -> ChangeBatch` and transport seams.
- [ ] Adapt reconcile incremental discovery to consume `ChangeBatch`; retain the
  named full-scan recovery and periodic drift check on any doubt.
- [ ] Run focused tests, the full coord-engine suite, and `git diff --check`.
- [ ] Commit, push/verify, and request `engine-v2-unit-3-change-detector`.

## Unit 4: Immutable Generation Builder and Publication Fence

**Invariant:** A partial build is resumable progress, never the current view;
the manifest advances last and only for a complete deterministic generation.

**Files:**

- Create: `packages/coord-engine/coord_engine/generation.py`
- Create: `packages/coord-engine/tests/test_v2_generation.py`
- Modify: `packages/coord-engine/coord_engine/projection.py`
- Modify: `packages/coord-engine/coord_engine/reconcile.py`
- Modify: `packages/coord-engine/tests/test_projection.py`
- Modify: `packages/coord-engine/tests/test_reconcile.py`
- Modify: `AGENTS.md`

- [ ] Write red tests for deterministic generation ids/bytes, independent
  section deadlines, recovery progress, write/read verification, crash after
  generation write, and stale-writer manifest races.
- [ ] Implement pure section results, immutable generation serialization, and
  the digest-bound current manifest.
- [ ] Publish generation first and manifest last; preserve the old manifest on
  any incomplete/unknown section or failed verification.
- [ ] Fold the existing projection publication fence into the new generation
  validation path and remove duplicate authority only after reader audit.
- [ ] Run focused tests, the full coord-engine suite, and `git diff --check`.
- [ ] Commit, push/verify, and request `engine-v2-unit-4-generation-builder`.

## Unit 5: Freshness Overlay and Public Read Path

**Invariant:** Every public fold validates one generation and overlays canonical
changes through an explicit coverage horizon; blind currentness is impossible.

**Files:**

- Create: `packages/coord-engine/coord_engine/public_read.py`
- Create: `packages/coord-engine/tests/test_v2_public_read.py`
- Modify: `packages/coord-engine/coord_engine/cli.py`
- Modify: `packages/coord-engine/coord_engine/projection.py`
- Modify: `packages/coord-engine/tests/test_query.py`
- Modify: `packages/coord-engine/tests/test_json_purity.py`
- Modify: `docs/coord/OUTPUT-CONTRACT.md`
- Modify: `packages/coord-engine/README.md`

- [ ] Write red tests for manifest/generation validation, digest mismatch,
  overlay-not-run, supported delta application, unsupported delta, overlap
  re-delivery, and lag-bound failure.
- [ ] Pin `[watermark - epsilon, now - epsilon]`, at-least-once dedup, reported
  `coverage_horizon`, and `UNKNOWN` when epsilon or feed coverage is unproven.
- [ ] Route `status`, `board`, `needs-me`, `search`, `inbox`, `digest`, `asks`,
  reviews, forge, roles, presence, and briefing through the shared public-read
  entry point without weakening their existing domain fields.
- [ ] Add one-value JSON and text/JSON parity tests for every migrated verb.
- [ ] Run focused tests, parser-discovered JSON purity, the full suite, and
  `git diff --check`.
- [ ] Commit, push/verify, and request `engine-v2-unit-5-public-read-overlay`.

## Unit 6: Migration, Release, and Fleet Proof

**Invariant:** v2 authority activates only after compatibility, live lag, and
fleet adoption are measured and recorded; merge alone is not completion.

**Files:**

- Modify: `packages/coord-engine/pyproject.toml`
- Modify: `packages/coord-engine/coord_engine/__init__.py`
- Modify: `packages/coord-engine/README.md`
- Modify: `docs/coord/GET-ON-THE-BUS.md`
- Modify: `docs/coord/BUS-V3.md`
- Modify: `docs/coord/OUTPUT-CONTRACT.md`
- Modify: `AGENTS.md`
- Modify: `scripts/adopt-latest.sh`
- Modify: `packages/coord-engine/tests/test_docs_install_pin.py`
- Create: `packages/coord-engine/tests/test_v2_migration.py`
- Create: `docs/coord/evidence/coord-engine-v2-epsilon-measurement.md`
- Create: `docs/coord/evidence/coord-engine-v2-fleet-verification.md`

- [ ] Write red migration tests for v1 bootstrap, mixed-fleet refusal, schema-2
  activation fence, downgrade reads, installer pin identity, and release docs.
- [ ] Add a bounded live measurement command/harness for feed visibility lag.
  Run it from at least two credentialed hosts, record host-neutral evidence and
  date, and configure epsilon at or above the observed maximum. An unmeasured
  epsilon blocks release.
- [ ] Bump package/engine version to `2.0.0`; update install/adoption docs and
  the pinned installer only after the measurement evidence exists.
- [ ] Run synthetic acceptance fixtures for every design matrix row.
- [ ] Run live queue, needs-me, review, forge, roles, presence, and reconcile
  checks from at least two credentialed hosts; record exact build ids and
  outcomes without secrets.
- [ ] Run `uv run pytest packages/coord-engine/tests -q`, then the repository
  suite required by the touched paths on both macOS and Linux.
- [ ] Commit, push/verify, and request `engine-v2-unit-6-release-fleet-proof`.
- [ ] After APPROVE and merge, verify every live host adopted exact `2.0.0` and
  that any live `UNKNOWN` returns nonzero before marking the P0 program done.
