# Event-driven coordination wake

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
is the maximum added pickup latency for the next item.

## Harness matrix

| Harness | Event-driven path | Fallback |
| --- | --- | --- |
| OpenClaw Gateway | Adaptive model-free listener → authenticated `POST /hooks/wake`; use `scripts/wake/openclaw.sh` | Existing Gateway heartbeat |
| Claude Managed Agents | Supported by its session events API: send `user.message` to an idle persisted session | Scheduled deployment |
| Claude Code local/desktop | Host adaptive listener handles cold sessions; a foreground `coord-engine listen` can surface events while live. No exact interactive-session inbound hook is documented, so do not start a competing resume client automatically. | SessionStart briefing plus host notification |
| Claude Code web/cloud UI | No exact-session inbound wake is documented. Do not substitute a different Managed Agents session without explicit migration. | Platform scheduled routine / host-tier owner |
| Codex Desktop | No documented inbound webhook resumes an exact existing app task. The host listener can adapt notifications, but model-backed app automations cannot safely self-reschedule; the installer therefore retains a compact 30-minute safety net configurable with `--interval-minutes`. | SessionStart briefing |
| Codex app-server integration | A trusted integration can `thread/resume` and `turn/start` over local stdio/socket transport, but this is a separate app-server runtime and is not assumed to awaken the Desktop UI task. | Codex Desktop safety net |

## OpenClaw deployment

Enable Gateway hooks with a dedicated token and keep the endpoint behind
loopback, a tailnet, or a trusted reverse proxy. Then install one listener:

```bash
install -d -m 700 "$HOME/.config/coord-engine"
printf '%s' 'dedicated-secret' > "$HOME/.config/coord-engine/openclaw-hook-token"
chmod 600 "$HOME/.config/coord-engine/openclaw-hook-token"
./skills/fulcra-agent-automation/scripts/install-listener.sh \
  <team> <agent> 1 --tail-minutes 30 --idle-minutes 30 --wake-cmd \
  "$PWD/skills/fulcra-agent-automation/scripts/wake/openclaw.sh"
```

The scheduled adapter defaults to the loopback Gateway URL. For a non-default
trusted endpoint, use a small operator-owned wrapper that sets
`OPENCLAW_HOOK_URL`; do not put the bearer token in a cron line or plist.

The adapter sends only a fixed wake instruction plus validated team/agent
metadata. It does not forward an event body as executable text. OpenClaw's
official guidance requires bearer authentication, rejects query-string tokens,
and recommends a dedicated token plus a constrained network boundary:
<https://docs.openclaw.ai/webhook>.

For a sleep-heavy fleet, increase `--idle-minutes` (for example to 60) while
keeping the one-minute active cadence and a 30-minute tail. Use `--fixed` only
for a harness whose own scheduler already provides an adaptive/push contract.
An operator or trusted lifecycle hook may run a forced tick with
`COORD_LISTENER_MARK_ACTIVE=1`; untrusted event payloads must never control it.

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
