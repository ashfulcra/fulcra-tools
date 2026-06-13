# Onboard a Codex (ChatGPT-desktop) session

Cold-start a Codex agent onto the fulcra-coord mesh. A session that was already
running won't have had its hooks fire, so it self-onboards by running these.
(Once `fulcra-coord install-codex` has run, future Codex sessions integrate
automatically via the Codex `SessionStart`/`PreCompact` hooks.)

Paste this into a Codex session:

```
You're joining the Fulcra agent-coordination mesh (fulcra-coord). Do this now:

1. Make the CLI available: canonical repo is ashfulcra/fulcra-tools, package at
   packages/fulcra-coord. If `fulcra-coord` isn't on PATH:
   `cd <…>/fulcra-tools/packages/fulcra-coord && git pull && uv tool install --reinstall --force .`
   (use `--reinstall`: uv SKIPS the rebuild when the version is unchanged, so a
   plain `--force` can leave you on an old subcommand set after a `git pull`.)
   NOTE: 0.15.3+ self-propagates — once installed, the CLI updates itself from
   the bus version manifest (default ON; configure update.json with your
   checkout path, see README "Self-update"). This manual step is only needed
   to cross onto 0.15.3, or with FULCRA_COORD_SELF_UPDATE=0.
2. Verify + auth:  `fulcra-coord doctor`   (if unauthed: `fulcra-api auth login`).
   If `doctor` reports `File commands: FAIL`, the resolved Fulcra CLI isn't
   exposing the `file` command group the bus runs on. The standard
   `fulcra-api` install ships it — reinstall (`uv tool install --reinstall
   --force fulcra-api`) or fix a mispointed `FULCRA_CLI_COMMAND`; see
   `docs/fulcra-cli-branch.md`.
   Confirm your build is current: `fulcra-coord --version` and
   `fulcra-coord capabilities` (lists supported commands — if a command this
   doc mentions is missing, your install is stale; reinstall per step 1).
3. Wire your Codex lifecycle hooks (SessionStart / PreCompact), durable inbox
   listener, thread heartbeat automation, and optional unattended wake:
   `fulcra-coord ensure-codex-watch --set-identity codex:<host>:<label> --with-wake`
   (SessionStart passes the current Codex thread id automatically; manual runs
   can add `--thread-id <codex-session-id>` to seed the same automation. The
   managed thread heartbeat defaults to every 15 minutes; use
   `--automation-interval-min <n>` only when you explicitly want a different
   recurring app-thread cost.)
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
   Also write Fulcra Continuity snapshots at durable pause points:
   `fulcra-coord snapshot TASK-... --reason "<pause-point>" --next "<pickup step>"`.
   Codex has no SessionEnd hook, so do this explicitly before handoff/review
   request, after long edit/test/push stretches, when the user says pause/done,
   and at overnight or idle stopping points. If the snapshot command prints
   quality warnings, enrich the task state or write a richer checkpoint before
   trusting it as a handoff packet.
7. (Optional) Agent-Tasks Fulcra timeline annotations:
   `export FULCRA_COORD_ANNOTATIONS=cli` and point
   `FULCRA_COORD_ANNOTATION_CLI` at a create-annotations-commands fulcra build
   (keep FULCRA_CLI_COMMAND on the standard CLI — they're decoupled on purpose).

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
  `SessionStart` / `PreCompact` hooks. Codex has **no `SessionEnd`**;
  `Stop` fires at end-of-turn, so fulcra-coord deliberately does not use it for
  parking; install the heartbeat if you want that backstop. The heartbeat keeps
  the loop alive, but it is not a substitute for explicit Continuity snapshots
  at durable handoff/idle/review boundaries.
- `install-listener` alone is notify-only: it polls the bus, writes the pending
  inbox surface, and emits a desktop notification. `ensure-codex-watch
  --thread-id <id>` writes a Codex thread heartbeat automation so the current
  thread keeps polling the bus every 15 minutes by default. `ensure-codex-watch
  --with-wake` also writes a reviewed `wake.json` entry so the listener can
  spawn a headless `codex exec` run when pending work appears. Headless wakes
  are marked with `FULCRA_COORD_CODEX_WAKE=1`, so if they fire SessionStart
  hooks they refresh hooks/listeners without retargeting the live app-thread
  heartbeat at the throwaway exec session.
- Identity convention: `codex:<host>:<label>`. Address Codex on the bus by the
  prefix `codex` (identity prefix-matching) or its full id.
- Annotations need a build with `create-data-type`; file-ops need the `file`
  group. No single Fulcra CLI build has both yet, hence the separate
  `FULCRA_COORD_ANNOTATION_CLI` pointer (see `docs/annotations.md`).
