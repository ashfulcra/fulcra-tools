# Fulcra Context MCP Live Distribution Audit

Date: 2026-06-04

## Public Surface Checks

- GitHub repo: reachable.
  - URL: `https://github.com/fulcradynamics/fulcra-context-mcp`
  - Visibility: public.
  - Default branch: `main`.
  - Repo description: empty.
  - Homepage: empty.
  - Topics: none observed.
  - Last pushed: 2026-05-15.

- User docs: reachable.
  - URL: `https://fulcradynamics.github.io/developer-docs/mcp-server/`

- PyPI package: reachable.
  - Package: `fulcra-context-mcp`
  - Latest observed PyPI version: `0.1.5`
  - Summary: `Fulcra Context MCP Server`

- GitHub tags: reachable.
  - Latest observed tag: `v0.1.5`

- Repo `main` package metadata:
  - Observed `pyproject.toml` version: `0.1.6`
  - Action: publish/tag `0.1.6` or align repo metadata before immutable registry submissions.

- Hosted MCP endpoint: auth-gated as expected.
  - URL: `https://mcp.fulcradynamics.com/mcp`
  - Observed response without auth: `401 Unauthorized`

- Glama listing: reachable.
  - Canonical resolved URL: `https://glama.ai/mcp/servers/fulcradynamics/fulcra-context-mcp`
  - Page content includes Fulcra, sleep, OAuth, and health terms.
  - Action: claim/improve metadata if an authorized path exists.

- mcp.so listing: reachable.
  - URL: `https://mcp.so/server/fulcra-context-mcp-server/fulcradynamics`
  - Page content includes Fulcra Context, `fulcra-context-mcp`, and health terms.
  - Page content did not visibly include sleep/OAuth terms in the simple fetch.
  - Action: claim/improve metadata if an authorized path exists.

- Smithery:
  - Guessed listing/search URLs returned permanent redirects to the simple audit client.
  - Prior local submission log says Smithery required GitHub login and Fulcra was not listed at that time.
  - Action: verify in browser or through authenticated Smithery CLI before marking listed.

## Immediate No-Auth Wins

1. Update public repo description, homepage, and topics from an authorized GitHub account.
2. Publish/tag `0.1.6` or align repo `main` with `0.1.5`.
3. Add `llms-install.md` to the public repo.
4. Add registry metadata drafts to a branch or release-prep PR.
5. Use the live audit as the baseline for Glama/mcp.so/Smithery claim work.

## Auth-Gated Items

- Smithery publish/claim.
- mcp.so claim/edit.
- Glama claim/edit if not already controlled by the Fulcra org.
- Product Hunt, Reddit, Discord, and community posts.

