# Reverse Specification Comparison

**Compared artifacts:**

- **Tycho:** `team/<team>/_coord/docs/2026-08-14-system-spec.md`, 381 lines,
  derived from `fulcra-tools` main `393a7449`.
- **Superpowers exercise:**
  `docs/superpowers/specs/2026-08-14-collect-coord-bus-rebuild-design.md`,
  446 lines after comparison corrections, plus the 661-line executable plan at
  `docs/superpowers/plans/2026-08-14-collect-coord-bus-rebuild.md`.

Both inspect the same source snapshot. The Superpowers output was source-first but
not blind: Tycho's existence and broad section outline were known before drafting;
the exact Tycho artifact was fetched only for the comparison pass. Claims below
are grounded in the pinned source rather than textual novelty.

## Executive Result

The outputs agree on the architecture and its most important safety laws. They are
complementary rather than contradictory:

- Tycho produced the better **single-document system briefing**. It is concise,
  cohesive, and unusually effective at orienting a reader who knows nothing.
- The Superpowers exercise produced the better **implementation contract**. It
  separates portable behavior from Fulcra bindings, spells out state machines and
  evidence races, and follows the spec with 80 test-first execution steps.
- Tycho's main material omission is the shipped append-only review-verdict shape
  and the evidence-binding rule that makes cached approval safe.
- The Superpowers draft initially under-specified consent and cross-account trust.
  The comparison caught that; the design and plan now state it normatively.

The best canonical package is Tycho's overview as the entry point, the independent
design as the normative deep specification, and the Superpowers plan as the build
and acceptance procedure.

## Dimension-by-Dimension Comparison

| Dimension | Tycho | Superpowers exercise | Assessment |
|---|---|---|---|
| Audience orientation | Opens with one human, one account, three products, and a compact diagram | Opens with objective, audience, design boundaries, then the system model | Tycho is faster for a demo; Superpowers is clearer for implementers |
| Shipped vs aspirational | Strong status statement and a dedicated in-flight section | Portable/Fulcra/as-built layers plus feature-gated no-janitor section | Equivalent intent; Tycho labels the snapshot more compactly |
| Fulcra substrate | Names four primitives, API floor, auth file behavior, server timestamps, and no CAS | Names the same primitives, no-CAS consequence, and trust/disclosure boundary | Tycho is stronger on concrete platform/auth details |
| Identity | Covers claim-based identity and four tag dimensions | Adds machine/cloud/harness/model reporting semantics, session override precedence, and identity movement | Superpowers is stronger on continuity/reporting behavior |
| Event protocol | Exact v1 envelope, channel authority, queue, poison, peek/consume, cursor v1/v2 | Same, with explicit dedup/order rules and authority/environment separation | Substantively aligned |
| Tasks and obligations | Covers states, block/unlock, operator asks, tell/respond, FYI | Gives the exact transition graph, terminal immutability, durable-first orchestration, backlog distinction, and atomic answer return | Superpowers is more mechanically complete |
| Reviews | Excellent operational gate: exact head, required set, rounds, three-head equality, settle and GC | Includes all of that plus append-only shard names, newest-per-reviewer fold, immutable-evidence digest, and unsafe mutable-cache refusal | Superpowers captures more shipped concurrency behavior |
| Presence and roles | Strong operational doctrine, cadence-aware liveness, nonce takeover, escalation | Adds the complete role result vocabulary and explicit UNKNOWN-not-VACANT rule | Superpowers is more precise; Tycho is more operationally readable |
| Folds and output | Strong explanation of deterministic folds, budgets, degraded rows, strict-consumer target | Gives the feed-overlay algorithm, projection eligibility, session read repair, and output envelope acceptance | Aligned; Superpowers is more executable |
| Continuity | Concise snapshot/park/resume incident rules | Specifies checkpoint fields, timestamp validation, tie-breaking, and park gate | Superpowers is more schema-oriented |
| Coord Engine | Complete capability inventory and runtime constraints | Same capabilities grouped as contracts, with thin-CLI architecture and task-by-task tests | Tycho is better as inventory; Superpowers is better as build guidance |
| Collect | Correct daemon shape, keychain/config boundary, plugin execution, account fingerprint | Adds Python floor, exact kind/mode enums, plugin discovery failure isolation, setup/health/freshness callbacks, KV/dedup, definition revalidation, worker protocol | Superpowers is materially more complete |
| No-janitor evolution | Correctly confines manifest/channel/fence to ratified in-flight work | Separates it as a feature-gated target and supplies explicit negative tests | Aligned; Superpowers makes rollout safety easier to execute |
| Consent/trust | Strong operator-law invariant and datashare context | Initially thin; corrected to a hard normative disclosure boundary | Tycho caught this earlier and more naturally |
| Acceptance | Lists interoperability requirements and mentions `acceptance pair` | Defines twenty design-level acceptance tests and sixteen TDD tasks with live bidirectional oracle tests | Superpowers is substantially stronger |

## Strong Agreements

Both outputs independently converge on these architectural truths:

1. Fulcra records are bounded event signals; Fulcra files hold durable coordination
   state.
2. There is no central broker and no correctness dependency on a resident daemon.
3. Last-writer-wins storage forbids safe cursor-v2 activation until CAS is proven.
4. At-most-once event delivery is acceptable only because obligations live in
   durable documents first.
5. UNKNOWN must remain distinct from clear, absent, approved, complete, or vacant.
6. Materialized projections are caches and must be rejected or overlaid when
   freshness cannot be proved.
7. Review approval is exact-head evidence, not a PR comment or model assertion.
8. Roles outlive sessions; presence and nonces make their leases observable.
9. Collect isolates plugin crashes and credentials behind a host-owned contract.
10. Fleet-wide authority belongs in a reviewed repository artifact; an annotation
    channel can safely act only as a wake hint under today's trust model.

This degree of convergence is meaningful because the two documents organize the
source differently: Tycho follows the product narrative, while the Superpowers
design follows authority boundaries and executable invariants.

## What Tycho Did Better

### 1. Demo-quality narrative

Tycho answers “what is this?” in the first page. The human/account framing and
small system diagram make Coord continuity legible before introducing protocol
mechanics. The independent design is denser and assumes the reader has chosen to
build.

### 2. Platform specificity

Tycho records the minimum `fulcra-api` version, OAuth credential shape, file mode,
server-assigned record timestamps, and datashare role. Those are useful bootstrap
facts and should remain in the canonical front-door document.

### 3. Operational language

Phrases such as “three heads equal,” “operator is batched, never dripped,” and
“claiming is not executing” compress incident-earned doctrine into memorable
rules. The Superpowers version translates these into tests but loses some teaching
power.

### 4. Scope discipline

Tycho keeps a broad system at 381 lines and puts all in-flight work in one place.
It is the more practical artifact to hand to an agent or human for initial
orientation.

## What the Superpowers Exercise Did Better

### 1. Portable contract versus current implementation

The design explicitly separates behavior that another implementation must honor
from Python module layout and CLI-parser accidents. That makes “rebuild” concrete:
compatibility is judged by wire formats, documents, folds, and failure semantics.

### 2. Exact state and evidence mechanics

It includes the full Task transition graph and review evidence behavior found in
`model.py`, `tasks.py`, and `review.py`. In particular, the shipped review verb uses
append-only names of the form:

```text
<head>--<reviewer>--<timestamp>-<digest>.md
```

The plain `<head>--<reviewer>.md` form remains readable but mutable. A cached
approval may therefore short-circuit only when every shard name is immutable and
the current evidence digest matches. Tycho's simplified path omits this race fix,
which matters to an interoperable writer.

### 3. Collect completeness

The source exposes more than “scheduled/service/manual plus credentials and
settings.” Collection mode is an independent enum; registry failures are isolated;
the run context owns atomic plugin KV, dedup claims, credential rotation, progress
and annotation events, freshness evidence, and definition resolution. These are
not UI decoration; they are plugin compatibility boundaries.

### 4. Executability

The implementation plan provides exact files, interfaces, failing-test intent,
commands, expected results, and commit boundaries. It also requires bidirectional
oracle tests against the pinned reference implementation and a disposable live
team. Tycho explains acceptance; the plan operationalizes it.

### 5. Negative-space tests

The plan repeatedly tests what must not happen: no cursor advance on UNKNOWN, no
event after failed durable write, no stale-cache approval, no degraded-vacant
classification, no plugin-wide crash, no secret echo, no forged directive
authority, and no below-fence mutation.

## Corrections and Risks

### Tycho output

1. **Review shard path is over-simplified.** It presents
   `<head>--<reviewer>.md` as the canonical shape but the shipped verb writes an
   append-only timestamp/digest suffix. A clean writer following only Tycho's path
   can reintroduce last-writer-wins evidence loss.
2. **Collect's public contract is compressed too far.** An independently authored
   plugin would not know all required collection modes, setup metadata, freshness
   behavior, KV/dedup capabilities, or registry isolation rules.
3. **Task states are listed, not fully specified.** Without the exact transition
   graph, another engine can accept transitions the current engine rejects.
4. **Acceptance is not directly executable.** It needs fixtures, reference-oracle
   comparisons, and negative-path commands to support a true rebuild.

### Superpowers output

1. **Initial trust-boundary omission.** The first draft stated that identity was
   not authorization but did not make consent enforcement normative. This is now
   corrected in the design and plan.
2. **Higher cognitive load.** The deep design plus plan exceeds one thousand lines.
   It should not replace Tycho's front-door explanation.
3. **CLI compatibility remains grouped, not enumerated flag-by-flag.** The plan
   intentionally treats behavior as architecture. A production migration still
   needs generated `--help` snapshots or golden parser tests for exact command
   spelling.
4. **Feature-gated target risk.** The no-janitor manifest design is carefully
   labeled, but implementers must not confuse a planned fleet fence with current
   deployed authority.

## Recommended Canonical Set

1. Keep Tycho's system specification as `docs/coord/SYSTEM-SPEC.md`, with its
   concise narrative and shipped-versus-in-flight status.
2. Correct its verdict path to document both the append-only writer shape and the
   readable legacy shape, including evidence-cache safety.
3. Link the independent rebuild design as the normative interoperability appendix.
4. Link the Superpowers plan as the clean-room implementation and acceptance
   procedure.
5. Generate a machine-readable contract bundle from tests: event/config schemas,
   frontmatter fixtures, CLI help snapshots, output-envelope fixtures, and
   reference-reader/writer compatibility cases.
6. Keep team-specific machine, cloud, harness, model, and identity-movement mappings
   on the Bus. The public repo contains only generic schemas and sanitized fixtures.

## Bottom Line

Tycho found the system's story. The Superpowers process found more of its proof
obligations. The outputs should be composed, not forced into a winner: use Tycho to
teach the architecture, the independent design to define interoperability, and the
plan to prove a rebuild actually behaves the same under failure.
