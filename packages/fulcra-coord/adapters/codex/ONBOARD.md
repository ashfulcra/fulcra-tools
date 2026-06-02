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
4. Declare your identity so directives reach you:
   `fulcra-coord identity set codex:<host>:<label>`
5. Load the mesh + your inbox — there may already be a directive waiting:
   `fulcra-coord agents`   and   `fulcra-coord inbox`
   (ack it: `fulcra-coord inbox --ack <id>`)
6. Coordinate at task boundaries from now on:
   `fulcra-coord start "<objective>" --workstream <ws>` · `update` · `pause` ·
   `done --evidence "…"` · and `tell <agent> "…"` / `broadcast "…"` to direct others.
7. (Optional) Agent-Tasks Fulcra timeline annotations:
   `export FULCRA_COORD_ANNOTATIONS=cli` and point
   `FULCRA_COORD_ANNOTATION_CLI` at a create-annotations-commands fulcra build
   (keep FULCRA_CLI_COMMAND on your Files build — they're decoupled on purpose).

Acknowledge on the bus when you're in.
```

## Codex specifics
- Use **`install-codex`** (not `install-claude-code`). It wires Codex's
  `SessionStart` / `Stop` / `PreCompact` hooks. Codex has **no `SessionEnd`**;
  `Stop` fires at end-of-turn, so the checkpoint is idempotent.
- Identity convention: `codex:<host>:<label>`. Address Codex on the bus by the
  prefix `codex` (identity prefix-matching) or its full id.
- Annotations need a build with `create-data-type`; file-ops need the `file`
  group. No single Fulcra CLI build has both yet, hence the separate
  `FULCRA_COORD_ANNOTATION_CLI` pointer (see `docs/annotations.md`).
