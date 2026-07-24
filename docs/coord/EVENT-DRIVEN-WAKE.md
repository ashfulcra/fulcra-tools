# Event-driven coordination wake

> **STATUS (2026-07-24):** this pre-build survey's relay contract and harness matrix were
> adopted by the wake-router build and are now implemented (`router run`/`router execute`,
> W4–W7). The per-harness adapter matrix here remains the canonical reference; the
> listener-centric operational text below describes the pre-router era and is retained for
> rationale.

The fleet uses one model-free `coord-engine listen` owner per agent identity.
Healthy quiet ticks emit no output and consume no model turn. A new event (or a
newly reported degraded source) invokes one fixed, operator-approved harness
adapter. The awakened session then runs the authoritative `briefing` fold; the
notification text is advisory, never the source of truth.

The host listener is adaptive by default. It polls every active minute while
work is arriving and for a configurable hot tail, then a local due-time gate
reduces source reads to the configured idle cadence. The scheduler may still
invoke the tiny shell tick each active minute, but skipped ticks do not call
`coord-engine`, Fulcra, or a model. Without source-side push, the idle interval
is the maximum added pickup latency for the next item. Transport degradation
uses a separate exponential retry backoff capped at the idle cadence, so an
outage cannot pin the listener hot. A failed harness wake is durably retried;
advancing the bus event cursor cannot silently lose delivery. Wake delivery
uses its own exponential backoff capped at the idle cadence, preventing a
persistently unavailable harness from spawning attempts every hot minute.
Bundled adapters also receive a compact, validated delta containing only event
kind and canonical slug (for example `DIRECTIVE:fix-listener-123`). This lets a
resumed session orient directly without forwarding bus-controlled titles,
outcomes, authors, or bodies into its wake prompt.

## Harness matrix

| Harness | Event-driven path | Fallback |
| --- | --- | --- |
| OpenClaw Gateway | Adaptive model-free listener → authenticated `POST /hooks/wake`; use `skills/fulcra-agent-automation/scripts/wake/openclaw.sh` | Existing Gateway heartbeat |
| Claude Managed Agents | Supported by its session events API: send `user.message` to an idle persisted session | Scheduled deployment |
| Claude Code local/desktop | Host adaptive listener handles cold sessions; a foreground `coord-engine listen` can surface events while live. No exact interactive-session inbound hook is documented, so do not start a competing resume client automatically. | SessionStart briefing plus an idempotency-keyed queued wake file consumed once on open |
| Claude Code web/cloud UI | No exact-session inbound wake is documented. Do not substitute a different Managed Agents session without explicit migration. | Standard router `delivered/` record for alignment to the agent's self-armed platform Routine; for this lane delivered means alignment-recorded, with `no_session_created: true` |
| Codex Desktop | Adaptive listener → `skills/fulcra-agent-automation/scripts/wake/codex.sh` → stable `codex exec resume <thread-id>` (the Codex session id). It resumes the exact persisted thread without bypassing approvals/sandboxing or forwarding raw event text. | Compact app-thread safety automation, configurable with `--interval-minutes` |
| Codex app-server integration | A trusted integration can alternatively `thread/resume` and `turn/start` over local stdio/socket transport. | `codex exec resume` adapter |

## OpenClaw deployment

Enable Gateway hooks with a dedicated token and keep the endpoint behind
loopback, a tailnet, or a trusted reverse proxy. The bundled adapter reads its
bearer token from `~/.config/coord-engine/openclaw-hook-token` (directory mode
`0700`, file mode `0600`). Mechanics, flags, and adapter contracts —
installer command lines, adaptive-cadence flags, wake env-var fields:
[`fulcra-agent-automation` SKILL](../../skills/fulcra-agent-automation/SKILL.md)
— the one home for listener/wake operations; the adapter is
`skills/fulcra-agent-automation/scripts/wake/openclaw.sh`.

The scheduled adapter defaults to the loopback Gateway URL. For a non-default
trusted endpoint, use HTTPS and a small operator-owned wrapper that sets
`OPENCLAW_HOOK_URL`; do not put the bearer token in a cron line or plist.
Plaintext HTTP is accepted only for loopback destinations, and the bearer
header is fed to curl through stdin config so it never appears in process argv.

The adapter sends only a fixed wake instruction, validated team/agent metadata,
and the kind/slug delta. It does not forward an event body as executable text. OpenClaw's
official guidance requires bearer authentication, rejects query-string tokens,
and recommends a dedicated token plus a constrained network boundary:
<https://docs.openclaw.ai/webhook>.

For a sleep-heavy fleet, increase `--idle-minutes` (for example to 60) while
keeping the one-minute active cadence and a 30-minute tail. Use `--fixed` only
for a harness whose own scheduler already provides an adaptive/push contract.
An operator or trusted lifecycle hook may run a forced tick with
`COORD_LISTENER_MARK_ACTIVE=1`; untrusted event payloads must never control it.

## Codex deployment

Use the `<thread-id>` already written to the managed Codex automation and an
absolute repository path. Installing the unattended wake command requires the
same explicit consent as every listener adapter. The installer command line and
the adapter env contract (`COORD_CODEX_THREAD_ID`, `COORD_CODEX_CWD`):
[`fulcra-agent-automation` SKILL §2](../../skills/fulcra-agent-automation/SKILL.md)
— the one home for listener/wake operations.

The adapter invokes the documented `codex exec resume` surface with `--all`
so launchd's working directory does not hide the target session. It deliberately
does not pass `--dangerously-bypass-approvals-and-sandbox`: the resumed thread's
ordinary policy remains the authority boundary. Keep the safety automation until
real event delivery has been observed on that host; after verification, increase
its cadence to a coarse recovery interval such as six hours.

## Relay contract

Fulcra Files currently exposes `recent_changes`, not a change webhook/SSE
subscription (the [platform capability map](../../skills/fulcra-fde/references/capability-mapping.md)
records “No webhooks / push”). A
central relay can therefore consolidate the fleet to **one model-free watcher**
and fan out native wakes, but it cannot honestly eliminate the final source
watcher until Fulcra adds a signed change-delivery surface. Per-session/model
listeners can still disappear; the remaining poll is cheap infrastructure.

That future central relay must preserve these properties:

- authenticated source and destination; dedicated, rotatable capabilities;
- allowlisted team, agent, harness, and target session identifiers;
- monotonic cursor or idempotency key with at-least-once delivery;
- bounded retry, dead-letter/audit trail, and observable last-delivered time;
- no arbitrary command, model, permission-mode, or session-key fields from an
  untrusted event;
- fail-visible degradation and a low-frequency model-free polling backstop.

OpenClaw already supplies a native wake endpoint. Claude Managed Agents exposes
an event-based session API that can resume idle sessions
(<https://platform.claude.com/docs/en/managed-agents/events-and-streaming>).
Codex app-server exposes `thread/resume` and `turn/start` for trusted clients,
but its WebSocket transport is experimental and should remain local/authenticated
(<https://developers.openai.com/codex/app-server/>). These are separate harness
contracts; the relay must not claim equivalence between their session types.
