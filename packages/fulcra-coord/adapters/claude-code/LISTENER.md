# Durable inbox listener — Claude Code

The coordination suite surfaces directed work (`tell` / `assign` -> someone's
inbox) the instant a session opens, via the SessionStart hook's **📥 Directives
for you** section. But that only fires when a session opens. To notice a
directive that arrives **while the agent is idle between sessions**, you need a
durable, periodic listener.

It is **notify-only**: it polls the inbox and, if there are open directives,
writes a surface file the next SessionStart injects and emits a desktop
notification. It never runs the directive — a human / the next session decides.

The single call the listener runs each tick is:

```
fulcra-coord notify-inbox --agent <me>
```

where `<me>` is your stable agent id (`$FULCRA_COORD_AGENT`, or the derived
`claude-code:<host>:<repo>`).

There are two ways to schedule it.

## 1. Scheduled remote agent (preferred)

The native Claude Code mechanism is a **scheduled remote agent** — a recurring
headless Claude run created through the harness scheduler (the `/schedule`
routine). It survives across sessions, needs no app window, and runs even when
you have no interactive session open.

Create a routine whose prompt is simply:

```
Run: fulcra-coord notify-inbox --agent <me>
```

on a cron cadence (e.g. every 10 minutes). The routine's only job is that one
call; `notify-inbox` does the poll + surface + notify and exits. Keep it pinned
to a cheap model — there's no reasoning to do, just the CLI call.

This is created via the harness scheduler, not by the CLI. `install-listener`
(below) is the harness-free fallback.

## 2. launchd / cron fallback (`install-listener`)

When the harness scheduler isn't available (a plain shell, a CI box, a server),
install a system-level schedule that runs the same command:

```
fulcra-coord install-listener --agent <me> --interval-min 10
```

- **macOS** -> a launchd LaunchAgent (`com.fulcra.coord.listener.<agent-slug>.plist`
  under `~/Library/LaunchAgents`), `StartInterval` every N minutes.
- **everything else** -> a managed crontab line tagged with a per-agent marker so
  uninstall is surgical.

The job is **per-agent**: its launchd label / plist basename / cron marker embed
the agent's slug (the same slug as the inbox view files). So multiple agents
co-located on one machine each get their own coexisting listener and a second
agent's install never overwrites the first's. (A legacy un-slugged
`com.fulcra.coord.listener.plist` from before 0.5.3 is superseded on install only
when it watched this agent.)

The scheduled command is resolved through `resolve_cli_argv()` so it works under
`uv tool` / source installs, not just `pip`-on-PATH. Contract mirrors
`install-heartbeat`: idempotent, `--dry-run` writes nothing, `--uninstall` is
surgical.

Remove it with:

```
fulcra-coord install-listener --agent <me> --uninstall
```

## OpenClaw

OpenClaw uses its **heartbeat** instead of a separate schedule: the shipped
`HEARTBEAT.md` runs `fulcra-coord notify-inbox` each beat (see
`fulcra_coord/openclaw.py`). Same notify-only behavior, folded into the existing
periodic heartbeat the gateway already runs.

## How it notifies

Delivery is layered and best-effort, so a directive is never silently dropped:

- **Tier 0 — inbox surface file** (guaranteed, zero-config, no network): the
  durable record the next SessionStart injects. Always reaches the operator on
  next session start, on every OS.
- **Tier 1 — real-time push** (opt-in): if `FULCRA_COORD_NOTIFY_WEBHOOK` is set,
  each tick POSTs the notification to that URL via stdlib `urllib`. This is what
  reaches your phone regardless of which host fired. The payload shape is
  auto-detected from the URL host — `discord` / `slack`, else **ntfy** plain-body
  (the generic default) — and `FULCRA_COORD_NOTIFY_FORMAT` (`ntfy|slack|discord|json`)
  overrides it. Point it at any commodity push service (a free or self-hosted ntfy
  topic, Pushover-style, Slack, Discord); it's not tied to specific infra.
  `FULCRA_COORD_NOTIFY_TIMEOUT` (default 5s) caps the POST so a hung endpoint can't
  stall the tick.
- **Tier 2 — native desktop** (best-effort local bonus): macOS `osascript`, Linux
  `notify-send` when present, else a stderr line. Never relied upon.

The push fires once per **new** directive (a seen-set dedups it), so it doesn't
re-alert on every poll for an inbox item you've already been told about.

## How the surface file is consumed

`notify-inbox` writes `inbox-pending-<agent-slug>.json` under the fulcra-coord
cache root (root-scoped). The SessionStart hook already computes directives live
via `fulcra-coord inbox --format json`; the surface file is the durable record a
listener leaves between ticks so a notification and the next session boot agree
on what's pending.
