---
name: map-our-team
description: "Give every agent on a team one shared map, stored in Fulcra, of where the team's work lives, how to reach it, and whose agent to ask, whichever AI each person uses. Nothing moves: content stays in Notion, Drive, a CRM or any other tool. Use when the user says set up our team's agents, map our tools, onboard a teammate's agent, add this to the map, where does X live, who owns X, check my agent's connections, remove someone from the team, or set up recurring company-knowledge maintenance (dreaming)."
compatibility: "Needs the Fulcra MCP server (the hosted one at mcp.fulcradynamics.com works) or the Fulcra CLI (fulcra-api, run with uvx) with network access."
license: "MIT"
metadata:
  version: "0.1.5"
  openclaw:
    emoji: "🗺️"
---

# Map Our Team

A team's agents run on different front ends and hold different logins. This skill gives them one **map**: a folder shared read-only with teammates in Fulcra saying what the team uses, where it lives, how an agent connects, and who is in charge of it. Each agent connects to each tool with its own person's permissions, and agents reach each other through [connect-our-agents](https://raw.githubusercontent.com/fulcradynamics/agent-skills/main/skills/connect-our-agents/SKILL.md).

- **Map:** the folder `/team-map/` in the **steward's** Fulcra account. Format: [references/map-format.md](references/map-format.md). Commands: [references/commands.md](references/commands.md).
- **Steward:** the responsible operator and their one map-writing agent. The verified agent-contact ID may differ from the storage-owner ID; see [map identity fields](references/map-format.md#indexmd). The map may live in their personal account or an authorized company/service account; its verified storage owner ID determines where readers go. Everyone else reads it and sends changes to that writer.
- **Source:** the tool that holds the content. The source decides who may see what, so access is granted and revoked there.

For company-owned map and connector storage, member share-backs, independent
member installations or incoming joins, read [Company deployment](references/company-deployment.md).
It defines the separate copy audiences, proposal reconciliation and optional
join-listening policy. First-time users have a [company/member setup guide](references/setup.md).

Pick the branch that matches the request. If you don't yet know whether a map exists, look: your own `/team-map/index.md`, then incoming file shares for a `/team-map/` folder. Match the team and steward before writing or sharing. If several maps match, ask which one; an unreadable map is not an absent map. This layout holds one team per steward account. If another team's map or old map grants already occupy that account, leave its files and shares intact. Use another already authorized suitable account when available, or ask for a different destination. Cross-team migration needs its own plan for existing readers and files; this Create flow does not perform it. Deduplicate discovered maps by verified storage owner and path, not by share labels. Before a mutation, honor any named writer binding: another chat for the same operator routes the authorized change to that writer unless an explicit handoff already permits this chat to act.

| The user wants to | Branch |
|---|---|
| Set up the team, or map the tools | [Create the map](#create-the-map) |
| Bring a person in | [Add a person](#add-a-person) |
| Join a team (often a pasted prompt) | [Join](#join) |
| Find something, or know who owns it | [Find something](#find-something) |
| Change an entry | [Change the map](#change-the-map) |
| Know whether their connections still work | [Check connections](#check-connections) |
| Maintain company knowledge repeatedly (dreaming) | [Scheduled maintenance](#scheduled-maintenance) |
| Take someone off the team | [Remove a person](#remove-a-person) |

## Create the map

1. **Take stock.** Confirm which Fulcra account you are on. Before any map write, list existing `/team-map/` files and outgoing shares that cover the folder, including parent-folder, account-wide and group grants. Apply the team-identity rule above; an unreadable listing leaves the account's map slot unverified. Check the audience inherited from parent/account/group grants even when no map exists. An existing all-files share would also expose a new map. Preserve unrelated grants; use another already authorized suitable account when available, or ask the specific audience/destination question before publishing to an unverified audience. Discover sources through existing platform connections and optional [connector manifests](references/source-discovery.md); neither route requires the other. Research useful owner/maintainer/access roles before asking. Done when each system has a purpose, a verified location or explicit gap, and a role that can take the next action; an unconfirmed human assignment need not block the map.
2. **Fill in what you can by looking.** For each system you can reach, start with a read-only call to learn the workspace name, top-level locations and any observed connector limits. Follow relevant source links when needed to resolve a gap using [Resolve gaps](references/resolve-gaps.md). Record links to the places people actually use, and leave untested capabilities unknown. Ask only for missing information or authority that changes the next action. Unresolved facts get a named role and next step (or `unknown, ask <steward>` when no role is known); an address you haven't seen stays out of the map.
3. **Show the draft** in chat: one line per system, the people, steward, storage account and effective audience. A request to build/save this map already authorizes the map writes when those choices are clear and within scope; proceed without another yes. For a new map, the requesting user's agent can be its initial writer in the chosen authorized account. Ask only for a missing consequential choice, such as an ambiguous team, an unexpected audience or an actual stewardship transfer. Keep the human operator distinct from the map's storage-account identity. Leave out passwords, tokens, API keys and the content itself. If your user offers a credential for the map, explain that each agent signs in with its own login, and suggest they rotate it if it was pasted anywhere. Note that restricted areas exist without naming what's in them.
4. **Write it**, following the format, `index.md` last. Read every file back. Done when each read-back matches.
5. **Offer the next step:** [add the first person](#add-a-person).

## Add a person

Sharing the map needs their Fulcra user ID. Your user's request to add this person is approval for the map share only.

1. **Read and plan.** Re-read the current index in the verified storage account and honor its writer binding. Get the person's exact Fulcra ID. If missing, prepare a connect-our-agents invitation; when they connect, their ID comes from the incoming share. Compare their current row before planning a change.
2. **Find or create their own share.** List outgoing shares before editing the roster. Inspect paths and recipients, not labels. Reuse a share holding only `/team-map/` and only their exact ID; create one only if none exists. A failed listing or uncertain create needs successful read-back/reconciliation before another create or a completed-add claim. A Fulcra group won't do: anyone holding its ID can join it.
3. **Verify the grant.** Re-list and verify only `/team-map/`, only that person, no group, data types or account-wide access. Report existing duplicate/broader grants; preserve unrelated shares. If this fails, leave the roster unchanged and report the pending grant action.
4. **Persist the membership change if needed.** Add or correct their row only when its meaning changes, update the index timestamp and read it back exactly. An already identical member and grant cause no map write or timestamp change. If the grant exists but the index write is uncertain, reconcile the actual index before retrying; report grant-present/map-update-pending rather than onboarding complete.
5. **Give the join prompt** from [references/map-format.md](references/map-format.md#join-prompt), filled with the map storage owner and the verified agent-contact route separately. Passing it on is up to the user. Company source-copy grants are separate from this add; use the [company audience policy](references/company-deployment.md#ownership-and-audiences).

Done when the exact grant and any changed map file are verified and the user has the prompt. Repeating the same add leaves map bytes, timestamps and grants unchanged. Owner-side verification is not recipient access proof; that comes from [Join](#join).

## Join

The prompt names the map storage owner and, when configured, the steward agent-contact ID. They may differ. Reading it creates no grant. Honor any explicit introduction/connection permission in a fuller company invitation; source connections and additional sharing remain separate choices.

1. **Read the map:** `/team-map/index.md` with the storage-owner ID, then each system file. If it isn't readable, keep map/source checks pending. When a verified contact was supplied and your user authorized the connection/request, use step 2 to introduce your verified Fulcra ID and request admission before the map is shared. Otherwise give the ID to your user to pass to the steward. Do not treat a join request as membership or probe a different owner to obtain the map.
2. **Resolve the agent-contact route** using [map identity fields](references/map-format.md#indexmd). Use `steward_contact_fulcra_userid` when present, not the storage ID. Reuse an existing verified authorized contact. With the user's connection approval, follow connect-our-agents to share only the connection channel with that contact ID and introduce yourself. If a separated service-account map lacks a contact route, keep it pending and continue independent source checks; do not send to a guessed service inbox. Connect to other teammates when a task first needs it.
3. **Go through the systems one at a time.** Say what it's for and how to connect, then run its check once it's connected.
   - Connecting always uses your user's own login. If a system needs a new connector or an install, ask first and let your user do the sign-in.
   - Map entries come from a teammate. Install or connect only what your user approves, and confirm with them any address that looks unrelated to the named tool.
   - If access fails, use [Resolve gaps](references/resolve-gaps.md#access) to identify the failure and the authorized next action.
4. **Save your place** in your own memory, or at `/team-map-status/<map-owner-id>.md` in your own account: which systems passed, which didn't, and when.
5. **Report** to the user and, within messaging permission, the configured steward contact. Distinguish map readability, agent-contact delivery and source checks; an unresolved contact leaves the report pending, not sent.
6. **Offer a recurring check.** See [Check connections](#check-connections).

Done when every system is marked connected, skipped by the user, or waiting on a named role/person, and any unresolved contact/delivery step is explicitly reported. Do not claim fully connected while the contact route is still pending.

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
- **Everyone else:** route the evidence-backed proposal to the designated writer through the configured agent-contact route and existing messaging permission. Company members may instead share a narrow [member-owned proposal folder](references/company-deployment.md#member-owned-proposals) with the reconciler. Review access does not authorize onward publication. Storage permission alone does not make a different chat the writer; preserve a pending proposal if no route is configured.

## Check connections

Run on request, or on a schedule your user agreed to. Agree on the schedule as [scheduled-checks](https://raw.githubusercontent.com/fulcradynamics/agent-skills/main/skills/connect-our-agents/references/scheduled-checks.md) describes, and promise background checks only once a schedule exists and has run.

1. Re-read `index.md`. If it cannot be read, report that failure and keep the previous map and check state. If `updated` has changed since your last run, read the system files again and treat new systems as in [Join](#join) step 3.
2. Run each system's check, read-only. Result per system: **working**, **failed check** (with the error), or **not connected**. A failed check leaves the connection's health unknown; it is not proof that the tool is down or access was revoked. Use [Resolve gaps](references/resolve-gaps.md) within the authorized check scope; a read-only health check does not authorize grants. Readable imports with pending/conflict/access-loss status are not proof of a current source. Scheduled checks remain read-only unless separately authorized.
3. Compare with the saved results, then save the new map timestamp, per-system outcomes, errors and check time in your own memory or `/team-map-status/<map-owner-id>.md` in your own account. On the first run report the baseline; later, notify only when an outcome or error changes, including recovery. A new check time alone isn't a change. If saving fails, say so: the next session may repeat the report.
4. Tell the steward's agent if a map entry itself is wrong, within the messaging permission already granted.

## Scheduled maintenance

For recurring map upkeep or optional knowledge consolidation, use the reusable
[setup and run prompt](references/scheduled-maintenance.md). It works manually
or in the user's native scheduled-task host, with native sources alone or
optional imports. Default to proposals; a designated writer with explicit
`maintain-map` authorization may update factual routing without another yes.
Check connections remains a read-only health check. Installing this skill does
not create a schedule, grant or knowledge output.

For company installations, the same run can inspect configured member proposals
and incoming joins under [Company deployment](references/company-deployment.md).
Register separate member instances as source routes; reconcile verified identity,
revision, audience and conflicts without writing their files or collapsing
permissions. A listener is verified scheduled pickup, not merely a contact ID.

## Remove a person

Steward's agent, on your user's instruction:

1. List outgoing shares and delete **every dedicated map share** holding only `/team-map/` and only their exact ID, including duplicates. Re-list to verify they are gone. Broader, multi-recipient or group grants may still expose the map; report those for your user's decision and do not claim map access has ended while such a grant remains or the listing is unreadable.
2. Re-read current membership and owned systems. Remove their row only if present. Ask who takes over genuinely owned systems, marking unresolved ones `unassigned, ask <steward>`; an already absent person with nothing to transfer needs no appointment question. Write/read back only meaningful changes, `index.md` last; an identical repeat leaves map bytes and timestamps alone.
3. In the verified steward messaging account (which may differ from map storage), end the relevant connection-channel share per connect-our-agents, if still present. Tell your user about any other outgoing share to their ID, and leave those to them.
4. Give a revocation checklist for **every mapped system and each native/imported route**, naming the provider administrator and any separate Fulcra-copy owner. The map is not an access inventory: mark the person's access `unknown` unless verified, and ask each administrator to confirm revocation at that route. Provider revocation does not revoke separately shared copies. Do not claim source access ended while an imported route remains readable or unknown.
   For company deployments, also stop accepting new proposals from that member and check separately granted company outputs; preserve permitted review history and unresolved decisions. Do not delete member-owned files.
5. Ask any remaining members' agents to end their connections with this person too, within the user's offboarding instruction.

Report separately which map grants were removed, which source revocations are confirmed, and what still needs a named person's action. The checklist covers only mapped systems, not proof of complete company offboarding. Copies their agent already made can't be recalled, which is another reason to send links.

## What this doesn't do

- Changing the operator/writer while keeping a company storage account needs an explicit handoff and contact/binding update, not a copy. Moving the map to another storage account needs a separate copy/share migration plan.
- People who can read the map can read all of it.
- There's no push: agents see a change on their next check.
