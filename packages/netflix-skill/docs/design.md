# fulcra-netflix skill — design

**Date:** 2026-07-03
**Status:** in review (coord2 team fulcra, review slug `netflix-skill-design`)
**Location:** `packages/netflix-skill/` in ashfulcra/fulcra-tools — the shippable skill lives at `packages/netflix-skill/skills/fulcra-netflix/` (same package-wraps-skill convention as `media-helpers`/`fulcra-media`), agent-skills folder layout inside so it can be PR'd upstream to `fulcradynamics/agent-skills` later

## What this is

A runtime-agnostic agent skill that takes a brand-new user from "I messaged a skill link to my bot" to "my Netflix viewing history lives in my Fulcra account as a Watched annotation, shared with the movie-night pool owner." It is the flagship concrete demo of Fulcra-as-context-layer: onboarding → export → import → share, driven entirely by the user's own agent over chat.

The skill composes three existing pieces rather than reinventing them:

- **Auth**: the device-code flow from `fulcradynamics/agent-skills` → `fulcra-onboarding` (URL + user code to the human, agent polls for completion).
- **Export walkthrough**: the slim-CSV steps from this repo's `fulcra-media` Netflix wizard (`packages/media-helpers/fulcra_media/wizards/netflix.py`).
- **Ingest pattern**: deterministic-UUID records POSTed to `/ingest/v1/record/batch`, per the `fulcra-ingest-beta` reference — but executed by a vendored, tested script instead of an LLM-improvised one.

## Why a vendored import script

`fulcra-api` (0.1.35) has no record-write command — `data-type create` makes definitions, but records only land via `POST /ingest/v1/record[/batch]` with a bearer token. The alternatives were:

1. **Vendored script** (chosen): `scripts/netflix_import.py`, PEP 723 inline deps, run via `uv run`. Every user's agent executes identical, tested code → identical schema across the pool, which the future group-recommendation work depends on.
2. Prompt-only (ingest-beta style, each agent writes its own parser): zero code to maintain but N users → N parsers and schema drift. Rejected.
3. `fulcra-media` CLI: battle-tested but requires cloning this monorepo (not on PyPI, sibling deps). Rejected for stranger-friendliness.

When `fulcra-api` grows a record-ingest command, the script's POST layer shrinks to a shell-out; the parse/UUID layer stays.

## Conversation state machine

The SKILL.md is written as a state machine. Each state defines: the message to send the human, the commands to run, the success signal, and the failure fallback. States are re-entrant — a returning user resumes wherever they left off (the skill probes: authed? def exists? records exist? share confirmed?).

### 1. HELLO
First message to the human. Contains, in order:
- One-paragraph pitch (what will happen, ~5 minutes of their time, what they get).
- **The share disclosure + instructions, up front** (Ash 2026-07-03): after import, they will share their Watched history with the pool owner — Fulcra ID `a24a9667-c2c6-4bbf-9a0f-36ea0afcb521` — by visiting `https://context.fulcradynamics.com/sharing?type=sending`, logging in with the same account they auth in step 2, and creating a share that includes annotations to that ID. Stated here so consent precedes auth.
- The step list so they know where they are throughout.

### 2. AUTH
- Preflight: `uv` present (install one-liner if not), then `uv tool install fulcra-api` (or `uvx`).
- `uv tool run fulcra-api user-info` — valid JSON means already authed; skip ahead.
- Else `uv tool run fulcra-api auth login --get-auth-url`; message the verification URL (as a clickable link) + user code; keep the device code private.
- **Watch for auth**: loop `auth login --device-code <code> --poll-timeout=5` roughly every 15s until success or device-code expiry (then mint a fresh URL). Announce success immediately — this is the "bot watches for auth" beat.

### 3. EXPORT
Message the slim-CSV steps (lifted from the fulcra-media wizard, same wording):
netflix.com/account → Profiles → pick profile → Viewing activity → click *Show More* until fully loaded → *Download all* → save `NetflixViewingHistory.csv`. Ask them to message the file back (or give a path, for local runtimes).
GDPR full export (`netflix.com/account/getmyinfo`) is mentioned as an optional later upgrade for real timestamps/durations — not the demo path (takes days).

### 4. IMPORT
Agent receives the CSV (chat attachment saved locally, or a user-supplied path) and runs:

```
uv run skills/fulcra-netflix/scripts/netflix_import.py <csv-path> --json
```

The script:
- **Def resolution (idempotent)**: find a DurationAnnotation def named `Watched` whose description carries the namespace marker `com.fulcradynamics.annotation.media.watched`; create via `fulcra-api data-type create --type duration` if absent. Never create duplicates.
- **Parse, auto-detecting variant**:
  - Slim (2 cols `Title,Date`, M/D/YY): each row → start = end = 12:00 UTC on the date, `timestamp_confidence: "low"`, `point_in_time: true` — mirroring the fulcra-media slim importer exactly.
  - GDPR 10-col (`ViewingActivity.csv`): real UTC start + duration, trailers/previews filtered, `timestamp_confidence: "high"`.
- **Record shape**: deterministic UUID per row (hash of raw row + namespace + ingest version); `metadata.source` chain `["com.netflix", "<file basename>", "agent.<runtime>", "com.fulcradynamics.annotation.<def-id>"]`; `data` JSON carrying `title`, `note`, and a `com.fulcra.content.*` fingerprint source-id compatible with `fulcra_common.cross_source_fingerprint`, so a user who later runs fulcra-media twins-dedups cleanly.
- **POST** via `/ingest/v1/record/batch` (JSONL), bearer from `$(uv tool run fulcra-api auth print-access-token)` — token never written to disk or chat.
- **Verify**: readback sample via `fulcra-api get-records`; tolerate indexing lag (report posted vs verified separately, as fulcra-media does).
- **Output**: one-line JSON envelope `{ok, total, posted, skipped_existing, verified, errors:[{stage,message}]}` (stages: `setup|auth|args|parse|post`). Exit 0/2. The agent narrates this conversationally ("Imported 412 titles, 0 duplicates").

Re-runs are safe: deterministic IDs make re-imports no-ops server-side.

### 5. SHARE
Immediately after import verification, walk the user through the manual share (same instructions as HELLO, now actionable):
1. Open `https://context.fulcradynamics.com/sharing?type=sending`.
2. Log in with the same account used in step 2.
3. Create a share that includes annotations, recipient Fulcra ID `a24a9667-c2c6-4bbf-9a0f-36ea0afcb521`.
4. Confirm back to the agent; agent congratulates and points at Context Web to browse their data.

`TODO(share-cli)`: when fulcra-api-python PR #47 (`share create/list-outgoing/…`) merges, replace the manual steps with the agent running `fulcra-api share create` itself and verifying via `share list-outgoing`. The skill carries this marked block so the swap is a one-file edit.

## Repo layout

```
packages/netflix-skill/
    pyproject.toml                 # dep-free; dev extra for pytest (package = test harness only)
    README.md
    docs/design.md                 # this spec
    fulcra_netflix/                # test-support shims for the vendored script
    skills/fulcra-netflix/         # THE SHIPPABLE ARTIFACT — what users message to their bot
        SKILL.md                   # the state machine, runtime-agnostic (shell-only contract)
        references/
            auth.md                # device-code flow details (adapted from fulcra-onboarding)
            netflix-export.md      # slim + GDPR walkthroughs (adapted from the fulcra-media wizard)
            record-schema.md       # exact def + record wire shapes, fingerprint rules
        scripts/
            netflix_import.py      # vendored importer (PEP 723; stdlib + httpx)
    tests/
        test_netflix_import.py     # parser/UUID/envelope tests over fixture CSVs
        fixtures/                  # synthetic slim + GDPR CSVs
```

The import script stays PEP 723 self-contained (runnable straight from a
skill checkout with `uv run`, no package install) — the package wrapper is
for monorepo test coverage, mirroring how `media-helpers` wraps
`skills/fulcra-media`.

SKILL.md follows the agent-skills conventions (frontmatter with name/description/license, user-invocable) and the fulcra-media skill's runtime-agnostic stance: works in Claude Code, OpenClaw, Hermes, Codex — anything that can run a subprocess and relay chat messages.

## Error handling

- Auth poll expiry → mint a new URL, re-message, don't fail the session.
- CSV that isn't a Netflix export (wrong headers) → `errors[{stage:"parse"}]`, agent asks for the right file, points back at EXPORT.
- Ingest 401 → re-run token mint once, then direct user to re-auth.
- Partial batch failures → envelope reports counts; re-run is safe (idempotent IDs).
- No network in sandboxed runtimes → detect per fulcra-onboarding's note and tell the user this runtime can't do CLI auth.

## Testing

- pytest over `netflix_import.py`: slim + GDPR fixture CSVs (synthetic, fixture-shaped like fulcra-media's tests — no personal data), UUID determinism, variant auto-detection, envelope schema, def-resolution idempotency (HTTP mocked).
- Live smoke: 3-row synthetic CSV against Ash's account, verified by readback, then removed via `DeletedRecord` tombstones.

## Out of scope (v1)

- IMDB/ratings enrichment, group recommendations, ELO leaderboards — later work that rides on the shared pool.
- CLI-driven sharing — lands with PR #47 (placeholder block ready).
- The blog-style public walkthrough — separate deliverable once the skill is proven end-to-end.
