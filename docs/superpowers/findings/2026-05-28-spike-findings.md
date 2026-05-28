# Spike Findings — Hermes-on-Daytona (2026-05-28)

Live de-risking spike (Task 1). All run against the real Daytona account +
OpenRouter key. Spike sandbox and `fhd-spike` snapshot were torn down after.

## UPDATE (decision): guest surface = locked-down dashboard (validated)

After this spike, the user chose to keep the prettier **Hermes dashboard** and
lock it down with a reverse proxy rather than use ttyd. I validated that it
works (see "Dashboard lockdown" section at the bottom). The ttyd notes below
remain as the simpler fallback, but the **chosen and validated approach is the
Caddy-fronted dashboard**.

## TL;DR — it works, with two design changes from the approved spec

The full guest flow was validated end-to-end in a browser: a guest opens a
signed preview URL → Hermes chat → agent greets, asks what to track, runs the
Fulcra device-code `auth login`, and surfaces the auth URL + code in chat for
the guest to complete in their own browser. **Two changes from the spec are
required (details below):**

1. **Guest surface = a chat-only web terminal (`ttyd` wrapping `hermes`), NOT
   the Hermes web dashboard.** The dashboard is a full admin console that
   exposes and edits our OpenRouter key (KEYS tab) — unsafe to hand to guests.
2. **The onboarding skill is delivered by copying files into
   `~/.hermes/skills/`, NOT via `hermes skills install`.** Hermes's built-in
   security scanner flags the skill as DANGEROUS (false positives) and blocks
   the hub installer; `--force` does not override.

## Confirmed: Daytona SDK (pinned)

- Package `daytona`; imports confirmed: `Daytona, DaytonaConfig, Image,
  CreateSnapshotParams, CreateSandboxFromSnapshotParams, SessionExecuteRequest`.
- Client: `Daytona(DaytonaConfig(api_key=..., api_url="https://app.daytona.io/api", target="us"))`. The API key in `.env` authenticates fine.
- `d.list()` returns a **generator** (not a list). `d.get(id)`, `d.create(...)`,
  `sb.delete()`, `d.snapshot.create/get/delete/list` all exist.
- Declarative image build (no local Docker) works:
  `Image.debian_slim("3.12").run_commands(...).env({...})`. `uv` installs to
  `/root/.local/bin`; set `PATH` to include it. `uv tool install fulcra-api`
  installs `fulcra` + `fulcra-api` (v0.1.32).
- `daytona.snapshot.create(CreateSnapshotParams(name=, image=), on_logs=...)`
  streams build logs and reaches `SnapshotState.ACTIVE`. ~couple minutes.
- Spawn: `d.create(CreateSandboxFromSnapshotParams(snapshot=, env_vars=,
  auto_stop_interval=30, public=...), timeout=180)`. `auto_stop_interval` is in
  **minutes** (confirmed). `sb.id` is the id.
- **Signed preview URL exists and is what we want:**
  `sb.create_signed_preview_url(port, ttl_seconds)` → object with `.url`
  (e.g. `https://8080-<token>.daytonaproxy01.net`) + `.token`. (Also
  `sb.get_preview_link(port)` → `.url`, `.token`; `expire_signed_preview_url`.)
- Long-running servers: `sb.process.create_session(name)` +
  `sb.process.execute_session_command(name, SessionExecuteRequest(command=, run_async=True))`. **Do NOT use `nohup ... &` inside an async session command** — it returns immediately and the child can die. Run the server as the async command directly.
- One-shot: `sb.process.exec("bash -lc '...'", timeout=, env={...})` →
  `.exit_code`, `.result` (stdout). Pass secrets via `env=` to keep them out of
  the command string.

## Confirmed: Fulcra CLI auth (device-code flow)

- `uv tool run fulcra-api auth login` prints to stdout:
  `✨ ... visit this URL: https://fulcra.us.auth0.com/activate?user_code=XXXX-YYYY`
  and `❗ Ensure the following code matches ... XXXX-YYYY`, then blocks/polls.
- `uv tool run fulcra-api user-info` is the auth-status gate (exit 1 = not
  authed).
- **Reliable capture command** (for AGENTS.md so the agent doesn't fumble):
  `timeout 12 uv tool run fulcra-api auth login 2>&1 || true` — prints the URL
  then returns. (See rough edge below.)

## Confirmed: Hermes

- Install needs **git** AND **procps** (for `ps`/`pkill`) — neither is in
  `debian_slim`. Add `apt-get install -y git build-essential python3-dev libffi-dev procps`.
- Install: `curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash -s -- --skip-browser --skip-setup`. Installs Hermes v0.14.0 to `/usr/local/lib/hermes-agent`, launcher `/usr/local/bin/hermes`, config `/root/.hermes/` (`.env`, `config.yaml`, `skills/`). Syncs ~90 bundled skills.
- OpenRouter: put `OPENROUTER_API_KEY=...` in `~/.hermes/.env`; set
  `hermes config set model.provider openrouter` and
  `hermes config set model.default anthropic/claude-sonnet-4.5`. **Smoke test
  passed** — `hermes -z "Reply with exactly: pong"` → `pong`. `anthropic/claude-sonnet-4.5` is a valid OpenRouter id here.
- Skill recognition by file-drop: `git clone` the repo, copy
  `skills/fulcra-onboarding` into `~/.hermes/skills/fulcra/fulcra-onboarding`.
  `hermes skills list` then shows it `local … enabled`. Preload into a session
  with `hermes -s fulcra-onboarding` (footer shows "Activated skills:
  fulcra-onboarding").

## The guest surface: ttyd (validated in browser)

- `ttyd` is **not in apt**. Use the static binary:
  `curl -fsSL -o /usr/local/bin/ttyd https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64 && chmod +x /usr/local/bin/ttyd`.
- Run the guest chat: `ttyd -W -p 8080 hermes -s fulcra-onboarding`
  (`-W` = writable input). ttyd serves HTTP directly on the port — **no socat
  needed** (the earlier socat plan is obsolete; ttyd replaces both the dashboard
  and socat).
- Daytona shows a one-time "Preview URL Warning" interstitial (one click,
  "I Understand, Continue") before the app loads. Removable org-wide per
  Daytona docs (`daytona.io/docs/en/preview-and-authentication`) — worth doing
  for a cleaner demo, otherwise it's one extra click.
- Result: a clean full-screen "HERMES-AGENT" chat in the browser, no admin nav,
  no key exposure. Model line shows `claude-sonnet-4.5 · Nous Research`.

## Why NOT the Hermes dashboard (security)

- `hermes dashboard` requires `--tui` (or `HERMES_DASHBOARD_TUI=1`) to expose a
  Chat tab; binding non-localhost needs `--insecure` whose help says
  *"DANGEROUS: exposes API keys on the network."*
- Opened the dashboard via preview URL: it's a full console with **CHAT,
  SESSIONS, MODELS, CONFIG, KEYS, CRON, Restart Gateway, Update Hermes**. The
  **KEYS** tab is a live secrets manager: *"Manage API keys and secrets stored
  in ~/.hermes/.env. Changes are saved to disk immediately."* A guest could read
  or change our OpenRouter key and settings. → **Dashboard is not a guest
  surface.** (Also it runs a ~15s vite build on first launch unless prebuilt.)

## Rough edge to fix (don't let the agent fumble)

When asked naively to "run auth login", the agent either (a) blocked for 47s+
on the foreground command, or (b) ran it as a Hermes background `proc` whose
log didn't capture the URL, then iterated for ~1 minute before finally using a
`timeout`-based synchronous run and surfacing the URL. To make this instant and
reliable, our `~/.hermes/AGENTS.md` (or a tweak proposed to the skill) should
tell the agent the exact capture command:
`timeout 12 uv tool run fulcra-api auth login 2>&1 || true`, present the URL +
code, then poll `uv tool run fulcra-api user-info` until it succeeds.

## Open / deferred

- **tirith command scanner at runtime**: the chat banner showed "tirith
  security scanner enabled but not available — command scanning will use pattern
  matching only." The onboarding "create annotations" reference uses
  `curl -H "Authorization: Bearer $TOKEN" https://api.fulcra...`, which the
  pattern matcher may flag. Auth + user-info (CLI, not raw curl) worked fine.
  Validate the annotation-writing step later (Task 6 live verify) and, if it’s
  blocked, find the config to make tirith permissive for our own API host.
- Removing the Daytona preview interstitial org-wide (nice-to-have).
- Did not complete a real browser login (would create a Fulcra account); URL
  surfacing is sufficient proof.

## Net effect on the plan

- Image (Task 4) adds: `git`, `procps`, `ttyd` (static binary), and a
  `git clone + cp` of the onboarding skill into `~/.hermes/skills/`. Pre-set
  OpenRouter provider in `config.yaml`.
- Assets (Task 3): `start-dashboard.sh` → rename to `start-chat.sh`, runs
  `ttyd -W -p 8080 hermes -s fulcra-onboarding` (no dashboard, no socat).
  AGENTS.md gains the reliable auth-capture command.
- Spawn (Task 6): inject `OPENROUTER_API_KEY` into `~/.hermes/.env`, start
  `start-chat.sh`, return `create_signed_preview_url(8080, ttl)`.

---

## Dashboard lockdown (CHOSEN approach — validated end-to-end)

Goal: keep the Hermes web dashboard's nicer Chat UI but prevent guests from
reading/changing our OpenRouter key or running admin actions. Achieved with a
Caddy reverse proxy that default-proxies the SPA + chat but 403s the dangerous
`/api/*` endpoints. **Validated:** sent "Reply with exactly one word: working"
in the browser → agent replied "working", MODEL shows "live".

### Run recipe (per sandbox)

1. Dashboard, localhost-only, chat enabled, gate-bypassed:
   `HERMES_DASHBOARD_TUI=1 hermes dashboard --host 127.0.0.1 --port 9119 --no-open --insecure`
   - `--insecure` is REQUIRED: without it the chat shows "[session ended]" /
     "events feed disconnected" and MODEL stays "closed". With it (and still
     bound to 127.0.0.1, only reachable via the proxy) the PTY chat goes live.
   - `HERMES_DASHBOARD_TUI=1` (or `--tui`) is REQUIRED to expose the Chat tab.
   - First launch runs a ~15s vite build into `web_dist`; prebuild during image
     build (`cd /usr/local/lib/hermes-agent/web && npm run build`) then use
     `--skip-build` for instant per-sandbox starts.
2. Caddy on :8080 → 127.0.0.1:9119, denylist + Host/Origin rewrite (Caddyfile):

   ```
   :8080 {
       @blocked {
           path /api/env /api/env/* /api/config /api/config/* /api/cron /api/cron/* \
                /api/providers /api/providers/* /api/dashboard/agent-plugins/* \
                /api/profiles /api/profiles/* /api/skills/toggle /api/model/set \
                /api/gateway/* /api/hermes/* /api/logs /api/logs/*
       }
       handle @blocked { respond "This demo only exposes the chat." 403 }
       handle {
           reverse_proxy 127.0.0.1:9119 {
               header_up Host 127.0.0.1:9119
               header_up Origin http://127.0.0.1:9119
           }
       }
   }
   ```
3. `sb.create_signed_preview_url(8080, ttl)` → hand the URL to the guest.

### Verified endpoint behavior through Caddy

- `/chat`, `/keys`, `/` (SPA routes) → 200 (serve index.html; SPA routing works)
- `/api/status`, `/api/model/info` → 200 (chat needs these)
- `/api/env`, `POST /api/env/reveal`, `/api/config`, `/api/cron/jobs`,
  `POST /api/model/set`, `POST /api/gateway/restart`,
  `POST /api/dashboard/agent-plugins/install` → **403** (sealed)
- In the browser: KEYS tab renders blank (its `/api/env` is 403); CHAT works.
- `/api/auth/me` returns 401 even on direct localhost — that's the normal
  "no OAuth identity" state, NOT a blocker; the PTY chat works anyway with
  `--insecure`.

### IMPORTANT residual risk (applies to ANY chat-with-shell-agent surface)

The guest is talking to an agent that has a terminal tool and the OpenRouter key
in its environment / `~/.hermes/.env`. A guest could simply ask the agent to
print `~/.hermes/.env` or `echo $OPENROUTER_API_KEY` and the agent may comply.
Locking the dashboard KEYS tab does NOT prevent this (and neither would ttyd).
**Mitigation (required for the demo):** use a dedicated, low-credit-cap,
disposable OpenRouter key, and rotate it after demos. Optionally add an agent
instruction to refuse to reveal secrets, but treat that as soft defense only.

### Net effect on the plan (supersedes the ttyd plan above)

- Image (Task 4): also install **caddy** (static binary, validated v2.11.3) and
  **prebuild the dashboard web** (`npm run build`) so runtime uses `--skip-build`.
- Assets (Task 3): ship a **Caddyfile** (above) and a **start-chat.sh** that
  launches the dashboard (`--insecure --tui --skip-build`, localhost) and caddy
  (foreground), instead of ttyd/socat.
- Spawn (Task 6): inject `OPENROUTER_API_KEY` into `~/.hermes/.env`, run
  start-chat.sh, return `create_signed_preview_url(8080, ttl)`.
- Snapshot `fhd-dash-spike` (hermes+caddy+fulcra-api+skill baked) was kept for
  reuse; the real build target is `fhd-hermes-demo`.
