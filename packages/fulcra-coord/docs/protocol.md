# Coordination Protocol

## When to use fulcra-coord

Agents write coordination updates only at **workflow boundaries**, not for every tool call or intermediate step.

### Write a coordination update when:

- Starting durable work that will outlast the current session
- Claiming or resuming an existing task
- Status changes: proposed → active, active → waiting, active → blocked, active → done
- Pausing or handing off to another agent
- Ending a session with unfinished work
- Marking work done or abandoned

### Do NOT write coordination updates for:

- Short one-message answers
- Internal tool steps within a single continuous task
- Read-only exploration
- Quick clarifications

---

## Before non-trivial work

Before starting meaningful work in a session, an agent should:

1. Read the current coordination view:
   ```bash
   fulcra-coord status --workstream <ws>
   # or
   fulcra-coord status --agent <my-agent-id>
   ```

2. Check for tasks already assigned to or waiting for this agent.

3. If resuming a task, download it and check `next_action` and `blocked_on`.

---

## Task lifecycle

```
proposed → active → waiting → active (resume)
                  → blocked → active (unblocked)
                  → done
                  → abandoned
```

- `proposed` — intended work, not yet started
- `active` — currently being worked on, claim is active
- `waiting` — paused, awaiting external input or handoff; `next_action` required
- `blocked` — cannot proceed; `blocked_on` required
- `done` — completed; `evidence` and `verification_level` required
- `abandoned` — will not be done; `reason` required

Terminal states (`done`, `abandoned`) cannot be transitioned away from without an explicit reopen.

---

## done requires evidence

When marking a task done, the agent must:

1. Provide concrete `evidence` — what was checked, merged, deployed, etc.
2. Set `verification_level` — `agent-verified | human-verified | automated | unverified`
3. Print a prominent user-visible line:
   ```
   >>> Marked TASK-... done: <evidence>
   ```

This is required. Marking a task done without evidence is disallowed.

---

## Pausing and handoff

When ending a session before task completion:

```bash
fulcra-coord pause TASK-... --next "Verify health endpoint at /health before marking done." --agent my-agent
```

The `next_action` field is the handoff note. The next agent or session reads it before continuing.

---

## Concurrency model

Fulcra Files does not support conditional writes (no ETag/if-version-match). This package uses **optimistic concurrency**:

1. Stat the remote task file before editing (`version_id` and `uploaded_at`)
2. Apply the edit locally
3. Stat again immediately before upload
4. If version changed, attempt a structured merge:
   - **Safe**: different tasks, or non-overlapping event appends
   - **Unsafe**: conflicting status changes → refuse with error, suggest reconcile
5. Upload and post-stat to record the new version

Multi-file fan-out (task + views) is not atomic. If view uploads fail after the task write, an operation marker is written flagging `needs_reconcile`. Views are repaired by the reconciler.

```bash
fulcra-coord reconcile
```

---

## Remote layout

Under the configured root (default `/coordination`):

```
/coordination/
  index.json                        ← global compact index
  views/
    active.json                     ← all active/waiting/blocked
    next.json                       ← proposed + waiting
    recently-done.json              ← done/abandoned within 7 days
    search-index.json               ← searchable records
  workstreams/
    {workstream}.json               ← per-workstream active view
  agents/
    {agent}.json                    ← per-agent active view
  tasks/
    TASK-YYYYMMDD-slug-hash.json    ← individual task files
```

Agents write task files directly. Views are materialized by the helper after each write.

---

## Claim semantics

Tasks have an optional `claim` field:

```json
"claim": {
  "claimed_by": "agent-a",
  "claimed_at": "2026-05-31T14:00:00Z",
  "claim_expires_at": "2026-05-31T16:00:00Z"
}
```

Claims are advisory. A stale claim (past `claim_expires_at` on an `active` task) is flagged by the reconciler. Claims are not enforced at the file layer — the reconciler detects and flags them.

---

## Background reconciler

The reconciler (`fulcra-coord reconcile`) should run periodically (every 15–30 minutes) to:

- Repair views after partial write fan-outs
- Flag stale claims
- Detect orphaned active tasks
- Prune `recently-done` beyond retention window

In scheduled environments, add it as a cron job or LaunchAgent.

---

## Retention

| Data | Retention |
|---|---|
| `recently-done` view | 7 days |
| Search index (done tasks) | 30 days |
| Task events (inline) | Last 20 events |
| Full event history | Monthly event files (future) |
