---
name: fulcra-content-integration
description: "Rework a piece of site content (Recipe, Blog post, docs page, landing section) so that Fulcra is integral to achieving that content's own goal — with concrete, reader-executable Fulcra how-to instructions, never bare mentions. Use when reviewing, improving, or drafting any fulcradynamics.com content that mentions Fulcra, or when asked whether a piece of content \"sells\" Fulcra well. Verifies every Fulcra claim AND every how-to step against delivered artefacts before writing. (Distinct from fulcra-content-review, which covers prose voice/claims discipline.)"
homepage: "https://github.com/ashfulcra/Fulcra-Webflow"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "🧲" } }
---

# fulcra-content-integration — make Fulcra integral, with the how-to on the page

Two tests, and a rework fails unless it passes BOTH:

1. **Integral:** if you deleted every Fulcra mention, would the piece fail at
   its own goal? If it would still work fine, Fulcra is bolted on.
2. **Executable:** can the reader go from this piece to a working Fulcra
   setup doing the thing described, without leaving to research? **A
   capability without its how-to is a name-drop.** "Your agent reads your
   sleep over MCP" is marketing until the piece says how to connect the
   agent, what to ask it, and where that lives.

The first version of this skill enforced only test 1 and shipped four drafts
full of capability talk with zero executable steps. Test 2 exists because of
that failure (2026-08-18). Do not repeat it.

## Inputs (read before rewriting anything)

- `canonical/FULCRA-FOR-AGENTS-2026-08-18.md` — THE canonical positioning
  and vocabulary. Write in its terms: Fulcra is a **user-owned context
  backend**; what accumulates is the owner's **context lake**; agents are
  clients of the context, not its owners; the three capabilities are
  **continuity, freshness, synthesis**; loops follow the **Feed-Driven
  Reactive Loop** (update summary since the last **watermark** → retrieve
  changes → respond → write back → advance the watermark); coordination is
  the **Shared Blackboard**; longevity is **Durable Owner Context**.
- The live references its References section names — read them **as
  rendered/served** (all verified 200 on 2026-08-18):
  [`fulcra.ai/llms.txt`](https://fulcra.ai/llms.txt),
  [agent onboarding](https://docs.fulcradynamics.com/agent-get-started.txt),
  [platform concepts](https://docs.fulcradynamics.com/fulcra-platform/),
  [CLI](https://docs.fulcradynamics.com/cli/),
  [MCP](https://docs.fulcradynamics.com/mcp/),
  [Groups](https://docs.fulcradynamics.com/groups/),
  [OpenAPI spec](https://api.fulcradynamics.com/openapi.json),
  [Python client docs](https://fulcradynamics.github.io/fulcra-api-python/).
- The site's own connect pages, **as rendered in production** — these are
  what you link readers to, so verify each is live first.
- `v2-payloads/DOCS-SITE-REVIEW-*.md` and `SURFACE-RECHECK-*.md` — verified
  surface facts; newer verification supersedes.
- The `fulcradynamics/agent-skills` repo — the delivered how-to material:
  `fulcra-connect` (connection paths + auth flow), `fulcra-get-started`,
  `fulcra-situational-awareness`, `fulcra-workspaces`, `fulcra-ingest`.
  `fulcradynamics/community-skills` for worked examples beyond the basics.

## Verified how-to fact sheet

Facts below were verified against delivered artefacts on the date shown.
**Re-verify each fact you use before it goes in a draft** (pages move,
production lags staging); update this sheet when a check changes a value.

| Fact | Value | Artefact / last verified |
|---|---|---|
| MCP server URL (any MCP-capable agent) | `https://mcp.fulcradynamics.com/mcp` | fulcra-connect skill; 2026-08-18 |
| Connect Claude page | `fulcradynamics.com/connect/claude` | rendered page HTTP 200; 2026-08-18 |
| Connect ChatGPT page | `fulcradynamics.com/connect/chatgpt` | rendered page HTTP 200; 2026-08-18 |
| MCP platform page | `fulcradynamics.com/platform/mcp` | rendered page HTTP 200; 2026-08-18 |
| CLI install/run | `uvx fulcra-api --help` to run; `uv add fulcra-api` / `pip install fulcra-api` to install (Python ≥3.12) | docs.fulcradynamics.com/agent-get-started.txt as served; 2026-08-18 |
| Docs pages | docs.fulcradynamics.com: `/fulcra-platform/`, `/cli/`, `/mcp/`, `/groups/`; agent guidance at `fulcra.ai/llms.txt` | rendered pages HTTP 200; 2026-08-18 |
| CLI auth | `fulcra auth login --get-auth-url` → open URL → `fulcra auth login --device-code <code>` | fulcra-connect skill + flow exercised live; 2026-08-18 |
| Delta call | CLI `fulcra data-updates` / MCP `get_data_updates` — data types with counts + file changes | live MCP call; 2026-08-18 |
| Agent skills index | `github.com/fulcradynamics/agent-skills` (install per-skill, e.g. `fulcradynamics/agent-skills/fulcra-situational-awareness`) | repo clone; 2026-08-18 |
| NOT live in production | `/platform/cli`, `/agents`, `/developers`, `/platform/python-sdk` (404) | probed; 2026-08-18 — do not link until shipped |

## Procedure

1. **State the content's own goal in one sentence.** Everything serves it.
2. **Inventory Fulcra mentions.** Classify each: *integral*, *bolted-on*,
   or — the class the first version missed — *capability without how-to*.
3. **Rework.** Bolted-on mentions become the mechanism of the piece's goal
   or get cut. If the goal genuinely has no Fulcra-shaped step, say so
   rather than forcing one.
4. **Attach the how-to to every capability.** Each "your agent can X" gets
   the reader-executable step, in the piece's own voice and format:
   - the connect path (connect page link for Claude/ChatGPT readers; the
     MCP URL for any MCP-capable agent; the CLI for terminal agents),
   - the literal ask or command that performs X ("read my workouts and
     sleep for the last two weeks", `fulcra data-updates`),
   - the skill install when a packaged skill IS the how-to.
   Recipes get a short "connect" block near the point of need, not a
   footer.
   **Know your how-to's audience.** Page copy speaks to the human: connect
   pages, plain steps. A copy-paste prompt speaks to the AGENT, and a
   newcomer judges it by what happens in the first minute after pasting:
   - **Value first.** The prompt must work immediately with zero setup.
     Never open with connection steps or Fulcra vocabulary — the agent
     does the piece's actual task first, from whatever the user can give.
   - **Connect at the moment it pays**, as an offer: after the first
     useful pass, the agent offers the connected version and explains
     what it adds in the piece's own terms.
   - **Delegate the connection to the agent, not the human.** The prompt
     points the agent at the agent-facing onboarding artefact —
     https://docs.fulcradynamics.com/agent-get-started.txt — to fetch and
     follow when the user says yes. No URLs, install commands, or protocol
     names for the human to parse inside a prompt.
   - **No unexplained jargon.** "MCP", "annotation", "timeline" only if
     the surrounding sentence makes them make sense to someone who has
     never heard of Fulcra.
5. **Verify every claim AND every step against the delivered artefact**
   before it ships: load the rendered page you link, run the command, hit
   the endpoint with valid auth, install the skill. Never verify against a
   README, a source repo, or a docs page's description. Banned evidence:
   unauthenticated endpoint probes (auth precedes routing) and raw-HTML
   string matching (strip tags, then match). UNVERIFIED → cut or flag,
   never hedge.
6. **Record before/after** on a copy (CMS: new draft item; originals
   untouched), with a claims-and-steps table: claim/step → artefact → how
   verified.

## Definition of done (all six, or it is not done)

- [ ] Goal stated; every section serves it
- [ ] No bolted-on mentions survive
- [ ] Every capability has its reader-executable step (test 2)
- [ ] Every linked page verified live in production; every command run
- [ ] Claims-and-steps table in the run record
- [ ] Originals untouched; changes on drafts; changelog entry written

## Site mechanics (standing rules)

- CMS work on **new draft items**; originals stay untouched.
- Cookbook and Blog rich text headings are **H3**.
- Every content change gets a `CHANGELOG-SITE.md` entry;
  `node ops/changelog-gate.js` and `node ops/publish-gate.js` must pass
  before any publish. **Staging only — publishing is always the user's
  manual action.**
- QA everything touched with `qa-rig/visualcheck.js` (desktop and
  `--mobile`) and `qa-rig/linkcheck.js`.
