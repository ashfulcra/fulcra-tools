# Map Our Team

Give every agent on a team a shared map of where its work lives, how to reach it,
and whose agent to ask. The map contains pointers and useful roles; content and
credentials stay at the sources. Each reader uses their own authorized native
connection or a separately granted imported-copy route.

For a company, use a company-owned Fulcra account for the map and approved
connector data. Members keep their own accounts and may run their own skill or
connector instances. One designated company dreaming agent reconciles their
member-owned proposals; nobody edits another account through a read-only share.

## Start here

- **New to Fulcra:** [company setup, member joining and copyable prompts](references/setup.md).
- **Use or install the skill:** [SKILL.md](SKILL.md), or ask a supported installer to run:

  ```sh
  npx skills add ashfulcra/fulcra-tools --skill map-our-team
  ```

- **Company accounts and member changes:** [ownership, audiences, proposals and incoming joins](references/company-deployment.md).
- **Recurring upkeep:** [one canonical maintenance recipe](references/scheduled-maintenance.md).
- **A stale fact, missing owner or failed access:** [resolve the gap](references/resolve-gaps.md).
- **Optional connector integration:** [source discovery contract](references/source-discovery.md).
- **Feedback or requests:** [agent-to-agent maintainer contact instructions](references/setup.md#feedback-or-requests-to-ashs-agents).

A signed-in Fulcra MCP or CLI is enough; no Collect, vault or coordination bus is
required. Agent messaging uses the external
[connect-our-agents](https://github.com/fulcradynamics/agent-skills/tree/main/skills/connect-our-agents)
skill. Apps that cannot install skills can read the linked instructions.

## How the pieces fit

The map is a small read-only shared folder, maintained by one named writer.
Owner-account agents honor that convention; it is not a cross-agent lock.
Storage, human operator and agent contact are separate identities. Changing the
writer can keep storage in place; moving accounts/imports needs a migration.
One team uses each steward account's map slot. See [the format](references/map-format.md).

Map membership grants only the map. Optional connector data needs its own exact
copy audience; provider ACLs do not automatically follow copied data. Notion
connection tokens and personal tokens have different access models. Attio
workspace tokens are not restricted to the permissions of the member who pasted
them. The [company reference](references/company-deployment.md#notion-and-attio-permissions)
explains these boundaries with current official sources. These skills do not
implement a provider ACL mirror.

Members share a narrow proposal route back, retaining their own source/producer
writers. Review-only content stays private; publication needs its own audience
approval. The company writer compares actual versions, evidence and current
bases, preserves conflicts, and avoids writes on identical repeats. Equal titles
or content do not establish the same resource across installations.

The optional maintenance recipe runs in an existing agent host, manually or on
its native scheduler. It can poll incoming joins and approved proposals. Default
join handling queues requests for the operator; accepting exact preapproved IDs
requires explicit delegated effects. Installing the skill creates no service,
account, schedule, grant or implicit knowledge output. Verify a real scheduled
run before saying a company agent listens. The setup guide supplies separate
company/member prompts and a manual fallback.

A producer can independently advertise existing imports through a manifest.
The map links its indexes; it does not import bodies again or edit producer
manifests, sync indexes or two-way working files. Optional connector deployments
have their own setup and release status; this repo does not bundle the external
[connector web app](https://github.com/fulcradynamics/connectors-webapp).

## Validation and limits

Version 0.1.6 checks effective audiences before saving member check status,
including inherited root/group shares. Unknown or unexpected readers leave the
checkpoint in an already authorized private location or chat while checks
continue; the skill preserves unrelated grants. The pre-map admission test now
matches the authorized introduction flow in Join.

The [acceptance scenarios](references/acceptance.md) are a live/simulated test
plan. On 2026-11-03, two authorized bot accounts verified Fulcra file
write/shared-read/readback, dedicated-share reuse and duplicate removal, and
denied reads after revocation. A bounded actual company map exercised native
and imported routing and source denial. These prove the measured operations,
not an exhaustive company inventory or all provider permission variants.

Earlier skill versions passed official Agent Skills validation, independent
review and isolated behavioral checks of creation, authorizations, lifecycle
retries and service-account identities. The canonical maintenance prompt was
manually exercised with synthetic knowledge outputs: proposal-only behavior,
owned-section publication/readback, unchanged repeat, missing authority,
conflicting writer and hostile source data. Manual exercises do not establish
unattended operation.

Version 0.1.5 adds the company/member deployment and novice setup references.
On 2026-11-04, isolated installed-skill retrieval and four novice/authorization
scenarios passed; a local synthetic reconciliation exercise passed 74 checks
across proposals, apply, repeat, revised/conflicting input and lost-share or
uncertain-write recovery. It preserved review-only content and parallel provider
namespaces, and found incoming joins despite an empty update hint. These are
agent procedure exercises, not live cross-company permission or scheduler proof.
Company proposal reconciliation, pre-map joins and permission distinctions still
need installation acceptance in the actual company account. No new company account,
production migration, member share or listening schedule is installed by this
documentation update. New-provider signup, Muse/Town onboarding and actual
recurring execution remain specific installation checks, not release claims.
