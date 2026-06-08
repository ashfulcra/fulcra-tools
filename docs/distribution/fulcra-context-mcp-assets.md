# Fulcra Context MCP Distribution Assets

Date: 2026-06-04

These are draft assets for `fulcra-context-mcp`. They should be copied into the public `fulcradynamics/fulcra-context-mcp` repo or submission forms after the package version mismatch is resolved.

## `llms-install.md` Draft

````markdown
# Install Fulcra Context MCP

Fulcra Context MCP lets MCP-capable AI clients access user-consented Fulcra context such as metrics, samples, sleep cycles, workouts, annotations, location context, and profile information.

## Remote MCP Endpoint

Use the hosted endpoint when your client supports remote MCP through `mcp-remote` or a compatible connector:

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

The hosted endpoint uses Fulcra OAuth. Do not paste Fulcra tokens into chat. Follow the browser/device authorization flow shown by your MCP client.

## Local MCP Server

Use the local package when you prefer stdio transport:

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

## Useful First Prompts

- "What Fulcra metrics are available?"
- "Show my sleep cycles for the last 7 days."
- "Summarize my workouts from this week."
- "Get my annotation catalog."
- "Fetch heart-rate samples from yesterday afternoon."

## Privacy Boundary

Fulcra Context MCP authorizes through Fulcra OAuth. MCP clients receive scoped MCP access and should never ask users to paste Fulcra refresh tokens or API credentials into chat.

## Troubleshooting

- Confirm your MCP client can reach `https://mcp.fulcradynamics.com/mcp`.
- For local use, confirm `uvx fulcra-context-mcp@latest` starts successfully.
- Use MCP Inspector for protocol-level debugging.
- If auth fails, restart the MCP client and repeat the Fulcra OAuth flow.
````

## Official Registry / MCPCentral `server.json` Draft

Exact schema may need adjustment to the target registry's current publisher format.

```json
{
  "name": "io.fulcradynamics.fulcra-context-mcp",
  "displayName": "Fulcra Context MCP",
  "description": "MCP server for consented Fulcra health, activity, sleep, location, workout, annotation, and metric context.",
  "repository": {
    "url": "https://github.com/fulcradynamics/fulcra-context-mcp",
    "source": "github"
  },
  "homepage": "https://fulcradynamics.github.io/developer-docs/mcp-server/",
  "license": "Apache-2.0",
  "categories": ["health", "productivity", "data"],
  "tags": [
    "mcp",
    "model-context-protocol",
    "fulcra",
    "personal-data",
    "health-data",
    "wearables",
    "quantified-self",
    "ai-agents",
    "oauth"
  ],
  "remotes": [
    {
      "type": "streamable-http",
      "url": "https://mcp.fulcradynamics.com/mcp",
      "auth": "oauth"
    }
  ],
  "packages": [
    {
      "registry": "pypi",
      "name": "fulcra-context-mcp",
      "runtime": "python",
      "command": "uvx",
      "args": ["fulcra-context-mcp@latest"]
    }
  ]
}
```

## `smithery.yaml` Draft

```yaml
name: fulcra-context-mcp
displayName: Fulcra Context MCP
description: MCP server for consented Fulcra health, sleep, activity, annotation, location, and metric context.
homepage: https://fulcradynamics.github.io/developer-docs/mcp-server/
repository: https://github.com/fulcradynamics/fulcra-context-mcp
license: Apache-2.0
tags:
  - health-data
  - personal-data
  - wearables
  - quantified-self
  - ai-agents
  - oauth
startCommand:
  type: stdio
  configSchema:
    type: object
    properties: {}
    additionalProperties: false
  commandFunction: |-
    (config) => ({
      command: "uvx",
      args: ["fulcra-context-mcp@latest"],
      env: {}
    })
```

## Cline Marketplace Issue Draft

Title:

```text
Add Fulcra Context MCP
```

Body:

````markdown
## Server

Fulcra Context MCP

## Repository

https://github.com/fulcradynamics/fulcra-context-mcp

## Documentation

https://fulcradynamics.github.io/developer-docs/mcp-server/

## What it does

Fulcra Context MCP lets MCP clients access user-consented Fulcra context: metric catalogs, time series, samples, sleep cycles, workouts, annotations, location context, and user profile information.

## Why it belongs in Cline

Cline users are already asking coding agents to reason across projects, calendars, health, activity, and personal state. Fulcra Context MCP gives those agents a consented personal-context layer while keeping Fulcra OAuth outside the chat and avoiding pasted tokens.

## Install

Remote endpoint through `mcp-remote`:

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

Local stdio server:

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

## Auth / privacy

Fulcra Context MCP uses Fulcra OAuth. Users should not paste Fulcra tokens or refresh credentials into chat. The MCP server maintains its own client authorization boundary.

## License

Apache-2.0
````

## Short Community Post Draft

```markdown
I am working on Fulcra Context MCP, a remote/local MCP server for bringing user-consented personal context into agent clients.

It exposes Fulcra metrics, samples, sleep cycles, workouts, annotations, location context, and profile data. The main design point is keeping OAuth out of chat: users authorize through Fulcra, and MCP clients get MCP-scoped access rather than raw Fulcra credentials.

Hosted endpoint: https://mcp.fulcradynamics.com/mcp
Repo: https://github.com/fulcradynamics/fulcra-context-mcp
Docs: https://fulcradynamics.github.io/developer-docs/mcp-server/

I would especially like feedback on the auth boundary, install path, and whether the tool names feel natural for agent clients.
```
