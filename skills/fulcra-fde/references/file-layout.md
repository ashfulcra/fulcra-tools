# Canonical engagement file layout

Everything lives in the **user's own** Fulcra file store. `fde-engine` owns
this layout; in degraded (engine-less) mode, reproduce it exactly with
`fulcra file upload/download/list`.

    fde/engagements/<slug>/
      engagement.md            # machine-managed state record (schema
                               # fulcra.fde.engagement.v1: phase, timestamps,
                               # phase_history) — in degraded mode, edit the
                               # frontmatter by hand and keep history honest
      intake/                  # brief.md + text extracts of the source materials
      intake/originals/        # BINARY source files (PDFs, decks, images,
                               # spreadsheets) — NOT sync'd; upload directly
                               # with `fulcra file upload` (see note below)
      interview/plan.md        # prioritized topic map (checked off as resolved)
      interview/findings.md    # streamed findings + assumption verdicts
      architecture.md          # capability map + gap register + tenancy
      plan.md                  # prototype verification plan + production plan
      prototype/verification.md# per-item PASS/FAIL record
      build/log.md             # milestone log + created Fulcra resource IDs
      retro.md                 # lessons; repeatable patterns -> playbook
    fde/playbook.md            # cross-engagement patterns (append per retro)

Local mirror default: `./fde/<slug>/` in the user's project. Sync is
explicit-direction: `fde-engine sync <slug> pull` at session start,
`... push` after local edits. The store's version history (`fulcra file
stat`) is the recovery path for a wrongly chosen direction.

## Text mirror vs. binary originals

The mirror sync carries the engagement's **text** working set (markdown docs).
It does **not** round-trip binaries — the transport decodes as UTF-8, so a
binary pull would corrupt bytes. So:

- **Text artifacts** (brief, findings, architecture, plan, verification, logs)
  live in the mirror and move with `fde-engine sync`.
- **Binary source materials** (PDFs, decks, images, spreadsheets) go under
  `intake/originals/`, which sync **skips in both directions**. Upload them
  straight to the store:
  `fulcra file upload <local.pdf> fde/engagements/<slug>/intake/originals/<name>.pdf`.
  Keep a plain-text extract in `intake/` (e.g. `intake/deck-text.md`) so the
  content is in the synced, greppable working set.
- A stray binary dropped elsewhere in the mirror won't crash a push — sync
  skips it and prints the `fulcra file upload` command to move it into the
  area.
