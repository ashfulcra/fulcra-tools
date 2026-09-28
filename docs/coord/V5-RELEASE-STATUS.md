# Coord v5 release status

The v5 bus is **not released or adopted**. This branch is packaging the tested
coordination core independently of the web application. Existing coord-engine,
coord-fold, team authority, and schedules remain unchanged.

| Release gate | Status | Completion evidence required |
|---|---|---|
| Standalone package | Verified locally | 328 tests across 18 extracted suites; packed offline install/bin/export smoke passes outside the source checkout, without Svelte or runtime dependencies. |
| Usable bus operations | In progress | Explicit user-owned enrollment, work publication/readback, authorized replay/backlog and checkpoint operations through the installed command. |
| Two-agent acceptance | Pending | Two real sessions use the installed package, with receipt, outstanding-work and recovery evidence; synthetic legs identified separately. |
| Upstream delivery | Pending | Reviewed feature PR, verified install instructions, public-safe fixtures, exact release artifact. |
| Fleet adoption | Not enabled | Explicit team enrollment and verified host-by-host adoption; never inferred from a passing local test. |

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
Presence/role enrollment, environment-specific wake adapters, automatic adaptive
listening, and the workspace skill's guided setup are not connected by this
package. The listener emits durable delivery actions; a harness integration must
actually deliver them and return correlated receipts. Checkpoint validation does
not by itself upload an artifact or restart a reasoning session.

The release test must distinguish real annotation exchange and native session
execution from fixture-backed handoff/access checks. Existing v4 commands remain
the team's operational entry point until an explicit, tested enrollment switches
a workstream. No pre-v4 history is imported.
