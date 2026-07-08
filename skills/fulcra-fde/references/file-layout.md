# Canonical engagement file layout

Everything lives in the **user's own** Fulcra file store. `fde-engine` owns
this layout; in degraded (engine-less) mode, reproduce it exactly with
`fulcra file upload/download/list`.

    fde/engagements/<slug>/
      engagement.md            # machine-managed state record (schema
                               # fulcra.fde.engagement.v1: phase, timestamps,
                               # phase_history) — in degraded mode, edit the
                               # frontmatter by hand and keep history honest
      intake/                  # original materials + brief.md
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
