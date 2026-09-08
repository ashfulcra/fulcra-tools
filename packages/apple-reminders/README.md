# Apple Reminders for Fulcra Collect

Choose the Reminders lists you want in Fulcra. Collect copies their tasks into
`vault/tasks/apple-reminders/` and checks for changes every five minutes while
your Mac is running. Complete an ordinary task in Reminders and its Fulcra copy
becomes resolved. Resolve the Fulcra copy and Collect completes it in Reminders.

In development for [Collect 0.1.2](https://github.com/ashfulcra/fulcra-tools/pull/757).
This package is not in the current Mac download. The steps below describe the
candidate's setup; native permission and live completion validation remain open.

## Set up

1. Open Reminders and give iCloud time to download your lists.
2. In Collect, choose **Apple Reminders → Set up**.
3. Click **Allow access** and approve the macOS Reminders prompt. Opening the
   setup screen checks permission but does not ask for it or start a sync.
4. Check the lists to sync. Nothing is selected for you. Read-only lists cannot
   be selected.
5. Leave **Preview only** on and choose **Enable & run preview** to check without
   uploading or changing reminders. When ready, open **Configure**, turn preview
   off, and choose **Enable & start sync**.

macOS grants access to Reminders as a whole. Collect applies your list selection
before fetching tasks for import. Removing a list or disabling the plugin stops
future uploads and completion writes; it leaves existing Fulcra copies alone.

## What syncs

Titles, notes, due dates, and completion state flow from Reminders to Fulcra.
Only completion flows back. Change titles, dates, lists, recurrence, and task
contents in Reminders. Deleting a reminder never counts as completing it, and
Collect does not delete your Fulcra copy.

Recurring reminders are imported, but must be completed in Reminders. The source
has no atomic check that a completion still refers to the occurrence a bot saw.
Completing automatically could advance the next occurrence by mistake.

Source reopening starts a new sync generation. Changing a Fulcra status back to
`open` does not reopen a completed reminder. A full Apple calendar sync can change
source identifiers; that can create a new Fulcra copy of an existing reminder.
Missing old identifiers never authorize a write.

## For agents resolving tasks

Read the latest file from `vault/tasks/apple-reminders/`. Change its frontmatter
`status` from `open` to `resolved`, preserving the source identity, generation,
and owned-section markers. Upload the updated file to the same path. Collect
checks the current source task before completing it. It rejects missing or
changed identities, malformed documents, stale generations, and recurring tasks.

Text outside the imported section and unrelated frontmatter survive ordinary
updates. Edit the source details in Reminders; edits inside Collect's owned
section can be replaced on import. See the [shared task sync contract](../task-sync/README.md).

## Troubleshooting

**No lists appear:** confirm Reminders access for Collect in macOS privacy
settings, open Reminders, and retry discovery. A denied or failed read is an
error, not an empty successful import.

**A change has not arrived:** the Mac must be awake, connected, and running
Collect. Confirm the list is still selected and preview is off, then use
**Run Now**. Runs save progress; a large library may need more than one run.

**A task did not complete:** check that it is an ordinary reminder in a writable,
selected list. A source edit can invalidate a pending completion. Refresh the
Fulcra copy before resolving it again. Keep private task contents and local
reports out of public issues.

## Development

From a checkout with the workspace environment installed:

```sh
uv run --package fulcra-apple-reminders --extra dev pytest packages/apple-reminders/tests -q
```

The provider uses an isolated EventKit process with bounded fetches and timeouts.
The Mac bundle includes the Reminders usage descriptions and routes
`--reminders-bridge` before loading the menu-bar UI. Tests use synthetic EventKit
objects and never complete personal reminders.

### Fulcra account changes

Interactive Fulcra sign-in/sign-out blocks task writes throughout the account
transition and invalidates workers started during it. If the token change or final
configuration save fails, writes stay blocked until an interactive retry succeeds.
Automatic token refresh does not clear that protection.

Settings and account changes wait for an active task write to finish. Each new
write checks your current selection and consent while holding a shared lock, so
an older worker cannot start another write after the change takes effect.
