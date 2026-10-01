# Coord v5 release status

Current evidence: 2026-10-01, after [PR #763](https://github.com/ashfulcra/fulcra-tools/pull/763),
[PR #764](https://github.com/ashfulcra/fulcra-tools/pull/764) and
[PR #765](https://github.com/ashfulcra/fulcra-tools/pull/765).

The v5 core is an **opt-in alpha foundation, not a completed fleet replacement**.
It delivers the coordination core independently of the web application. Existing coord-engine,
coord-fold, team authority, and schedules remain unchanged.

| Release gate | Status | Completion evidence required |
|---|---|---|
| Standalone package | Alpha.3 current suite passed | 426 unit and four installed-package tests; seven merge-commit CI checks passed. Zero runtime dependencies. [Pins and provenance](V5-ACCEPTANCE.md#current-bounded-release-evidence--2026-10-01). |
| Usable bus operations | Verified for core alpha | Explicit owned enrollment; installed publication/readback, authorized views/digests and checkpoint commands. |
| Two-agent acceptance | Passed for bounded slice | Real question/answer exchange through annotations, separate caches, restart retention and local checkpoint validation. [Evidence and limits](V5-ACCEPTANCE.md). |
| Upstream delivery | Foundation and follow-ups merged | [PR #762](https://github.com/ashfulcra/fulcra-tools/pull/762) foundation, #763 retained handoff receipts, #764 queryability documentation, and #765 explicit Claude dispatch. #765 merged 2026-10-01 at 10:45:58 UTC; merge is not npm publication or fleet adoption. |
| Fleet adoption | Not enabled | Explicit team enrollment and verified host-by-host adoption; never inferred from a passing local test. |
| Presence and roles | Implemented and live-tested | Separate presence clocks, causal claims, retained checkpoint lineage; independent successor test. |
| Native listener enrollment | Implemented; Codex idle wake tested | Evidence-based planner and native recipe; trial paused after successful wake. |
| Claude CLI delivery | Implemented; bounded installed resume/replay evidence reviewed | Exact private target, journal claim, fixed full model-ID request and no automatic retry. Peer authenticated artifacts were reviewed, not independently rerun. Native acceptance is not acknowledgment or lifecycle proof. |
| Native lifecycle | Limited evidence; further acceptance remains | Earlier Codex owned-process SIGTERM/resume used a test-only broker. No shipped cross-harness lifecycle adapter, outage or fleet proof. This work does not wait for provider completeness. |
| Lightweight onboarding | Bundled | Positive entry guide and reversible local instruction block; upstream Workspaces remains a separate reviewed change. |

## Implemented core

- Typed work, assignment, question, checkpoint and handoff events.
- Authorized causal replay and explicit conflict reporting.
- Open-work, owner-attention, completed-history and lost-track projections.
- Portable checkpoint packages and successor readiness checks.
- Annotation readback and local durable publication/observation journals.
- Listener routing, prepared attempts, receipt recording and restart deduplication.
- Explicit journal-claimed Claude CLI resume with bounded runtime/output.
- Local checkpoint byte verification and private durable import of operator-trusted
  handoff verification receipts; neither independently proves remote access.

These are components, not a claim of autonomous fleet operation. Annotation
records are work authority; local journals are operational caches. A bounded
partial read does not establish complete history or an empty backlog.

## Not part of this release slice

Public Gatekeeper deployment, anonymous guest provisioning, stacked-team
delegation, Linear integration and automatic legacy migration. Cross-account
and additional harness adoption require their own tests. The existing wire
namespaces are retained during extraction; a product command rename is not a
protocol migration.

The package README is the command/install reference. This page is the release
gate ledger; update it when evidence changes, not when a task merely starts.

## What this does not yet replace

The full coordination rebuild is larger than this first installable bus core.
Source-backed presence/roles and capability-based enrollment now connect to the
package. A Codex native scheduled turn was tested, and another session retrieved
an uploaded checkpoint and recorded a successor role claim. The listener emits
durable delivery actions; the native recipe executes them and applies suggested
adaptive intervals through harness tools, not an external daemon. No unattended
fleet listener was installed by these tests.

Remaining gates for broader adoption: other harness live adapters,
busy/restart/outage/permission/upgrade acceptance, a sustained cost/latency trial,
explicit fleet cutover, and upstream
Workspaces adoption. An independent tester now reports real-session bus operations
across Claude Code, Codex CLI, Hermes and OpenClaw; see the acceptance ledger for
the verified scope versus tester-reported evidence and remaining native-wake gaps.
The Workspaces annotation PR is intentionally
separate and is not claimed to use this package's wire contract automatically.

The release test must distinguish real annotation exchange and native session
execution from fixture-backed handoff/access checks. Existing v4 commands remain
the team's operational entry point until an explicit, tested enrollment switches
a workstream. No pre-v4 history is imported.

## Next delivery: bounded native lifecycle acceptance

Extend lifecycle and recovery tests through supported harness mechanisms using
isolated, owned sessions. Keep natural exit/resume, crash recovery, busy delivery,
outage and permission/upgrade behavior as separate evidence. The earlier bounded
Codex SIGTERM/resume test used a test-only broker; it does not supply a shipped
cross-harness adapter. No newer crash acceptance is claimed here.

This can proceed before the provider defines complete-history coverage.
[Provider issue #115](https://github.com/fulcradynamics/fulcra-api-python/issues/115)
requests an ingestion-ordered completeness contract; accepted ownership and that
contract remain unconfirmed. This limits complete-history/full-handoff claims,
not the bounded release or independent native lifecycle work. Keep partial/unknown
views honest: no all-clear, automatic takeover or invented completeness receipt.
Full handoff readiness still needs independently verified artifact/access checks,
accepted successor ownership and the required source coverage. Local receipt
import retains an operator attestation; it does not independently create that proof.
