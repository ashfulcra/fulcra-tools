# fulcra-apple-notes

Syncs Apple Notes into a Fulcra vault as markdown, with attachments.

One way for now (Notes → vault), and additive: it writes only under
`vault/notes/apple/` and never touches the rest of the vault.

## Install

```bash
uv pip install -e packages/apple-notes
fulcra-collect enable apple-notes
```

It runs inside the collect daemon, which is deliberate: the Notes store
lives in a TCC-protected group container, so the daemon holds Full Disk
Access where an arbitrary shell does not.

## Settings

`~/.config/fulcra-collect/config.toml`:

```toml
[plugin_settings.apple-notes]
dry_run           = false  # true: decode and count, write nothing
limit             = 0      # >0: only process the first N notes
max_attachment_mb = 25     # skip attachments larger than this
```

## What it writes

```
vault/notes/apple/<slug>-<uuid8>.md      one file per note
vault/notes/apple/_attachments/<uuid>/   attachment files
vault/notes/apple/.sync-state.json       per-note hash + mod date
```

Each note keeps its body inside an owner-fenced section:

```markdown
<!-- section:apple-note owner:fulcra-collect/apple-notes -->
...note content...
<!-- /section:apple-note -->
```

Anything you write **outside** that fence survives every later sync. The
sync only ever rewrites what is inside it.

## Safety properties

These are the behaviours worth knowing, because a sync into a vault you
already use can do real damage:

- **Deletions are marked, never applied.** A note removed from Apple Notes
  gets `apple-deleted: true` and keeps its body — at that point the vault
  copy is the only copy.
- **A note that fails to decode is skipped, not written empty.** Writing an
  empty body over a good note would destroy content; staleness will not.
- **A failed read of the sync state aborts the run.** Treating a transport
  failure as "no state" would re-upload the entire library and look like a
  successful first run.
- **A partial pass never concludes notes were deleted.** The live set is
  built from the full store read, before any limit is applied.
- **Runs checkpoint and stop before the worker timeout.** The collect worker
  is killed at 900s; a first sync of a large library takes far longer, so
  runs save every 50 notes and stop at 780s. Each run leaves durable
  progress and the next resumes.

## Observability

Every run writes a report, because the worker's logger output does not
reach a file an operator can read — a sync could otherwise do nothing and
still report `done`:

```
~/Library/Logs/fulcra-collect/apple-notes-last-run.json   latest run
~/Library/Logs/fulcra-collect/apple-notes-runs.jsonl      history
```

The report carries content aggregates (`markdown_chars`, `notes_empty_body`,
`notes_with_structure`) as well as counts, so a decoder regression that
produced empty bodies shows up as a quality drop rather than a clean run.

## Store schema notes

The reader handles these Apple Notes schema details:

- Notes, folders, attachments and media all share `ZICCLOUDSYNCINGOBJECT`
  (214 columns), discriminated by `Z_ENT`. **Column meanings differ by
  entity**: a note's mod date is `ZMODIFICATIONDATE1` and its title
  `ZTITLE1`; `ZMODIFICATIONDATE`/`ZTITLE` belong to attachments.
- **Some note rows are husks** — no body, no title, no folder, no mod
  date, and not marked deleted. Change detection keyed on a mod date can miss valid notes, so notes are filtered on body presence instead.
- **Not every attachment has a backing file.** Some are inline
  tables, links and drawings. The media join is a LEFT join so those rows
  are kept and flagged rather than silently dropped.
- Attachment files sit **two** levels below `Media/<media-uuid>/`, not one.
- Bodies are gzip-wrapped protobuf; `body.py` decodes the wire format
  directly rather than taking a protobuf dependency.

## Two-way sync

Not implemented. The state it needs is recorded from the first write —
each note carries `apple-uuid`, `apple-modified` and `apple-hash` — so a
future writer can tell vault-side edits from Notes-side ones without a
re-import.

## Two-way sync

Detection is built and working; writeback is built but **gated on a macOS
permission that has not been granted**.

### Modes

```toml
[plugin_settings.apple-notes]
mode = "sync"        # default: one-way Apple Notes -> vault
# mode = "reconcile" # report what changed on each side; writes nothing
# mode = "writeback" # push vault edits back into Apple Notes
```

### Reconcile (works today, no permissions)

Classifies every note using three inputs — the Apple note, the vault file,
and what the last sync wrote. Two of the three is not enough: a vault that
differs from Apple does not say *which side moved*.

| Status | Meaning |
|---|---|
| `unchanged` | neither side moved |
| `apple_changed` | normal forward sync |
| `vault_edited` | you edited it in the vault — writeback candidate |
| `conflict` | both sides moved; not resolved automatically |
| `new_in_apple` / `missing_in_vault` / `deleted_in_apple` | membership changes |

Only notes whose vault file looks newer than our own write are downloaded.
Unchanged files are skipped; candidate edits are downloaded for comparison.

Report: `~/Library/Logs/fulcra-collect/apple-notes-reconcile.json`

### Writeback (blocked)

Pushes `vault_edited` notes back via AppleScript. It **refuses** by default:

- any note with attachments — replacing a body is expected to drop them
  (**unverified**: confirming it needs the consent below)
- any `conflict`
- any note no longer in the store
- any file whose owner fence was removed (we cannot delimit what to push)

It is dry-run unless explicitly disabled, because bulk body replacement
can lose content and cannot be automatically undone.

**Blocked on:** TCC Automation consent for Notes. AppleScript currently
returns `AppleEvent timed out (-1712)`, which is what an unanswered consent
prompt looks like. Approve under System Settings → Privacy & Security →
Automation, then run with `mode = "writeback"`, `dry_run = false`.

Note that a dry run does **not** probe AppleScript, because probing
launches Notes.app; set `probe_automation = true` to check availability.
