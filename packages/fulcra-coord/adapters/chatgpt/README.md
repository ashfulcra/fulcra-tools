# ChatGPT adapter — Custom GPT + OpenAPI Action (read + write)

This adapter lets a **ChatGPT (consumer product) conversation participate in
`fulcra-coord` coordination — both reading and writing**. A Custom GPT,
configured with the artifacts here, can answer "what's going on / what was I
working on?" by reading the pre-built coordination views, **and** report
milestones back to the bus through the coordination **facade** (`facade/`).

Reads go directly against the real Fulcra HTTP API. Writes (and an equivalent
one-call read) go through the facade — a thin service that wraps the
`fulcra_coord` package so the task upload + view rebuild happen correctly
server-side. See "Reads direct, writes via the facade" below and the design spec
(`docs/superpowers/specs/2026-06-01-chatgpt-integration-design.md`,
section "Decided direction & API grounding").

## Files

| File | What it is |
| --- | --- |
| `openapi.yaml` | OpenAPI 3.1 schema for the GPT Action. Two servers (the facade base URL, set at deploy; and `https://api.fulcradynamics.com` for direct reads). Read ops: `resolveCoordinationFile`, `downloadCoordinationFile`, `statCoordinationFile`. Facade ops: `reportMilestone` (write), `getCoordinationStatus` (read). |
| `INSTRUCTIONS.md` | The Custom GPT's system instructions (identity, mint-a-session_key convention, start-of-session status read, `reportMilestone` milestone reporting, caveats). |
| `facade/` | The write facade service (FastAPI). Implements `POST /coordination/report` + `GET /coordination/status` by wrapping `fulcra_coord`. See `facade/README.md`. |
| `custom-gpt/` | **Turnkey copy-paste bundle** for a hosted, facade-only Custom GPT: `INSTRUCTIONS.md` (final system instructions), `openapi.yaml` (facade ops with a `{{FACADE_BASE_URL}}` tunnel placeholder), and `SETUP.md` (step-by-step). Use this when you just want the facade-backed GPT stood up fast; the files above remain the full read+write reference. See `custom-gpt/SETUP.md`. |

The OpenAPI + INSTRUCTIONS files are the **canonical, version-controlled
definition** of the GPT. The "repo as distribution" idea survives here in
spirit: you build the GPT by pasting/importing these, and you edit them here
(not only in the GPT UI).

## Reads direct, writes via the facade (the load-bearing decision)

The umbrella goal is "every agent reports milestones and can answer what's going
on." For ChatGPT the **read** half is a clean direct API call; the **write**
half is not expressible as a single Custom GPT Action call, so it is delegated
to the facade. Grounded in the real API:

- **Reading a view is a clean two-call sequence** the Action can make:
  `GET /input/v1/file_upload?path=...&name=...` to resolve a path to a `file_id`
  (core.py `resolve_filepath` / `list_files`), then
  `GET /input/v1/file_upload/{file_id}/download` to fetch the JSON
  (core.py `download_file`). The coordination views (`/coordination/index.json`,
  `/coordination/views/{active,next,recently-done,search-index}.json`) are
  **already materialized** by `fulcra-coord`, so a read is genuinely just
  "download a pre-built file."

- **Writing a milestone is NOT a single endpoint.** A correct write must:
  1. upload a `tasks/TASK-*.json` task file, **and**
  2. rebuild *all* the materialized views (`index`, `active`, `next`,
     `recently-done`, `search-index`) with **optimistic-concurrency + merge**
     (`fulcra_coord/views.py` `build_all_views`, plus the stat-based
     change detection in `remote.py`). That reconciliation is CLI logic, not an
     API call.
  Worse, the Fulcra upload itself is a **two-step presigned flow**
  (core.py `upload_file`: `POST /input/v1/file_upload` returns a presigned
  `url`, then a **second** `POST` of the bytes to that opaque storage URL). The
  second leg is not a stable, declarable OpenAPI operation a GPT Action can
  drive. So even a naive "just upload one file" is awkward, and a *correct*
  milestone write is out of reach for the Action.

**Decision: option (A)** from the spec — the read Action plus a **thin
coordination facade** for writes (a single endpoint accepting
`{agent_id, session_key, summary, next_action, ...}` that does the upload + view
rebuild server-side). Options (B) "append a raw event for a reconciler to fold
in" and (C) "full file manipulation in the GPT" were rejected: (B) still needs
the two-step presigned upload the Action can't cleanly do, and (C) would have
the model hand-rebuild five view files with concurrency — fragile and unsafe.

**That facade is now built** (`facade/`, see `facade/README.md`). It wraps the
`fulcra_coord` package (`schema.make_task` / `apply_*` + `cli._write_task_and_views`),
exposing `POST /coordination/report` and `GET /coordination/status`. So
**both read and write are now possible** for ChatGPT. The remaining work is
operational: deploying the facade on a host where `fulcra-api` is authenticated
and ChatGPT can reach it over HTTPS, plus its inbound token. The heartbeat
reconciler is still useful as a backstop for any ChatGPT-owned task left
`active` (ChatGPT has no end-of-session hook to park it), but it is no longer
the *only* path to a durable milestone.

## Create the Custom GPT

> Requires a ChatGPT plan that can create GPTs (Plus or higher).

1. **New GPT.** ChatGPT → left sidebar → **GPTs** → **+ Create** → open the
   **Configure** tab (skip the conversational builder).
2. **Name / description.** e.g. name "Fulcra Coordination", description
   "Reads my fulcra-coord status and tells me what I'm working on."
3. **Instructions.** Open `INSTRUCTIONS.md`, copy the fenced `text` block, paste
   it into the **Instructions** field. Replace `<user-or-workspace>` in the
   agent id with the user/workspace label you want.
4. **Deploy the facade first** (needed for writes). See `facade/README.md`.
   Note its public base URL and the `FULCRA_COORD_FACADE_TOKEN` you set. Then
   edit `openapi.yaml` `servers[0]` (and the per-operation `servers:` on
   `reportMilestone` / `getCoordinationStatus`) from the `REPLACE-ME` placeholder
   to that base URL.
5. **Add the Action.** Scroll to **Actions** → **Create new action** →
   **Import** (or "Edit" the schema box) and paste the entire contents of
   `openapi.yaml`. The editor should show five operations: the three reads
   (`resolveCoordinationFile`, `downloadCoordinationFile`, `statCoordinationFile`,
   server `https://api.fulcradynamics.com`) and the two facade ops
   (`reportMilestone`, `getCoordinationStatus`, server = your facade URL).
6. **Configure auth** (see next section) — both the Fulcra read auth and the
   facade token.
7. **Save / Publish** (private to you is fine; see the auth caveat for sharing).
8. **Smoke test.** In the preview, ask "what am I working on?" (confirm a read),
   then report a milestone and confirm the GPT calls `reportMilestone` and gets
   back a `task_id`.

## Auth

Per the spec finding: **prefer OAuth, fall back to a shared API key only for a
strictly private single-user GPT.** The schema declares both
(`fulcraOAuth` and `fulcraBearer`).

- **OAuth (recommended).** In the Action's **Authentication** → **OAuth**, wire
  the Fulcra Auth0 endpoints. The schema's defaults
  (`authorizationUrl: https://fulcra.us.auth0.com/authorize`,
  `tokenUrl: https://fulcra.us.auth0.com/oauth/token`, scopes
  `openid profile email offline_access`) come straight from `core.py`'s OIDC
  config — but **confirm the client_id / callback registration with the Fulcra
  Auth0 tenant before relying on it** (see open question). Once set, ChatGPT
  shows "Sign in to fulcradynamics.com"; thereafter it sends
  `Authorization: Bearer <user-token>` on every call — exactly the header
  core.py uses. This gives per-user identity and no shared secret in the GPT.
- **API key (fallback).** Action **Authentication** → **API Key** →
  **Auth Type: Bearer**, paste a Fulcra access token. OpenAI encrypts it at
  rest, but it is a **single shared credential** baked into the GPT — acceptable
  only if the GPT is private to you. **Do not share a GPT configured this way.**

**Facade auth (for the write ops).** The `reportMilestone` and
`getCoordinationStatus` operations use a *separate* shared bearer token
(`facadeBearer`) — the `FULCRA_COORD_FACADE_TOKEN` on the facade host, **not** a
Fulcra token. The facade uses the host's own `fulcra-api` credentials to reach
Fulcra. Since a Custom GPT Action applies one auth config per server, host the
facade on its own domain and set its auth to **API Key → Bearer** with that
token. See `facade/README.md` for the full two-leg auth model.

## Limitations (read these)

- **No deterministic hooks.** ChatGPT has no start/end/compaction lifecycle
  event. The start-of-session status read and milestone reporting are
  **model-chosen and best-effort**, driven only by the Instructions. If the
  model skips the start read, ask "what am I working on?" to force it.
- **Writes need the facade deployed.** Milestone writes go through
  `reportMilestone`, served by the facade (`facade/`). If the facade isn't
  deployed/reachable the write fails and the GPT falls back to milestone-as-
  prose. Deploying the facade (a running service on a Fulcra-authed host
  reachable by ChatGPT) is the remaining operational step.
- **Reconciler is a backstop, not the only write path.** Because ChatGPT can't
  park a task on exit, the server-side reconciler still re-flags a stale
  `active` task this GPT left behind — but with the facade, a durable milestone
  no longer *depends* on the reconciler.
- **Scheduled Tasks** can approximate a daily "warm me a status" but cannot be
  relied on as a write backbone, and may not see the Action in an unattended
  session (validate before depending on it).
- **Manual paste is the floor.** With no Action/auth at all, pasting
  `fulcra-coord status` output into the chat still gives the model usable
  context.

## Open question (carried in the spec)

Does `api.fulcradynamics.com` expose an OAuth flow a Custom GPT Action can drive
end-to-end (a registered client_id + ChatGPT's callback URL allowed in the Auth0
app), or only the CLI's own device-code login? If only the latter, OAuth here
needs a small registration step (not new infra). This single answer also gates
the future **write facade**, which is the remaining design decision.
