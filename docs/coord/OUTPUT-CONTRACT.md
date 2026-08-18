# coord-engine machine-output contract

**Contract version: 2** (rides the engine pin scheme — a pin names the engine
build AND the output-contract version it serves; a contract bump is a fleet
event, never a silent shape change). Contract 2 introduces the Class A
envelope (design: `oc2-oc3-envelope-design-r2`, APPROVED 2026-08-18) and is
adopted ONE VERB PER PR: a verb whose `--json` stdout is an object carrying
`"contract": 2` serves the envelope below; a verb still emitting a bare
top-level array is on the contract-1 shape, which remains documented and
valid until that verb's own ladder PR. The stamp inside the value is the
shape detector — consumers never sniff.

## Verb classes

**Class A — bare-array folds** (`needs-me`, `board`, `inbox`, `asks`,
`obligations`, `search`): migrate to the envelope. **Class B —
domain-envelope verbs** (`queue`, `review status`, `roles status`): already
lead stdout with one decisive domain object and are EXEMPT from reshaping —
a stronger existing envelope is never replaced by a weaker generic one;
their only contract-2 change is additive `contract`/`source` stamps where no
key collision exists, `queue`'s result/error fields and cursor-v2 delivery
token preserved byte-for-byte.

## The Class A envelope

The FIRST and ONLY JSON value on stdout: `{"contract": 2, "health":
DATA|CLEAR|DEGRADED|UNKNOWN, "source", "as_of"?, "degraded": [marker types],
"basis": [failure classes], "rows": [...]}`. `health` is transport/fold
health ONLY (never a domain state; rows keep their own fields). Ordered
selection: **UNKNOWN** — the authority itself untrusted (`source-unreadable`
/ `source-invalid` / `fallback-failed`): rows MUST NOT be acted on;
**DEGRADED** — readable authority, partial coverage (`budget-cut`,
`subset-unreadable`, `role-resolution-partial`): rows are a usable FLOOR,
absence-inference forbidden; **DATA** / **CLEAR** — complete scan; CLEAR is
the only health licensing "there is nothing for me". rc is a pure function
of health (UNKNOWN|DEGRADED → 3, DATA|CLEAR → 0), sealed with the envelope
BEFORE row serialization, and applies in text mode too.

## Consumer guidance

- Parse stdout as ONE value; object with `contract` → trust `health` first;
  bare array → contract 1, scan for marker rows.
- ANY incomplete parse is UNKNOWN. Prefix extraction from a truncated
  buffer is FORBIDDEN — the envelope's claim under truncation is LOUD
  failure, not early state. Never truncate an authority read with shell
  tools (`tail`/`head`) — bound at a supported surface (future OC4
  continuation tokens) or treat the verb as unavailable (named fleet defect
  class, 2026-08-18).
- rc 3 with rows present means a floor — act on what is present only if
  your task tolerates "at least this much exists", and say so.

**Status:** clauses marked **ENFORCED** are pinned by
`packages/coord-engine/tests/test_output_contract.py` and CI-failing;
clauses marked **TARGET** are the ratified direction: probeable targets are
executable `xfail(strict=True)` tests in that file (behavior landing without
its flip fails CI via strict XPASS); unprobeable ones are documentation-only
registry entries. The flip is made in the same PR as the behavior.

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
- **OC2 — envelope first (ENFORCED for `needs-me` — ladder PR after flip 1;
  TARGET for the remaining Class A verbs; C03, C05, C06).** The FIRST JSON
  value on stdout is the Class A envelope above — health, source, basis, and
  rows INSIDE one object; degradation markers may repeat as rows but the
  envelope is authoritative. Nothing decisive may live only in the tail.
  (`needs-me`'s stderr envelope line stays as a courtesy duplicate; the
  stdout envelope is the authority.)
- **OC3 — rc early and meaningful (ENFORCED for `needs-me`, same PR as its
  OC2 flip; TARGET elsewhere; C04, C05).** The process rc is a pure function
  of envelope health, sealed before row serialization: DEGRADED/UNKNOWN
  required folds exit nonzero even when partial rows were served — in text
  mode too. (This deliberately widened `needs-me`'s old forge-only rc:
  degraded role/review folds now fail closed as well.) The yield half — a
  command that must yield past a harness budget first emits an operation
  token envelope that survives wrapper projection, and the token resumes
  the SAME read — stays TARGET (the envelope reserves `"operation"`).
- **OC4 — bounded rows (TARGET; C03).** Row output is bounded/paginated;
  exceeding the bound produces a continuation token in the envelope, never
  an unbounded array whose tail carries the verdict.
- **OC5 — act-on-it fields (ENFORCED; C01, C12).** Every actionable row
  carries the fields needed to act without a second lookup: review rows
  carry `of`, exact `head`, required reviewers, canonical slug; blocked
  rows carry `blocked_on`, `unlock`, `ptr`, next action — independently
  rendered, never `or`-collapsed. The review-row enrichment landed as
  ladder flip 1: `review-pending` rows serve `of` and `head` on every
  path (projection schema v2 plus both raw folds), with an EXPLICIT null
  when the register doc genuinely lacks the field (a legacy headless
  review has no head to serve — the register is the honest source, never
  a guess), and `review request` requires `--of` so new registers always
  carry the pointer.
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

1. **Contract PR (landed)**: contract doc; strict-consumer harness;
   ENFORCED fixtures (OC1 stream purity for `needs-me`/`board`/`inbox`
   `--json` plus the exact C07 queue-warning shape proven onto stderr;
   OC5 blocked-row field presence; OC10 marker vocabulary). Probeable
   TARGETS are executable tests marked `xfail(strict=True)` so behavior
   landing without its flip is a hard CI failure, not a silent pass. The
   remaining pack cases (yield token, pagination, bus-only verdict,
   capability subtraction, duplicate aliases, batch-liveness trace) sit
   in a documentation-only registry until their probes have
   infrastructure to stand on; full queue-payload purity joins OC1 when
   a records-capable test transport lands.
2. **Flip 1 (landed)**: OC5 review-row `of`+`head` enrichment — served
   from the v2 reviews projection and both raw folds, xfail probe
   flipped to ENFORCED in the same PR, per policy.
3. **Envelope PR 1 (landed)**: contract bumps to 2; `needs-me` serves the
   Class A envelope, rc follows health, the OC2 strict-xfail probe flips
   to ENFORCED, health-rule fixtures added.
4. **Next**: the remaining Class A verbs, one PR each — `board`/`inbox`,
   then `asks`/`obligations`/`search`; Class B additive stamps LAST, after
   every Class A verb proves the pattern (`queue` reshaping: never).
5. **Then**: OC6 canonical-writer enforcement, OC7 capability stamps, OC8
   identity, OC9 cadence classes.

A clause flip without its fixture flip — or the reverse — is a review
CHANGES by policy.
