# Onboard a Codex (ChatGPT-desktop) session

Cold-start a Codex agent onto the fulcra-coord mesh. A session that was already
running won't have had its hooks fire, so it self-onboards by running these.
(Once `fulcra-coord install-codex` has run, future Codex sessions integrate
automatically via the Codex `SessionStart`/`Stop`/`PreCompact` hooks.)

Paste this into a Codex session:

```
You're joining the Fulcra agent-coordination mesh (fulcra-coord). Do this now:

1. Make the CLI available: canonical repo is ashfulcra/fulcra-tools, package at
   packages/fulcra-coord. If `fulcra-coord` isn't on PATH:
   `cd <…>/fulcra-tools/packages/fulcra-coord && uv tool install --force .`
2. Verify + auth:  `fulcra-coord doctor`   (if unauthed: `fulcra-api auth login`)
3. Wire your Codex lifecycle hooks (SessionStart / Stop / PreCompact):
   `fulcra-coord install-codex`        (+ optional `fulcra-coord install-listener --agent codex:<host>:<label>`)
4. Declare a clear, stable, human-legible identity (vendor:host:purpose) so
   directives reach you and the human can tell who's who on the bus:
   `fulcra-coord identity set codex:<host>:<label>`
   (identity is scoped per working directory — set it once per repo). Always
   identify yourself in what you direct at others.
5. Load the mesh + reload your context — there may already be a directive waiting:
   `fulcra-coord agents` · `fulcra-coord inbox` · `fulcra-coord resume`
   (ack an inbox item: `fulcra-coord inbox --ack <id>`)
6. Coordinate at task boundaries from now on:
   `fulcra-coord start "<objective>" --workstream <ws>` · `update` · `pause` ·
   `done --evidence "…"` · and `tell <agent> "…"` / `broadcast "…"` to direct others.
   When you need the OPERATOR to do something, mark it with
   `fulcra-coord block <id> --on-user "<the ask>"` — it lands on the human's
   `needs-me` plate and leads their next SessionStart.
7. (Optional) Agent-Tasks Fulcra timeline annotations:
   `export FULCRA_COORD_ANNOTATIONS=cli` and point
   `FULCRA_COORD_ANNOTATION_CLI` at a create-annotations-commands fulcra build
   (keep FULCRA_CLI_COMMAND on your Files build — they're decoupled on purpose).

Acknowledge on the bus when you're in.
```

## Operator setup (one-time)
Personalize the human handle so `block --on-user` / `needs-me` address you by
name (default is the neutral `human`):
`fulcra-coord human set <your-name>` (e.g. `fulcra-coord human set ash`), then
`fulcra-coord needs-me` shows what's blocked on you across all agents.

## Working-directory hygiene (one worktree per session)
**Each agent session should operate in its OWN git worktree (or clone) — never
share one checkout across concurrent sessions.** Sharing a working tree makes
sessions fight over a single index and `HEAD`: commits from different branches
interleave, one session's merge/rebase leaves conflict markers staged in
everyone's tree, and in-progress edits get swept into unrelated commits. This is
the structural partner to per-cwd identity (step 4): a distinct worktree gives
this session an isolated checkout *and* its own persisted identity.
```bash
git worktree add ../fulcra-tools-<purpose> -b codex/<purpose> origin/main
cd ../fulcra-tools-<purpose>
fulcra-coord identity set codex:<host>:<purpose>
```
If you see conflict markers or staged files you did not create, you're sharing a
checkout — move to your own worktree instead of committing over another session.

## Codex specifics
- Use **`install-codex`** (not `install-claude-code`). It wires Codex's
  `SessionStart` / `Stop` / `PreCompact` hooks. Codex has **no `SessionEnd`**;
  `Stop` fires at end-of-turn, so the checkpoint is idempotent.
- Identity convention: `codex:<host>:<label>`. Address Codex on the bus by the
  prefix `codex` (identity prefix-matching) or its full id.
- Annotations need a build with `create-data-type`; file-ops need the `file`
  group. No single Fulcra CLI build has both yet, hence the separate
  `FULCRA_COORD_ANNOTATION_CLI` pointer (see `docs/annotations.md`).
