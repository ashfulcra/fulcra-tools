---
name: fulcra-agent-presence-cli
description: "coord-engine presence/agents/roles-lease commands + the Presence shard shape."
---

# Fulcra Agent Presence — CLI reference

```bash
coord-engine presence beat <team> [--agent X] [-w ws]... [-s TEXT]  # write/refresh presence/<agent>.md
coord-engine presence show <team> [--json]                          # roster: live/idle/stale
coord-engine agents <team> [--json]                                 # liveness + open work per agent
coord-engine roles claim   <team> <role> [--agent X]                # lease shard write/refresh
coord-engine roles release <team> <role> [--agent X]                # lease shard delete
```

Shard shape (`team/<team>/presence/<agent-slug>.md`):
```yaml
---
type: Presence
agent: claude-code:host:repo
workstreams: [web, coord2]
summary: shipping the A3 layer
timestamp: 2026-07-02T12:00:00Z
---
```
Liveness bands: live ≤1h, idle ≤24h, stale >24h (undatable = stale). The broadcast roster (used by
directives) = everyone not stale.
