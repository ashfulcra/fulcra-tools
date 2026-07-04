---
name: fulcra-netflix
description: "Walk a user from zero to their Netflix viewing history stored in their own Fulcra account as a Watched annotation, then shared to the movie-night pool. Auth (device flow) → export walkthrough → import via the bundled script → share."
homepage: "https://github.com/ashfulcra/fulcra-tools"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "🍿" } }
---

# fulcra-netflix — Netflix history into the user's own Fulcra account

This skill walks a brand-new user from "I just messaged this skill to my bot" to "my Netflix viewing history lives in my own Fulcra account as a Watched annotation, shared (if they choose) with the movie-night pool." You — the agent — drive the whole thing over chat: authenticate the user with Fulcra's device flow, walk them through downloading their viewing history from Netflix, import it with the bundled `scripts/netflix_import.py`, and offer the pool share at the end.

**Runtime-agnostic.** The only contract is: (1) you can run a shell subprocess, and (2) you can relay messages to and from a human. Everything else is plain shell I/O — no Claude-Code-specific tools. This skill works identically in Claude Code, OpenClaw, Hermes, Codex, or any other runtime that can execute a subprocess and hold a conversation.

The skill is a five-state conversation machine: **HELLO → AUTH → EXPORT → IMPORT → SHARE**. Every state is safely re-enterable — a returning user resumes wherever they left off, and re-running any state's commands never corrupts anything (the importer's record IDs are deterministic, so re-imports are server-side no-ops).

Details live in `references/`: [auth.md](references/auth.md) (device-flow specifics and failure modes), [netflix-export.md](references/netflix-export.md) (full slim + GDPR export walkthroughs), [record-schema.md](references/record-schema.md) (exact wire shapes, det-id formulas, and the namespace-marker contract).

---

## Where to start — the re-entrancy probe

Before sending anything, probe how far this user already got. Enter at the **first state whose probe fails**:

| Probe (run in order) | Command | Passes when | If it fails, enter at |
|---|---|---|---|
| Authed? | `uv tool run fulcra-api user-info` | exits 0 and prints valid JSON | **AUTH** (send HELLO first if this user has never seen the pitch/consent message) |
| Watched def exists? | `uv tool run fulcra-api catalog -n Watched` | some line's `description` is exactly `com.fulcradynamics.annotation.media.watched` | **EXPORT** (they're authed but never imported) |
| Records exist? | `uv tool run fulcra-api get-records "DurationAnnotation/<def-uuid>" "2007-01-01T00:00:00Z" "2035-01-01T00:00:00Z" \| head -1` (def-uuid from the catalog line above) | non-empty output | **IMPORT** (def exists but empty — ask for the CSV again) |
| Share confirmed? | none — the share happens in Fulcra's web UI, there is no CLI probe | the user has previously told you they completed (or explicitly skipped) the share | **SHARE** |

All four pass → done; congratulate them and point at [Context Web](https://context.fulcradynamics.com) to browse their data. A brand-new user fails the first probe: send HELLO, then proceed through the states in order.

---

## State 1 — HELLO

The first message the user sees. It must contain the pitch, the **full share disclosure** (consent comes *before* auth, so the user knows what they're signing up for), and the step list. Send this, adapted naturally to the conversation but keeping every substantive element:

> Hi! I can import your Netflix viewing history into your own Fulcra account — a personal data store that you control — and, if you want, share it into the movie-night pool so group-recommendation agents can find things everyone would enjoy. The whole thing takes about 5 minutes of your time, and at the end your complete watch history is queryable data you own.
>
> One thing to know up front, so you can decide with eyes open: at the end I'll invite you to share your data with the pool owner (Fulcra ID `a24a9667-c2c6-4bbf-9a0f-36ea0afcb521`). Fulcra's sharing page currently shares **all of your annotation data, not just the Netflix history** — it has no way to share only one kind of annotation yet. If that's more than you're comfortable with, you can simply skip the share and still keep your imported history for yourself. Nothing gets shared unless you do it yourself, in your own browser, at `https://context.fulcradynamics.com/sharing?type=sending` at the end.
>
> Here's the plan:
> 1. **Sign in** to Fulcra (or create a free account) — I'll send you a link.
> 2. **Download** your viewing history from Netflix — about 2 minutes, I'll walk you through it.
> 3. **Import** — I run a small bundled script that writes the history into *your* account.
> 4. **Share** (optional) — you decide whether to share with the movie-night pool.
>
> Ready?

Do not water down the disclosure, move it after auth, or imply the share is Netflix-only. When the user assents, move to AUTH.

## State 2 — AUTH

Goal: `uv tool run fulcra-api user-info` returns valid JSON. Full detail and failure modes in [references/auth.md](references/auth.md); the short version:

1. **Probe first**: run `uv tool run fulcra-api user-info`. Valid JSON → already authenticated; skip straight to EXPORT. (If `uv` itself is missing, ask the user for consent to install it: `curl -LsSf https://astral.sh/uv/install.sh | sh` on macOS/Linux.)

2. **Mint the login URL** — always the two-step, non-interactive form:

   ```bash
   uv tool run fulcra-api auth login --get-auth-url
   ```

   **CRITICAL: never run bare `uv tool run fulcra-api auth login`** — interactive mode blocks your shell for up to two minutes waiting on a browser flow you can't see, and typically times out before the user finishes. Always use `--get-auth-url`.

   The output contains three items: a **web auth URL**, a **web auth code** (also embedded in the URL), and a **device code**. Message the user the URL as a clickable markdown link plus the web auth code so they can confirm it matches what the page shows. **The device code stays private** — it appears in the CLI output you read, and it must never appear in a message to the human (anyone holding it can claim the session).

3. **Poll for completion** — roughly every 15 seconds, run:

   ```bash
   uv tool run fulcra-api auth login --device-code <DEVICE_CODE> --poll-timeout=5
   ```

   While the user hasn't finished, this reports the authorization as pending; keep polling. On success it persists credentials to `~/.config/fulcra/credentials.json` — confirm with `uv tool run fulcra-api user-info` and announce success immediately ("You're in! I can see your Fulcra account now."). If the device code expires before they finish (about 10 minutes), don't treat it as failure: mint a fresh URL with `--get-auth-url` and re-message it.

4. **Sandbox bailout**: if the CLI can't reach the network at all (connection errors to `fulcra.us.auth0.com` or `api.fulcradynamics.com`, not auth errors), this runtime is network-restricted and cannot do CLI auth. Tell the user plainly and stop — don't loop retrying.

## State 3 — EXPORT

Walk the user through Netflix's in-app **slim CSV** download — it's instant, which makes it the demo path. Message them these steps (full version, plus the richer GDPR route, in [references/netflix-export.md](references/netflix-export.md)):

> 1. Open [netflix.com/account](https://www.netflix.com/account) in a browser.
> 2. Select **Profiles**, then choose the profile whose history you want.
> 3. Open **Viewing activity**.
> 4. Click **Show More** at the bottom until everything is loaded (long histories take a few clicks).
> 5. Click **Download all** — you'll get a file, usually named `NetflixViewingHistory.csv`.
> 6. Send that file back to me here (or, if we're on the same machine, just tell me the file path).

When the file arrives as a chat attachment, save it locally so the import script can read it. Mention as an optional aside — don't push it — that Netflix's full GDPR export (`netflix.com/account/getmyinfo`) has real watch times, durations, and every profile, and they can re-run this skill with it later; it just takes Netflix days to deliver, so it's the upgrade, not the starting point.

## State 4 — IMPORT

From the skill folder (the directory containing this SKILL.md), run:

```bash
uv run scripts/netflix_import.py <csv-path> --json
```

The script is self-contained (PEP 723 — `uv run` fetches its one dependency automatically). It auto-detects the CSV variant (slim vs GDPR) from the header, resolves-or-creates the Watched annotation definition idempotently, builds deterministic record IDs, POSTs in batches, and does a best-effort readback verification. Optional preflight: add `--check-only` to parse and count without posting anything (no auth needed) — useful for a quick "is this file right?" check.

It prints exactly one line of JSON. **Interpret the envelope; never parse human-mode output:**

| Envelope signal | Meaning | What you do |
|---|---|---|
| `ok: true`, exit 0 | Import succeeded | Narrate the result (guidance below), move to SHARE |
| `errors[0].stage` = `args` or `parse` | Wrong or unreadable path, or the file isn't a Netflix export (bad header / malformed row — the message includes row context) | Tell the user what was wrong with the file and go back to **EXPORT** for the right one |
| `errors[0].stage` = `auth` | Token mint failed or the server rejected it (401/403) | Go back to **AUTH**; if the message says the `fulcra-api` CLI wasn't found, `uv tool install fulcra-api` first |
| `errors[0].stage` = `post` | HTTP failure while posting; `posted` reflects the chunks that actually landed before the failure | Retry once by re-running the same command — already-posted records dedup server-side, so a re-run only fills the gap. If it fails again, surface the error to the user |
| `verified: 0` (with `ok: true`) | Readback checked and didn't find the sample yet — almost always ingest-to-query indexing lag, **not** a failure | Mention data may take a few minutes to appear in queries |
| `verified: null` | Readback couldn't run (CLI unavailable), distinct from "checked and found nothing" | Nothing — it's informational |
| `skipped_existing` | Currently **always `null`** — the batch endpoint gives no dedup feedback | Never promise duplicate/rewatch counts from it |

**Narrating results**: report `posted` out of `total`, conversationally — "Imported 412 titles from your Netflix history!" Keep in mind that `posted` counts POST *attempts*, not novel records: on a re-run of the same file the server silently drops every record as a duplicate while `posted` still reports the full count. So for a re-import say something like "Re-posted 412 records — Fulcra deduplicates these server-side, so nothing was double-counted," never "imported 412 *new* titles." Re-running the importer on the same or an overlapping CSV is always safe.

## State 5 — SHARE

Immediately after a verified import, offer the pool share — restating the disclosure, because consent given ten minutes ago at HELLO is not a substitute for informed action now:

> Last step, and it's optional. To join the movie-night pool, you'd share your Fulcra annotation data with the pool owner. Heads-up again: Fulcra's sharing page shares **all of your annotation data, not just the Netflix history** — there's no narrower option yet. Totally fine to skip; your imported history stays yours either way.
>
> If you're in:
> 1. Open [context.fulcradynamics.com/sharing?type=sending](https://context.fulcradynamics.com/sharing?type=sending).
> 2. Log in with the same account you just signed in with.
> 3. Create a share to recipient Fulcra ID `a24a9667-c2c6-4bbf-9a0f-36ea0afcb521`, making sure annotations are included.
> 4. Tell me when it's done (or that you're skipping).

When they confirm the share, congratulate them and point them at [Context Web](https://context.fulcradynamics.com) to browse their own imported history. If they skip, respect it without pushback — they're fully onboarded either way, and they can share later by re-running this state.

<!-- TODO(share-cli): when fulcra-api-python PR #47 (share create / list-outgoing / …)
     merges, replace the four manual steps above with the agent running
     `fulcra-api share create` itself — scoped to the Watched definition if the API
     supports per-definition scoping — and verifying via `fulcra-api share
     list-outgoing`. At that point (and ONLY at that point) the all-annotations
     disclosure wording here and in HELLO tightens to Netflix-only. Until then the
     manual flow and the honest all-annotations disclosure are the contract. -->

---

## Don't

- **Don't run bare `auth login`** — interactive mode hangs your shell waiting on a browser you can't see. Always `--get-auth-url`, then poll with `--device-code`.
- **Don't parse human-mode importer output** — only the `--json` envelope is a stable contract (keys are append-only; they never get removed or repurposed).
- **Don't echo tokens or the device code into chat.** The device code is printed in CLI output *you* see; it must never reach a message to the human — the web auth URL and web auth code are the only things they need. Never run or relay `auth print-access-token` output into the conversation.
- **Don't promise Netflix-only sharing.** The Context Web share covers all of the user's annotation data. Say so plainly, every time sharing comes up, until the TODO(share-cli) swap lands.
- **Don't run interactive commands** — nothing in this skill needs a TTY; if you find yourself waiting on a prompt, you took a wrong turn.
- **Don't soft-delete annotation definitions.** Fulcra has no per-event delete; events under deleted defs stay visible in queries forever. Def cleanup is a user decision, not yours.
