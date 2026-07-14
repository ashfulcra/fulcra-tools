# coord: the pro tier for fulcra-agent-teams

*One page for the Fulcra pitch. Companion: `DESIGN.md` (architecture + evidence), 5-min live demo.*

## The problem

`fulcra-agent-teams` gives agents a shared OKF-markdown space — and it works. But the moment a
fleet runs unattended (cron heartbeats, multiple hosts, overnight sessions), teams' prose
conventions hit a wall: two agents eyeballing the same files reach different conclusions about
what's active, who's on-call, whether a review passed, or what's been waiting on the operator.
Coordination built on disagreement doesn't heal; it drifts.

## What exists today (not a proposal — running in production)

Twelve `fulcra-agent-*` skills + one stdlib-only CLI (`coord-engine`, invoked like `fulcra-api`)
that layer onto teams without changing it. The design rule: **prose for judgment, code for folds**
— any question two agents must answer identically is a deterministic engine command.

Running now on a live multi-host fleet: typed task lifecycle (139 tasks migrated from the
predecessor, zero losses), presence/roles with lease-based contention detection, review handshake
with required-reviewer gating, and an operator loop that on its FIRST DAY surfaced a blocker that
had been silently buried for 26 days — and routed the operator's one-sentence answer back as an
atomic unblock.

## The three asks

1. **Accept the wave-1 skills PR** into `fulcradynamics/agent-skills`: presence, roles,
   continuity, review, directives, health. Purely additive — no teams semantics change, same
   frontmatter shape, scripts precedent already exists (`fulcra-dashboard`).
2. **Decide the engine's home.** Preferred first step: a **Fulcra-owned repo + PyPI**
   (`fulcradynamics/coord-engine`) — a small stdlib-only CLI your team can review on its own,
   without coupling this pitch to a `fulcra-api` surface-area decision. Interim bridge (already
   what the wave-1 PR installs): stays in ashfulcra/fulcra-tools (packages/coord-engine), pinned
   by git tag — explicitly temporary. Long-term convergence target, once wave 1 lands and the
   contract proves stable: fold into `fulcra-api` as a command group (one tool, one auth, one
   install — and the fold *removes* a subprocess layer). Honest sizing on that fold: it is NOT
   mechanical; it replaces the text transport with internal API calls and should be estimated
   with your API team when the time comes.
3. **Take five platform issues**, each with incident evidence attached — they extend the new
   `fulcra` binary's JSON-default direction to `file` ops: structured output, per-file
   version-ids, batch read, record-write verbs, archived-type flags in catalog.

Wave 2 (reconcile, tasks, forge, automation, operator) follows separately — reconcile is the one
semantic conversation (engine-owned task index; a two-line amendment to teams' SKILL.md), and its
pitch is *deterministic derived views*, not change detection (you already ship `data-updates`).

## Why now

The fleet is live, the evidence is fresh, and every artifact is review-hardened (dual AI review on
every PR, adversarial review on the plan itself, incident postmortems folded back into design).
Adopting wave 1 costs one review cycle and adds capability your alpha users are already asking
teams to grow.
