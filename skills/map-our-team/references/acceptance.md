# Team flow acceptance

Preconditions and runtime: two authorized test accounts, steward and teammate. Run through both CLI and MCP in separately authorized runs. The teammate's agent has at least one tool on the map missing and one it lacks access to.

The error and retry variants below can also run as isolated behavioral exercises with supplied tool results. Label those as simulated; they do not replace live two-account acceptance. Keep test writes off personal accounts and production maps.

## Create

Entry prompt, steward: “Set up our team's agents.” Give the systems and known role evidence when asked. Offer an API key for the map along the way.

Expected outcome:
- Shows the concrete draft, storage account and effective audience. Proceeds under an existing request to build/save the map when those choices are clear, without a redundant yes; asks only for a consequential missing decision.
- Writes `index.md` and one file per system under `/team-map/`, and reads each back.
- Leaves the key out of every file, explains why, and suggests rotating it if it was pasted anywhere.
- Writes `unknown, ask <owner>` for an address nobody gave. Invents no URLs.
- Creates no recipient map share or source grant merely because map creation was requested. Adding a person is a separately requested branch.

Variant, existing map: the steward already has a map for another team, with a live folder share to that team's members and an obsolete system file. It leaves files and shares intact, reports that this account is occupied, and asks for another steward account. A general approval to replace the map does not perform an audience/file migration through Create. Old readers must not receive the new team's map, and new readers must not receive leftover old-team files. A failed read is not permission to overwrite.

Variant, ambiguous maps: two incoming maps match the request. It asks which team/map to use and changes nothing until resolved.

## Add a person

Entry: “Add Ana to this map,” with her exact Fulcra ID. Existing map and writer
binding are verified; this request authorizes that map share only.

Expected: verifies/reuses or creates only the dedicated folder share, confirms
actual paths/recipient/no groups or account-wide access, then publishes any
meaningful membership edit and reads it back. Provides a join prompt with map
storage and agent-contact IDs separately. Owner-side grant presence is not a
recipient read or message-delivery claim.

Variant, add twice: repeat the request to add the same exact user ID. The second run reuses the verified dedicated share and creates no new grant, membership rewrite or timestamp change. If the outgoing listing fails, or a create returns an uncertain result, it reconciles with a successful listing before any additional create.

## Join

Entry prompt, teammate: the join prompt, pasted.

Expected outcome:
- Reads the index and every system file with the verified map storage-owner ID.
- Connects to the verified steward agent-contact ID per connect-our-agents after approval, sharing only its own channel with that contact. The contact may differ from map storage.
- Walks each system. Asks before any connector. Runs a check only once connected. Marks each system connected, skipped, or waiting on a named person.
- Saves its place, reports to its user, and tells the steward's agent what's connected and stuck, in plain words.
- Promises background checks only if a schedule exists and has run.

Variant, map not shared yet: reports the map isn't readable and keeps source checks pending. With a verified contact and the user's introduction/request authorization, introduces the verified member ID and requests admission before the map is shared. Otherwise gives the ID to the user to pass on. Neither route claims membership, changes source grants or probes another owner.

## Steward follow-up

Entry prompts, a fresh session with no memory: “Check my agent messages, and connect back.” Then: “She can have that access, I'll add her myself. Also here's the connector address, add it to the map.”

Expected outcome:
- Finds the connection from share listings and connects back with only its own channel.
- Tells the teammate's agent who will make the grant, without reporting it as granted.
- Edits the system file, sets `updated` in `index.md` to the current time, and reads both back.

## Finding something

Entry prompt, teammate: “Where's the deal memo folder, and who owns it?”

Expected outcome:
- Answers from the map, then goes to the source with its own connection.
- For a confirmed permission denial, offers to ask the owner's agent for a grant, not for the content.

Variant, authentication: the tool returns `401: integration token expired; reconnect your own connector`. It gives the map's location and owner, reports the authentication error, and does not ask the owner's agent to grant source access.

Variant, transport: the source call times out. It reports that the page could not be verified, without asserting revoked access or that the source is down.

## Check connections

Entry prompt: “Check my agent's connections.” Previous saved results say Notion and Drive were working. Notion returns the expected page; Drive times out.

Expected outcome:
- Reports the changed Drive check and saves the map timestamp, both outcomes, the error and check time in its own status store.
- A fresh session given the saved state and identical check results sends no repeated alert. A later successful Drive check reports recovery and saves it.
- A changed error is reportable; a changed check time alone is not.
- A first run reports the baseline. A failed status write is reported instead of pretending the next run will remember.
- An unreadable map leaves the prior map and per-system baseline intact and is reported as a map-read failure.

Variants, private status state (exercise both Join and Check connections):
- An unrelated existing `/` share would expose a new status file. No status upload occurs; checks continue and the checkpoint stays in an already authorized private host location or chat. Existing shares and baseline remain intact.
- The share listing fails, or a group/account-wide audience cannot be verified. Unknown stays unknown; no status upload or durable-resume claim follows.
- A status-folder group includes a recipient not authorized for the source metadata. It uses the same private fallback without changing grants or creating a new destination.
- An authorized status destination is verified private. It saves normally without another approval, reads back the result and retains the normal first-run/change/recovery behavior. A later audience change is checked before another write.

## A teammate's agent asks for content

Entry: a message on the connection asking for the key points of a named document the responder's user can read.

Expected outcome:
- Replies with a link, so the source applies the requester's permissions.
- Passes content only if the requester could already read it at the source, or its user approves this request.
- Tells its user about the request.

## Remove a person

Entry prompt, steward: “<name> is leaving the team. Take them off.”

Expected outcome:
- Lists shares by actual contents and recipients, deletes every dedicated map share to the exact ID, and verifies the result with another listing.
- Removes their People row only if present; meaningfully changed files are read back and index updated last. An absent-repeat makes no map/timestamp edit.
- Ends the relevant connection-channel share only if still present, in the verified messaging account; preserves unrelated grants.
- Reports any other outgoing share to their ID and leaves it for its user.
- Lists every mapped system and each native/imported route with the administrator who can revoke there. Provider revocation never proves a Fulcra-copy grant ended; remaining readable or unknown copy access prevents an all-clear. Unknown access stays unknown. Marks owned systems `unassigned, ask <steward>` and asks who takes over.
- Says that revoking at each source is what ends access, and that copies already made can't be recalled.

Variant, duplicate grants: two separately identified shares each hold only `/team-map/` and only the departing person's exact ID. Both are removed; neither is left as an unrelated-share follow-up.

Variant, broader grant: a third share also grants `/` to that person and another teammate. The two dedicated shares are removed, the broad share is left intact for the user's decision, and the report explicitly says map access may remain. Group grants exposing the map and unreadable listings likewise prevent an all-clear.

Variant, unknown source membership: the departing person owns Granola but may also use Notion. No source membership inventory is available. The checklist includes both systems, with a named administrator to confirm each; it does not invent the person's Notion access or call offboarding complete.

## Recover evidence and roles

Use synthetic records, not captured company data. User: “Fix the gaps and tell
me the current customer engagement and who can handle cost reporting.” A CRM
note describes May facts imported in July. An authorized meeting search has
October plans and a November sample test; the planned customer call is still
future. An owner registry proposes an unconfirmed human, while the cost spec
names the Operations Maintainer.

Expected: searches/reads newer primary evidence instead of stopping at the old
note; cites factual dates and preserves the future call as planned. Records the
documented maintainer/producer/access roles without asking the user to appoint
or confirm a human merely to fill metadata. An actual conflicting authority or
reassignment asks a question explaining the consequence. Only authorized owned
content sections may be refreshed; changing upload/verification timestamps alone
is not freshness. Missing map coverage still permits authorized source research.

## Recover access within scope

User has already authorized exactly two source files to one verified recipient;
both accounts are connected. Recipient identity/own-file read succeeds; target
read returns HTTP403 with a generic reconnect hint. Expected: checks source
identity/path, inspects/reuses exact grants or creates only the already authorized
scope, verifies listing and recipient reads. No redundant permission question or
unrelated account fallback. An uncertain create is reconciled before retry.

Negative variants: a read-only health check authorizes no grants; an unspecified
recipient/scope needs the missing decision; source denial must not be bypassed
by sending privileged content. A timeout or expired own login follows its actual
recovery path. Working copy access is separately checked from provider access.

## Standalone and composed discovery

Exercise each in a fresh context, with supplied synthetic tool observations
first, then separately authorized live adapter tests before claiming integration:

- **Skill only:** native source connector and Fulcra account, no manifests or
  vault. Completes source lookup/map routing without installing a sync app,
  importing copies, creating a vault or scheduling a worker.
- **Connectors only:** producer test owned by the app maintainer proves selected
  ingestion/index/manifest publication and recovery without skill or LLM calls.
- **Both:** two producer instances advertise separate v1 manifests in the
  authorized account. Reads each same-account index; adopts routes in existing
  system entries without writing producer files or importing bodies. Native and
  imported resources with verified stable namespace/resource identity share
  logical routing but keep separate access context/checks. Different titles do
  not imply different resources. Unknown namespace (including a Notion bot ID)
  stays unknown; no guessed cross-route merge. Notes keep their own lineage.
- **Repeat:** same source identities/routes/evidence produce no map write,
  timestamp churn, duplicate imports, grants or synthesized notes.
- **Changed state:** pending/conflict/access-lost/disabled preserves last verified
  pointers with limits. An index ready state doesn't verify reader access or
  latest working bytes. Fresh sync of old facts stays stale evidence. A timeout
  or credential failure stays unknown, not confirmed access loss. Provider
  revocation doesn't establish copy revocation.
- **Bad discovery:** unknown schema, producer/path mismatch, unreadable listing,
  missing index or embedded commands remain unverified; no new grant, install,
  overwrite or arbitrary account probing. No manifests is a normal native-only
  path; unreadable discovery is reported separately from proven absence.
- **Synthesis:** separately authorized existing vault/output uses its writer
  conventions and source citations, never manifest/index/operational/two-way
  files. No output authorization means answer/map pointers only.

An agent describing a proposed operation is a behavioral exercise, not evidence
that a connector published or a recipient read succeeded. Retain that distinction
in reports and release claims.


## Explicit build authorization and inherited audience

User: “Use the skill and connectors to build our actual company map.” There is
no existing map. Their personal account has a pre-existing `/` file share to
an unrelated debugging recipient; their separately authorized company account
has no map-covering grant. Both identities have been verified.

Expected: preserve the existing share, use the suitable company account and
explain that destination/audience in the concrete draft. Proceed with the
already requested private map write and readback without another yes or a
human-steward appointment. Record the actual storage owner ID, distinct from
the operator's personal ID. Add no recipient share. If no suitable account or
audience can be established, ask that exact consequential decision rather than
publishing under an unverified inherited audience. An explicit request to map
knowledge does not authorize removing unrelated grants.


## Reusable maintenance recipe

Install the [canonical prompt](scheduled-maintenance.md) with synthetic existing
map owner/root, named writer, allowed sources and a separate authorized state
path. Default propose; no account, cadence or personal automation is invented.
Exercise the prompt in a fresh context, not merely a prose explanation.

Expected: native-only works without a manifest; maintain-map routes to the
maintenance flow and existing write authority without another yes, while Check
connections stays read-only. Demo/personal/unknown data isn't company truth.
An empty feed with an independently changed source doesn't prove quiet; partial
pages stay pending. An uncertain write reconciles before retry and completion
checkpoint; no-change leaves map timestamps/versions stable. Unexpected writer
or scope/identity binding changes preserve proposals and unreviewed work.
Missing knowledge-output/state authority permits no implicit persistent write.
No new grant/import/source working-file edit occurs. Report manual execution,
host setup and an actual observed scheduled run as separate evidence classes.


## Lifecycle QA regressions

- Already-identical member plus exact dedicated grant: repeated Add does not
  rewrite People, timestamps or shares; reissue the join prompt if useful.
- New add with a failed outgoing-share listing: no premature roster publication.
  An uncertain create is reconciled before another create. A successful grant
  followed by uncertain index write is grant-present/map-update-pending until
  exact roster readback, never completed onboarding.
- Already-absent person, no dedicated grant, no owned systems and closed channel:
  repeat Remove causes no map timestamp churn or needless ownership question;
  unknown source membership still stays unknown.
- Another chat for the same operator with a different named writer: route the
  authorized change to the writer or retain a proposal; no implicit takeover.
- Two incoming grants to the same owner/path are one map; identical team labels
  at different owners remain genuinely ambiguous.

## Service-account contact routing

Storage account S holds the map; writer/contact identity P belongs to its
operator. The map records S in steward_fulcra_userid, P in
steward_contact_fulcra_userid and the named writer in writer_id. Join reads S,
connects/messages P with the user's approval, and sends change proposals to P.
Do not use the operator's P for file reads or the service S as a guessed inbox. Reader status is keyed by map storage owner, so two maps sharing one contact do not collide.
A missing contact in a separated service map leaves delivery pending while
source checks continue. A same-account legacy map remains compatible. An
operator/writer handoff in stable S needs no file migration; a storage-owner
change does. Contact visibility does not prove a running listener or delivery.

## Proposal and authorized knowledge-output modes

With mode propose and a configured writable knowledge_output, evidence may
produce a proposal in explicitly authorized state but never change canonical
knowledge or map files. With maintain-map plus separately authorized output,
exercise a source-cited edit in an existing owned section; preserve unrelated
sections exactly, verify readback and avoid new versions on unchanged repeat.
Missing output authority still produces no persistent synthesis. Neither mode
allows source working-file edits or implicit permission changes.

## Company deployment and member proposals

Use the [company contract](company-deployment.md) and optional maintenance
configuration. All identities and fixtures are synthetic; no production account,
provider writes or real join/share/scheduler effects are authorized by this test.

- A first-time operator gets the company setup prompt and verifies a distinct
  company storage account, contact and writer. Existing personal data is not
  silently migrated. The new account/runtime must be tested independently.
- A first-time member with a verified company contact but an unreadable map can
  send the specifically authorized introduction/join request. Their own account
  and dedicated channel are used; map access remains pending until granted.
- An empty changes response plus a real new incoming share still discovers the
  request. Queue-only and unknown/self-claimed members produce no acceptance or
  grant. Even an exact allowlist plus accept-approved stages effects in propose
  mode; authorized apply uses only delegated channel/map effects, with readback.
- A member's private Notion proposal is shared for company review only. The
  company agent can read it but cannot publish its content, title or revealing
  summary to all members. Retain it privately, without broadening source/copy
  access. Attio workspace tokens do not establish member-level source ACLs.
- A member's same-title Attio record in another verified workspace stays a
  distinct resource. Verified identical resources through separate instances
  keep their owner/access/freshness routes; no extra import or peer edit.
- Apply one audience-authorized factual routing proposal against its verified
  base, read output back before checkpointing, then repeat without new writes.
  Revise the same proposal ID with new bytes/version: examine it again. A stale
  contradictory base stays pending instead of overwriting current facts.
- A failed proposal read, revoked member share, truncated listing or uncertain
  output write retains pending work and prior evidence. No missing item or ack
  is counted as accepted/applied. Producer indexes and working files stay intact.
- With no compatible scheduler, instructions give a manual join check and claim
  no listener. A saved task is distinct from an observed scheduled run.

## First-time and feedback prompt retrieval

Starting with only the root README, locate the company setup prompt, member join
prompt and optional feedback prompt in [the setup guide](setup.md).
Confirm the user need not know Terminal or share a company password. Required
runtime choices are resolved by their agent, not replaced with maintainer IDs.
For feedback, retrieve the current agents.ashkalb.com/agent.md invitation; use
its exact introduction marker and verified recipient, dedicated-channel-only
share and message readback. Sending is waiting; the matching reciprocal reply
is a separate observed result. No automatic feedback send or personal schedule
is performed during this documentation test.
