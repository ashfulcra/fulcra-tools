# Event-driven coordination wake

The fleet uses one model-free `coord-engine listen` owner per agent identity.
Healthy quiet ticks emit no output and consume no model turn. A new event (or a
newly reported degraded source) invokes one fixed, operator-approved harness
adapter. The awakened session then runs the authoritative `briefing` fold; the
notification text is advisory, never the source of truth.

## Harness matrix

| Harness | Event-driven path | Fallback |
| --- | --- | --- |
| OpenClaw Gateway | Supported: authenticated `POST /hooks/wake`; use `scripts/wake/openclaw.sh` | Existing Gateway heartbeat |
| Claude Managed Agents | Supported by its session events API: send `user.message` to an idle persisted session | Scheduled deployment |
| Claude Code local/desktop | No inbound hook into an exact interactive UI session is documented. A foreground `coord-engine listen` can surface events while the session is live; do not start a competing resume client automatically. | SessionStart briefing plus host notification |
| Claude Code web/cloud UI | No exact-session inbound wake is documented. Do not substitute a different Managed Agents session without explicit migration. | Platform scheduled routine / host-tier owner |
| Codex Desktop | No documented inbound webhook resumes an exact existing app task. The installer therefore uses a compact 30-minute safety-net automation, configurable with `--interval-minutes`. | SessionStart briefing |
| Codex app-server integration | A trusted integration can `thread/resume` and `turn/start` over local stdio/socket transport, but this is a separate app-server runtime and is not assumed to awaken the Desktop UI task. | Codex Desktop safety net |

## OpenClaw deployment

Enable Gateway hooks with a dedicated token and keep the endpoint behind
loopback, a tailnet, or a trusted reverse proxy. Then install one listener:

```bash
install -d -m 700 "$HOME/.config/coord-engine"
printf '%s' 'dedicated-secret' > "$HOME/.config/coord-engine/openclaw-hook-token"
chmod 600 "$HOME/.config/coord-engine/openclaw-hook-token"
./skills/fulcra-agent-automation/scripts/install-listener.sh \
  <team> <agent> 1 --wake-cmd \
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

## Relay contract

A future central relay may replace per-host polling, but it must preserve these
properties:

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
