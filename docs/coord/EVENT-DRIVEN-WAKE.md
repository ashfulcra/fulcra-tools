# Event-driven coordination wake — archived design note

> **STATUS (2026-07-27, removal completed 2026-08-03): HISTORICAL except the
> adapter matrix.** The relay contract and harness matrix were adopted by the
> wake-router build (`router run`/`router execute`, W4–W7) and the per-harness
> adapter matrix remains the canonical reference. Everything else — the
> per-agent `listen` owner, adaptive host listeners, poll cadences — describes
> the retired pre-v3 era: agents now read their event queue on every wake
> ([BUS-V3.md](BUS-V3.md)) and run no resident listeners at all; the `listen`
> verb itself was removed from the engine (PR #523), so none of the commands or
> knobs in the historical sections below exist any more. Retained for rationale
> and the matrix only; build nothing new from the historical text.

## The retired design (pre-v3, past tense throughout)

The fleet used one model-free `coord-engine listen` owner per agent identity.
Healthy quiet ticks emitted no output and consumed no model turn. A new event
(or a newly reported degraded source) invoked one fixed, operator-approved
harness adapter. The awakened session then ran the authoritative `briefing`
fold; the notification text was advisory, never the source of truth.

The host listener was adaptive by default. It polled every active minute while
work was arriving and for a configurable hot tail, then a local due-time gate
reduced source reads to the configured idle cadence. The scheduler could still
invoke the tiny shell tick each active minute, but skipped ticks did not call
`coord-engine`, Fulcra, or a model. Without source-side push, the idle interval
was the maximum added pickup latency for the next item. Transport degradation
used a separate exponential retry backoff capped at the idle cadence, so an
outage could not pin the listener hot. A failed harness wake was durably
retried; advancing the bus event cursor could not silently lose delivery. Wake
delivery used its own exponential backoff capped at the idle cadence,
preventing a persistently unavailable harness from spawning attempts every hot
minute. Bundled adapters also received a compact, validated delta containing
only event kind and canonical slug (for example `DIRECTIVE:fix-listener-123`),
letting a resumed session orient directly without forwarding bus-controlled
titles, outcomes, authors, or bodies into its wake prompt.

Several of those properties were carried forward into the wake router and the
queue read rather than dying with the watcher: never letting degradation stop
the wake cadence, at-least-once delivery against a durable cursor, and the
no-raw-event-text-in-wake-prompts rule all survive in the current
architecture ([BUS-V3.md](BUS-V3.md), `_coord/router/`).

## Harness matrix

| Harness | Event-driven path | Fallback |
| --- | --- | --- |
| OpenClaw Gateway | Router adapter → authenticated `POST /hooks/wake` (host-local `$COORD_WAKE_ADAPTER_DIR` adapter; the bundled `wake/openclaw.sh` was removed with the listener stack, cleanup slice 1) | Existing Gateway heartbeat |
| Claude Managed Agents | Supported by its session events API: send `user.message` to an idle persisted session | Scheduled deployment |
| Claude Code local/desktop | Scheduled bus v3 queue read (`coord-engine queue`) on the agent's harness-native wake; the router's queued-wake-file lane covers directed wakes. No exact interactive-session inbound hook is documented, so do not start a competing resume client automatically. | SessionStart briefing plus an idempotency-keyed queued wake file consumed once on open |
| Claude Code web/cloud UI | No exact-session inbound wake is documented. Do not substitute a different Managed Agents session without explicit migration. | Standard router `delivered/` record for alignment to the agent's self-armed platform Routine; for this lane delivered means alignment-recorded, with `no_session_created: true` |
| Codex Desktop | Router adapter (host-local `codex-exec-resume`) → stable `codex exec resume <thread-id>` (the Codex session id). It resumes the exact persisted thread without bypassing approvals/sandboxing or forwarding raw event text. The bundled `wake/codex.sh` was removed with the listener stack. | Compact app-thread safety automation, configurable with `--interval-minutes` |
| Codex app-server integration | A trusted integration can alternatively `thread/resume` and `turn/start` over local stdio/socket transport. | `codex exec resume` adapter |

## OpenClaw (historical deployment; the current adapter is the router's)

The bundled OpenClaw listener adapter was removed with the listener stack; the
live adapter is host-local on the router's executor
(`$COORD_WAKE_ADAPTER_DIR/<adapter>.sh`). The retired deployment enabled
Gateway hooks with a dedicated token, kept the endpoint behind loopback, a
tailnet, or a trusted reverse proxy, and read its bearer token from
`~/.config/coord-engine/openclaw-hook-token` (directory mode `0700`, file mode
`0600`). Its installer command lines, adaptive-cadence flags
(`--idle-minutes`, `--fixed`), forced-tick knob (`COORD_LISTENER_MARK_ACTIVE`),
and wake env-var fields were all removed with the stack — git history has the
mechanics.

The security posture, which still applies to any current host-local adapter
posting to a Gateway: use HTTPS for any non-loopback endpoint; never put a
bearer token in a cron line, plist, or process argv (the retired adapter fed
it to curl through stdin config); send only a fixed wake instruction with
validated team/agent metadata and the kind/slug delta — never an event body as
executable text. OpenClaw's official guidance requires bearer authentication,
rejects query-string tokens, and recommends a dedicated token plus a
constrained network boundary: <https://docs.openclaw.ai/webhook>.

## Codex (historical deployment; the current adapter is the router's)

The retired unattended wake command used the `<thread-id>` already written to
the managed Codex automation and an absolute repository path, and required the
same explicit consent as every listener adapter; its installer and env
contract (`COORD_CODEX_THREAD_ID`, `COORD_CODEX_CWD`) were removed with the
stack. What carries forward in the router's `codex-exec-resume` adapter: it
invokes the documented `codex exec resume` surface with `--all` so launchd's
working directory does not hide the target session, and it deliberately does
not pass `--dangerously-bypass-approvals-and-sandbox` — the resumed thread's
ordinary policy remains the authority boundary. Keep the safety automation
until real event delivery has been observed on that host; after verification,
increase its cadence to a coarse recovery interval such as six hours.

## Relay contract

Fulcra Files currently exposes `recent_changes`, not a change webhook/SSE
subscription (the [platform capability map](../../skills/fulcra-fde/references/capability-mapping.md)
records “No webhooks / push”). A
central relay can therefore consolidate the fleet to **one model-free watcher**
and fan out native wakes, but it cannot honestly eliminate the final source
poll until Fulcra adds a signed change-delivery surface. The remaining poll is
cheap infrastructure — this was the argument for retiring per-session/model
listeners, and it held.

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
