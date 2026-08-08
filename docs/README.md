# docs — index

A map of what's in `docs/`, marked by who it's for. If you're a founder (or a
founder's agent) meeting this repo for the first time, read the **cold-reader**
docs and skip the rest — the internal ones are Fulcra-team history and roadmap,
kept for provenance, not written for you.

## Start here (cold reader)

- [`how-do-i-get-my-data.md`](how-do-i-get-my-data.md) — every data source
  Fulcra Collect can pull from today, and the pathway for each. No account
  needed to read.
- [`collect.md`](collect.md) — what Fulcra Collect is and how the local
  import daemon works.
- [`coord-DESIGN.md`](coord-DESIGN.md) — the design of the coord
  agent-coordination layer: why deterministic folds, what the engine owns.
- [`coord/GET-ON-THE-BUS.md`](coord/GET-ON-THE-BUS.md) — the from-zero
  quickstart for joining the coord bus (install → auth → team bootstrap →
  join), including remote/sandboxed hosts.
- [`coord/BUS-V3.md`](coord/BUS-V3.md) — the bus v3 contract (adopted
  2026-07-27): events as typed records, documents as files, one queue read
  per wake, no resident listeners.
- [`coord/wake-router-SPEC.md`](coord/wake-router-SPEC.md) and
  [`coord/wake-router-PLAN.md`](coord/wake-router-PLAN.md) — the gated spec and
  implementation plan for the wake router + engagement model build
  (status **as of 2026-07-24**: implemented, shadow window running, drawdown
  pending acceptance — the spec's own banner is the dated source, and the engine
  is the operational truth): one fleet wake policy instead of N resident
  listeners, cloud-first hosting.
- [`coord/wake-router-ADDENDUM-1-event-substrate.md`](coord/wake-router-ADDENDUM-1-event-substrate.md)
  — normative addendum (tasks E1–E3): the `data-updates` feed as the authoritative
  change ledger and feed-driven delta folds (incremental reconcile, listen/briefing,
  router scan) with fail-closed full-scan fallbacks.
- [`coord/atc-DESIGN.md`](coord/atc-DESIGN.md) — the design of ATC,
  capability-matched model routing on subscription caps.
- [`coord/alias-resolution-DESIGN.md`](coord/alias-resolution-DESIGN.md) — how a
  renamed agent's stranded obligations get joined to its current identity, and
  the boundary that keeps an alias table from becoming a privilege-escalation
  primitive: aliases resolve reads, never authority.
- [`coord/roles/examples/`](coord/roles/examples) — five worked example roles
  (Coordinator, Coder, Reviewer, Maintainer, LocalAgent), extracted from real
  deployments and generalized; copy one to your team's store and fill in the
  particulars there.
- [`TESTING.md`](TESTING.md) — how to run the suites and install Collect as a
  launchd agent.

## Team-internal material

Anything specific to one team that runs this toolkit — agent rosters, incident
evidence, rollout records, internal pitch material, audit artifacts — lives in
**that team's file store**, not in this repository. This repo is the tools.

- [`coord/COORD2-README.md`](coord/COORD2-README.md) — provenance pointer for
  the coord subtree's migration history.
- [`coord/proposals/`](coord/proposals) and [`proposals/`](proposals) —
  design proposals that shipped code still cites as its source. Historical, but
  load-bearing: two `coord-engine` modules name them as their design.
- [`skill-quality-pattern.md`](skill-quality-pattern.md) — the maintainer
  convention for skill quality across the `fulcra-agent-*` set.
