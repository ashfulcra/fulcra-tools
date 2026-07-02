---
name: fulcra-agent-directives-cli
description: "coord-engine directive verbs + the ack shard shape."
---

# Fulcra Agent Directives — CLI reference

All verbs create/read ordinary Task docs (`team/<team>/task/<slug>.md`) — a directive is a task with an
`assignee`. Run `coord-engine reconcile <team>` (or let the heartbeat) to refresh the inbox aggregate.

```bash
coord-engine tell      <team> <assignee> "<title>" [-p P1] [-s "…"] [-n "…"] [--from <me>]
coord-engine broadcast <team> "<title>" [flags]                  # assignee '*'
coord-engine remind    <team> <assignee> <when> "<title>"        # when: ISO | 5d | 36h | 10m
coord-engine later     <team> "<title>"                          # @backlog
coord-engine handoff   <team> <slug> --to <agent> [--checkpoint CHK-…] [-n "…"]
coord-engine inbox     <team> --agent <X> [--json] [--all]       # --all includes @backlog
coord-engine inbox     <team> --agent <X> --ack <slug>
coord-engine respond   <team> <slug> --outcome "…" [-e "…"] [--agent <X>]
```

Ack shard (`team/<team>/_coord/acks/<slug>/<agent-key>.md`):
```yaml
---
type: Ack
agent: claude-code:host:repo
timestamp: 2026-07-02T12:00:00Z
---
```
The filename key is collision-safe (`slug+sha1[:6]`); reconcile trusts the frontmatter `agent:` only when
it round-trips to the filename. Response shards live at `_coord/responses/<slug>/<stamp>.md`.
