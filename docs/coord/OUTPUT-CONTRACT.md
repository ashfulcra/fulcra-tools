# coord-engine machine-output contract

**Contract version: 1** (rides the engine pin scheme — a pin names the engine
build AND the output-contract version it serves; a contract bump is a fleet
event, never a silent shape change).

**Status:** clauses marked **ENFORCED** are pinned by
`packages/coord-engine/tests/test_output_contract.py` and CI-failing;
clauses marked **TARGET** are the ratified direction with fixtures already
written and registered as pending — flipping one to ENFORCED is a one-line
registry change in that test file, made in the same PR as the behavior.

**Provenance:** every clause below kills a named incident from the codex
strict-consumer evidence pack
(`_coord/agents/codex-coder/reports/2026-08-15-codex-compat-evidence-pack.md`,
incidents C01–C12). The consumer this contract serves is STRICT: one parse,
bounded read, no history, no guessing. Making consumers more permissive was
considered and rejected — a lenient reader turns UNKNOWN into false CLEAR
(pack conclusion 2).

## The strict consumer (normative reader model)

A conforming consumer:

1. reads at most **8 KiB** of stdout unless the first envelope supplies a
   continuation token;
2. performs **exactly one JSON parse** on stdout and rejects prefix/suffix
   prose;
3. makes **no lookup** beyond a returned `ptr`/`of`;
4. treats exit 0 as unclaimable when top-level state is
   UNKNOWN/INVALID/DEGRADED;
5. holds **no fleet history** — only declared schema, build, and
   capabilities;
6. never infers alias equivalence or alternate write paths;
7. resumes a yielded operation from its token without re-issuing the read.

## Clauses

- **OC1 — stream purity (ENFORCED; C07).** With `--json`, stdout carries
  JSON only. Diagnostics, warnings, and breadcrumbs go to stderr. A consumer
  parsing stdout alone must succeed with one parse. (Consumers in
  stream-merging harnesses must be handed stdout separately; the engine's
  side of the contract is purity per stream.)
- **OC2 — envelope first (TARGET; C03, C05, C06).** The FIRST JSON value on
  stdout is an envelope carrying `state` (CLEAR/DATA/DEGRADED/UNKNOWN),
  `source`, coverage (`scanned`/`total` where bounded work ran), and a
  `contract` version stamp. Rows follow inside or after the envelope;
  degradation markers may repeat as rows but the envelope is authoritative.
  Nothing decisive may live only in the tail. (`needs-me` already emits its
  envelope line to stderr for truncation survival — under OC2 it moves into
  the stdout envelope; the stderr echo stays as a courtesy.)
- **OC3 — rc early and meaningful (TARGET; C04, C05).** The process rc is
  determined by the envelope, not the tail: DEGRADED/UNKNOWN required folds
  exit nonzero even when partial rows were served. A command that must yield
  past a harness budget first emits an operation token envelope that
  survives wrapper projection, and the token resumes the SAME read.
- **OC4 — bounded rows (TARGET; C03).** Row output is bounded/paginated;
  exceeding the bound produces a continuation token in the envelope, never
  an unbounded array whose tail carries the verdict.
- **OC5 — act-on-it fields (ENFORCED for blocked rows via the pr-625
  fixtures; TARGET for review rows — C01 is a live gap: `review-pending`
  rows today carry only name/state/pending_required; C12).** Every
  actionable row carries the fields needed to act without a second lookup:
  review rows carry `of`, exact `head`, required reviewers, canonical slug;
  blocked rows carry `blocked_on`, `unlock`, `ptr`, next action —
  independently rendered, never `or`-collapsed. The review-row enrichment
  is the first ladder flip.
- **OC6 — one canonical write path (TARGET; C02, C11).** A verdict exists
  iff its register shard exists; bus signals are derived, never
  constitutive. The register declares its shard schema version; the verb is
  the only supported writer and never guesses between naming eras (readers
  migrate, writers refuse).
- **OC7 — capability identity (TARGET; C08).** Every machine envelope and
  presence stamp carries the exact build (full pin/commit), the contract
  version, and the capability set. Same-semver capability subtraction is a
  CI failure (`capabilities across builds` comparison), not a runtime
  surprise.
- **OC8 — canonical artifact identity (TARGET; C09).** A review register
  binds (repository, PR/artifact, head). Opening a second live register for
  the same identity is refused or recorded as an explicit supersession —
  never two pending truths for one artifact.
- **OC9 — cadence-declared liveness (TARGET; C10).** Presence declares a
  cadence class (`resident`, `batch`, `burst`) and next expected
  observation; liveness folds scale grace to declared cadence. A batch
  worker is not dead for being between bursts.
- **OC10 — degradation is typed everywhere (ENFORCED for the marker
  vocabulary; C05, C06).** Degraded results are typed rows AND (under OC2)
  envelope state; a partial result never renders indistinguishable from a
  complete one.

## Enforcement ladder

1. **This PR**: contract doc; strict-consumer harness; ENFORCED fixtures
   (OC1 stream purity for `queue`/`needs-me`/`board` `--json`; OC5 review
   and blocked-row field presence; OC10 marker vocabulary), plus the full
   TARGET fixture set registered pending — the adversarial cases from the
   pack (missing `of`, bus-only verdict, degradation beyond the 8 KiB
   boundary, merged-stream warning, yield-with-empty-body, same-semver
   capability subtraction, duplicate aliases, batch-liveness trace) exist
   in the file and are flipped on as their clauses land.
2. **Next**: OC2/OC3 envelope-first on the read verbs (one verb per PR,
   fixture flipped in the same PR).
3. **Then**: OC6 canonical-writer enforcement, OC7 capability stamps, OC8
   identity, OC9 cadence classes.

A clause flip without its fixture flip — or the reverse — is a review
CHANGES by policy.
