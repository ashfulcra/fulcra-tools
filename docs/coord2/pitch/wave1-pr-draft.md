# Wave-1 upstream PR — staged draft

*Everything needed to open the PR to `fulcradynamics/agent-skills` the day the pitch lands ask-1.
Blocked only on: (a) Ash's pitch outcome, (b) a GitHub identity that can fork/push (the `ashfulcra`
gh token is expired — Ash renews or pushes).*

## Mechanical steps (coord-maintainer, ~30 min)

1. Fresh drift re-check of upstream HEAD (risk 3 — repo moves weekly). Log deltas; adjust below.
2. Fork `fulcradynamics/agent-skills`; branch `coord2-wave1`.
3. `git mv`-equivalent copy of SIX skills into `skills/`:
   `fulcra-agent-presence`, `fulcra-agent-roles`, `fulcra-agent-continuity`,
   `fulcra-agent-review`, `fulcra-agent-directives`, `fulcra-agent-health`.
4. Per-skill polish AT COPY TIME (not before — coord2 remains the working repo until acceptance):
   - `homepage:` → `https://github.com/fulcradynamics/agent-skills`
   - Engine install line → pinned current release tag (v1.3.0 or later).
   - Verify cross-skill links only reference wave-1 siblings or fulcra-agent-teams; links to
     wave-2 skills (reconcile/tasks/operator) get "(optional companion, not yet upstream)" or drop.
5. PII scan the copied tree (standing rule). Reference-command verification: DONE 2026-07-04 —
   all six skills exercised end-to-end on a scratch team (presence beat/show, roles
   claim/status/release incl. nonce echo, continuity snapshot/resume, review verdict fold,
   directives tell→inbox, health doctor+fold); scratch team deleted after. Re-run only if skills
   change between now and the copy.
6. Open PR with the body below; dual review (opus + Codex) on OUR side before marking ready.

## PR title

    Add six fulcra-agent-* coordination skills (presence, roles, continuity, review, directives, health)

## PR body (draft)

Six optional skills that layer durable multi-agent coordination onto `fulcra-agent-teams` —
purely additive: no changes to teams' semantics, files, or prose. Each installs independently and
follows the existing repo shape (`skills/<name>/SKILL.md` + `references/`; scripts precedent:
`fulcra-dashboard`).

**Shared design rule:** prose for judgment, code for folds. Any question two agents must answer
identically (who's live, is this role vacant, did this review pass) is a command in a small
stdlib-only companion CLI (`coord-engine`, installed by git tag; invoked like `fulcra-api`), never
a prose instruction — because prose folds drift between agents.

- `fulcra-agent-presence` — heartbeat shards; live/idle/stale fold; broadcast reach.
- `fulcra-agent-roles` — claimable leases on durable roles; HELD/VACANT/CONTESTED; SLA
  escalation. (Distinct from a member's `role.md`: that describes a member; this tracks a role
  that outlives sessions. The SKILL.md opens with exactly this positioning.)
- `fulcra-agent-continuity` — structured snapshots + deterministic resume brief for cron/fresh
  sessions.
- `fulcra-agent-review` — verdict handshake with APPROVED/CHANGES/PENDING fold and
  required-reviewer gating.
- `fulcra-agent-directives` — tell/broadcast/remind/handoff with per-agent ack'd inboxes.
- `fulcra-agent-health` — doctor preflight + fleet health fold (who heals the team, who went dark).

Running in production on a multi-host fleet; 200 engine tests; every PR dual-AI-reviewed with
adversarial passes (lineage in the repo's DESIGN.md). A second wave (typed tasks, reconcile,
forge, automation, operator loop) exists and follows separately — reconcile touches one teams
convention (engine-owned task index) and deserves its own conversation.

## Out of scope for this PR
Engine relocation (tracked separately per the Track-2 decision), wave-2 skills, teams amendments.
