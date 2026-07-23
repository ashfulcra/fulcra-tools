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
- [`coord/wake-router-SPEC.md`](coord/wake-router-SPEC.md) and
  [`coord/wake-router-PLAN.md`](coord/wake-router-PLAN.md) — the gated spec and
  implementation plan for the wake router + engagement model build (stage 3 in
  execution): one fleet wake policy instead of N listeners, cloud-first hosting.
- [`coord/atc-DESIGN.md`](coord/atc-DESIGN.md) — the design of ATC,
  capability-matched model routing on subscription caps.
- [`TESTING.md`](TESTING.md) — how to run the suites and install Collect as a
  launchd agent.

## Internal (Fulcra-team history, roadmap, and audits)

Kept for provenance; safe to skip on a first read.

- [`coord/pitch/`](coord/pitch) — the internal Fulcra pitch for coord
  (one-pager, wave-1 PR draft, live-demo script). Written for the Fulcra team,
  not a cold recipient.
- [`coord/COORD2-README.md`](coord/COORD2-README.md) — provenance pointer for
  the coord subtree's migration history.
- [`coord/proposals/`](coord/proposals) and [`proposals/`](proposals) —
  historical design proposals.
- [`analysis/`](analysis) and [`audits/`](audits) — internal analysis and QA
  audit artifacts.
- [`fulcra-coord-0.13.0-rollout.md`](fulcra-coord-0.13.0-rollout.md) — rollout
  notes for the deprecated first-generation `fulcra-coord`.
- [`skill-quality-pattern.md`](skill-quality-pattern.md) — the maintainer
  convention for skill quality across the `fulcra-agent-*` set.
