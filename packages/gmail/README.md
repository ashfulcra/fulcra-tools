# fulcra-gmail

The local **Gmail relay** for Fulcra. It runs on the operator's machine, polls
their authorized Gmail accounts read-only, and for each email that matches a
local filter rule it performs the configured actions. The usual rule writes
the selected email to the operator's Fulcra Files and optionally posts a pointer to
that email on the operator's coord bus so an agent can act on it. Gmail queries
and authentication necessarily contact Google; selected message content is
uploaded to Fulcra, and optional AI rule suggestions have a separate opt-in.

Access is read-only (`gmail.readonly`); the relay never sends, modifies, or
deletes mail.

This is an [unsupported monorepo experiment](../../README.md). Read-only Gmail
access protects the mailbox from modification; it does not make a rule that
uploads too much mail a good rule.


## Use with Collect

Gmail is included in the [Mac installer](../../docs/collect.md#get-started-new-user).
Choose **Gmail → Set up** in the dashboard. It requires a Google OAuth client as
well as account sign-in, so there is more setup here than for a local Notes
import. Follow [the OAuth client instructions](#one-time-create-the-oauth-client)
below, then connect accounts and configure the rules that select messages.
Installing Collect alone does not connect Gmail or choose what to upload.

## How it works

A scheduled collect plugin polls every 15 minutes. Each poll walks every
authorized account against every applicable rule and, per pair:

1. **Search.** Runs the rule's Gmail query (`messages.list`), fully paginated.
2. **Refine.** Fetches each candidate and applies the rule's local post-filters
   (`from_regex`, `subject_regex`, `has_attachment`). Only messages that pass
   are *effective matches*; candidates that fail leave no trace anywhere.
3. **File.** Writes each effective match to Fulcra Files at a deterministic
   path, then records it in an append-only per-account ledger.
4. **Relay.** If the rule's actions include `relay`, posts a coord-bus directive
   naming the match — after the file step, so the pointer always resolves.

The first poll searches the last seven days by default; a rule's `backfill`
setting can widen that window. Later polls overlap the cursor by 24 hours.
The cursor advances only through the contiguous run of fully-processed messages,
so a crash or a transient failure re-processes the affected message on the next
poll rather than skipping it. Files writes are idempotent by path; relay
directives are deduplicated by a deterministic key, so retries converge on one
directive rather than a fan-out.

Accounts are keyed by an opaque `account_id` (a uuid minted when the account is
added). The email address is metadata — never a path segment or a keychain key.
One account failing auth is fail-soft: it is marked `auth_failed` and skipped
while the others keep polling.

## For operators

### One-time: create the OAuth client

Create a single OAuth client in the Google Cloud console. Use **User Type:
External** and application type **Desktop app**, then publish the app and add the
`gmail.readonly` scope. Two choices matter:

- **External**, not Internal, so both Workspace and personal `@gmail.com`
  accounts can authorize. For a personal-use client, the wizard describes
  publishing to **In production** while unverified. Google shows an unverified
  app warning and imposes a **100-new-user cap**. That is a limit, not blanket
  permission to distribute an unverified app: review Google's
  [audience rules](https://support.google.com/cloud/answer/15549945?hl=en) and
  [restricted-scope verification requirements](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification)
  for your deployment. Security-assessment requirements depend on how restricted
  data is accessed, stored, and transmitted.
- **Desktop app**, not Web application, because the relay is a local desktop
  program. A Desktop client has **no redirect URI to register** — Google
  auto-allows the loopback `http://127.0.0.1:9292/api/oauth/callback` the relay
  uses. Google also treats a Desktop client's secret as non-confidential (it is
  meant to be embedded in distributed software), which is what lets one shared
  client serve many installs.

The setup wizard carries the exact click path. Paste the client id and secret
into the wizard when prompted.

> One-time reveal: copy the client secret from the console's copy button rather
> than transcribing it. If it is lost, add a second secret on the same client —
> Desktop clients support secret rotation.

### Per account: add it

Run the wizard's **Add account** step (or open
`http://127.0.0.1:9292/api/oauth/gmail/add-account/start`). Pick the Google
account, click through the unverified-app warning, and grant `gmail.readonly`.
The relay discovers the authorized address from the token itself, so the account
is bound to whatever you actually approved. Re-authorizing a known address
rotates its token in place; a new address is added alongside the others.

If you switch the OAuth client (for example from a Web to a Desktop client),
every account must re-authorize once — refresh tokens are bound to the client
that issued them.

### Build a rule

Open the rule builder at `http://127.0.0.1:9292/api/gmail/rules/ui` (reachable
from the Collect dashboard's Gmail plugin). The builder is example-first:

1. Search a bound account with a Gmail query (`from:otter.ai`, `subject:invoice`,
   `receipt`, and so on).
2. Mark results **✓ should match** or **✗ should not** in the results list.
3. Click **Derive rule**. The builder computes the traits your ✓ examples share
   and your ✗ examples don't — sender, sender domain, mailing-list id, a shared
   subject keyword, whether all have attachments — and offers them as editable
   chips. (An optional **Suggest with AI** button proposes a rule from the
   labeled examples; it is opt-in and shows what it sends to the model: sender,
   subject, and snippet
   from the labeled examples. It needs the optional `ai` dependency and a
   configured model backend.)
4. Read the preview: how many recent messages the draft matches, whether your ✓
   examples are caught, and whether any ✗ examples slip through. Tighten the
   chips until it reads clean.
5. Choose actions — **file**, and optionally **relay** to a named agent — give
   the rule a name, and save.

Saved rules appear in a list on the same page; edit, duplicate, enable, disable,
and delete them there. The relay polls on its own from then on. Editing a rule's
matching criteria bumps its version and starts a fresh processed set, so the
new version starts with the rule's first-run backfill window. Previously filed
messages in that window can be processed again under the new version.

### Route matches to an agent (relay)

Filing puts every match in your Files; relaying additionally pings an agent when
a new match lands. To turn it on:

1. Set the plugin's **coord relay team** (`relay_team`) — the coord team the
   directive is posted to.
2. On the rule, add `relay` to its actions and set `relay_to` to the recipient
   agent's identity.

For a synthetic example, set `relay_team = "example-team"` and give a rule
`actions = ["file", "relay"]` with `relay_to = "example-assistant"`. Replace
both names with your own team and recipient. The recipient finds the pointer
in its coord inbox. Keep `file` in the action list when the agent needs the
selected-email artifact.

Relay runs during polling. Adding the action can relay already-filed messages
encountered in the overlap window; changing matching criteria starts a fresh
rule version and backfill window. It is not a full historical replay command.
The pipeline reads back the directive before marking its relay action complete,
but the recipient still needs its normal queue read and reconciliation to act
on it. **A required relay with no working backend remains incomplete and holds
the cursor**; set `relay_team` and install/configure coord-engine, or use a
file-only rule.

### Privacy

Search and preview fetch messages from Google; filtering and deterministic rule
derivation happen locally. The builder does not write those previews to Fulcra.
Optional AI suggestions send the consented example fields to the configured
model provider. The
plugin's own logs carry only opaque ids, rule ids, and decision reason-codes —
never an email subject, address, or body. The selected-email JSON written to
Fulcra Files is stored **in clear** (readable JSON) in your own Fulcra account;
if you need it opaque at rest, encrypt-on-write is a documented follow-up, not
the current behavior.

## For agents

When a rule fires, the relay writes the selected email to Fulcra Files and —
if the rule includes the `relay` action — posts one directive to the coord bus.
There are correspondingly two ways to consume the relay.

### Pull: read the Files directly

Every effective match lands at a deterministic path in the operator's Fulcra
Files:

```
/collect/gmail/<account_id>/<yyyy-mm>/<message_id>.json
```

`<yyyy-mm>` is derived from the message's own timestamp, not the poll time;
missing or unparseable dates use `undated`. Any
agent authorized to the operator's Fulcra account can list and read these:

```
fulcra-api file list      /collect/gmail/<account_id>/<yyyy-mm>
fulcra-api file download  /collect/gmail/<account_id>/<yyyy-mm>/<message_id>.json  out.json
```

With a `file` action, each file is the selected email as JSON:

```json
{
  "message_id": "…",
  "thread_id": "…",
  "headers": { "From": "…", "To": "…", "Subject": "…", "Date": "…" },
  "bodies": { "text/plain": "…", "text/html": "…" },
  "attachments": [ { "filename": "…", "mimeType": "…", "size": 12345, "attachmentId": "…" } ]
}
```

Attachments are metadata only — filename, type, size, and Gmail's opaque
`attachmentId` — never the bytes.

The path convention is stable, so an operator can point an agent at
`/collect/gmail/…` and let it list-and-read on whatever schedule suits it. This
works with no relay configuration.

### Push: receive a bus directive

If a rule's actions include `relay` and the plugin has a `relay_team`
configured, each new effective match posts a directive to that team, addressed
to the rule's `relay_to` agent. The directive is a claim check, not the email:
its title carries an opaque `outbox_key` and it points at the Files path. It
carries **no** subject, address, or body — the content stays in Files, and the
agent fetches it from there.

The receiving agent sees the directive in its coord inbox
(`coord-engine inbox <team> --agent <id>`); no polling of Gmail or prior
knowledge of the relay is needed. Delivery is exactly-once-visible: the
directive's identity is a deterministic function of the match, so a
crash-and-retry converges on a single directive rather than a duplicate. As
noted above, a freshly-emitted directive becomes inbox-visible after the bus's
normal propagation and index refresh, so an agent watching for relayed work
should treat "not there yet" as "check next tick," not "nothing came."

To wire it up, an operator sets the plugin's `relay_team` and adds `relay` +
`relay_to` to the rule (see *Route matches to an agent* above).

## Module map

| Module | Responsibility |
|---|---|
| `client` | Per-account Gmail REST client: `list_message_ids(q)` (paginated), `get_message(id)`, `get_profile()`. Refresh-on-401; `invalid_grant` marks the account `auth_failed` fail-soft. |
| `accounts` | `AccountRegistry`: opaque `account_id` ↔ email; shared client + per-account tokens in the OS keychain; the B4 add-account flow (single-use `state` nonce, `getProfile` binding). |
| `rules` | Parse and validate rules; build the server query; apply post-filters to decide effective matches with privacy-safe reason codes. A `relay` action without `relay_to` is rejected at parse time. |
| `convert` | Gmail payload → the selected-email JSON above. |
| `ledger` | Append-only per-account JSONL; processed-set keyed by `(message_id, rule_id, rule_version)`; deterministic relay outbox key. |
| `cursors` | Per-`(account, rule)` contiguous-frontier watermark. |
| `files_writer` | Writes the selected email to the deterministic Fulcra Files path. |
| `relay` | Builds and posts the exactly-once-visible bus directive, then reads it back before the pipeline marks it done. |
| `pipeline` | The crash-safe poll: paginate → refine → order oldest-first → file → ledger → relay → ledger → advance the watermark. |
| `collect_plugin` | The scheduled plugin: setup wizard, per-account health, poll loop, `relay_team` config. |
| `collect_routes` | The Gmail-specific add-account OAuth endpoints (start + callback). |
| `rules_routes` / `rules_derive` / `rules_preview` / `rules_ai` / `rules_ui` | The rule builder: JSON endpoints, deterministic derivation, live preview, opt-in AI, and the served page. |

## Develop

```
# From the repository root after workspace setup:
uv run --package fulcra-gmail --extra dev pytest packages/gmail/tests/ -q
uv run --package fulcra-gmail --extra dev ruff check packages/gmail
```

Tests use synthetic ids, emails, and tokens with a fake httpx transport and a
fake keychain — no network, no real secrets, PII-grep clean. The operator setup
prose here, the wizard click path, and the `AGENTS.md` entry must agree; this is
asserted by [test_operator_docs_agree.py](tests/test_operator_docs_agree.py).
Use the [Collect source setup](../collect/README.md#running-from-source) for
workspace installation. The package registers the `gmail` plugin; it has no
standalone Gmail CLI.
