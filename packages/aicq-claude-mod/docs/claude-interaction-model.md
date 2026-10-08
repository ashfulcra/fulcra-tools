# AICQ in Claude Code: interaction model and interop proposal

This document maps the AICQ product spec (the ChatGPT plugin concept) onto the
Claude Code mod in this package. It also proposes the small wire additions
that would let a Claude agent and a ChatGPT agent agree on the state of a
shared collaboration.

Status: drafted 2026-10-07 against connect-our-agents/1 (agent-skills PR
#227, open at the time) and the legacy mesh and `fulcra.workspaces/1`
formats.

## Spec surface → Claude Code affordance

| AICQ spec | ChatGPT (MCP Extensions) | Claude Code mod |
|---|---|---|
| Global entrypoint: contacts, current work, outcomes | `aicq_open` app view | `/aicq` pane: sidebar (My Agents / Friends Agents, search) + "Your agents at work" cards + Recent outcomes |
| Thread entrypoint beside the chat | `aicq_thread` tab | The same pane docks beside the transcript; the collaboration view opens from any card or contact |
| Share through `@AICQ` | Composer mention | `/aicq share <contact> <file>` and the model tool `aicq_share` (upload → per-recipient file share → versioned artifact) |
| Inline receipt | `_meta.ui.resourceUri` | Toast + status line; the collaboration shows "Waiting for agent" with waiting time |
| Add shared work to the chat | `ui/update-model-context` | **Add to chat**: a visible, titled context block that rides the next prompt, removable from the band above the prompt |
| Continue / use returned changes | `ui/message` | **Continue in chat** and **Use these changes** (a prompt with provenance that tells the agent to reconcile, not overwrite) |
| Work states | — | Waiting for agent · Working · Decision needed · Prepared for approval · Completed · Paused / Unable, derived from the exchange, with explicit updates taking precedence |
| Typical response time | — | Median of substantive replies only; receipts, acks and heartbeats never count; "Not enough history yet" below 3 samples |
| When to check | Scheduled tasks; MCP Events optional | A `$.clock.every` timer inside the session (default 120s) that spends no model turns; the status line shows the last check and any sources it could not read |
| What to do on arrival | Settings | **Notify me** / **Notify me with a draft** / **Respond; check with me on consequential decisions** / **Respond; notify me about results**. Pause stops automatic responses for a collaboration, and each collaboration gets at most 4 automatic turns an hour |
| Invite | Revocable link + onboarding | `aicq_invite` (creates a connect-our-agents channel and the invitation text; nothing is shared yet) → `aicq_connect` (shares the channel, sends the intro; reports **Ready** only once the peer's channel is visible) |

The noticing problem is the reason this exists. A Claude Code mod can poll
outside the model and queue a turn into the idle session (`$.prompt.submit`).
That gives the respond modes a real host mechanism, with no heartbeats in the
conversation, no local scripts and no hourly limit.

## Interop proposal (for connect-our-agents)

connect-our-agents/1 drops fields outside its schema. Until a schema change
lands, this mod puts work state in the body as a leading marker line, which
every reader still shows as text:

```
[state:working]
[purpose:Find a time for Friday's model review]
Bob's agent has the model; checking calendars now.
```

The proposal is three optional fields on the channel schema, so that every
client agrees on the spec's "window on the work":

| Field | Type | Meaning |
|---|---|---|
| `work_state` | enum `working`, `waiting`, `decision_needed`, `prepared_for_approval`, `completed`, `paused`, `unable` | An explicit work update. It is distinct from transport receipts and acks. |
| `purpose` | string ≤ 140 | One line describing what the collaboration is for, sent on its first message. |
| `collaboration_id` | uuid | Groups messages across topics and sessions. Absent, clients group by (peer, topic), which is what this mod does today. |

Rules a reader applies:
- An `ack` is a receipt only. It never counts as a reply and never changes
  the work state.
- The latest explicit `work_state` wins unless a later substantive message
  moved the thread on.
- Response-time estimates use substantive replies only.

## Wire compatibility in this mod

- **Reads:** connect-our-agents/1 (`Event/<uuid>`, top-level fields), legacy
  mesh (`MomentAnnotation/<uuid>`, envelope in `note`), and
  `fulcra.workspaces/1` (same-account workspace).
- **Writes:** each contact's own format. It uses v1 when the peer shares a
  v1 channel with you, and legacy mesh otherwise (mesh peers do not read v1).
- **Delivery:** every send is read back before it is reported as delivered.
  Failed reads show as degraded and are never shown as an empty inbox.
