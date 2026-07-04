# Teams convergence — coord + continuity as optional layers on `fulcra-agent-teams`

A proposal set exploring whether coord (this repo) and `fulcra-continuity` can be rebuilt as **optional
packages layered on top of Fulcra's official (alpha) `fulcra-agent-teams` skill**
(`fulcradynamics/agent-skills`), rather than as a parallel system.

Reverse-engineered from coord + continuity as-built (v0.15.16) and a full read of the official skill.

## Documents (read in order)
1. **[00-coord-vs-agent-teams.md](00-coord-vs-agent-teams.md)** — reversed functional spec of coord +
   continuity as-built, and a structured comparison against the official `fulcra-agent-teams` skill
   (shared DNA, what each has/lacks, trade-offs, strategic read). Verified against source.
2. **[01-teams-as-substrate.md](01-teams-as-substrate.md)** — the architecture: take teams as the base
   tier and layer coord's power (L1 reconcile/views, L2 typed tasks, L3 directives, L4 roles, L5
   review+forge, L6 continuity, L7 automation) as optional packages. Feasibility verdict + failure modes.
3. **[02-L1-coord-reconcile.md](02-L1-coord-reconcile.md)** — implementable design for the linchpin
   package: scan the OKF team namespace, heal `task/index.md`/`log.md`, emit a `_coord/summaries.json`
   aggregate, serve query verbs. Grounded in OKF v0.1.

## One-line thesis
coord = structured coordination **platform**; `fulcra-agent-teams` = lightweight OKF-markdown
**convention** over the generic file CLI — same core DNA (Fulcra Files as bus, per-agent inbox,
continuity-for-cron, consent-gated automation). Making teams the substrate and coord the opt-in "pro
tier" unifies the two, with OKF v0.1 explicitly permitting coord's typed frontmatter + synthesized views.

## Status
Proposal / design. Not yet implemented. Key validations before coding are listed in doc 02 §9
(chiefly: `fulcra-api file` last-writer-wins + `stat`/`mtime` guarantees).
