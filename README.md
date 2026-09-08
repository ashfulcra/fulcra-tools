# Fulcra Tools

Vibe-coded by Fulcra’s lawyer, on Fulcra’s own primitives. Unofficial,
unsupported, and a useful thing to point your agents at.

This is a monorepo of software and skills for working with
[Fulcra](https://fulcradynamics.com). Agents use the coordination tools to
share work and the continuity tools to pick it up again after a session ends.
There are also tools for shared knowledge, preferences, and building things
on Fulcra. **Collect** is the optional app that gives those agents access to
local apps, exports, and other context they cannot reach from where they run.
You do not need Collect to use Fulcra, the coordination bus, or continuity.

## Note from the human: this is how I use Fulcra

I use Fulcra to build stuff and get stuff done by coordinating long-running
agents across platforms. They capture ideas, plan together, assign work to
one another, and review each other’s changes. The useful part is that their
work survives the session that produced it.

The agents [coordinate](COORDINATION-PROTOCOL.md) over typed records and
versioned files in a Fulcra account, saving
[checkpoints](skills/fulcra-agent-continuity/SKILL.md) along the way. Another
session can read what happened, see what is still owed, and pick up the work.
The model or harness can change. The context stays with its owner.

**coord** carries the shared obligations: tasks, roles, reviews, and replies.
**continuity** carries a session’s objective, decisions, open questions, and
next actions. A checkpoint does not cancel what an agent owes the others;
resuming means checking both.

The bots also built Collect. A cloud agent cannot read a Notes database on
your Mac or recover a podcast history from your local backup. Collect runs
where that data lives and brings selected sources into Fulcra, where your
authorized agents can use them. Other sources have their own APIs or exports;
Collect gives those imports a common place to run. Some of the importers also
work as standalone commands.

This is the stuff that grew out of using Fulcra, made available for other
people to inspect and try. Some pieces are experiments. Some have been used
extensively. None of that makes this a supported Fulcra product.

## Pick the part you need

- **Get agents working together:** [join the bus](docs/coord/GET-ON-THE-BUS.md).
- **Keep work across sessions:** [continuity](packages/fulcra-continuity/README.md)
  and its [agent skill](skills/fulcra-agent-continuity/SKILL.md).
- **Keep shared knowledge or preferences:** [Vault](packages/fulcra-vault/README.md)
  and [Prefs](packages/fulcra-prefs/README.md).
- **Bring local apps and exports into Fulcra:** [Collect](#collect-on-a-mac)
  and the [source guide](docs/how-do-i-get-my-data.md).
- **Build something on Fulcra:** the [FDE skill](skills/fulcra-fde/SKILL.md).
- **Find a particular tool:** the complete [package index](#package-index)
  and [skill index](#skill-index) below.

## The demo: point two agents at this repo

1. Have both agents read [`AGENTS.md`](AGENTS.md), then follow the
   [bus quickstart](docs/coord/GET-ON-THE-BUS.md).
2. Each installs the Fulcra client and starts sign-in:
   `uv tool install fulcra-api`, then `fulcra auth login`.
   You handle the browser sign-in and any permission prompt from your agent’s
   environment. Agents using the same Fulcra account can share a team bus.
3. Have one agent assign a small task and the other complete it and report back.
   [`coord-engine`](packages/coord-engine/README.md) records the task and reply;
   the skills supply the working conventions.
4. Save a checkpoint, end a session, and resume it. Check the open obligations
   as well as the checkpoint. That is the loop worth trying.

The shared state lives in your Fulcra account. The basic bus needs no separate
coordination server or broker. Agents read their queues when they wake; optional
router and automation tools have their own setup and operating requirements.
See the [bus contract](docs/coord/BUS-V3.md).

You can read the docs, inspect the code, install skills, and run local help or
tests without a Fulcra account. Reading or writing a Fulcra store requires its
owner’s authorization. [`FULCRA-PRIMITIVES.md`](FULCRA-PRIMITIVES.md) maps the
CLI, HTTP, and MCP surfaces so an agent can choose what its environment supports.

## Package index

Every directory under `packages/` is listed here. Each README covers that
package’s setup, behavior, and limits. Install the pieces you need; the repository
is not a requirement to run every piece together.

### Agent coordination, continuity, and context

| Package | What it does |
|---|---|
| [coord-engine](packages/coord-engine/README.md) | Team tasks, directives, roles, reviews, presence, continuity, routing, and other bus bookkeeping. |
| [coord-fold](packages/coord-fold/README.md) | Folds coordination events into bounded, resumable obligation views. |
| [coord-mesh](packages/coord-mesh/README.md) | Cross-account coordination through outboxes and scoped data shares. |
| [coord-tracker-bridge](packages/coord-tracker-bridge/README.md) | Projects coordination work into external trackers; includes a Linear adapter and separate planning/apply steps. |
| [fulcra-continuity](packages/fulcra-continuity/README.md) | Standalone checkpoints and resume briefs for long-running work. |
| [fulcra-vault](packages/fulcra-vault/README.md) | Shared markdown knowledge, links, owned sections, and generated indexes in Fulcra Files. |
| [fulcra-prefs](packages/fulcra-prefs/README.md) | Preference signals, compiled views, and consent-gated preference sharing and decisions. |
| [fde-engine](packages/fde-engine/README.md) | Tracks the interview, design, prototype, and build lifecycle used by the FDE skill. |
| [fulcra-okf](packages/fulcra-okf/README.md) | Parse, validate, and emit Open Knowledge Format documents. |
| [fulcra-common](packages/fulcra-common/README.md) | Shared API clients, record and definition helpers, and import utilities. |

### Collect, connectors, and importers

| Package | What it does |
|---|---|
| [collect](packages/collect/README.md) | Optional plugin host: scheduling, setup, credentials, run state, and the local dashboard server. |
| [menubar](packages/menubar/README.md) | The macOS app, installer bundle, status controls, and quick recording. |
| [web-ui](packages/web-ui/README.md) | Collect’s dashboard and setup wizard; static HTML, CSS, and JavaScript. |
| [apple-notes](packages/apple-notes/README.md) | Copies Notes and available attachments to Fulcra Files; separate experimental writeback. |
| [attention](packages/attention/README.md) | Browser attention collection, including the [Chrome extension](packages/attention/chrome/README.md). Extensions send directly to Fulcra; Collect’s entry helps with setup. |
| [dayone](packages/dayone/README.md) | Imports selected journal entries from a local Day One database or JSON export. |
| [gmail](packages/gmail/README.md) | Read-only Gmail polling, filtering, file uploads, and optional bus relay. |
| [media-helpers](packages/media-helpers/README.md) | Media-history importers for APIs, exports, local libraries, feeds, and webhooks. |
| [csv-importer](packages/csv-importer/README.md) | Maps CSV columns into Fulcra records with import checks and deduplication. |
| [netflix-skill](packages/netflix-skill/README.md) | An agent-led Netflix viewing-history import and optional sharing workflow. |
| [purpleair](packages/purpleair/README.md) | Polls selected cloud or local-network sensors and imports air-quality readings. |
| [labs](packages/labs/README.md) | Validates agent-extracted lab results and imports measurements into Fulcra. |

The retired `fulcra-coord` and `fulcra-coord-files` implementations remain in
git history. New coordination work uses the packages above.

## Skill index

Skills are instructions an agent follows. They are useful independently of the
Mac app; each skill names the tools and access its workflow needs. This index
includes the skills under `skills/` and those bundled inside packages.

| Skill | Use it for |
|---|---|
| [Coordinator discipline](skills/coordinator-discipline/SKILL.md) | Coordinator working rules and follow-through. |
| [ATC](skills/fulcra-agent-atc/SKILL.md) | Capability and headroom-based model routing. |
| [Automation](skills/fulcra-agent-automation/SKILL.md) | Scheduled reconciliation and resumable wakes. |
| [Cloud coordinator](skills/fulcra-agent-cloud-coordinator/SKILL.md) | Running a coordinator whose state survives container replacement. |
| [Continuity](skills/fulcra-agent-continuity/SKILL.md) | Snapshot, park, and resume an agent’s work. |
| [Directives](skills/fulcra-agent-directives/SKILL.md) | Assignments, reminders, backlog, replies, and handoffs. |
| [Durable state](skills/fulcra-agent-durable-state/SKILL.md) | Restoring an agent’s working files from its Fulcra stash. |
| [Forge](skills/fulcra-agent-forge/SKILL.md) | Connecting GitHub evidence to bus reviews. |
| [Health](skills/fulcra-agent-health/SKILL.md) | Tooling, store, and fleet health checks. |
| [Operator](skills/fulcra-agent-operator/SKILL.md) | Surfacing questions that need a human and returning answers to waiting agents. |
| [Presence](skills/fulcra-agent-presence/SKILL.md) | Agent liveness, current work, and broadcast reach. |
| [Reconcile](skills/fulcra-agent-reconcile/SKILL.md) | Maintaining task indexes and queryable views. |
| [Review](skills/fulcra-agent-review/SKILL.md) | Independent review requests and verdicts. |
| [Roles](skills/fulcra-agent-roles/SKILL.md) | Durable responsibilities, leases, and vacancy escalation. |
| [Tasks](skills/fulcra-agent-tasks/SKILL.md) | Typed task status, ownership, and completion evidence. |
| [Content review](skills/fulcra-content-review/SKILL.md) | Checking human-facing prose for supported claims, voice, and evidence. |
| [FDE](skills/fulcra-fde/SKILL.md) | Taking a product idea through discovery, prototype, and build. |
| [Lab results](skills/fulcra-lab-results/SKILL.md) | Reading, verifying, and importing lab reports. |
| [Sealed secrets](skills/sealed-secrets/SKILL.md) | Encrypted credential bundles and verification after recovery. |
| [CSV import](packages/csv-importer/skills/fulcra-csv/SKILL.md) | Mapping a user’s CSV into Fulcra. |
| [Preferences](packages/fulcra-prefs/skill/SKILL.md) | Capturing and using user-owned preferences. |
| [Vault](packages/fulcra-vault/skill/SKILL.md) | Maintaining shared knowledge in Fulcra Files. |
| [Media import](packages/media-helpers/skills/fulcra-media/SKILL.md) | Selecting and running a media-history importer. |
| [Netflix](packages/netflix-skill/skills/fulcra-netflix/SKILL.md) | Importing viewing history and setting up optional sharing. |

## Collect on a Mac

Use Collect when you want one of its connectors or a common place to schedule
imports. It is one way to put data into Fulcra; standalone importers and direct
API writes are also available.

**[Download Collect 0.1.1 beta for Apple silicon](https://github.com/ashfulcra/fulcra-tools/releases/download/collect-v0.1.1-macos-arm64/Fulcra-Collect-macOS-arm64.dmg)**

Open the disk image, drag Collect into Applications, open it, and follow the
setup prompts. The [installation guide](docs/collect.md#get-started-new-user)
covers sign-in and source permissions. The app includes its Python runtime;
you do not need Terminal or Homebrew to install it.

The released beta includes Apple Notes and the sources listed in the
[plugin-status table](docs/collect.md#plugin-status). Bundled does not mean
configured or verified with every account. **Reminders and Todoist are in
[development for 0.1.2](https://github.com/ashfulcra/fulcra-tools/pull/757), not
in the current download.** They add selected lists/projects and ordinary task
completion in both directions; recurring tasks must be completed in the source.

## Working on the repo

Point an agent at [`AGENTS.md`](AGENTS.md). Package READMEs explain standalone
installs; the [documentation index](docs/README.md) links the broader guides.
For a full Mac development checkout, `bash scripts/setup.sh` installs the
workspace dependencies and runs the tests. It does not start Collect for you.
See [`docs/TESTING.md`](docs/TESTING.md) for the development setup and platform
requirements.

Every repository change must update the relevant READMEs in the same PR. Keep
this package and skill index complete, and distinguish released behavior from
work in progress. Examples and fixtures must be synthetic. Personal content,
credentials, and local diagnostic reports stay out of the public repository.
See the [privacy rules](docs/PRIVACY.md).

Changes go through a PR and an independent review by a different agent identity.
The review handshake lives on the bus; a GitHub comment alone does not satisfy
it. [`AGENTS.md`](AGENTS.md) has the full conventions, including how to log work,
request review, and close tasks with evidence.
