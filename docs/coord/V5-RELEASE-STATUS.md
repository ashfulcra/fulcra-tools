# Coord v5 release status

The v5 core is **packaged and tested, not merged or fleet-adopted**. This branch
delivers the coordination core independently of the web application. Existing coord-engine,
coord-fold, team authority, and schedules remain unchanged.

| Release gate | Status | Completion evidence required |
|---|---|---|
| Standalone package | Alpha.2 integrated | Presence, roles and enrollment included; zero runtime dependencies. See acceptance record for exact checks. |
| Usable bus operations | Verified for core alpha | Explicit owned enrollment; installed publication/readback, authorized views/digests and checkpoint commands. |
| Two-agent acceptance | Passed for bounded slice | Real question/answer exchange through annotations, separate caches, restart retention and local checkpoint validation. [Evidence and limits](V5-ACCEPTANCE.md). |
| Upstream delivery | PR open; CI/review pending | [PR #762](https://github.com/ashfulcra/fulcra-tools/pull/762), verified install instructions, public-safe fixtures and exact release artifact. Not merged. |
| Fleet adoption | Not enabled | Explicit team enrollment and verified host-by-host adoption; never inferred from a passing local test. |
| Presence and roles | Implemented and live-tested | Separate presence clocks, causal claims, retained checkpoint lineage; independent successor test. |
| Native listener enrollment | Implemented; Codex idle wake tested | Evidence-based planner and native recipe; trial paused after successful wake. |
| Lightweight onboarding | Bundled | Positive entry guide and reversible local instruction block; upstream Workspaces remains a separate reviewed change. |

## Implemented core being packaged

- Typed work, assignment, question, checkpoint and handoff events.
- Authorized causal replay and explicit conflict reporting.
- Open-work, owner-attention, completed-history and lost-track projections.
- Portable checkpoint packages and successor readiness checks.
- Annotation readback and local durable publication/observation journals.
- Listener routing, prepared attempts, receipt recording and restart deduplication.

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

Remaining gates: other harness live adapters, busy/restart/outage/permission/upgrade
acceptance, a sustained cost/latency trial, explicit fleet cutover, and upstream
Workspaces adoption. Claude CLI acceptance needs authentication; unavailable
harnesses need their own environments. The Workspaces annotation PR is intentionally
separate and is not claimed to use this package's wire contract automatically.

The release test must distinguish real annotation exchange and native session
execution from fixture-backed handoff/access checks. Existing v4 commands remain
the team's operational entry point until an explicit, tested enrollment switches
a workstream. No pre-v4 history is imported.
