# Todoist for Fulcra Collect

Choose the Todoist projects you want in Fulcra. Collect copies their tasks to
`vault/tasks/todoist/` and checks for changes every five minutes while your Mac is
awake, online, and running Collect. Complete an ordinary task in Todoist and its
Fulcra copy becomes resolved. Resolve the Fulcra copy and Collect completes the
Todoist task.

Included in [Collect 0.1.2 for Mac](../../docs/collect.md#get-started-new-user).
Todoist tests
use synthetic responses; discovery and completion have not been verified with a
live Todoist account.

## Set up

1. In Collect, choose **Todoist → Set up**.
2. Open Todoist on the web. Click your avatar, then **Settings → Integrations →
   Developer → Copy API token**. See Todoist's
   [API token instructions](https://www.todoist.com/help/todoist/integrations/find-your-api-token-Jpzx9IIlB).
3. Return to Collect and paste the token into **Todoist API token**. Collect stores
   it in your OS Keychain. No developer account, OAuth application, or checkout is
   needed to connect through the app.
4. Check the **Projects to sync**. Nothing is selected for you. Projects reported
   as read-only cannot be selected.
5. Leave **Preview only** on, then choose **Enable & run preview**. Preview reads
   and checks tasks without uploading files, completing Todoist tasks, or saving
   sync checkpoints.
6. When ready, open **Configure**, turn preview off, and choose **Enable & start
   sync**.

Opening setup or discovering projects does not start a sync. Removing a project
or disabling the plugin stops future imports and completion writes. Existing
Fulcra copies and Todoist tasks are left in place.

## What syncs

Titles, descriptions, due dates, and completion state flow from Todoist to Fulcra.
Only completion flows back. Manage titles, descriptions, dates, recurrence,
project membership, and deletion in Todoist. Changing a Fulcra task back to
`open` does not reopen it in Todoist. A source reopening starts a new sync
generation so an older resolution cannot complete the reopened task.

Recurring tasks are imported, but must be completed in Todoist. The API cannot
atomically check that a completion still refers to the occurrence Collect read;
an automatic completion could otherwise advance the next occurrence.

To resolve an imported task, read its latest Fulcra file and change frontmatter
`status: open` to `status: resolved`. Preserve its source identity, generation,
and imported-section markers, then upload to the same path. Collect checks the
current source task and selection before completing it. Unrelated frontmatter
and text outside the imported section survive ordinary updates. Details are in
the [shared task sync contract](../task-sync/README.md).

## History and limits

Each run reads all pages of selected active tasks and the last **30 days of
explicit completion history**. A missing task, deletion, failed request, or
incomplete page set never counts as completion. If a page cannot be read, the
snapshot fails and does not authorize completion writes.

Current active tasks take precedence over older completion history. Collect
also saves the last observed active update under the verified Todoist account
and task identity, rejecting older completion evidence after a restart. Those
observations are saved only after a complete snapshot, never during preview.

Completion history proves that a completion happened; it does not prove that
Collect saw every later transition. A task reopened and deleted entirely
between polls cannot be reconstructed reliably. Offline gaps longer than 30
days can also leave completions unknown. Review those cases in Todoist rather
than treating a missing Fulcra update as proof of the source status.

Reads have request, page, response-size, and time limits. Exceeding a limit is an
error, not a successful empty result. Completion retries reuse the same command
UUID and require Todoist's per-command confirmation. Todoist does not document
how long it retains those UUIDs, and fresh revision checks cannot eliminate
every race with another client.

## Connection and privacy

The token stays in the OS Keychain. Task details are read from Todoist and copied
to your Fulcra Files account when preview is off. Local sync checkpoints stay in
Collect's private plugin store and are isolated by account. Error reports omit
API response bodies and tokens. Do not paste tokens, private task contents, or
local sync state into public issues or repository fixtures.

If projects do not appear, check the token and retry discovery. If a change has
not arrived, confirm that Collect is running, the project is selected, and
preview is off, then use **Run Now**. For a completion conflict, refresh the
Fulcra copy and check whether the task changed or is recurring before resolving
it again.

## Development

From the repository root with the workspace environment installed:

```sh
uv run --package fulcra-todoist --extra dev pytest packages/todoist/tests -q
```

Tests use synthetic HTTP transports and private-state substitutes; they require
no live account and make no real source writes. Provider coverage includes
pagination failures, account identity, missing and deleted tasks, reopening,
recurring-task refusal, revision conflicts, uncertain retries, command-level
errors, stored observations across restarts, and preview behavior.

### Fulcra account changes

Interactive Fulcra sign-in/sign-out blocks task writes throughout the account
transition and invalidates workers started during it. If the token change or final
configuration save fails, writes stay blocked until an interactive retry succeeds.
Automatic token refresh does not clear that protection.
