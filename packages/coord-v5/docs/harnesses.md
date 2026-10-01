# In-harness listeners

One reporting listener serves an enrolled environment/harness; addressed role/job workers remain separate and reasoning sessions disposable. Its job is to receive bus traffic, preserve coverage and route attention through locally configured native messaging, not execute the workers' tasks. Prefer bounded subagents for worker reasoning or implementation. A deliberate self-ticking executor may continue its own assigned task, but only as a separate opt-in mode. Store the native listener registration with the private workspace descriptor. A fresh listener reconstructs unfinished obligations from annotations and recovers ambiguous delivery from its receipts before resending.

Verified native event ingress is the preferred enrollment path when dispatch, recovery and scoped read/publish are also current and unattended operation was requested. A schedule can backstop that path; verified ticks or session loops are fallbacks when event ingress is absent, unsupported or unknown. The planner's `native-event` mode does not implement an event adapter or prove that a product supports event ingress. Verify actual reception and idle worker delivery in the enrolled environment before advertising them.

Generate `instruction_mode:"listener"` through the [instructions command](onboarding.md#capability-assessment)
for this reporting session; use `executor` only for separately authorized owned-task execution.
Keep these role blocks in listener/executor local/session instructions, never shared repository-wide
worker instructions. The default `worker` block is unchanged. Listener reports cover its own reads,
routes, conflicts and reconciliation, never worker claims/progress/completion or invented receiver
acknowledgments. Executor recovery needs explicit ownership and context; a wake cannot authorize
adoption. Mode selection installs nothing: fallback ticks/loops and direct executor self-ticking
each require separate authorization. Generated prose is not runtime enforcement or consumer-agent proof.

## Codex desktop

Use locally verified native thread messaging from an active listener turn. Assess event ingress separately; use native thread heartbeats/scheduled tasks as a verified fallback or backstop, not a mandatory event-path component. Do not configure an external command as though it could call an app-only tool. Local scheduled runs require the app and machine to be available; verify permissions in the actual scheduled turn. See [Codex scheduled tasks](https://learn.chatgpt.com/docs/automations).

Verify the exact native cross-session route locally, including human authorization and target restrictions. An observed Codex ancestor-target restriction is evidence about that route, not a universal prohibition on messaging peer sessions. Keep route identifiers and target mappings private; availability in an active turn does not prove an idle receiver will wake.

The enrollment plan's `listener_scope` uses the dispatcher's `codex` harness key. Keep one private listener SQLite journal for that scope and configure exact job-to-thread routes with `coord-v5 listener configure`. `active` routes target an existing thread; `dormant` routes request a successor; `retired` routes notify their configured coordinator. Successor creation is a harness action, not an implied role grant.

Use this bounded native listener job recipe, substituting only local approved configuration paths and identity:

1. On actual event reception or a fallback tick, read the owned annotation source with `transport read`; obtain both fresh events and durable obligations with `observation`. Preserve partial/unavailable coverage and the durable cursor/dedup state; do not treat a failed read as empty or advance past an unprocessed gap.
2. Pass that observation to `listener prepare` using the registered scope, journal and holder. Inspect its known attention items and prepared actions.
3. Execute only the prepared supported native message action for a locally configured route. Preserve the journal's delivery intent before invoking native messaging. Save `accepted`, `error`, or `unknown` via `listener settle` using the exact wake, attempt and target. Keep that native receipt separate from receiver acknowledgment and task progress/completion. A receiver acknowledges with `listener ack` after matching its target and wake IDs.
4. Report `needs_successor`, `needs_coordinator`, conflicts, failed reads and uncertain delivery to the configured coordinator. For a successor, retrieve role/job checkpoint plus the fresh event tail and verify artifact access before transferring responsibility. Reconcile an uncertain send rather than creating a new identity to retry it.
5. For an event path, keep the verified event registration active. If a native heartbeat is the fallback or backstop, use the returned interval for that heartbeat only. Preserve a faster operator interval. Update through the native scheduling tool while preserving the full prompt, target and notification preferences. Failed or incomplete reads are not quiet time; scheduled retries retain the short error interval. Never cancel the environment listener merely because a workstream finished.
6. Stay quiet for unchanged non-actionable state; notify on meaningful work, failure, completion or user decision. Reasoning sessions publish their own contact/progress/checkpoints; the listener reports only its own successful reads.

The journal's recurrence is a suggestion until the native schedule update succeeds. Record both values. Live idle-wake proof does not establish restart, host-outage, permission-prompt or upgrade behavior; keep those capability fields unknown until tested. Do not restart the user's app or host as part of ordinary enrollment.

## Other environments

### Claude Code: explicit local delivery

The package now supports opt-in `coord-v5 listener dispatch-claude`, using the
canonical enrollment harness `claude-code`. Configure a private
`target:{kind:"claude-code",sessionId:UUID,cwd:ABS}` route and pass the prepared
action's `.arguments` on stdin with trusted `--executable ABS`. Existing Codex
thread routes and native heartbeat steps are unchanged. See the [dispatcher
contract](../README.md#explicit-claude-cli-delivery) for fixed flags requesting
the full model ID `claude-haiku-4-5-20251001` and a $0.25 provider budget,
tool-free execution, 60-second/64-KiB bounds, exact result correlation and safe
failure codes. Exact argv is enforced; served
model and provider billing are not. Inspect actual assistant-model telemetry:
the served model may differ from the requested model ID, and auxiliary `modelUsage`
does not identify the conversational model. Sub-budget success does not prove
a hard spend ceiling.

Preparing is not sending. An accepted native result is not receiver acknowledgment,
source completeness, a schedule, or lifecycle acceptance. The local journal claims
before launch; repeated and ambiguous attempts require reconciliation, including
crash-left claims. Do not resume the same session elsewhere concurrently: the
fence is local to one journal/scope, not global busy detection. The operator must
verify the CLI/version, existing session, trusted CWD and credential availability
without copying them into public artifacts. No sign-in, new-session creation or
force takeover is performed. Fixture and packed-install success are local tests,
not authenticated native acceptance or restart/outage evidence.

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

Capture harness/version, listener registration, actual event reception/execution time, scheduled time when applicable, source coverage, native delivery intent/receipt, receiver acknowledgment and observed model usage when available. Test idle, busy, restart, outage, credentials, permission prompts and version drift separately. Unknown usage remains unknown. A 24-hour cost/latency experiment is a separate measurement, not an extrapolation from one successful wake.
