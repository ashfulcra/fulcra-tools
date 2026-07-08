# Capability mapping + gap register

Read `FULCRA-PRIMITIVES.md` first (repo root of ashfulcra/fulcra-tools), then
verify the installed surface: `fulcra --version`, `fulcra catalog`. The doc
states its own staleness rules — trust the installed CLI over the repo.

## Mapping

For each product need from the interview findings, name the primitive:

| Product need | Fulcra primitive |
|---|---|
| User-defined event/measurement streams | annotation **definitions** (`fulcra data-type create`: moment, duration, boolean, numeric, scale) |
| Writing timeline data | records via ingest (`POST /ingest/v1/record[/batch]`) — there is NO record edit/delete; corrections are superseding records |
| Documents, images, arbitrary state | file library (`fulcra file ...`) — versioned, path-addressed |
| Grouping/labeling | tags (`fulcra tag ...`) |
| Reading data back | `fulcra get-records`, catalog, time series, sleep/location/calendar helpers |
| "What's new since I looked" | `fulcra data-updates "<range>"` — the polling substrate |
| Read-only agent access (chat clients) | MCP server (11 read-only tools) |
| Passive collection | Context app (iOS; Android alpha), Collect daemon plugins, Attention extension |

Anything unmapped goes in the **gap register** of `architecture.md`, each with
a design-around:

| Standing gap (verify against current primitives doc) | Design-around |
|---|---|
| No webhooks / push | poll `data-updates` on a schedule (Collect-daemon pattern) |
| No record delete/replace | model corrections as superseding records; fold at read time |
| MCP is read-only | writer agents need shell (tier 1) or REST (tier 2) |
| No cross-user reads (datashare unreleased) | single-account fallback + documented path to user-owned |
| No server-side compute | local-first daemons; anything hosted is the user's own infra |

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
