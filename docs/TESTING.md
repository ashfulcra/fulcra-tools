# Testing Fulcra Collect — first-time walkthrough

This document is for someone who has just cloned the repo and wants to
run Fulcra Collect for real — daemon, menubar, and the Trakt onboarding
wizard — end to end.

For contributors adding code, run the automated test suite instead:

```
uv run pytest packages/ -q
```

---

## Prerequisites

| Requirement | Notes |
|---|---|
| macOS 13 (Ventura) or newer | The menubar and launchd integration require macOS. |
| Python 3.12 | `brew install python@3.12` if you don't have it. `uv` (below) manages the venv. |
| [uv](https://docs.astral.sh/uv/) | `brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| A Fulcra account | Sign up at [fulcradynamics.com](https://fulcradynamics.com). Free tier is fine. |
| A Trakt.tv account | Sign up at [trakt.tv](https://trakt.tv) — free. Needed for the Trakt milestone test. |

---

## Step 1 — Clone and install

```bash
git clone https://github.com/fulcradynamics/fulcra-tools.git
cd fulcra-tools
uv sync
```

`uv sync` resolves all workspace packages (collect, media-helpers, etc.)
and installs them into a local `.venv`. This takes 30–60 seconds on first
run; subsequent syncs are fast.

Verify the install:

```bash
uv run fulcra-collect --help
```

You should see the CLI help text. If you see `command not found`, make
sure `.venv/bin` is on your PATH or always prefix commands with `uv run`.

---

## Step 2 — Start the daemon

The daemon runs as a background process. For first-time testing, start
it in the foreground so you can see its logs:

```bash
uv run fulcra-collect daemon
```

Expected output:

```
INFO     fulcra_collect: web UI: http://127.0.0.1:9292
```

The daemon binds to a stable port (default **9292**). The URL is also
written to `~/.config/fulcra-collect/web-url` for backwards compatibility
with the menubar and ad-hoc tools, but the value is now the same on every
restart. To use a different port — for example if 9292 collides with
another service on your machine — edit
`~/.config/fulcra-collect/config.toml`:

```toml
[daemon]
web_port = 9595
```

and restart the daemon. If the port is already in use you'll see a clear
`port N is in use; set [daemon] web_port = ... in config.toml` error
instead of a cryptic bind failure.

Leave this terminal open. Open a second terminal for the remaining steps.

To install as a persistent background service (starts on login):

```bash
uv run fulcra-collect install
uv run fulcra-collect start
```

---

## Step 3 — Install the fulcra CLI (for browser sign-in)

The wizard signs you in to Fulcra by handing off to the official
`fulcra` CLI, which opens a browser tab for you and handles the OAuth
device-authorization flow. Install it once with:

```bash
uv tool install fulcra-api
```

Verify it's on PATH:

```bash
which fulcra
```

You don't need to run `fulcra auth login` yourself — the wizard will
invoke it for you in Step 5.

> **No CLI? Token fallback.** If you can't or won't install the CLI,
> the wizard falls back to a paste-token form. Get a token at
> [fulcradynamics.com](https://fulcradynamics.com) → avatar → **Settings**
> → **API tokens** → **Generate new token**, then paste it where prompted.

---

## Step 4 — Open the onboarding wizard

```bash
open $(cat ~/.config/fulcra-collect/web-url)
```

This opens the web UI in your default browser. On first launch you will
see the **Dashboard** with a prompt to add your first plugin.

If the browser shows "Could not connect", the daemon is not running.
Go back to Step 2.

---

## Step 5 — Sign in to Fulcra

The wizard's first screen offers **Sign in with Fulcra**.

1. Click the button.
2. A second browser tab opens with the Fulcra sign-in page. Sign in (or
   confirm if you're already signed in).
3. The tab confirms the connection and the wizard advances to the plugin
   list automatically.

The flow times out after 2 minutes — if it does, click **Sign in with
Fulcra** again.

If the button does not appear (the wizard says "checking for the fulcra
CLI…" and then shows a paste-token form instead), the `fulcra` CLI is
not on PATH. Either install it (Step 3) and refresh, or paste a token
from fulcradynamics.com → Settings → API tokens.

---

## Step 6 — Walk Trakt onboarding

From the plugin list, find **Trakt watch history** and click **Set up**.
The wizard has 7 steps:

### Step 1 of 7 — Introduction

Read the overview of what Trakt does. Click **Next**.

### Step 2 of 7 — Create a Trakt OAuth app

You need to register a personal OAuth application on Trakt so Fulcra
Collect can access your watch history without storing your Trakt password.

1. Click **Open Trakt** — this takes you to
   [trakt.tv/oauth/applications](https://trakt.tv/oauth/applications).
2. Click **New Application**.
3. Use these settings:
   - **Name:** Fulcra Collect  (or anything you like)
   - **Redirect URI:** copy the URL shown in the wizard's next step.
     It looks like `http://127.0.0.1:<port>/api/oauth/trakt/callback`.
     If you don't know it yet, leave a placeholder and edit the app
     after step 3.
   - **Scopes:** leave the defaults.
4. Click **Save App**.
5. Trakt shows you the **Client ID** and **Client Secret**. Keep this
   tab open.

### Step 3 of 7 — Paste your Trakt OAuth credentials

Back in the wizard:

1. Paste the **Client ID** into the first field.
2. Paste the **Client Secret** into the second field.
3. Click **Save credentials**.

If the Redirect URI in your Trakt app doesn't match yet, go back to
Trakt, edit the app, and paste the exact URL the wizard showed you.

### Step 4 of 7 — Sign in to Trakt

Click **Authorize with Trakt**. A new browser tab opens at trakt.tv.

1. Log in to Trakt if prompted.
2. Click **Allow** to grant Fulcra Collect access.
3. The tab shows "Signed in to trakt" and closes automatically.
4. The wizard detects the successful callback and advances to the next
   step. If it doesn't, click the **Check again** button.

### Step 5 of 7 — Verify connection

The wizard calls Trakt's API to confirm your token is valid. You should
see:

- A green "Connected" badge.
- Your Trakt username.
- A preview of your 5 most recent watches.

If this step shows an error, the most common cause is that the OAuth
code expired (Trakt gives you ~5 minutes). Go back to Step 4 and
re-authorize.

### Step 6 of 7 — Choose or create a Fulcra definition

The wizard lists your existing Fulcra annotation definitions (or offers
to create one named "Watched").

- If you have an existing "Watched" duration annotation, select it.
- Otherwise click **Create new "Watched" definition** and Fulcra Collect
  will create one automatically on first sync.

### Step 7 of 7 — Done

Click **Enable plugin**. Trakt is now active.

---

## Step 7 — Verify it is working

### In the web UI

- The **Dashboard** → **Recently** feed should show entries as Trakt
  syncs (first sync runs within 6 hours, or click **Run now** on the
  Trakt row in the Preferences → Plugins tab).

### In config.toml

```bash
grep -A5 'trakt' ~/.config/fulcra-collect/config.toml
```

Expected: `trakt` appears in the `enabled` list.

### From the CLI

```bash
uv run fulcra-collect status
```

Look for `trakt` in the output with `enabled: true` and a `last_run`
timestamp after the first sync runs.

---

## Running the automated smoke test (optional)

Before manual testing, or after making code changes, run the synthetic
smoke script. It walks the entire Trakt onboarding flow in-process with
all outbound HTTP mocked:

```bash
uv run python scripts/smoke_trakt.py
```

All 21 steps should print green checkmarks and the script exits 0. If
any step fails, the script identifies which route is broken and what it
expected.

---

## Troubleshooting

**"Sign-in didn't complete within 2 minutes"**
The fulcra CLI polls for 2 minutes after opening the browser. If you
took longer, just click **Sign in with Fulcra** again. If it keeps
timing out, run `fulcra auth login` manually in a terminal to see what
the CLI reports.

**"The fulcra CLI is not on PATH" / no Sign-in button**
Install it with `uv tool install fulcra-api`, then refresh the wizard.
Or click **Use a token instead** to fall back to the paste-token form.

**"Fulcra rejected the token" (paste-token path)**
Re-copy the token from fulcradynamics.com → Settings → API tokens.
Select all, copy, paste — whitespace at either end silently breaks JWT
validation.

**"Trakt API error: invalid_grant"**
The OAuth authorization code has a 5-minute expiry. Re-run the OAuth
step: go back to Step 4 in the wizard and click **Authorize with Trakt**
again.

**Wizard shows "Could not connect to the daemon"**
Run `uv run fulcra-collect status` in a terminal. If the daemon isn't
running, start it with `uv run fulcra-collect daemon` (or
`uv run fulcra-collect start` if you installed it as a service).

**Menubar icon not visible after starting the menubar app**
The menubar app is separate from the daemon. Run it with:
```bash
uv run --package fulcra-menubar python -m fulcra_menubar
```
If the icon still doesn't appear, check the terminal for import errors
(usually a missing PyObjC dependency — run `uv sync --extra macos
--package fulcra-menubar` first).

**Web UI shows a blank page or JavaScript error**
The web UI lives at `packages/web-ui/dist/`. Make sure `dist/index.html`
exists. If not, the daemon falls back to an error JSON — it will say
`"error": "web UI not built"`. The static files should be present in
the repo; if they are missing, check `git status packages/web-ui/dist/`.

**Trakt shows "Not signed in to Trakt yet" after OAuth succeeded**
The credentials are stored in the OS keychain (macOS Keychain Access).
Run the health check manually:
```bash
curl -sH "Authorization: Bearer $(cat ~/.config/fulcra-collect/web-token)" \
  "$(cat ~/.config/fulcra-collect/web-url)/api/plugin/trakt/credentials" | python3 -m json.tool
```
If `access_token` shows `"missing"`, re-run the OAuth step.

---

## Attention browser extension

The Attention plugin used to run its own loopback HTTP server on port
8771 to receive events from the browser extension. That standalone relay
is gone — the daemon now hosts the extension endpoint directly at:

```
http://127.0.0.1:9292/api/extension/attention
```

(swap 9292 for your custom `web_port` if you set one).

To wire up the extension:

1. Walk the Attention plugin's onboarding wizard in the Fulcra Collect
   web UI. It will ask you to pick a bearer token (the
   `extension-token`) and prompt you to paste it into the extension's
   options page along with the URL above.
2. The daemon validates each POST against the `extension-token` you set
   in the keychain; mismatched or missing tokens return 401.
3. Use `fulcra-attention status` to inspect the per-machine state file
   (definition id, hostname tag, per-client watermarks). The
   `fulcra-attention relay` subcommand and the launchd-install half of
   `fulcra-attention setup` are gone. The `setup` subcommand still exists
   but now only tags this machine's events with its hostname.

## What the sync does

Once Trakt is enabled, Fulcra Collect:

1. Fetches your watch history from `api.trakt.tv/users/me/history` every
   6 hours (configurable in Preferences → Plugins → Trakt → Interval).
2. Writes each new watch as a `DurationAnnotation` to your Fulcra account
   under the definition you chose in Step 6.
3. Deduplicates by watch event ID so re-syncing does not create
   duplicate annotations.

The activity feed on the Dashboard shows each sync attempt with a
summary count and any errors.
