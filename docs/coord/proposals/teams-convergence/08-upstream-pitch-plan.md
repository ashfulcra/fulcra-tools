# Upstream pitch plan — getting coord adopted by Fulcra

Execution plan for the internal-champion play defined in `07-upstream-plan.md` (adversarially
reviewed, APPROVE, 2026-07-04). Ash pitches; coord-maintainer packages and executes. The three
carry-forward risks from the review are baked in below.

## Phase 0 — Package (coord-maintainer, ~1 session, no decisions needed)

Produce an `upstream-ready` branch + evidence pack so the pitch can happen any day Ash picks:

1. **Skills polish**: strip coord-repo-relative links; flip `homepage:` placeholders; confirm each
   of the 11 skills stands alone (PR#33/#34 doctrine + PR#36 prose already landed — re-verify after).
2. **DESIGN.md**: single upstream-facing doc replacing `docs/proposals/` history — architecture
   (prose skills over one deterministic engine), tier framing (teams = base, coord = pro), review
   lineage (opus + Codex on every PR, adversarial passes), and the operator-loop story.
3. **Evidence pack** (one page, all verifiable):
   - 200 engine tests green; stdlib-only; installs from git tag (proven at v0.4.0 → v1.3.0).
   - Live migration: 139/139 tasks, acceptance green, identical-ids policy.
   - Incident postmortems: duplicate-timeline-tracks (catalog shape drift) → 0.15.17/0.15.18 fixes;
     slug-length; merge-race discipline.
   - Live operator-loop win: a 26-day-buried ask surfaced and answered the first day the loop ran.
4. **Drift re-check** (risk 3): fresh clone of `fulcradynamics/agent-skills` IMMEDIATELY before
   packaging; re-verify A2/A3/A4/A9 claims; log deltas. Repeat before EACH PR in Phase 2.
5. PII scan everything outbound (standing rule).

## Phase 1 — Pitch (Ash, one meeting + one decision)

- **Demo script** (5 min, live on `team/fulcra`, no deck): `briefing` → `board` → `asks` +
  `answer` round-trip → `roles status` (lease + nonce warning) → `health`. Real data beats slides.
- **One-pager**: problem (teams is conventions; multi-agent fleets need deterministic folds),
  what exists today (11 skills, engine, live fleet), tier framing, the three asks below.
- **The three asks**:
  1. Accept wave-1 skills PR (additive, no semantic changes to teams).
  2. Decide the engine's home (Track 2). Present with explicit API-team sizing (risk 1): the fold
     into fulcra-api is NOT mechanical — it replaces the subprocess/text transport with internal
     APIs; estimate jointly with their team. Fallback ladder: fulcra-api fold > Fulcra-owned repo
     \+ PyPI > stays in ashfulcra/coord2 by tag (interim only).
  3. Hand over Track-3 platform issues (file JSON/version-id/batch-read, record-write verbs,
     archived-type flags) — each with the incident evidence attached.
- **Positioning per risk 2**: reconcile's pitch leads with *deterministic derived views* ("two
  agents always agree on the fold"), NOT change detection — upstream teams already ships
  `data-updates`, so change detection is table stakes.

## Phase 2 — Execute (coord-maintainer, gated on Phase-1 outcomes)

1. **Wave-1 PR** to `fulcradynamics/agent-skills`: presence, roles, continuity, review,
   directives, health (pure additions). Drift re-check first; usual dual review before opening.
2. **Wave-2 PR(s)**: reconcile (+ the teams SKILL.md amendment: "if reconcile is installed, do not
   hand-edit the index" — this is the one semantic conversation), tasks, forge, automation,
   operator. Sequenced after wave-1 lands to keep the bite size reviewable.
3. **Engine** per the Track-2 decision (port plan sized with API team if fold; repo transfer if
   option 2; tag-pin docs if option 3).
4. **Post-acceptance**: coord → thin dev mirror; fleet reinstalls from upstream (setup script
   URL flip); phase-3 incumbent freeze proceeds independently of upstreaming.

## Timeline & owners

| Step | Owner | Trigger |
|---|---|---|
| Phase 0 package | coord-maintainer | now (next idle loop cycles) |
| Phase 1 pitch | Ash | when package ready + Ash schedules |
| Wave-1 PR | coord-maintainer | pitch yields ask-1 yes |
| Wave-2 + amendment | coord-maintainer | wave-1 merged |
| Engine move | both | Track-2 decision |

## Explicitly not in scope
Migration tooling, incumbent 0.15.x, host-local pins, proposal-doc history (07's exclusions stand).
