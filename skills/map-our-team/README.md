# map-our-team

Give every agent on a team one shared map of where the team's work lives, how to reach it, and whose agent to ask.

Teams rarely use one AI. One person works in Claude, another in Muse, another in something released last week. Moving the company's knowledge into yet another store is a project nobody wants. This skill moves nothing: Notion stays in Notion, the CRM stays the CRM. One person's agent, the steward's, keeps a short read-only map in Fulcra, and every teammate's agent reads it, connects to each tool with its own person's login, and checks that those connections still work.

When an agent can't reach something, the map says who owns it, and [connect-our-agents](https://raw.githubusercontent.com/fulcradynamics/agent-skills/main/skills/connect-our-agents/SKILL.md) reaches that person's agent. Agents send links rather than content, so each route applies its own permissions. Imported Fulcra copies have separate grants: offboarding checks both provider access and any mapped copies.

The map holds pointers only: no content and no credentials. Everyone it's shared with can read all of it, and its designated steward agent maintains it. Owner-account agents must honor that single-writer convention; it is not an enforced cross-agent lock. The storage-owner account and steward's agent-contact identity may differ. A writer/operator handoff can keep company storage in place; moving storage to another account needs a separate copy/share migration.

Works with the Fulcra CLI or the Fulcra MCP and accounts already connected in
your agent's platform. Built on Fulcra file shares and connect-our-agents. No
Collect, sync app, vault or background worker is required.

When a source is stale, ownership is unclear or access fails, the skill
[researches the gap](references/resolve-gaps.md), records useful role routing and
asks only for a decision needed to proceed. It can carry out an already authorized
narrow access fix; a map share or health check does not authorize source grants.

Optional sync apps can advertise their existing outputs through the
[versioned discovery contract](references/source-discovery.md). The map links
those connector-owned indexes; it never edits sync working files or imports the
same content again. Native and imported routes keep separate access checks.
Knowledge synthesis uses a separately authorized output such as an existing
vault section; installing this skill creates no scheduled consolidation worker.
The contract's presence here is not a claim that any app release implements it.

## Use it

Point your agent at [SKILL.md](SKILL.md), or install it with:

```sh
npx skills add ashfulcra/fulcra-tools --skill map-our-team
```

It needs a Fulcra account and a signed-in Fulcra CLI or MCP connector. Each
person connects to source tools with their own login. The map supports one
team per steward account; its exact layout is in
[the map format](references/map-format.md).

When testing an unmerged branch, replace `main` in the generated join prompt's
skill URL with that branch or commit until it is merged.

## Validation

Version 0.1.1 was checked on 2026-11-03 with the official Agent Skills
validator, CLI help and isolated fresh-agent exercises for creating,
sharing, joining, repeated adds, connection checks, offboarding and an
occupied steward account. The [acceptance scenarios](references/acceptance.md)
are a test plan for live and simulated exercises, not a record of live results.

A live MCP probe on two authorized bot accounts verified exact own/shared
read-backs, files added after sharing, an updated index, reuse of an existing
dedicated share, removal of duplicate dedicated shares, and denied reads after
revocation. Test files were soft-deleted and unrelated shares were preserved.
This verifies the storage/sharing operations, not the full two-agent workflow.

Muse/Town onboarding, newly connecting third-party sources, CLI writes and
actual recurring execution remain untested by these exercises. Simulated checks do not establish those live capabilities.

Version 0.1.2 adds gap recovery and optional producer-manifest adoption.
On 2026-11-03 it passed official skill validation, fresh-agent behavioral
exercises for recovery/authorization and native-only/composed discovery, and
independent review (which caught and corrected imported-copy offboarding).
Those exercises use synthetic supplied observations, not live provider sync.
The [acceptance plan](references/acceptance.md) covers repeat/failure variants.
Live adapter publication and combined operation must be verified against the
specific app release before advertising that integration as working.

Version 0.1.3 fixes a redundant Create approval exposed by an actual company-map
build. A clear request to build/save a map authorizes its writes; the skill
still checks inherited audiences and asks for consequential missing choices.
An authorized company/service account can store the map, with its account ID
kept separate from the human operator's personal ID. This changes no source
permissions and authorizes no additional sharing.

For recurring upkeep, the [scheduled-maintenance recipe](references/scheduled-maintenance.md)
provides one agent-neutral setup/run prompt and per-installation source, writer,
mode, state and bounds contract. Use it manually or in a native agent scheduler;
no background worker or personal schedule is installed. Native-only operation,
partial coverage, uncertain writes, unchanged repeats and independent knowledge
output authorization remain explicit. A saved host task is not observed
scheduled execution.

On 2026-11-03, v0.1.3 passed official skill validation, cold authorization/audience
exercises, actual company-map write/readback and fresh-agent source-routing
checks. The recurring prompt was manually executed in a local synthetic sandbox,
including unchanged repeat, uncertain writes, conflicting writer, missing state
permission and changed source scope. These are not observed scheduled runs;
no personal automation was created. Real evidence remains private.

Version 0.1.4 separates map storage from the verified steward message route,
and records an optional named writer binding. Repeated add/remove operations
leave unchanged map files/timestamps alone. Add verifies the grant before roster
publication and reads the membership edit back; uncertain writes stay pending.
Service-account joining and lifecycle retries are explicit acceptance cases.

Proposal mode now leaves both the map and canonical knowledge output unchanged,
even when the output is writable. Authorized state may retain proposals;
publication requires maintain-map mode and the separate output authorization.

The 0.1.4 QA pass checked service-account Join and add/remove retry traces,
and manually executed the exact canonical maintenance recipe with synthetic
knowledge-output fixtures. Proposal mode produced zero canonical writes;
authorized apply preserved unrelated owned sections, and unchanged repeat
produced zero versions/writes. Missing authority and source-instruction cases
remained bounded. These are simulated/manual tests, not live membership changes
or observed scheduled execution. Live map/source read checks are recorded
separately; no new grant or connection was created by this QA pass.
