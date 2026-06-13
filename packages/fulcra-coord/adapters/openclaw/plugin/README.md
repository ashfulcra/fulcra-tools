# Fulcra Coordination — OpenClaw Plugin (Track B)

The deterministic per-session upgrade over the file-based Track A hooks. This is
an **OpenClaw Plugin-SDK plugin** that registers three in-process lifecycle
hooks. Its unique differentiator is **deterministic per-session start/end**:
there is no file-based `session:start` automation event, and session-end is
plugin-only — so true per-session `session_start` / `session_end` requires the
plugin. The before-compaction checkpoint is also provided here as
`before_compaction`, but it is **not** Track B's differentiator: per the
authoritative OpenClaw docs, `session:compact:before` is a file-based automation
event, so Track A already checkpoints compaction with a file hook. The plugin's
`before_compaction` is the underscore-form equivalent for single-plugin installs.

## What it does — the three checkpoints

| Hook | Trigger | Action (shells `fulcra-coord`) |
|------|---------|--------------------------------|
| `session_start` | a new session begins | `fulcra-coord status` → surface in-flight + stale work into the session log |
| `before_compaction` | OpenClaw is about to summarize history (guaranteed context loss) | **ALWAYS** `fulcra-coord update <task> --summary "…"` plus `fulcra-coord snapshot <task> --reason openclaw-before-compaction`; status stays `active` |
| `session_end` | a real teardown (`idle` / `daily` / `reset` / `deleted` / `shutdown` / `restart`) | `fulcra-coord pause <task> --next "…" --snapshot` → parks `active`→`waiting` and writes a continuity checkpoint. **Skips `compaction`** (the session continues) |

The session's task is resolved via the CLI's session→task pointer, keyed on
OpenClaw's stable **`sessionKey`** through the `FULCRA_COORD_SESSION_KEY` env
pointer fallback that Track A added (see `fulcra_coord/session_link.py`). Every
hook is **fail-safe**: any error is swallowed so a coordination failure never
blocks a session, a compaction, or a shutdown.

## Why a plugin (vs. Track A's `~/.openclaw/hooks/` files)

`session_start` and `session_end` are **plugin-only** — there is no file-based
`session:start` automation event, and session-end fires only as a typed plugin
hook — so a real installed plugin is the only way to get true per-session
start/end. (`before_compaction` is *also* exposed file-based as
`session:compact:before`, which Track A uses; the plugin's `before_compaction` is
the underscore-form equivalent bundled here for single-plugin installs.) The
plugin needs an `npm`/`tsc` build step the `fulcra-coord` CLI cannot perform for
you. Track A installs by dropping files; Track B installs by building and
registering this plugin.

> The TypeScript here is validated against the OpenClaw Plugin-SDK **source**
> (`github.com/openclaw/openclaw`, `docs.openclaw.ai/plugins/hooks`), not a live
> runtime — `fulcra-coord`'s repo has no OpenClaw daemon to run it against. Every
> SDK call carries an inline source citation in `src/index.ts`.

## Install

### 1. Materialize the plugin sources

```bash
fulcra-coord install-openclaw --with-plugin [--plugin-dir <dir>]
```

This writes the plugin source tree (`package.json`, `openclaw.plugin.json`,
`tsconfig.json`, `src/index.ts`, `src/openclaw-sdk.d.ts`, `.npmrc`, `.npmignore`,
this README) to `<dir>` (default `~/.openclaw/plugins/fulcra-coord/`) — it does
**not** build or register it, because that needs `npm`. The command prints the
exact follow-up steps.

### 2. Build and register (manual — needs npm)

```bash
cd ~/.openclaw/plugins/fulcra-coord    # or your --plugin-dir
npm install                            # devDeps only; peers omitted (.npmrc)
npm run build                          # tsc → dist/index.js
openclaw plugins install .             # register the built plugin with OpenClaw
```

`openclaw plugins install <path-or-spec>` registers the plugin; once installed
and the gateway restarts, the three hooks fire automatically for every session.

> **`npm install` does NOT install the `openclaw` peer.** The bundled `.npmrc`
> sets `omit=peer`, so a clean install pulls only the build-time devDependencies
> (`typescript`, `@types/node`) — there is no `node_modules/openclaw`. The
> `openclaw` runtime is the *host* that loads this plugin; it is provided at load
> time, never bundled (a bundled copy makes `openclaw plugins install .` choke).
> `tsc` still resolves `import … from "openclaw/plugin-sdk/plugin-entry"` against
> the ambient `src/openclaw-sdk.d.ts` shim, so the build works offline without the
> real package present. `.npmignore` excludes `node_modules` as a backstop so it
> can never be staged into what `openclaw plugins install .` registers.

### 3. Verify

```bash
openclaw plugins inspect fulcra-coord --runtime --json   # confirm it loaded
fulcra-coord doctor                                       # confirm CLI auth/connectivity
```

## Relationship to Track A

Track A and Track B coexist. Track A (boot/heartbeat prompts + `session:compact:before`
/ `gateway:shutdown` / `agent:bootstrap` file hooks) installs cleanly with no
build step and covers compaction, gateway shutdown, and bootstrap. Track B adds
the **deterministic per-session start/end** fidelity Track A structurally cannot
(no file-based `session:start`; `session:end` is plugin-only). Compaction is
covered by *both* — Track A's `session:compact:before` file hook and Track B's
`before_compaction` plugin hook both checkpoint; if you run both, the duplicate
`update`/`snapshot` pair is harmless. Likewise the `gateway:shutdown` file hook
(Track A) and the plugin `session_end` hook (Track B) both aim to park active
work on shutdown — both are idempotent (`pause` on an already-`waiting` task is a
no-op), so the overlap is safe.
