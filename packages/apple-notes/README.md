# Apple Notes for Fulcra Collect

Your notes can be useful to an agent without moving your writing out of Apple
Notes. This macOS Collect plugin copies notes and available attachments into
your Fulcra vault and checks for changes every six hours. Normal import leaves
the originals unchanged. It is one of the [monorepo's unsupported
experiments](../../README.md), with an ordinary one-way import path and a much
more experimental writeback path.

Apple Notes ships in the [current Mac beta](../../docs/collect.md#plugin-status).
The setup below enables one-way import. Experimental writeback has separate
controls and is described under [Advanced modes](#advanced-modes).

## Set up

1. [Install Fulcra Collect](../../docs/collect.md#get-started-new-user) and sign in.
2. Open **Notes** on your Mac and let iCloud finish downloading your notes.
3. In Collect, choose **Apple Notes → Set up**.
4. Follow the **Full Disk Access** step. Add **Fulcra Collect** from Applications
   in **System Settings → Privacy & Security → Full Disk Access**, restart
   Collect, then click **Verify access**.
5. Leave **Preview only** off to upload, then choose **Enable & start sync**.
   To check without uploading, turn on **Preview only** and choose **Enable & run preview**.

Find your copies in `vault/notes/apple/` in your Fulcra account. Large libraries
can need several runs. Each run saves progress before stopping; **Run Now** resumes.
Apple Notes is included in the Mac app; no separate plugin installation is needed.
The wizard's **Next**, **Back**, and **Skip** buttons do not start an import.
Only the explicit enable action starts the chosen sync or preview mode.

## What is preserved

- Notes deleted in Apple Notes are marked in the vault and retained.
- The imported body lives between `<!-- section:apple-note owner:fulcra-collect/apple-notes -->`
  and `<!-- /section:apple-note -->`. Text outside those markers and unrelated
  frontmatter fields survive updates. Edits inside the imported section can be
  replaced by Apple Notes changes.
- If you remove the markers, Collect refuses to overwrite that file.
- A failed database read or body decode does not replace good content with an
  empty note. The database reader uses a consistent SQLite backup.
- Attachments without available files, including some tables and drawings,
  appear as unsupported attachments. Previously uploaded links are retained
  if a local file temporarily becomes unavailable. Files above 25 MB are skipped.
- Attachment content fingerprints detect changes even when the file size stays
  the same. An upgrade from the earlier prototype may upload attachments once
  to establish these fingerprints.

## Account and consent (development branch)

The [migration branch](../../docs/collect-v1.md) now uses
[`CollectNotesFiles`](fulcra_apple_notes/collect_io.py) with Collect's captured
account, source epoch and raw settings. It does not inherit the default CLI
account or change process-wide credentials. API requests recheck the current
Collect principal; same-account silent refresh is allowed. Signed storage URLs
use a separate client without Fulcra credentials. Unknown/partial Files listings
are errors, never evidence of missing state. The shared
[HTTP completeness checks](../collect/README.md#files-read-completeness) reject
partial status/headers and malformed or contradictory totals before Notes can
write follow-on files or checkpoints.
They also compare downloaded bytes with the exact version's size when provided,
before decoding or merging. A short HTTP 200 cannot silently drop text outside
the imported owner fence. Malformed sizes fail; older size-less listings remain
supported without that independent length proof.

Sync, reconciliation and experimental writeback pass this adapter explicitly.
Source guards run around snapshots, between Notes/attachment reads, per note,
and during attachment hashing. Remote effects and native writeback hold the
account/configuration lease through a final consent check and the operation.
Disabling the source or changing its account/settings prevents further effects;
an already-entered leased operation finishes before a supported transition.
Revoking writeback opt-in is checked again at the actual AppleScript mutation.
Attachments are hashed and uploaded from the same private temporary snapshot;
a changed live file cannot be uploaded under a stale content hash. MIME detection
continues to use file contents, including when its extension is misleading.

Local-only sync preview needs no Fulcra auth and contacts no cloud service, but
still honors source revocation. Reconciliation/writeback read cloud state even
in preview, so they require Collect sign-in. Reconciliation checkpoint scope
includes the destination account, source container and exact sync baseline.
Files paths, ownership fences, attachment limits and additive deletion behavior
are unchanged. This remains a Files-only plugin; it creates no Event/Metric
destination. These changes have not shipped in the released 0.1.4 app; live
library, cutover and packaged-app checks remain migration gates.

## Troubleshooting

**Access is not verified:** open Notes first, confirm Full Disk Access for
Collect, then restart Collect. A source installation needs access for the
Python executable running the daemon instead of the downloadable app.

**Nothing has uploaded:** confirm Fulcra sign-in, that the plugin is enabled,
and that **Preview only** is off. Use **Run Now** and inspect its result.

**A large import stopped:** a run has a ten-minute work budget. Saved progress
is resumed on the next run. Keep the Mac awake and connected during import.

Run reports are local files under `~/Library/Logs/fulcra-collect/`:
`apple-notes-last-run.json` and `apple-notes-runs.jsonl`. These reports can
contain private identifiers and errors; do not post them unredacted to GitHub.

**Manually removed a vault copy:** the current importer may leave it absent
until the source note changes. It does not yet offer a full repair pass.

## Advanced modes

The default setup is one-way import. `mode = "reconcile"` in
`[plugin_settings.apple-notes]` reports changes on either side without writing.
Reconciliation reads each tracked vault note to detect edits, including those made
soon after a sync. Large scans save private local progress and stop before the
worker timeout; run again to continue. Reports live in `apple-notes-reconcile.json`
in the log directory. Check `complete` and `notes_remaining` to distinguish a
finished scan from a saved checkpoint. The report lists up to 200 changes with
the full total; experimental writeback requires a complete scan and evaluates
the complete list. Checkpoints contain content hashes, not note bodies, and are
cleared after completion so the next scan checks for new edits.

AppleScript writeback is experimental and excluded from the setup wizard.
It requires `mode = "writeback"` **and** a separate `writeback_enabled = true`;
`dry_run = true` previews the plan. Do not enable actual writes on valuable
notes without testing. Writeback refuses conflicts, missing ownership markers,
and notes with any attachment, including inline tables or drawings. macOS
Automation permission is also required; a timeout alone does not prove why
AppleScript failed. Partial write failures mark the run failed and are recorded
in the local writeback report.

## Development

From a repository checkout with the workspace environment installed:

```sh
uv run --package fulcra-apple-notes --extra dev pytest packages/apple-notes/tests -q
```

The tests use synthetic Notes stores. Keep personal library sizes, note titles,
contents, paths, account identifiers, and real exports out of commits and PRs.
There is no standalone Apple Notes CLI: the `apple-notes` entry point is run by
Collect. For source installation, use the [Collect workspace instructions](../collect/README.md#running-from-source).
