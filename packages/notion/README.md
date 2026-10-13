# Notion pages for Collect

Copies the Notion pages you choose into Fulcra Files so your agents can read
them. It leaves Notion alone. This is part of the unofficial, unsupported
Fulcra Tools collection; the source is available, but the published Collect
0.1.3 installer does not include this plugin yet.

The `notion-pages` plugin checks every six hours while Collect is running.
No pages are selected for you, and **Preview only** starts on. Turn it off
when you want uploads. Copies go to `vault/imports/notion/<page-id>.md`.
An unchanged page is skipped; a changed page replaces its Fulcra copy. Removing
a page from your selection, deleting it in Notion, or disconnecting the plugin
does not delete an existing Fulcra copy.

## Set it up

This version needs an internal Notion connection. A workspace owner must create
it. If that isn't you, the owner needs to do the first three steps. A public
"Connect Notion" sign-in flow is not included yet.

1. Open the [Notion Developer portal](https://www.notion.so/profile/integrations)
   and create an internal connection for the workspace containing your pages.
   Give it **Read content** access. Leave content updates, inserts and user
   information permissions off.
2. Open the connection's **Configuration** tab and copy its API token.
3. In Notion, open each page you want to copy. Click **••• → Add connections**
   and select your connection. Giving the connection access to a parent may
   also grant access to children; Collect still reads only the page roots you
   explicitly choose and their ordinary nested blocks.
4. In Collect's plugin dashboard, choose **Notion pages → Set up**. Paste the
   token into **Notion connection token**. Paste a page link on each line under
   **Pages to copy**. Keep **Preview only** on.
5. Run **Check the selected pages**. You should see the selected page titles;
   this step uploads nothing. Then choose **Enable & run preview**.
6. When the preview is right, turn **Preview only** off in the plugin settings
   and run the plugin again. Sign in to Fulcra in Collect first.

Collect stores the token in Keychain and page selections in its private local
configuration. Don't put either in this repository. Setup follows Notion's
[authorization guide](https://developers.notion.com/guides/get-started/authorization)
and [connection capabilities](https://developers.notion.com/reference/capabilities).

## What gets copied

Page titles and ordinary text, headings, lists, checkboxes, code, quotes,
callouts, tables and columns become Markdown. Rich text becomes plain text;
formatting and links are not reproduced exactly. Tables retain their cell text
but do not preserve Notion's layout.

Child pages, linked pages, databases, synced blocks, attachments, images and
embeds get an omission marker. Collect does not follow those references or
download attachment URLs. To copy a child page, select its own page link too.
Other page properties, comments, relations, users and workspace search are not
read. A connection with broader permissions does not expand Collect's selection.

Reads use Notion API version `2026-03-11` at its fixed API origin. Pagination
and ordinary nested blocks follow the
[block children API](https://developers.notion.com/reference/get-block-children).
This is a page-content snapshot, not a full Notion backup.

## Limits and failures

Each run allows up to 50 selected pages, 5,000 blocks per page, 10,000 blocks
total, 16 levels of nesting, 1,000 requests and ten minutes of Notion reads.
Responses are limited to 2 MiB each and 50 MiB total; rendered pages to 2 MiB.
Requests are spaced at least 0.35 seconds apart. A rate-limit response stops
the run rather than retrying immediately. Earlier successfully uploaded pages
keep their progress; the failing page's hash is never saved.

Setup checks also stop if the page selection, connection or account transition
changes during the check, even before the plugin is enabled.

A missing, deleted, inaccessible, malformed or oversized page fails the run;
it is not silently treated as an empty page. Missing text/checkbox fields and
inconsistent pagination fail rather than replacing a good copy with incomplete
content. Notion and transport errors omit
provider response text, tokens and private paths. Unsupported content gets a
marker rather than a claim that the copy is complete.

Uploads use Collect's own signed-in Fulcra account. A missing or expired sign-in
requires reconnecting in Collect; the plugin does not fall back to a CLI
account. Hashes are scoped to the destination account. Disabling the plugin,
changing its page selection, credentials or preview mode, or switching the
Fulcra account revokes pending uploads. A shared configuration lease lets an
upload already in progress finish before a settings change is saved.

## Run from source

From the repository root:

```bash
uv sync --all-packages --extra dev --extra macos
uv run --all-packages --extra dev --extra macos pytest packages/notion/tests
uv run --all-packages --extra macos fulcra-collect daemon
```

On Linux, omit `--extra macos`. The page reader needs no Apple APIs; credential
storage still needs a working system keyring.

Synthetic tests cover selection, pagination, nested blocks, omission markers,
limits, previews, upload failures, account changes and revoked selection. Live
Notion access and a signed installer containing this plugin still need to be
verified before release. Tests do not require a personal account.
