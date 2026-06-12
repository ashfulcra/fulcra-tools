# Schema Reference

## Task record

Schema version: `fulcra.coordination.task.v1`

```json
{
  "schema": "fulcra.coordination.task.v1",
  "id": "TASK-20260531-deploy-search-abc12345",
  "title": "Deploy search service to staging",
  "status": "active",
  "origin": "human",
  "priority": "P1",
  "workstream": "devops",
  "surface": "local:claude-code",
  "source": {
    "channel": "devops",
    "message_id": null,
    "conversation_label": "#devops"
  },
  "owner_agent": "claude-code",
  "agent_instance": "claude-code:local",
  "collaborators": [],
  "linked_workstreams": [],
  "tags": [
    "agent:claude-code",
    "kind:ops",
    "priority:P1",
    "status:active",
    "workstream:devops"
  ],
  "current_summary": "Terraform apply succeeded. Verifying health endpoint.",
  "next_action": "Run smoke tests against staging.",
  "blocked_on": null,
  "claim": {
    "claimed_by": "claude-code",
    "claimed_at": "2026-05-31T14:00:00Z",
    "claim_expires_at": null
  },
  "done": {
    "done_at": null,
    "done_by": null,
    "evidence": null,
    "verification_level": null,
    "confidence": null
  },
  "checklist": [],
  "links": {
    "local_ticket": null,
    "files": [],
    "prs": [],
    "remote_files": []
  },
  "events": [
    {
      "at": "2026-05-31T14:00:00Z",
      "type": "created",
      "by": "claude-code",
      "summary": "Task created.",
      "evidence": null
    },
    {
      "at": "2026-05-31T14:02:00Z",
      "type": "active",
      "by": "claude-code",
      "summary": "Status changed to active.",
      "evidence": null
    }
  ],
  "created_at": "2026-05-31T14:00:00Z",
  "updated_at": "2026-05-31T14:02:00Z",
  "last_touched_by": "claude-code",
  "last_touched_in": "local:claude-code"
}
```

---

## Field reference

| Field | Type | Description |
|---|---|---|
| `schema` | string | Always `fulcra.coordination.task.v1` |
| `id` | string | `TASK-YYYYMMDD-slug-hash8` format |
| `title` | string | Short durable objective |
| `status` | enum | `proposed\|active\|waiting\|blocked\|done\|abandoned` |
| `origin` | string | `human\|agent` |
| `priority` | enum | `P0\|P1\|P2\|P3` |
| `workstream` | string | Team/topic grouping (e.g. `devops`) |
| `surface` | string | Origin surface (e.g. `local:claude-code`, `discord:#devops`) |
| `owner_agent` | string | Primary responsible agent |
| `agent_instance` | string | Specific instance (e.g. `claude-code:repo:my-repo`) |
| `collaborators` | array | Other agents involved |
| `tags` | array | Structured tags: `kind:ops`, `status:active`, etc. |
| `current_summary` | string | One-to-two sentence current state |
| `next_action` | string | What should happen next (required when paused) |
| `blocked_on` | string\|null | Blocking reason (required when blocked) |
| `claim` | object | Optional advisory claim with expiry |
| `done` | object | Done metadata (required when status=done) |
| `checklist` | array | Optional subtask list |
| `links` | object | Related tickets, files, PRs |
| `events` | array | Bounded event log (last 20 events) |
| `created_at` | ISO datetime | Creation timestamp |
| `updated_at` | ISO datetime | Last modification |
| `last_touched_by` | string | Agent that last modified |
| `last_touched_in` | string | Surface of last modification |

---

## Status values

| Status | Meaning | Required fields |
|---|---|---|
| `proposed` | Intended work, not started | — |
| `active` | Currently in progress | — |
| `waiting` | Paused, awaiting input or handoff | `next_action` |
| `blocked` | Cannot proceed | `blocked_on` |
| `done` | Completed | `done.evidence`, `done.verification_level` |
| `abandoned` | Will not be done | reason in events |

---

## Status transitions

```
proposed → active, waiting, abandoned
active   → waiting, blocked, done, abandoned
waiting  → active, blocked, abandoned
blocked  → active, waiting, abandoned
done     → (terminal — requires explicit reopen)
abandoned→ (terminal — requires explicit reopen)
```

---

## Task ID format

```
TASK-{YYYYMMDD}-{slug}-{hash8}
```

- `YYYYMMDD` — creation date (UTC)
- `slug` — title slugified (lowercase alphanumeric + hyphens, max 24 chars)
- `hash8` — 8-character random hex suffix for uniqueness

Example: `TASK-20260531-deploy-search-abc12345`

---

## Verification levels (done)

| Level | Meaning |
|---|---|
| `agent-verified` | Agent checked the outcome |
| `human-verified` | Human confirmed |
| `automated` | CI/test suite confirmed |
| `unverified` | Done state asserted without verification |

---

## Views

### index.json
```json
{
  "schema": "fulcra.coordination.index.v1",
  "updated_at": "...",
  "counts": {
    "by_status": {"active": 3, "waiting": 1},
    "by_workstream": {"devops": 2},
    "by_agent": {"claude-code": 2},
    "inbox": {"codex-h-r": 1}
  },
  "active": [/* compact task summaries */],
  "recent_done": [/* compact summaries, last 7 days */]
}
```

### views/active.json, next.json, recently-done.json
```json
{
  "schema": "fulcra.coordination.view.v1",
  "view": "active",
  "updated_at": "...",
  "tasks": [/* compact task summaries */]
}
```

### views/search-index.json
```json
{
  "schema": "fulcra.coordination.search_index.v1",
  "updated_at": "...",
  "records": [
    {
      "id": "TASK-...",
      "title": "...",
      "status": "active",
      "priority": "P2",
      "workstream": "devops",
      "owner_agent": "claude-code",
      "tags": ["kind:ops", "status:active"],
      "summary": "...",
      "task_file": "/coordination/tasks/TASK-....json",
      "updated_at": "..."
    }
  ]
}
```

### workstreams/{ws}.json
```json
{
  "schema": "fulcra.coordination.workstream_view.v1",
  "workstream": "devops",
  "updated_at": "...",
  "active": [/* compact summaries */],
  "recent_done": [/* compact summaries */]
}
```

### Retired views (2026-06-11): agents/{agent}.json, views/inbox/{slug}.json

The per-agent views (`fulcra.coordination.agent_view.v1`, one
`agents/{agent}.json` per owner/toucher identity) and the per-assignee inbox
views (`fulcra.coordination.inbox_view.v1`, one `views/inbox/{slug}.json` per
open-directive assignee) are **no longer materialized**. They were rebuilt and
uploaded on every write/reconcile (~35+ files per pass at current fleet size)
and read by nothing: the `agents`/`resume` surfaces fold the
`views/summaries.json` aggregate client-side, and `inbox` recomputes from the
task summaries (the materialized inbox file went stale the moment an inbox
emptied, so it had already been demoted from the read path). The per-assignee
counts survive as the `counts.inbox` fold inside `index.json`.

Files already on a bus under `agents/` or `views/inbox/` are inert leftovers
from older writers: they are deliberately **not deleted** (bus-state cleanup is
deferred pending a Fulcra service review) and are simply never refreshed again.
