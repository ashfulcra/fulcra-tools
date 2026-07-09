# Build doctrine — prototype first, then the real thing

## plan.md has two parts

**Part 1 — prototype verification plan.** List the riskiest
design/functionality assumptions, riskiest first. Each entry: the assumption,
how the prototype will exercise it, and what PASS looks like. The list MUST
include a **deployment rehearsal**: the prototype gets installed/run the way
the production system will be (target machine, auth flow, scheduling), not
just on the FDE's dev setup. An untested deployment plan is an assumption
like any other.

The list MUST also include a **real-data binding** item: the prototype reads
the user's actual Fulcra data (existing types) and writes real records to any
custom types it defines — PASS means the core value loop ran on real data end
to end, not on fixtures. If every item on the risk list could pass against
mock data, the prototype isn't testing the product.

**Part 2 — provisional production plan.** Whole-product milestones (Fulcra
carries everything it can; conventional infra covers the rest), explicitly
marked provisional until the prototype validates them.

## Prototype phase

- Build in the user's project directory or a repo they choose — never in
  fulcra-tools.
- **Bind to real Fulcra data before writing feature code — never simulate.**
  1. Discover what the user already has: `fulcra catalog` (each returned type
     carries a `related_cli_commands` field naming how to query it) and
     `fulcra data-updates "<range>"` (what's actually flowing). Standard
     streams — HRV, heart rate, steps, location/visits, workouts, sleep,
     calendar — already exist; **read them** with `fulcra get-records` / the
     metric & event helpers. Do not reinvent them as local fixtures.
  2. For anything Fulcra genuinely doesn't carry, **create the custom data
     type** (`fulcra data-type create …`) and write real records via **REST
     ingest** (there's no `fulcra` CLI write verb). Watch the custom-type
     trap: you POST to the **base** type endpoint
     (`POST /ingest/v1/record/MomentAnnotation`) and name your definition in
     the record's `sources` array as
     `com.fulcradynamics.annotation.<definition-uuid>` — a custom uuid as a
     URL path segment 404s. Fetch the record schema first from the v1 catalog.
     Full mechanism: `references/capability-mapping.md` → "Writing records".
     Record every created definition's ID in `build/log.md` — you'll need
     them and should not re-look them up.
  Mock arrays, seeded fixtures, and simulated series are a prototype FAIL:
  they test none of the product's real risk (does the data exist? is it
  shaped as the plan assumed? does the value loop survive real, messy data?).
- Implement the core value loop **against that real data**, then run the
  verification plan. Record every item's result in `prototype/verification.md`
  as PASS or FAIL-with-why.
- **State the data mode explicitly** — it is dangerously easy to build a
  Fulcra-*shaped* prototype on sample data and imply it's reading Fulcra.
  `prototype/verification.md` MUST open with a line:

      Data mode: sample | Fulcra live read | Fulcra live read/write

  and the report MUST list exactly which Fulcra primitives are actually
  connected (which data types read via which command; which custom types
  written via ingest). `Data mode: sample` is not a passing prototype — it's a
  FAIL on the real-data binding item, recorded honestly so the gap is visible
  rather than hidden.
- FAIL results that invalidate the architecture or plan: transition backward
  (`fde-engine phase <slug> architecture` or `plan`), revise, return.
- **User gate:** present the verification record; the user decides
  build / iterate / stop.

## Production build phase

- Execute Part 2 milestone by milestone; verify each before the next; log to
  `build/log.md`.
- Stack defaults when the user has no standing preference: Python ≥3.10 + uv,
  `fulcra-api` (CLI for shell paths, Python lib for app code), local-*compute*
  processes (code runs locally because the platform has no server-side
  compute — the Collect daemon is the reference pattern for anything that must
  run on a schedule). "Local" is about where the *code* runs, never where the
  *data* lives: data always lives in Fulcra, never in a local mock or fixture.
- Where the harness supports subagents, dispatch independent milestones to
  them; otherwise execute sequentially. The plan is the contract either way.

## Retro phase

`retro.md`: what repeated from previous engagements, what was missing from
this skill, which platform gaps bit hardest. Append repeatable patterns to
`fde/playbook.md` in the user's store. A pattern seen in 2+ engagements
belongs upstream: open an issue/PR against ashfulcra/fulcra-tools.
