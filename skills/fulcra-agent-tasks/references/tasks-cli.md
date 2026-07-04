---
name: fulcra-agent-tasks-cli
description: "coord-engine task lifecycle commands + the OKF Task frontmatter shape."
---

# Fulcra Agent Tasks — CLI reference

All commands are `uv tool run coord-engine task …` (needs `fulcra-api auth login`). The engine
parses→validates→writes the OKF Task doc; it never lets an illegal status transition through.

## Commands
```bash
coord-engine task start  <team> <title> [--workstream W] [--status S] [--priority P0..P3]
                                        [--assignee A] [--summary TEXT] [--next TEXT]
                                        [--kind K] [--force]
coord-engine task update <team> <name>  [--status S] [--priority P] [--assignee A]
                                        [--summary TEXT] [--next TEXT] [--blocked-on TEXT]
coord-engine task done   <team> <name>  --evidence TEXT      # evidence required
coord-engine task block  <team> <name>  [--blocked-on TEXT | --on-user ASK]  # --on-user assigns to FULCRA_COORD_HUMAN/human + tags needs:human
coord-engine task pause  <team> <name>  --next TEXT          # waiting; next action required
coord-engine task abandon <team> <name> --reason TEXT        # terminal; reason required
coord-engine task assign <team> <name>  <assignee>           # set/redirect assignee
```
- `<name>` is the slug of the task file (e.g. `fix-the-widget` for `task/fix-the-widget.md`).
- `start` refuses to overwrite an existing task unless `--force`.
- `update`/`done` reject an illegal transition (e.g. `done → active`) with a non-zero exit + message.

## The OKF Task doc it writes
```yaml
---
type: Task
title: Fix the widget
description: <--summary>            # OKF; becomes the reconcile index bullet
timestamp: 2026-07-01T18:00:00Z    # stamped on every write
tags: [workstream:web, kind:bug]
id: fix-the-widget
status: active                     # proposed|active|waiting|blocked|done|abandoned
priority: P1
owner: <the invoking identity>
assignee: ash
next_action: write the test
---
# Fix the widget

- 2026-07-01T18:00:00Z: active → blocked (evidence: …)   # a dated note appended per write
```

## After writing
Run `coord-engine reconcile <team>` to refresh `task/index.md` + the queryable aggregate, then use
`coord-engine board/status/needs-me/search` (the `fulcra-agent-reconcile` skill).
