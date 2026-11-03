# map-our-team

Give every agent on a team one shared map of where the team's work lives, how to reach it, and whose agent to ask.

Teams rarely use one AI. One person works in Claude, another in Muse, another in something released last week. Moving the company's knowledge into yet another store is a project nobody wants. This skill moves nothing: Notion stays in Notion, the CRM stays the CRM. One person's agent, the steward's, keeps a short read-only map in Fulcra, and every teammate's agent reads it, connects to each tool with its own person's login, and checks that those connections still work.

When an agent can't reach something, the map says who owns it, and [connect-our-agents](https://raw.githubusercontent.com/fulcradynamics/agent-skills/main/skills/connect-our-agents/SKILL.md) reaches that person's agent. Agents send links rather than content, so each tool keeps applying its own permissions, and revoking someone's access at the tool still ends it.

The map holds pointers only: no content and no credentials. Everyone it's shared with can read all of it, and only the steward's agent can change it. The map lives in the steward's Fulcra account, so moving stewards means copying it over and re-sharing.

Works with the Fulcra CLI or the Fulcra MCP. Built on Fulcra file shares and connect-our-agents.

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

Muse, Town, real third-party connections, CLI writes and recurring execution
remain untested. Simulated checks do not establish those live capabilities.
