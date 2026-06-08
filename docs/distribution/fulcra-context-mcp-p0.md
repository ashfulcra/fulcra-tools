# Fulcra Context MCP P0 Distribution Runbook

Date: 2026-06-04

This runbook turns the broader Fulcra distribution target sheet into a concrete first pass for `fulcra-context-mcp`. It focuses on high-intent surfaces where builders already search for MCP servers, client install paths, or agent skills.

## Current Package Facts

- Public repo: `https://github.com/fulcradynamics/fulcra-context-mcp`
- Public remote endpoint: `https://mcp.fulcradynamics.com/mcp`
- User docs: `https://fulcradynamics.github.io/developer-docs/mcp-server/`
- PyPI package: `fulcra-context-mcp`
- PyPI latest observed: `0.1.5`
- GitHub latest tag observed: `v0.1.5`
- Repo `main` package version observed: `0.1.6`
- Existing listing: Glama, at `https://glama.ai/mcp/servers/@fulcradynamics/fulcra-context-mcp`
- Existing repo hygiene gap: GitHub repo has no description, homepage, or topics set.

## Release Hygiene First

Before new registry submissions, resolve the package metadata mismatch:

1. Decide whether `0.1.6` is ready to release.
2. If yes, publish `fulcra-context-mcp@0.1.6` and tag `v0.1.6`.
3. If no, align repo metadata back to the published package version before generating registry metadata.
4. Add GitHub repo metadata:
   - Description: `MCP server for consented Fulcra health, activity, sleep, location, and annotation context.`
   - Homepage: `https://fulcradynamics.github.io/developer-docs/mcp-server/`
   - Topics: `mcp`, `model-context-protocol`, `fulcra`, `personal-data`, `health-data`, `wearables`, `quantified-self`, `ai-agents`, `oauth`

## Positioning

Short listing description:

> Fulcra Context MCP gives AI assistants consented access to a user's Fulcra context: sleep, activity, workouts, annotations, location, metrics, and profile data through a remote or local MCP server with OAuth handled outside the chat.

One-liner:

> A privacy-conscious MCP server for bringing consented personal context from Fulcra into Claude, Cursor, VS Code, Codex, ChatGPT-compatible connectors, and other agent clients.

Long description:

> Fulcra Context MCP connects AI tools to Fulcra's user-consented personal context API. Agents can retrieve metric catalogs, time series, samples, sleep cycles, workouts, annotations, location context, and user profile details without receiving raw OAuth credentials in the chat. It supports a hosted remote MCP endpoint and local `uvx` execution, making it useful for Claude Desktop, Claude Code, Cursor, VS Code/Copilot, OpenAI remote MCP workflows, Perplexity, and other MCP-capable clients.

Primary tags:

`MCP`, `AI agents`, `personal context`, `health data`, `wearables`, `sleep`, `activity`, `annotations`, `OAuth`, `quantified self`

Security / privacy copy:

> Fulcra Context MCP uses Fulcra OAuth for account authorization. The MCP client receives MCP-scoped access rather than Fulcra refresh tokens, and the public docs should keep users on a URL/device-code/auth-browser flow instead of asking them to paste secrets into chat.

## Available Tools To Mention

- `get_annotations`
- `get_workouts`
- `annotations_catalog`
- `get_metrics_catalog`
- `get_metric_time_series`
- `get_metric_samples`
- `get_sleep_cycles`
- `get_location_at_time`
- `get_location_time_series`
- `debug_token_info`
- `get_user_info`

Avoid leading with `debug_token_info` in public copy; keep it in support/troubleshooting docs only.

## P0 Distribution Board

### MCP Registries

- Official MCP Registry
  - Status: To do.
  - Move: create canonical registry metadata after version alignment.
  - Asset: `server.json`, public endpoint/package URL, release notes, auth model.
  - Blocker: package version mismatch must be resolved first.

- Smithery
  - Status: To verify / auth-gated.
  - Move: publish or claim listing.
  - Asset: `smithery.yaml`, install snippets, tags, screenshots or demo GIF.
  - Blocker: requires GitHub/login authority.

- Glama
  - Status: Listed; improve.
  - Move: claim/update metadata and improve conversion copy.
  - Asset: better repo metadata, icon, docs URL, OAuth explanation.

- mcp.so
  - Status: Listed/improve according to current target sheet; earlier form was login-blocked.
  - Move: verify live listing ownership and update copy/tags.
  - Asset: short description, GitHub/docs links, category tags.
  - Blocker: likely requires Google/GitHub login to claim or edit.

- Cline MCP Marketplace
  - Status: To do.
  - Move: open marketplace submission once README and `llms-install.md` are polished.
  - Asset: 400x400 icon, README, `llms-install.md`, security notes, install command.

- MACH Alliance MCP Registry
  - Status: To do.
  - Move: submit enterprise-oriented MCP metadata.
  - Asset: endpoint, auth model, support details, capabilities list.

- MCPCentral
  - Status: To do.
  - Move: publish via `mcp-publisher`/MCPCentral flow.
  - Asset: `server.json`, verified namespace, package metadata.

- mcpservers.org
  - Status: To do.
  - Move: submit free listing.
  - Asset: concise metadata and install path.

### Client Install Paths

- Claude Code MCP configuration
  - Status: To do.
  - Move: add first-class `claude mcp add` docs.
  - Asset: command examples for remote and local modes.

- Claude Desktop / Team connectors / Connector Directory
  - Status: To do.
  - Move: document remote connector setup and directory-review path.
  - Asset: OAuth explanation, admin notes, support/privacy copy.

- OpenAI remote MCP / ChatGPT custom connector / Responses API
  - Status: To do.
  - Move: create remote MCP test guide and Python/JS snippets.
  - Asset: connector URL, test prompts, allowlisted tools, OAuth notes.

- Perplexity local and remote MCPs
  - Status: To do.
  - Move: create setup guide.
  - Asset: local command, remote URL, auth notes, test prompts.

### CLI / Package Distribution

- PyPI
  - Status: Active but version needs alignment.
  - Move: publish/tag latest release and ensure project metadata is complete.
  - Asset: trusted publishing, project URLs, classifiers, README rendering.

- `uv tool` / `uvx`
  - Status: Partially documented.
  - Move: make `uvx fulcra-context-mcp@latest` prominent and tested.
  - Asset: install snippet, auth flow, troubleshooting note.

- `pipx`
  - Status: To do.
  - Move: document `pipx run` / `pipx install` path if supported by Python version.
  - Asset: install snippet and Python version caveat.

- Homebrew tap
  - Status: Later P0 only if CLI adoption needs it.
  - Move: create formula after package release flow is stable.
  - Asset: formula, checksums, update process.

### Community / Launch Surfaces

- Hacker News Show HN
  - Status: To do after registry/client docs are clean.
  - Move: launch as technical demo, not wellness marketing.
  - Asset: working demo, repo link, tradeoffs, clear novelty.

- r/mcp and MCP Discord
  - Status: To do after docs are clean.
  - Move: peer build-share asking for feedback.
  - Asset: architecture diagram, install snippet, transparent affiliation.

- Quantified Self Forum / QS Access
  - Status: To do after a case-study artifact exists.
  - Move: personal data access/reflection workflow post.
  - Asset: method writeup, screenshots, privacy posture.

- Open mHealth / Apple Developer Forums HealthKit
  - Status: To do with standards-first framing.
  - Move: contribute technical notes, not launch copy.
  - Asset: schema/data-access mapping, limitations, privacy boundaries.

- Product Hunt
  - Status: Later.
  - Move: launch only after the install funnel and demo are polished.
  - Asset: short video, screenshots, maker comment, three concrete use cases.

## Ready-To-Paste Snippets

Remote Claude Desktop via `mcp-remote`:

```json
{
  "mcpServers": {
    "fulcra_context": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://mcp.fulcradynamics.com/mcp"]
    }
  }
}
```

Local `uvx`:

```json
{
  "mcpServers": {
    "fulcra_context": {
      "command": "uvx",
      "args": ["fulcra-context-mcp@latest"]
    }
  }
}
```

Repo metadata command, if run by an authorized GitHub account:

```bash
gh repo edit fulcradynamics/fulcra-context-mcp \
  --description "MCP server for consented Fulcra health, activity, sleep, location, and annotation context." \
  --homepage "https://fulcradynamics.github.io/developer-docs/mcp-server/" \
  --add-topic mcp \
  --add-topic model-context-protocol \
  --add-topic fulcra \
  --add-topic personal-data \
  --add-topic health-data \
  --add-topic wearables \
  --add-topic quantified-self \
  --add-topic ai-agents \
  --add-topic oauth
```

## Next Work Packet

1. Resolve/tag/publish version `0.1.6`, or align repo version to `0.1.5`.
2. Add GitHub repo description/homepage/topics.
3. Add or update `llms-install.md` in the public repo.
4. Draft `server.json` for official registry / MCPCentral.
5. Draft `smithery.yaml`.
6. Prepare Cline marketplace issue text.
7. Verify Glama and mcp.so live listings and claim/edit paths.

