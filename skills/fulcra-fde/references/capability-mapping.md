# Capability mapping + gap register

Read `FULCRA-PRIMITIVES.md` first (repo root of ashfulcra/fulcra-tools), then
check the *installed* surface, not the repo: `uv tool list | grep fulcra-api`
for the version, `fulcra data-type --help` as a feature probe. The doc
states its own staleness rules — trust the installed CLI over the repo.

## Discover what the user already has (before you map anything)

Fulcra already collects a lot. Before deciding a need means a *new* data type —
or worse, a local fixture — check what is live in the user's own account:

```bash
fulcra catalog                  # every queryable data type + metadata; each
                                # entry's `related_cli_commands` names how to read it
fulcra data-updates "<range>"   # what data actually flowed in a window
```

(`data-available` / `data-sources` are REST-only — there is no such CLI verb;
`catalog` + `data-updates` are the CLI discovery path.) If a need maps to a
stream Fulcra already carries — HRV, heart rate, steps, location/visits,
workouts, sleep stages/cycles, calendar events — **bind to that existing type**
(`fulcra get-records`, the metric/event helpers). Only `data-type create` for a
stream Fulcra genuinely doesn't have. Reuse existing, create only the novel,
**simulate nothing**: a product on invented local data isn't on Fulcra at all.

## Mapping

For each product need from the interview findings, name the primitive:

| Product need | Fulcra primitive |
|---|---|
| A stream Fulcra already collects (HRV, location, workouts, sleep, …) | **reuse the existing data type** — find it in `fulcra catalog`, read via `fulcra get-records` / metric & event helpers (don't recreate it) |
| A stream genuinely novel to this product | annotation **definitions** (`fulcra data-type create`: moment, duration, boolean, numeric, scale) |
| Writing timeline data | records via ingest (`POST /ingest/v1/record[/batch]`) — there is NO record edit/delete; corrections are superseding records |
| Documents, images, arbitrary state | file library (`fulcra file ...`) — versioned, path-addressed |
| Grouping/labeling | tags (`fulcra tag ...`) |
| Reading data back | `fulcra get-records`, catalog, time series, sleep/location/calendar helpers |
| "What's new since I looked" | `fulcra data-updates "<range>"` — the polling substrate |
| Read-only agent access (chat clients) | MCP server (11 read-only tools) |
| Passive collection | Context app (iOS; Android alpha), Collect daemon plugins, Attention extension (see docs/collect.md in fulcra-tools) |

Anything unmapped goes in the **gap register** of `architecture.md`, each with
a design-around:

| Standing gap (verify against current primitives doc) | Design-around |
|---|---|
| No webhooks / push | poll `data-updates` on a schedule (Collect-daemon pattern) |
| No record delete/replace | model corrections as superseding records; fold at read time |
| MCP is read-only | writer agents need shell (tier 1) or REST (tier 2) |
| No cross-user reads (datashare unreleased) | single-account fallback + documented path to user-owned |
| No server-side compute | local-first daemons; anything hosted is the user's own infra |

The last two rows are inferred from the platform surface (poll-style reads,
local daemon patterns only) — re-verify against the current primitives doc
before relying on them.

## Tenancy decision

North star: **each end-user owns their data in their own Fulcra account**;
the product requests access. If the engagement needs cross-user access today,
a single business-owned account (namespaced paths/tags per end-user) is
acceptable — but `architecture.md` MUST carry a "path to user-owned" section
describing the migration once datashare ships. Never present single-account
as the destination.

## Output shape (`architecture.md`)

1. Product summary (two paragraphs, from findings not from the deck)
2. Capability map (table: need → primitive → notes)
3. Gap register (table: gap → design-around → risk)
4. Tenancy decision + path to user-owned (if applicable)
5. Open risks (parked P1 topics, unvalidated assumptions)

Get the user's explicit approval on this doc before `fde-engine phase <slug> plan`.
