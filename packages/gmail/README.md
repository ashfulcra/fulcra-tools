# fulcra-gmail

The local **Gmail relay** for Fulcra: a read-only, multi-account Gmail poller
that runs entirely on the operator's machine. It authorizes N Gmail accounts
(Google OAuth, `gmail.readonly` only), and — in later tasks — polls each with
local filter rules, lands selected emails in Fulcra Files behind an append-only
privacy ledger, and relays matches to an agent over the coord bus. Nothing
leaves the machine except the artifacts the operator's rules choose.

> Credit: the original design is **ArcBot's** (openclaw). Its June MVP was
> unrecoverable; this is a clean-room rebuild on current `main` that preserves
> ArcBot's architecture.

## Multi-account model

Accounts are a first-class dimension keyed by a stable, **opaque `account_id`**
(uuid4 minted at add-time). The email address is registry **metadata** — never a
keychain-key or Files-path segment. One dead account is fail-soft: it is marked
`auth_failed` and skipped; other accounts keep polling.

## What Task 1 ships (this slice)

| Module | Surface |
|---|---|
| `fulcra_gmail.client` | `GmailClient(account_id, *, registry, transport=None)` — `list_message_ids(q)` (fully paginated, follows `nextPageToken` to exhaustion, no order assumption), `get_message(id, format="full")`, `get_profile()`. Refresh-on-401; a `400 invalid_grant` on refresh marks the account `auth_failed` and returns fail-soft (`[]`/`None`) without raising. Gmail REST v1 base `https://gmail.googleapis.com/gmail/v1`. No history API. Module helpers: `generate_pkce`, `build_authorize_url`, `exchange_code`, `refresh_access_token`, `fetch_profile`. |
| `fulcra_gmail.accounts` | `AccountRegistry(*, store=None, keychain=None, transport=None)` — opaque `account_id` ↔ email; shared OAuth client + per-account refresh tokens in the OS keychain (via collect's `credentials`); non-secret registry rows + the nonce setup-session map in a JSON doc. Ops: `set_client_credentials`, `begin_add_account`, `complete_add_account`, `list_accounts`, `get_account`, `find_by_email`, `get_refresh_token`, `set_status` / `mark_auth_failed`, `remove_account`. |

### B4 — OAuth `state` + account binding

The OAuth `state` param is an **unguessable single-use nonce** (10-min TTL), not
an account label. `complete_add_account` consumes it exactly once, atomically;
**missing / mismatched / replayed / expired all reject with no token stored**.
Only after a valid consume does it exchange the code, then **discover** the
authorized address via `users.getProfile` and name the registry + keychain from
*that* address — never from an operator hint. A re-auth of a known address
rotates the token in place (no duplicate row); a new address mints a new
`account_id`.

## Auth setup (operator)

Create one **Internal / Web** OAuth client in the Workspace Cloud console (single
redirect `http://127.0.0.1:9292/api/oauth/callback`), paste the client-id/secret,
then run "Add account" once per Gmail account. Scope is `gmail.readonly` only —
never modify/send/delete.

## Develop

```
uv sync --all-packages --all-extras
uv run pytest packages/gmail -q
uv run ruff check packages/gmail
```

All tests use synthetic ids/emails/tokens with a fake httpx transport + a fake
keychain — no network, no real secrets, PII-grep clean.
