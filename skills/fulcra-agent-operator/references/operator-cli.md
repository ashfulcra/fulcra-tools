---
name: fulcra-agent-operator-cli
description: "The ask/answer verbs + the orchestrator pull-diff-surface-relay loop."
---

# Fulcra Agent Operator — CLI reference

```bash
# raise an ask (agent):
coord-engine task block <team> <slug> --on-user "<self-contained ask incl options + default>"

# pull asks (orchestrator heartbeat) — oldest first, each row carries age_hours:
coord-engine asks <team> [--human <handle>] [--json]

# deliver the answer (operator / orchestrator relaying the operator):
coord-engine answer <team> <slug> --with "<answer>"
#   -> one write: OPERATOR ANSWER in next_action + body, blocked->active,
#      assignee=owner (their listener fires), needs:human stripped.
```

Orchestrator loop skeleton:
```
every heartbeat:
  asks = coord-engine asks <team> --json
  new  = asks - previously_surfaced        # by slug
  aged = [a for a in asks if a.age_hours > NAG_THRESHOLD and not recently_nagged(a)]
  surface(new + aged) to the operator      # notification / chat / digest
  for each operator reply: coord-engine answer <team> <slug> --with "<reply>"
```
The human handle defaults to `$FULCRA_COORD_HUMAN` (else `human`). `answer` exits 1 on a non-ask or an
ownerless ask — a reply can never be silently dropped.
