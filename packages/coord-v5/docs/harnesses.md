# In-harness listeners

One listener serves an enrolled environment/harness; role/job reasoning sessions remain disposable. Store the native listener registration with the private workspace descriptor. A fresh listener reconstructs unfinished obligations from annotations and recovers ambiguous delivery from its receipts before resending.

## Codex desktop

Use native thread heartbeats/scheduled tasks and native thread messaging from an active listener turn. Do not configure an external command as though it could call an app-only tool. Local scheduled runs require the app and machine to be available; verify permissions in the actual scheduled turn. See [Codex scheduled tasks](https://learn.chatgpt.com/docs/automations).

The enrollment plan's `listener_scope` uses the dispatcher's `codex` harness key. Keep one private listener SQLite journal for that scope and configure exact job-to-thread routes with `coord-v5 listener configure`. `active` routes target an existing thread; `dormant` routes request a successor; `retired` routes notify their configured coordinator. Successor creation is a harness action, not an implied role grant.

Use this bounded native listener job recipe, substituting only local approved configuration paths and identity:

1. Read the owned annotation source with `transport read`; obtain both fresh events and durable obligations with `observation`. Preserve partial/unavailable coverage.
2. Pass that observation to `listener prepare` using the registered scope, journal and holder. Inspect its known attention items and prepared actions.
3. Execute only the prepared supported native message action for a locally configured route. Save `accepted`, `error`, or `unknown` via `listener settle` using the exact wake, attempt and target. A native send acceptance is not worker acknowledgment or completion. A receiver acknowledges with `listener ack` after matching its target and wake IDs.
4. Report `needs_successor`, `needs_coordinator`, conflicts, failed reads and uncertain delivery to the configured coordinator. For a successor, retrieve role/job checkpoint plus the fresh event tail and verify artifact access before transferring responsibility. Reconcile an uncertain send rather than creating a new identity to retry it.
5. Use the returned interval for this native heartbeat only. Preserve a faster operator interval. Update through the native scheduling tool while preserving the full prompt, target and notification preferences. Failed or incomplete reads retain short error retry; they are not quiet time. Never cancel the listener merely because a workstream finished.
6. Stay quiet for unchanged non-actionable state; notify on meaningful work, failure, completion or user decision. Reasoning sessions publish their own contact/progress/checkpoints; the listener reports only its own successful reads.

The journal's recurrence is a suggestion until the native schedule update succeeds. Record both values. Live idle-wake proof does not establish restart, host-outage, permission-prompt or upgrade behavior; keep those capability fields unknown until tested. Do not restart the user's app or host as part of ordinary enrollment.

## Other environments

| Harness | Self-assessment path |
|---|---|
| Claude Code live session | Inspect native loop/scheduled-task and channel tools in that session. Verify workspace access, busy delivery, expiration and resume behavior. A session loop shares the session's lifetime. |
| Claude desktop | Inspect desktop-native scheduling and messaging separately from CLI capabilities. Verify the receiving session and its tool permissions. |
| Claude cloud | Inspect cloud routines and the new-session recovery path, including its actual minimum interval. Rehydrate role/job context rather than assuming a local session is resumed. |
| Codex cloud / ChatGPT web | Probe scheduling and connected record/file tools in the scheduled environment itself; desktop tool availability does not prove cloud access. |
| Regular Claude web | Start interactive with verified tools. Upgrade only after an idle invocation and cross-session delivery are demonstrated. |
| OpenClaw / Hermes | Inspect the installed gateway's native scheduling and notification tools; test lifecycle and credential access in that environment. Keep gateway health separate from worker progress. |

Official starting points: [Claude scheduled tasks](https://code.claude.com/docs/en/scheduled-tasks), [Claude channels](https://code.claude.com/docs/en/channels), [OpenClaw heartbeat](https://docs.openclaw.ai/gateway/heartbeat), [Hermes cron](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron). These identify candidate native surfaces; the local capability report and live tests decide what is actually usable.

## Acceptance record

Capture harness/version, listener registration, scheduled time, actual execution time, source coverage, native delivery receipt, receiver acknowledgment and observed model usage when available. Test idle, busy, restart, outage, credentials, permission prompts and version drift separately. Unknown usage remains unknown. A 24-hour cost/latency experiment is a separate measurement, not an extrapolation from one successful wake.
