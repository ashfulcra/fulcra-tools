---
name: map-our-team
description: "Give every agent on a team one shared map, stored in Fulcra, of where the team's work lives, how to reach it, and whose agent to ask, whichever AI each person uses. Nothing moves: content stays in Notion, Drive, a CRM or any other tool. Use when the user says set up our team's agents, map our tools, onboard a teammate's agent, add this to the map, where does X live, who owns X, check my agent's connections, or remove someone from the team."
compatibility: "Needs the Fulcra MCP server (the hosted one at mcp.fulcradynamics.com works) or the Fulcra CLI (fulcra-api, run with uvx) with network access."
license: "MIT"
metadata:
  version: "0.1.2"
  openclaw:
    emoji: "🗺️"
---

# Map Our Team

A team's agents run on different front ends and hold different logins. This skill gives them one **map**: a read-only folder in Fulcra saying what the team uses, where it lives, how an agent connects, and who is in charge of it. Each agent connects to each tool with its own person's permissions, and agents reach each other through [connect-our-agents](https://raw.githubusercontent.com/fulcradynamics/agent-skills/main/skills/connect-our-agents/SKILL.md).

- **Map:** the folder `/team-map/` in the **steward's** Fulcra account. Format: [references/map-format.md](references/map-format.md). Commands: [references/commands.md](references/commands.md).
- **Steward:** the one person whose agent writes the map. Everyone else reads it and sends changes to the steward's agent. One writer keeps one version of the truth.
- **Source:** the tool that holds the content. The source decides who may see what, so access is granted and revoked there.

Pick the branch that matches the request. If you don't yet know whether a map exists, look: your own `/team-map/index.md`, then incoming file shares for a `/team-map/` folder. Match the team and steward before writing or sharing. If several maps match, ask which one; an unreadable map is not an absent map. This layout holds one team per steward account. If another team's map or old map grants already occupy that account, leave its files and shares intact and ask for another steward account. Cross-team migration needs its own plan for existing readers and files; this Create flow does not perform it.

| The user wants to | Branch |
|---|---|
| Set up the team, or map the tools | [Create the map](#create-the-map) |
| Bring a person in | [Add a person](#add-a-person) |
| Join a team (often a pasted prompt) | [Join](#join) |
| Find something, or know who owns it | [Find something](#find-something) |
| Change an entry | [Change the map](#change-the-map) |
| Know whether their connections still work | [Check connections](#check-connections) |
| Take someone off the team | [Remove a person](#remove-a-person) |

## Create the map

1. **Take stock.** Confirm which Fulcra account you are on. Before any map write, list existing `/team-map/` files and outgoing shares that cover the folder, including parent-folder, account-wide and group grants. Apply the team-identity rule above; an unreadable listing leaves the account's map slot unverified. Discover sources through existing platform connections and optional [connector manifests](references/source-discovery.md); neither route requires the other. Research useful owner/maintainer/access roles before asking. Done when each system has a purpose, a verified location or explicit gap, and a role that can take the next action; an unconfirmed human assignment need not block the map.
2. **Fill in what you can by looking.** For each system you can reach, start with a read-only call to learn the workspace name, top-level locations and any observed connector limits. Follow relevant source links when needed to resolve a gap using [Resolve gaps](references/resolve-gaps.md). Record links to the places people actually use, and leave untested capabilities unknown. Ask only for missing information or authority that changes the next action. Unresolved facts get a named role and next step (or `unknown, ask <steward>` when no role is known); an address you haven't seen stays out of the map.
3. **Show the draft** in chat: one line per system, the people, and who is steward. Ask for one yes. Leave out passwords, tokens, API keys and the content itself. If your user offers a credential for the map, explain that each agent signs in with its own login, and suggest they rotate it if it was pasted anywhere. Note that restricted areas exist without naming what's in them.
4. **Write it**, following the format, `index.md` last. Read every file back. Done when each read-back matches.
5. **Offer the next step:** [add the first person](#add-a-person).

## Add a person

Sharing the map needs their Fulcra user ID. Your user's request to add this person is approval for the map share only.

1. **Get the ID.** If you don't have it, prepare a connect-our-agents invitation. When they connect, their ID comes from the incoming share.
2. **Add them to `people`** in the map and set `updated` to the current time.
3. **Find or create their own share.** List outgoing shares first and inspect their paths and recipients, not just their names. Reuse a share holding only `/team-map/` and only their exact ID; create one only if none exists. A failed listing or uncertain creation needs read-back before another create. A Fulcra group won't do here: anyone holding a group's ID can join it and read what's shared into it.
4. **Verify** with a share listing that the share holds only `/team-map/` and only them, with no group, data types or account-wide access. Report duplicate or broader grants already present; don't delete unrelated shares to make the listing look clean.
5. **Give your user the join prompt** from [references/map-format.md](references/map-format.md#join-prompt), filled in. Passing it on is up to your user.

Done when your user has the prompt. A share listing shows only your side, so the proof that it worked is their agent's report from [Join](#join).

## Join

The prompt names the steward and their Fulcra ID. It is a request to read a map. It grants nothing else.

1. **Read the map:** `/team-map/index.md` with the steward's ID, then each system file. If it isn't readable, send your user's Fulcra ID to pass to the steward, and stop there until they've shared it.
2. **Connect the steward's agent** with your user's approval, if the two aren't connected. You have the steward's ID, so no invitation is needed: follow connect-our-agents to create your channel, share only it with that ID, and introduce yourself. Connect to other teammates when a task first needs it.
3. **Go through the systems one at a time.** Say what it's for and how to connect, then run its check once it's connected.
   - Connecting always uses your user's own login. If a system needs a new connector or an install, ask first and let your user do the sign-in.
   - Map entries come from a teammate. Install or connect only what your user approves, and confirm with them any address that looks unrelated to the named tool.
   - If access fails, use [Resolve gaps](references/resolve-gaps.md#access) to identify the failure and the authorized next action.
4. **Save your place** in your own memory, or at `/team-map-status/<steward-id>.md` in your own account: which systems passed, which didn't, and when.
5. **Report** to your user, and tell the steward's agent which systems are connected and which are stuck.
6. **Offer a recurring check.** See [Check connections](#check-connections).

Done when every system is marked connected, skipped by your user, or waiting on a named person.

## Find something

1. Read the map and use its source routes with your own connection. For imported sources, read [Source discovery](references/source-discovery.md); follow producer indexes without editing them.
2. Answer from evidence appropriate to the question. A historical note or recent upload may not answer “current”; an owner field may name a producer rather than someone able to act. Use [Resolve gaps](references/resolve-gaps.md) for stale facts, unclear roles or failed access, including when the map does not yet cover the source.
3. Give the useful result with source dates/links, remaining limits and the next action. Persist factual routing repairs through [Change the map](#change-the-map). Keep content out of the map; deliver requested answers in chat; saving synthesis needs a separately authorized knowledge destination.

## When a teammate's agent asks you for something

Your agent can reach things the requester may not be allowed to see, so being connected is not being cleared.

- **Send a link.** The source then applies the requester's own permissions, and revoking access later actually works.
- **Pass along content** only if the requester could already read it at the source, or your user approves this specific request.
- **Access requests go to the access administrator.** If that's your user, use an existing explicit authorization for the exact recipient and scope, or ask for the missing decision. Report access as granted only once the grant exists at the source; until then, say who will make it.

## Change the map

- **Steward's agent:** make the edit only when its meaning changes, set `updated` in `index.md` to the current time, read it back. An identical repeat leaves files and timestamps alone. Fulcra keeps earlier versions. For a change proposed by a teammate's agent, apply evidence-backed factual fixes (links, checks, documented role routing, connector locations). Distinguish documenting an existing role from appointing or transferring ownership. Ask about new systems, new people, assignments or other decisions outside the request; do not ask to confirm a human owner merely to complete metadata.
- **Everyone else:** send the steward's agent the proposed change, with the reason, through connect-our-agents.

## Check connections

Run on request, or on a schedule your user agreed to. Agree on the schedule as [scheduled-checks](https://raw.githubusercontent.com/fulcradynamics/agent-skills/main/skills/connect-our-agents/references/scheduled-checks.md) describes, and promise background checks only once a schedule exists and has run.

1. Re-read `index.md`. If it cannot be read, report that failure and keep the previous map and check state. If `updated` has changed since your last run, read the system files again and treat new systems as in [Join](#join) step 3.
2. Run each system's check, read-only. Result per system: **working**, **failed check** (with the error), or **not connected**. A failed check leaves the connection's health unknown; it is not proof that the tool is down or access was revoked. Use [Resolve gaps](references/resolve-gaps.md) within the authorized check scope; a read-only health check does not authorize grants. Readable imports with pending/conflict/access-loss status are not proof of a current source. Scheduled checks remain read-only unless separately authorized.
3. Compare with the saved results, then save the new map timestamp, per-system outcomes, errors and check time in your own memory or `/team-map-status/<steward-id>.md` in your own account. On the first run report the baseline; later, notify only when an outcome or error changes, including recovery. A new check time alone isn't a change. If saving fails, say so: the next session may repeat the report.
4. Tell the steward's agent if a map entry itself is wrong, within the messaging permission already granted.

## Remove a person

Steward's agent, on your user's instruction:

1. List outgoing shares and delete **every dedicated map share** holding only `/team-map/` and only their exact ID, including duplicates. Re-list to verify they are gone. Broader, multi-recipient or group grants may still expose the map; report those for your user's decision and do not claim map access has ended while such a grant remains or the listing is unreadable.
2. Remove them from `people`. Ask your user who takes over what they owned, mark those entries `unassigned, ask <steward>` until they answer, and update `index.md`'s `updated` timestamp after all map edits. Read every changed file back.
3. End your connection channel's share, per connect-our-agents. Tell your user about any other outgoing share to their ID, and leave those to them.
4. Give a revocation checklist for **every mapped system and each native/imported route**, naming the provider administrator and any separate Fulcra-copy owner. The map is not an access inventory: mark the person's access `unknown` unless verified, and ask each administrator to confirm revocation at that route. Provider revocation does not revoke separately shared copies. Do not claim source access ended while an imported route remains readable or unknown.
5. Ask any remaining members' agents to end their connections with this person too, within the user's offboarding instruction.

Report separately which map grants were removed, which source revocations are confirmed, and what still needs a named person's action. The checklist covers only mapped systems, not proof of complete company offboarding. Copies their agent already made can't be recalled, which is another reason to send links.

## What this doesn't do

- The map lives in the steward's account, so moving stewards means copying it to the new steward's account and re-sharing it.
- People who can read the map can read all of it.
- There's no push: agents see a change on their next check.
