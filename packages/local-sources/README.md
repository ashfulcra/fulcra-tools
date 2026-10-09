# fulcra-local-sources

Two Fulcra Collect plugins for files already on your Mac:

- **Local folders and Obsidian** copies supported files from one folder you
  choose into `vault/imports/local-folders/<collection>/`.
- **Meeting transcript exports** watches one folder for Markdown, text, VTT,
  SRT, and supported JSON transcripts, then writes normalized Markdown under
  `vault/meetings/imported/<collection>/`.

No provider account or API key is involved. You still sign in to Fulcra because
that is where the copies go.

This package is part of the [unsupported, vibe-coded Fulcra Tools repo](../../README.md),
built by Fulcra's lawyer for his own agents. Read the limits before pointing it
at a folder that matters.

## Set it up in Collect

These plugins are in the 0.1.4 source tree. The published 0.1.3 Mac installer
does not contain them yet.

1. Open Collect's dashboard and choose **Local folders and Obsidian** or
   **Meeting transcript exports**.
2. Choose **Set up**, then **Choose folder**. The macOS picker supplies the
   path directly to the local Collect daemon; you do not type it. The picker
   response contains only the folder name. Like other path settings, the saved
   path is visible to the authenticated dashboard when you reopen configuration.
3. Give the destination a short collection name.
4. Review the eligible-file preview. Turn on **Preview only** if you want the
   first run to write nothing.
5. Choose **Enable & start sync**.

Local folders run every six hours. Transcript folders run every fifteen
minutes so an app that exports after each call can feed Collect without its own
connector. **Run Now** checks immediately.

The selected absolute path stays in Collect's private configuration on this
Mac. Fulcra receives the collection name, relative paths, and selected file
contents. Logs and sync state contain counts, hashes, relative paths, and remote
paths; they do not contain the absolute local path.

## What is included

Local folders accept:

`md`, `txt`, `html`, `htm`, `csv`, `json`, `yaml`, `yml`, `pdf`, `png`, `jpg`,
`jpeg`, `gif`, `webp`, `docx`, `xlsx`, and `pptx`.

Markdown and Obsidian attachments keep their relative layout. Collect uploads
files verbatim. It does not rewrite Obsidian links or turn Office files into
Markdown.

Transcript folders accept:

`md`, `txt`, `vtt`, `srt`, and JSON objects containing either transcript text
or a `segments`/`utterances` list. Subtitle timestamps and cue numbers are
removed. The source export is never changed.

## Safety and sync behavior

- Nothing is selected or enabled by default.
- Traversal never follows symbolic links.
- Hidden files and folders, version-control metadata, generated dependency
  folders, and likely credential files are excluded.
- A run is limited to 5,000 eligible files, 50 MiB per file, and 2 GiB total.
  Exceeding the file or total limit stops the run instead of silently taking a
  partial snapshot. Oversized individual files are reported as skipped.
- Content hashes prevent unchanged files from uploading again. A changed file
  replaces its own remote path after a successful upload.
- Source deletions never delete the Fulcra copy. Moving or renaming a source
  file can leave the earlier copy in Fulcra.
- The destination is additive and owned by the plugin. Do not use the same
  collection name for unrelated folders unless you want them to share a remote
  directory.

## Source install and tests

From the repository root:

```bash
uv sync --all-packages
uv run --package fulcra-local-sources pytest
uv run --all-packages fulcra-collect daemon
```

The package registers `local-folders` and `meeting-transcripts` in the
`fulcra_collect.plugins` entry-point group. The macOS bundle gets the package
from `packages/menubar/pyproject.toml`; `bundle_manifest.py` derives its wheel
and import checks from that list.
