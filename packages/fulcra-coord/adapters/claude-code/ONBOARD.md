# Onboard an already-running Claude Code session

> **Canonical coord guide:** [`fulcra-coord/SKILL.md`](../../SKILL.md) — the runtime-agnostic when/how-to-use reference (quick-reference + load-bearing rules). This file is the Claude Code-specific onboarding layer.

A new session is wired automatically once `fulcra-coord install-claude-code`
has run (its SessionStart hook fires at launch). A session that was ALREADY
running when hooks were installed must onboard manually — run these now:

1. Check setup: `fulcra-coord doctor`
   - If unauthed: `fulcra-api auth login` and complete the device flow.
   - If `doctor` reports `File commands: FAIL`, the resolved Fulcra CLI isn't
     exposing the `file` command group the bus runs on. The standard
     `fulcra-api` install ships it — reinstall (`uv tool install --reinstall
     --force fulcra-api`) or fix a mispointed `FULCRA_CLI_COMMAND`; see
     `docs/fulcra-cli-branch.md`.
   - Confirm the build is current: `fulcra-coord --version` /
     `fulcra-coord capabilities` (lists supported commands). If a command this
     doc mentions is missing, your install is stale — from the package dir run
     `git pull && uv tool install --reinstall --force .` (use `--reinstall`: uv
     skips the rebuild when the version is unchanged).
   - **0.15.3+ self-propagates:** once on 0.15.3 or later, the CLI updates
     itself from the bus version manifest at session start / listener ticks
     (default ON; configure `update.json` with your checkout path — see the
     README "Self-update" section). The manual update above is only needed to
     cross onto 0.15.3 or on hosts opted out via `FULCRA_COORD_SELF_UPDATE=0`.
2. Wire future sessions (idempotent): `fulcra-coord install-claude-code --global`
3. **Declare a clear, stable, human-legible identity** so directives reach you
   and the human can tell who's who on the bus:
   `fulcra-coord identity set vendor:host:purpose`
   (e.g. `claude-code:DeskbookPro:fulcra-coord`). Identity is now scoped per
   working directory, so each repo holds its own — set it once per repo.
   **Always identify yourself** in what you direct at others.
4. Load current in-flight work + reload your context:
   `fulcra-coord status`  and  `fulcra-coord resume`
   (`resume` shows your active work, what's blocked on you, what you owe others,
   and what's blocked on the human).
5. If you are continuing or claiming a task, run `fulcra-coord start ...` or
   `fulcra-coord update <id> --status active --agent claude-code:<host>:<repo>`.
   This stamps this session's task pointer, so PreCompact/SessionEnd hooks
   checkpoint it for the rest of this session's life.

Report milestones as you work (start / done / block); the hooks handle
start-surfacing, pre-compaction checkpoints, and session-end parking.

**When you need the operator to do something, mark it with**
`fulcra-coord block <id> --on-user "<what you need them to do>"` — this blocks
the task on the human, lands it on their `needs-me` plate, and surfaces it at
the top of their next SessionStart. It's how "blocked on the human" becomes
visible instead of buried in a summary.

## Working-directory hygiene (one worktree per session)

**Each agent session should operate in its OWN git worktree (or clone) — never
share one checkout across concurrent sessions.** When several sessions point at
the same working tree they fight over a single index and `HEAD`: commits from
different branches interleave, one session's `git merge`/`rebase` leaves
conflict markers staged in *everyone's* tree, and an in-progress change can get
swept into an unrelated session's commit. (We hit all three on the
`fulcra-tools` monorepo.)

This is the structural partner to the per-cwd identity scoping (step 3): a
distinct worktree gives this session both an isolated checkout *and* its own
persisted identity, so neither your git state nor your bus identity collides
with a sibling session.

```bash
# From an existing clone, give this session its own worktree + branch:
git worktree add ../fulcra-tools-<purpose> -b <vendor>/<purpose> origin/main
cd ../fulcra-tools-<purpose>
fulcra-coord identity set claude-code:<host>:<purpose>   # per-cwd identity
```

If you find conflict markers or unrelated staged files you did not create, you
are almost certainly sharing a checkout — stop and move to your own worktree
rather than committing over another session's work.

## Operator setup (one-time)

Personalize the human handle so `--on-user` / `needs-me` address you by name
(default is the neutral `human`):

```bash
fulcra-coord human set <your-name>     # e.g. fulcra-coord human set ash
fulcra-coord needs-me                  # what's blocked on you, across all agents
```
