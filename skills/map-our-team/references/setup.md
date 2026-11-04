# Set up company knowledge, then invite members

This guide starts with no Fulcra account. A company-owned account stores the
company map and approved connector data. Each person keeps a separate account.
The company agent receives proposals and reconciles them through one designated
writer. The skills are unofficial, unsupported tools; no Collect installation
or local database is required for this flow.

## Company operator: prepare the account

1. Open the [Fulcra setup instructions](https://docs.fulcradynamics.com/agent-get-started.txt)
   with your agent. Use a company-controlled identity for the company account;
   complete signup/sign-in in the browser yourself. Keep recovery access with
   the company. Your members will sign in to their own accounts later.
2. Add Fulcra to the agent app if it is not connected. Use the Fulcra plugin
   when offered, or add the hosted MCP URL `https://mcp.fulcradynamics.com/mcp`
   in the app's connector/MCP settings. Approve sign-in as the company account.
   Ask the agent to verify its current account before continuing. If the app
   cannot use MCP but can run commands, have the agent follow the setup guide's
   CLI login flow and show you the browser approval link. Never paste a token
   into chat or share a company login with members.
3. Give the agent the company prompt below. It can read the skill directly;
   supported skill installers can instead run:

   ```sh
   npx skills add ashfulcra/fulcra-tools --skill map-our-team
   npx skills add fulcradynamics/agent-skills --skill connect-our-agents
   ```

   These are agent installation commands, not a requirement to learn Terminal.
   Reading the linked skill is enough when the app cannot install skills.
4. Choose company sources and who may receive each copy. Start with native
   connectors or one narrow import. A map share gives locations, not all source
   data. [Notion and Attio rules](company-deployment.md#notion-and-attio-permissions)
   explain why a source token and an imported-copy audience are separate.
   Follow the connector app's own setup guide; confirm its verified Fulcra
   destination is the company account before any import. The external
   [connector template](https://github.com/fulcradynamics/connectors-webapp)
   has its own installation/release status; a hosted demo is not your company
   deployment. Do not use someone else's personal app or credentials.
5. Supply the exact Fulcra IDs of approved members when they have accounts.
   Choose a listening cadence, timezone and stop/pause rule in your agent host.
   A useful starting choice is checking joins every 15–30 minutes, subject to
   the host's limits. Run once and inspect a real scheduled run. A task that
   exists but has never run is configured, not proven to be listening.

### Copy and paste: company account

Replace the bracketed team name. The agent should discover or ask for the other
choices as they become necessary; do not paste credentials.

```text
Set up [COMPANY NAME]'s company knowledge system using a new company-owned
Fulcra account. I have never used Fulcra. Walk me through any browser signup
or sign-in I must do, using https://docs.fulcradynamics.com/agent-get-started.txt.
Verify the actual connected account; do not assume my personal account is the
company account.

Use https://raw.githubusercontent.com/ashfulcra/fulcra-tools/main/skills/map-our-team/SKILL.md
and its references/company-deployment.md. Use connect-our-agents from
https://github.com/fulcradynamics/agent-skills for private agent connections.

Create the company map in /team-map/ once the company identity and audience
are clear. Record the company storage-owner ID, verified company agent-contact
ID and one named map/dreaming writer. Save a private installation record with
the account identities, source scopes, approved copy audiences and writer.
An existing map or personal imports need a separate migration decision.

Help me connect the company sources I choose. Keep company connector data in
the company account, using each producer's setup instructions. Verify Notion
credential/page capabilities and Attio token level/scopes; neither copies
provider permissions automatically into Fulcra. Share only the map with exact
members I approve. Prepare separate narrow data-copy shares only after the
audience for that selected content is approved. Do not expose restricted
content through a broadly shared folder or a synthesized summary.

Set up the reusable scheduled-maintenance recipe from this skill for the
company dreaming agent. It should inspect approved company routes and narrow
member-owned proposal folders, reconcile actual versions and evidence, retain
conflicts, and keep members' own skill/connector instances as separate routes.
Start with a manual proposal pass. Apply factual map repairs only under the
named writer; persistent knowledge needs a separately chosen output/section
and audience. Never write proposals into connector working files.

The company contact must check incoming joins even before a map is shared.
Use the company-deployment join policy. Start queue-only and ask me for exact
approved member IDs. If I want automatic acceptance, obtain my explicit
accept-approved policy for those exact IDs and channel/map-only effects;
everyone else waits for approval. A self-claimed company name/domain is not
membership proof. New connector-data grants remain separate.

Help me choose cadence, timezone, notifications and a stop/pause rule, then
create one native scheduled task with the canonical prompt and verified
configuration, if this host supports it. Tell me how to view, run and stop it.
Verify the correct accounts/tools in its actual runtime and observe its first
scheduled run before saying it is listening. If the host cannot do this,
explain that joins will wait for manual checks and give me that check prompt.

Give me a filled member invitation containing the verified company map-owner
and agent-contact IDs, a join prompt, and a list of completed/pending checks.
Keep unavailable sources and permissions explicit; continue independent work.
```

For a manual pickup, tell the company agent: “Use map-our-team's company
deployment and maintenance references to check incoming join requests and
approved member proposals now. Apply only my saved membership, writer and
publication policy; retain everything else for a decision.”

The task host supplies the clock and available tools. For Codex/ChatGPT, use
the app's **Scheduled** view; local tasks require the computer/app/project to
remain available. Cloud runs need their own available connections. Consult
[current scheduling documentation](https://learn.chatgpt.com/docs/automations).
The repo's [Fulcra Agent Automation](https://github.com/ashfulcra/fulcra-tools/blob/main/skills/fulcra-agent-automation/SKILL.md)
is for an existing coord team; do not install its retired listener or bootstrap
a coordination bus just to receive company joins.

## Company members: join with your own account

1. Ask the company operator for the invitation. It must name the company,
   verified map storage-owner ID and verified company agent-contact ID.
2. Paste the member prompt below into your own agent app. Follow its Fulcra
   browser signup/sign-in instructions as yourself. Keep your account separate
   from the company's; you do not need its credentials.
3. The agent requests the private connection and reads the shared map. Before
   approval, “waiting for the company” is normal. If the company agent is
   offline or has no working schedule, the operator must check manually.
4. Approve only the sources you want connected and any specific shares back.
   Your own connector instances can coexist. You do not need to re-import
   company data merely to join. Member edits to provider data use their own
   provider permissions; proposed company map/knowledge changes use the
   member-owned proposal route.

### Copy and paste: member

The operator fills the two IDs before passing this on.

```text
Join [COMPANY NAME]'s company knowledge system on my behalf. I have never used
Fulcra. Help me sign up/sign in as myself using
https://docs.fulcradynamics.com/agent-get-started.txt and verify my account.

Company map storage-owner Fulcra ID: [COMPANY MAP OWNER ID]
Company agent-contact Fulcra ID: [COMPANY CONTACT ID]

Use https://raw.githubusercontent.com/ashfulcra/fulcra-tools/main/skills/map-our-team/SKILL.md
and its references/company-deployment.md. Use connect-our-agents to connect
with this exact company contact. I authorize a dedicated connection channel
to that account, an introduction and a join request containing my verified
Fulcra ID. Share only that channel; read it back and retain the message ID.
If the map is not shared yet, request admission and report waiting. Do not
claim company membership or contact acceptance until the actual grants/reply
are verified.

Read /team-map/index.md with the company storage-owner ID once it is shared.
Check each source I choose with my own login or an explicitly granted company
copy route. Keep source access and map/contact access separate. Ask before new
source connections or additional sharing. I do not authorize sharing personal
files, importing all my data or running provider writes just to join.

If I already use map-our-team or my own connectors, keep my account, instances
and writer bindings intact. Register only the source routes I approve, with
their actual provider namespace and revisions. Do not deduplicate by title or
merge permissions. If I ask for a company map/knowledge change, prepare a
proposal in my account under the skill's member-proposal convention. Show me
its exact contents and intended publication audience before asking for any
new folder share. Sharing a proposal for company review is not permission to
publish it to everyone. The company dreaming agent reconciles it; you do not
write into company or connector working files.

Report what I can read, what is waiting on approval, and how to check for a
reply. Offer recurring checks only if I want them; verify a real run before
promising background operation.
```

## Check the setup together

Use a harmless test item intended for the member's actual audience:

- Company: exact map grant exists and index/roster readback matches. Member:
  shared map read succeeds, reciprocal channel and introduction reply are seen.
- Company source reader and member each check their own allowed route. A denied
  restricted source stays denied; map membership supplies no privileged fallback.
- Member submits one approved routing proposal. Company runs proposal mode,
  then an authorized apply. Verify output before checkpoint; repeat produces no
  new map versions. A conflicting/stale proposal remains pending.
- Two member instances advertising the same verified resource retain separate
  access routes; different namespaces never collapse because titles match.
- Check one actual scheduled company run, including incoming-share enumeration,
  while an unknown-account request remains queued. Inspect pause/stop controls.

These are installation acceptance steps, not claims they already ran for a new
company. Existing personal or test-account evidence cannot prove its setup.

## Feedback or requests to Ash's agents

[agents.ashkalb.com](https://agents.ashkalb.com) is an optional agent-to-agent
contact route for the maintainer. It is separate from company membership and
from Fulcra support. Read its current [agent invitation](https://agents.ashkalb.com/agent.md)
to resolve the recipient and protocol; do not copy a recipient ID from an old
guide. No company map/source access is needed to report a problem.

### Copy and paste: feedback or request

```text
Send my feedback/request about Fulcra Tools to Ash Kalb's agents through
https://agents.ashkalb.com. Read its current /agent.md invitation and use
connect-our-agents. Reuse my verified existing connection if present.

My feedback/request: [DESCRIBE IT HERE]

I authorize a dedicated channel shared only with the recipient in that current
invitation, and this introduction/feedback. For a new connection, begin the
introduction with the exact line "via: agents.ashkalb.com" and include my name,
your agent name and purpose. Send from my own account, verify the stored
message and retain its ID. Follow the invitation's reciprocal-share and reply
checks; a successful send means waiting, not that Ash read or accepted it.

Include the relevant skill/app version, intended result, actual result and a
small sanitized reproduction. Do not include credentials, company documents,
private IDs or logs, or broaden any share. Ask before sending a private
attachment or details beyond this request. Show me the sent message and how
to check replies. Do not create recurring checks unless I request them.
```

The invitation says pickup depends on Ash's host being available. Silence is
not a rejection. For follow-ups, use the existing connection and message
reference rather than creating another channel.
Use the current connect-our-agents send-recovery procedure if a send is uncertain:
reconcile the exact stored message and honor stop/permission signals before
considering its bounded retry. The invitation supplies the recipient and
introduction; its older same-ID retry wording is not a guarantee that reposting
is safe. Do not retry blindly or change tools to bypass a denial.
