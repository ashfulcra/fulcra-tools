# Build doctrine — prototype first, then the real thing

## plan.md has two parts

**Part 1 — prototype verification plan.** List the riskiest
design/functionality assumptions, riskiest first. Each entry: the assumption,
how the prototype will exercise it, and what PASS looks like. The list MUST
include a **deployment rehearsal**: the prototype gets installed/run the way
the production system will be (target machine, auth flow, scheduling), not
just on the FDE's dev setup. An untested deployment plan is an assumption
like any other.

**Part 2 — provisional production plan.** Whole-product milestones (Fulcra
carries everything it can; conventional infra covers the rest), explicitly
marked provisional until the prototype validates them.

## Prototype phase

- Build in the user's project directory or a repo they choose — never in
  fulcra-tools.
- Provision Fulcra resources first (data-type definitions, tags, file
  layout); record every created definition's ID in `build/log.md` — you will
  need them and should not re-look them up.
- Implement the core value loop, then run the verification plan. Record every
  item's result in `prototype/verification.md` as PASS or FAIL-with-why.
- FAIL results that invalidate the architecture or plan: transition backward
  (`fde-engine phase <slug> architecture` or `plan`), revise, return.
- **User gate:** present the verification record; the user decides
  build / iterate / stop.

## Production build phase

- Execute Part 2 milestone by milestone; verify each before the next; log to
  `build/log.md`.
- Stack defaults when the user has no standing preference: Python ≥3.10 + uv,
  `fulcra-api` (CLI for shell paths, Python lib for app code), local-first
  processes (the platform has no server-side compute — the Collect daemon is
  the reference pattern for anything that must run on a schedule).
- Where the harness supports subagents, dispatch independent milestones to
  them; otherwise execute sequentially. The plan is the contract either way.

## Retro phase

`retro.md`: what repeated from previous engagements, what was missing from
this skill, which platform gaps bit hardest. Append repeatable patterns to
`fde/playbook.md` in the user's store. A pattern seen in 2+ engagements
belongs upstream: open an issue/PR against ashfulcra/fulcra-tools.
