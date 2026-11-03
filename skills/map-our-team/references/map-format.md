# Map format

The map is a small folder of linked Markdown with YAML metadata. It borrows the directory layout from [OKF](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md), but its team metadata in `index.md` is a custom format, not a strict OKF 0.2 bundle. Keep it short enough to read in one sitting: it holds pointers, never content, and never credentials.

```
/team-map/
  index.md            what any agent reads first
  systems/<slug>.md   one per tool
  workflows/<slug>.md optional: how the team does a recurring thing
```

## index.md

It carries its own instructions, because some front ends have no skills installed and only see the Fulcra tools.

```markdown
---
type: Team Map
title: Acme team map
steward: Sam Lee
steward_fulcra_userid: 00000000-1111-4222-8333-444444444444
updated: <current UTC time, ISO 8601>
---

# Acme team map

For any agent working for someone on this team. Read this, then the system
files you need. Connect to each system with your own person's login. To change
the map, message the steward's agent. To reach a person's agent, use
connect-our-agents:
https://raw.githubusercontent.com/fulcradynamics/agent-skills/main/skills/connect-our-agents/SKILL.md
Full instructions:
https://raw.githubusercontent.com/ashfulcra/fulcra-tools/main/skills/map-our-team/SKILL.md

Being connected to a teammate's agent is not clearance. Send links so the tool
applies each person's permissions, and pass along content only if the requester
could already read it there or your person approves.

## People

| Name | What they do | Agent front end | Fulcra user ID | Owns |
|---|---|---|---|---|
| Sam Lee | Ops | Claude | 00000000-1111-4222-8333-444444444444 | Notion, Attio |
| Ana Ruiz | Investing | Muse | 00000000-1111-4222-8333-555555555555 | Granola |

## Systems

| System | What lives there | Owner | File |
|---|---|---|---|
| Notion | Company wiki, deal memos | Sam Lee | systems/notion.md |
| Attio | CRM, companies and contacts | Sam Lee | systems/attio.md |
| Granola | Meeting notes | Ana Ruiz | systems/granola.md |
```

## systems/\<slug\>.md

```markdown
---
type: System
title: Notion
owner: Sam Lee
owner_fulcra_userid: 00000000-1111-4222-8333-444444444444
updated: <current UTC time, ISO 8601>
---

## What lives here
Company wiki and deal memos. Notion is the system of record.

## Where
- Wiki: https://www.notion.so/acme/Wiki-0123456789abcdef0123456789abcdef
- Deal memos: https://www.notion.so/acme/Memos-fedcba9876543210fedcba9876543210
- Read-only copy for agents without Notion: /notion/ in Sam's Fulcra account

## Connect
Add Notion's connector in your front end and sign in with your own account:
https://mcp.notion.com/mcp

## Get access
Sam grants access per page in Notion. Ask Sam, or Sam's agent. If the owner cannot grant access, name the actual administrator here, or write `unknown, ask <owner>`.

## Check
Read the Wiki URL above. Passes when the expected Wiki page in the Acme workspace comes back.

## Limits
Page reads checked; other capabilities untested. No limits observed in that check.

## Sensitive
Some pages are restricted in Notion. Links only.
```

Change `updated` in `index.md` on every edit to any map file, because that's what agents watch. Keep every heading; write "none" where one doesn't apply, and `unknown, ask <owner>` where nobody knows yet. A **check** is one read-only call with a pass condition anyone can apply, so it never needs to create, change or delete anything. Record observed connector limits; one successful read does not establish that there are no other limits. **Limits** is where hard-won knowledge goes, such as what a connector can't do, so each agent doesn't rediscover it.

## Join prompt

Fill in the brackets. The steward's user passes this on themselves.

> I'd like your agent on our team's shared map. Give it this message:
>
> Join the [team name] team map in Fulcra. The steward is [steward name], Fulcra user ID [steward id]. If you aren't connected to Fulcra yet, start with https://docs.fulcradynamics.com/agent-get-started.txt. Read the instructions at https://raw.githubusercontent.com/ashfulcra/fulcra-tools/main/skills/map-our-team/SKILL.md and follow the Join section. The map is at `/team-map/index.md` in the steward's account. Ask me before connecting anything or sharing any of my data.
