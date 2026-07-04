# Fulcra device-flow auth — the full detail

How the agent gets the user authenticated with Fulcra using the `fulcra-api` CLI's
OAuth Device Authorization Flow, without ever handling a password and without any
interactive terminal session.

> **Provenance:** adapted from the `fulcra-onboarding` skill in
> [fulcradynamics/agent-skills](https://github.com/fulcradynamics/agent-skills) (MIT),
> reworked for the two-step `--get-auth-url` / `--device-code` form so no
> background process has to be babysat.

## Preconditions

- `uv` is installed (`uv --version` succeeds). If not, ask the user for consent
  first, then install: `curl -LsSf https://astral.sh/uv/install.sh | sh`
  (macOS/Linux) or `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"` (Windows).
- Always invoke the CLI as `uv tool run fulcra-api …`. Don't rely on bare
  `fulcra-api` being on PATH — spawned subshells in many agent runtimes don't
  inherit it — and don't trust `which fulcra-api` to tell you whether it's
  installed. `uv tool run` resolves and caches the tool automatically.

## Step 0 — check whether auth is even needed

```bash
uv tool run fulcra-api user-info
```

Exit 0 with valid JSON means the user is already authenticated — stop here, say
nothing about logging in, and move on. Any non-zero exit means proceed with the
device flow below.

## Step 1 — mint the auth URL (non-interactive)

```bash
uv tool run fulcra-api auth login --get-auth-url
```

This returns immediately (no polling, no hang) with three items:

```
Web auth URL: https://fulcra.us.auth0.com/activate?user_code=XXXX-YYYY
- Web auth code: XXXX-YYYY
- Device code: <24-char opaque string>
```

Handling rules:

- **Relay to the human**: the web auth URL (as a clickable markdown link) and the
  web auth code, so they can confirm the code on the page matches. The URL
  already embeds the code as a query parameter, so the Auth0 page pre-fills it —
  the user just confirms and signs in (or creates an account; same flow).
- **Keep private**: the **device code**. It is the credential the CLI uses to
  claim the completed session — anyone holding it during the validity window can
  finish the login as this user. It appears in CLI output the agent reads; it
  must never appear in a chat message, log excerpt, or error report shown to the
  human.
- In a public or group channel, also warn the user to treat the auth URL itself
  as sensitive — it shouldn't be pasted onward or shared.

**Never run bare `uv tool run fulcra-api auth login`** (no flags). Interactive
mode blocks the shell polling for up to two minutes, can't show you the URL
until it's too late in some runtimes, and typically times out before a human
finishes a browser sign-in. The two-step form exists precisely so agents don't
have to manage a hanging foreground process.

## Step 2 — poll for completion

Roughly every 15 seconds, run:

```bash
uv tool run fulcra-api auth login --device-code <DEVICE_CODE> --poll-timeout=5
```

Each invocation polls for up to 5 seconds and returns. Three outcomes:

1. **Pending** — the user hasn't finished the browser flow yet. Not an error;
   wait ~15 seconds and poll again. Don't nag the user on every tick.
2. **Success** — credentials are persisted to
   `~/.config/fulcra/credentials.json` (mode-restricted; never print its
   contents). Confirm with `uv tool run fulcra-api user-info` and announce
   success to the user immediately — the "your bot noticed you signed in" beat
   is the point of watching.
3. **Expired** — device codes are valid for roughly **10 minutes**. If the code
   expires before the user finishes, this is routine, not a failure: go back to
   Step 1, mint a fresh URL + codes, and re-message the new link with a light
   "that link timed out — here's a fresh one." Never re-send the old URL; its
   code is dead.

Do **not** start a second `--get-auth-url` while a live code is still pending —
each mint invalidates nothing server-side, but messaging the user two different
codes guarantees confusion about which page to trust. One live code at a time.

## Failure modes

| Symptom | Diagnosis | Action |
|---|---|---|
| `user-info` keeps failing right after a successful-looking poll | Credentials write raced the check | Wait ~3 s, retry `user-info` up to 3 times before concluding anything |
| Poll reports expired | Code TTL (~10 min) elapsed | Mint a fresh URL (Step 1), re-message it |
| Connection errors to `fulcra.us.auth0.com` or `api.fulcradynamics.com` (DNS failure, connect timeout — *not* 4xx auth errors) | The runtime's sandbox blocks outbound network | Tell the user plainly that this runtime can't do CLI auth, and stop — don't retry-loop against a wall |
| `uv: command not found` | No `uv` on the host | Consent-gated install (see Preconditions) |

## After auth

- Tokens are minted on demand by downstream tooling (`fulcra-api auth
  print-access-token`); the import script shells out for one per run and never
  writes it to disk or output. The agent never needs to see, store, or relay a
  bearer token.
- Re-auth is rarely needed — credentials refresh from
  `~/.config/fulcra/credentials.json`. If a later command returns 401/403,
  re-run this flow from Step 0.
