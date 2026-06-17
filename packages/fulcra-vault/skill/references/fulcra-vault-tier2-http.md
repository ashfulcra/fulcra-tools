# Tier-2: HTTP-only access

For agents that can make HTTP requests but cannot run shell commands. The vault
is plain markdown under `/vault` in Fulcra Files, so the Files API is the whole
interface.

## Authenticate (device flow)

1. POST to the Fulcra device-authorization endpoint to get a `device_code` and a
   `user_code`; show the user the verification URL and code.
2. Poll the token endpoint with the `device_code` until the user approves; you
   receive an access token.
3. Send `Authorization: Bearer <token>` on every request. NEVER print or store
   the token.

(The CLI does this for you via `fulcra auth login`; tier-2 agents replicate it
against the same endpoints the Fulcra SDK uses.)

## Read

- List the vault: GET the Files listing for the `/vault` prefix.
- Read a note: GET `/vault/<Note Title>.md`.
- Session start: GET `/vault/HOT.md` and prepend it to your context. Empty or
  404 → no vault yet; tell the user to onboard from a CLI-capable agent.

## Write

Writing safely requires the same discipline the CLI enforces, by hand:

1. GET the note and its current version/stat.
2. Edit only the bytes inside your owned section
   (`<!-- section:<slug> owner:<agent> -->` … `<!-- /section:<slug> -->`), or
   append one timestamped line under `## Log`.
3. Re-check the version; if it changed since your GET, abort and retry.
4. PUT the full updated markdown back to `/vault/<Note Title>.md`.
5. Append one line to `/vault/LOG.md` recording the write.

Derived files (`MAP.md`, `HOT.md`, `.index/links.json`) are rebuilt by the CLI's
`reindex`/`map`; a tier-2 agent should not hand-maintain them. Leave a note in
the log so a CLI-capable agent refreshes them later.

Keep frontmatter flat (scalars and scalar lists) and validate it round-trips
before PUTting — a malformed note breaks later section edits.
