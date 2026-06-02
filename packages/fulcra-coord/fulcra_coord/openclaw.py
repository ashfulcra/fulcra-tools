"""OpenClaw auto-integration — Track A artifact templates + installer.

This mirrors ``claude_code.py`` for OpenClaw (formerly Clawdbot/Moltbot). The
artifact *contents* are the single source of truth here (shipped in the wheel);
``install_openclaw`` materializes them into OpenClaw's file-based automation
hooks dir (default ``~/.openclaw/hooks/``) so a one-time install wires every
agent the gateway runs.

Track A is the file-based-automation slice that installs cleanly today:

  * ``BOOT.md`` / ``HEARTBEAT.md`` — prompt-style files that drive the agent to
    run ``fulcra-coord status`` at gateway boot and periodically. These are
    *agent-driven* (the model reads and acts on them); there is nothing to
    unit-test in their runtime behaviour beyond "we materialized the file".
  * ``fulcra-coord-compact/`` — a ``session:compact:before`` automation hook
    (``HOOK.md`` + ``handler.ts``) that ALWAYS checkpoints the session's active
    task by shelling ``fulcra-coord update`` before OpenClaw summarizes history
    (guaranteed context loss). This is the file-based analog of the Claude Code
    ``PreCompact`` hook; ``session:compact:before`` IS a file-based automation
    event (correcting an earlier reading that put it Plugin-SDK-only), so the
    before-compaction guarantee ships in Track A — no plugin required.
  * ``fulcra-coord-shutdown/`` — a ``gateway:shutdown`` automation hook
    (``HOOK.md`` + ``handler.ts``) that parks the session's active task as
    ``waiting`` by shelling ``fulcra-coord``, under OpenClaw's ~5s shutdown
    budget.
  * ``fulcra-coord-bootstrap/`` — an ``agent:bootstrap`` automation hook that
    runs ``fulcra-coord status`` and folds the surfaced in-flight/stale work
    into the session's ``MEMORY.md`` bootstrap slot via the mutable
    ``event.context.bootstrapFiles`` array.

SDK alignment (2026-06-01): the two ``handler.ts`` templates were corrected to
the real OpenClaw file-based automation-hook API, verified against the published
docs (``docs.openclaw.ai/automation/hooks``) and GitHub source
(``openclaw/openclaw``):

  * Event shape — ``InternalHookEvent`` carries ``type``/``action``/
    ``sessionKey`` at top level and event-specific data under ``context``
    (``src/hooks/internal-hook-types.ts``). The shutdown handler's top-level
    ``event.sessionKey`` access was already correct.
  * ``bootstrapFiles`` — the spec's open question #2 is resolved: it is
    ``event.context.bootstrapFiles`` (NOT top-level), and each element is a
    ``WorkspaceBootstrapFile`` object ``{ name, path, content?, missing }`` whose
    ``name`` must be a recognized basename (``AGENTS.md``/``SOUL.md``/``TOOLS.md``/
    ``IDENTITY.md``/``USER.md``/``HEARTBEAT.md``/``BOOTSTRAP.md``/``MEMORY.md``;
    ``src/agents/workspace.ts``). Arbitrary generated temp paths are rejected, so
    the bootstrap handler attaches inline ``content`` to the ``MEMORY.md`` slot
    instead of the original temp-file-push guess, and replaces a prior block
    idempotently to dodge the documented same-array-reference accumulation bug.

These handlers still cannot be RUN in this repo (no ts runtime / no OpenClaw SDK
here). What IS unit-tested is the installer: which files land where, idempotency,
dry-run, and surgical uninstall.

Idempotency / uninstall strategy: everything Track A materializes lives under
the managed hooks root in files/dirs that carry the ``fulcra-coord`` name
(``BOOT.md``/``HEARTBEAT.md`` use a tagged marker block; the hook dirs are named
``fulcra-coord-*``). Uninstall removes exactly those, never the user's own
curated boot/heartbeat content.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

# Placeholder the handler.ts templates carry; the materializer replaces it with
# json.dumps(resolve_cli_argv()) — a real JSON array literal — so the resolved
# argv (including a spaced argv[0]) is preserved without any `.split()` (C1).
PLACEHOLDER_ARGV_JSON = "__FULCRA_COORD_ARGV_JSON__"

# Marker block fencing our managed content inside BOOT.md / HEARTBEAT.md so a
# re-install or uninstall is surgical and never clobbers the user's own boot /
# heartbeat prose (the user actively curates these per the deployment doc).
_BEGIN = "<!-- fulcra-coord:begin (managed; do not edit between markers) -->"
_END = "<!-- fulcra-coord:end -->"

BOOT_MD_BODY = """\
## Fulcra coordination — surface in-flight work at boot

At gateway boot, before doing anything else, check the shared coordination bus
for work that may have been left running and forgotten:

1. Run: `fulcra-coord status`
2. If it shows tasks that are **active** (especially any owned by this agent, or
   any active task with no recent update — "possibly-forgotten" work), surface a
   short summary of them to the operator so nothing churns silently in the
   background.
3. If the bus is clean (no active or stale work), say nothing about it and
   reply `NO_REPLY`.

This runs once per gateway boot (a real session-resumption boundary, since the
gateway runs as a launchd daemon). Keep it lightweight and fail-safe: if
`fulcra-coord` is missing or errors, ignore it and continue booting.
"""

HEARTBEAT_MD_BODY = """\
## Fulcra coordination — periodic stale-work check

On each heartbeat:

1. Run: `fulcra-coord status`
2. If any **active** task (especially one owned by this agent) has **no recent
   update** — it has gone stale and may be forgotten — flag it briefly to the
   operator.
3. Run: `fulcra-coord notify-inbox` — this is the OpenClaw listener: it polls
   for **directives addressed to this agent** and, if any exist, surfaces them
   (for the next session boot) and notifies the operator. Notify-only; it never
   runs the directive.
4. Otherwise reply `HEARTBEAT_OK`.

Keep this tiny and cheap: pin the heartbeat to a cheap model and do not
accumulate context here. Fail-safe — if `fulcra-coord` errors, treat it as
`HEARTBEAT_OK` and move on.
"""

# --- gateway:shutdown automation hook -------------------------------------

SHUTDOWN_HOOK_MD = """\
---
name: fulcra-coord-shutdown
description: "Park this session's active fulcra-coord task as waiting on gateway shutdown."
metadata:
  { "openclaw": { "emoji": "🦞", "events": ["gateway:shutdown"], "requires": { "bins": ["fulcra-coord"] } } }
---

# fulcra-coord — gateway shutdown checkpoint

When the gateway stops, the task this session was actively working should be
parked as `waiting` (not left `active`, which would look like live work that
nobody is doing). This handler resolves the session's task via the
`fulcra-coord` session pointer and pauses it.

Bounded under OpenClaw's ~5s shutdown budget: a single short-timeout CLI call.
Fail-safe — any error returns cleanly and never blocks shutdown. A hard
`kill -9` fires no hook at all; the heartbeat reconciler is the backstop for
that case.
"""

SHUTDOWN_HANDLER_TS = """\
// fulcra-coord gateway:shutdown handler — park the session's active task.
//
// Validated against the OpenClaw file-based automation-hook API:
//   * Handler shape `export default async function handler(event)` and the
//     `execFileAsync` shell pattern: docs/automation/hooks.md (the
//     gateway:pre-restart example) and src/hooks/internal-hook-types.ts
//     (`InternalHookHandler = (event: InternalHookEvent) => Promise<void> | void`).
//   * Event fields `event.type` / `event.action` / `event.sessionKey` are
//     top-level on InternalHookEvent (src/hooks/internal-hook-types.ts L3-16).
//   * `gateway:shutdown` is a real file-based event and carries
//     `event.context.reason` + `event.context.restartExpectedMs`, with a ~5s
//     wait budget (docs/automation/hooks.md, "Gateway lifecycle events").
// It still cannot be RUN here (no ts runtime / no OpenClaw SDK in this repo);
// the installer (which materializes it) is what the Python suite tests.
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

// Keep well under the ~5s gateway:shutdown budget.
const WRITE_TIMEOUT_MS = 4000;

// Resolved at install time (Gap 1) as a real JSON argv ARRAY (C1): may be
// `["/abs/fulcra-coord"]` or `["<python>", "-m", "fulcra_coord"]`. A JSON array
// — not a string we `.split()` — so an argv[0] containing a space (e.g. an
// interpreter under "Application Support") survives intact. execFile runs no
// shell, so passing bin + args separately cannot word-split either.
const FULCRA_COORD_CMD: string[] = __FULCRA_COORD_ARGV_JSON__;

export default async function handler(event: any): Promise<void> {
  try {
    if (event?.type !== "gateway" || event?.action !== "shutdown") return;

    // The stable conversation bucket — survives compaction/daily/idle resets.
    // Top-level on InternalHookEvent (NOT under event.context).
    const sessionKey: string | undefined = event?.sessionKey;
    if (!sessionKey) return;

    // Resolve the task this session owns via the CLI's session pointer.
    // FULCRA_COORD_SESSION_KEY lets the CLI key the pointer on the OpenClaw
    // sessionKey (see fulcra_coord/session_link.py).
    const childEnv = { ...process.env, FULCRA_COORD_SESSION_KEY: sessionKey };
    const { stdout } = await execFileAsync(
      FULCRA_COORD_CMD[0],
      [...FULCRA_COORD_CMD.slice(1), "__session-task", sessionKey],
      { timeout: WRITE_TIMEOUT_MS, env: childEnv },
    );
    const taskId = (stdout || "").trim();
    if (!taskId) return;

    await execFileAsync(
      FULCRA_COORD_CMD[0],
      [...FULCRA_COORD_CMD.slice(1), "pause", taskId, "--next", "Gateway shutdown; resume from last next_action."],
      { timeout: WRITE_TIMEOUT_MS, env: childEnv },
    );
  } catch {
    // Fail-safe: never block or fail gateway shutdown on a coordination error.
    return;
  }
}
"""

# --- agent:bootstrap automation hook --------------------------------------

BOOTSTRAP_HOOK_MD = """\
---
name: fulcra-coord-bootstrap
description: "Inject surfaced in-flight/stale fulcra-coord work into the system prompt at agent bootstrap."
metadata:
  { "openclaw": { "emoji": "🦞", "events": ["agent:bootstrap"], "requires": { "bins": ["fulcra-coord"] } } }
---

# fulcra-coord — agent bootstrap context injection

While OpenClaw builds the bootstrap files for a new session (before the system
prompt is finalized), this handler runs `fulcra-coord status` and folds the
surfaced in-flight + possibly-forgotten work into the session's **MEMORY.md**
bootstrap file via the mutable `event.context.bootstrapFiles` array.

## SDK constraint that shapes this handler (validated against source)

`bootstrapFiles` is **not** an array of arbitrary path strings. Each element is
a `WorkspaceBootstrapFile` object — `{ name, path, content?, missing }` — and
`name` MUST be one of OpenClaw's recognized bootstrap basenames (`AGENTS.md`,
`SOUL.md`, `TOOLS.md`, `IDENTITY.md`, `USER.md`, `HEARTBEAT.md`, `BOOTSTRAP.md`,
`MEMORY.md`; src/agents/workspace.ts, docs/automation/hooks.md). A made-up name
like `coordination-status.md` is rejected by the loader. So instead of pushing a
new temp file (the original Track-A guess, which the real API does not support),
this handler injects an **inline `content`** entry under the `MEMORY.md` slot —
the one recognized slot semantically meant for "context to remember", and the
slot least likely to be pre-populated from disk for a fresh agent.

This is best-effort enrichment, not the deterministic guarantee. The robust
per-session surfacing path is the Track B Plugin-SDK `session_start` hook; the
durable prose fallback is BOOT.md / HEARTBEAT.md. Fail-safe — any error injects
nothing and returns cleanly.
"""

BOOTSTRAP_HANDLER_TS = """\
// fulcra-coord agent:bootstrap handler — fold surfaced work into MEMORY.md.
//
// Validated against the OpenClaw agent:bootstrap API (source-cited):
//   * The mutable array is `event.context.bootstrapFiles`, NOT
//     `event.bootstrapFiles` (src/hooks/internal-hooks.ts AgentBootstrapHookContext;
//     docs/automation/hooks.md "Bootstrap events"). agentId / sessionKey /
//     sessionId also live on event.context.
//   * Each element is a `WorkspaceBootstrapFile` object
//     `{ name, path, content?, missing }`, and `name` must be a recognized
//     basename (AGENTS.md, SOUL.md, TOOLS.md, IDENTITY.md, USER.md,
//     HEARTBEAT.md, BOOTSTRAP.md, MEMORY.md) — src/agents/workspace.ts
//     (WorkspaceBootstrapFile / WorkspaceBootstrapFileName). Arbitrary temp-file
//     names are rejected, so we attach inline `content` to the MEMORY.md slot
//     rather than pushing a generated temp path (the original guess).
//   * Accumulation hazard: the bootstrap cache can hand back the same array
//     reference across calls, so a naive push would compound on every new
//     session. We mutate idempotently — replace any existing fulcra-coord block
//     in MEMORY.md rather than appending a fresh entry each time.
// Still not RUNNABLE here (no ts runtime / no OpenClaw SDK); the installer that
// materializes it is what the Python suite tests.
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

// Status is a read off the critical path; allow the CLI's normal read timeout.
const READ_TIMEOUT_MS = 8000;

// Resolved at install time (Gap 1) as a real JSON argv ARRAY (C1): an absolute
// `["/abs/fulcra-coord"]` or `["<python>", "-m", "fulcra_coord"]`. A JSON array
// — not a string we `.split()` — so a spaced argv[0] survives intact.
const FULCRA_COORD_CMD: string[] = __FULCRA_COORD_ARGV_JSON__;

// Recognized bootstrap basename we fold our surfaced work into.
const MEMORY_NAME = "MEMORY.md";
const BLOCK_BEGIN = "<!-- fulcra-coord:begin -->";
const BLOCK_END = "<!-- fulcra-coord:end -->";

interface BootstrapFile {
  name: string;
  path: string;
  content?: string;
  missing: boolean;
}

function withCoordBlock(existing: string, summary: string): string {
  const block =
    BLOCK_BEGIN +
    "\\n# Fulcra coordination — in-flight / possibly-forgotten work\\n\\n" +
    summary +
    "\\n" +
    BLOCK_END;
  // Idempotent: replace a prior block instead of stacking (guards the
  // same-array-reference accumulation hazard documented above).
  const re = new RegExp(BLOCK_BEGIN + "[\\\\s\\\\S]*?" + BLOCK_END, "g");
  if (re.test(existing)) return existing.replace(re, block);
  return existing.trim() ? existing.trimEnd() + "\\n\\n" + block + "\\n" : block + "\\n";
}

export default async function handler(event: any): Promise<void> {
  try {
    if (event?.type !== "agent" || event?.action !== "bootstrap") return;

    const ctx = event?.context;
    const files: BootstrapFile[] | undefined = ctx?.bootstrapFiles;
    if (!Array.isArray(files)) return;

    // sessionKey lives on event.context for agent:bootstrap (not top-level).
    const sessionKey: string | undefined = ctx?.sessionKey;
    const childEnv = sessionKey
      ? { ...process.env, FULCRA_COORD_SESSION_KEY: sessionKey }
      : process.env;

    const { stdout } = await execFileAsync(
      FULCRA_COORD_CMD[0],
      [...FULCRA_COORD_CMD.slice(1), "status"],
      { timeout: READ_TIMEOUT_MS, env: childEnv },
    );
    const summary = (stdout || "").trim();
    if (!summary) return;

    const existing = files.find((f) => f?.name === MEMORY_NAME);
    if (existing) {
      // Fold into the existing MEMORY.md slot, replacing any prior block.
      existing.content = withCoordBlock(existing.content ?? "", summary);
      existing.missing = false;
    } else {
      // No MEMORY.md slot yet — add one carrying only our block. `path` is
      // advisory metadata here; `content` is what OpenClaw injects.
      files.push({
        name: MEMORY_NAME,
        path: MEMORY_NAME,
        content: withCoordBlock("", summary),
        missing: false,
      });
    }
  } catch {
    // Fail-safe: never block or fail bootstrap on a coordination error.
    return;
  }
}
"""

# --- session:compact:before automation hook -------------------------------

COMPACT_HOOK_MD = """\
---
name: fulcra-coord-compact
description: "Checkpoint this session's active fulcra-coord task before compaction (guaranteed context loss)."
metadata:
  { "openclaw": { "emoji": "🦞", "events": ["session:compact:before"], "requires": { "bins": ["fulcra-coord"] } } }
---

# fulcra-coord — before-compaction checkpoint

When OpenClaw is about to compact a session (silently summarizing history into a
shorter form), detail is reliably lost. This handler runs *before* that happens:
it resolves the task this session owns via the `fulcra-coord` session pointer and
ALWAYS stamps a fresh checkpoint `update` on it, so the task does not look stale
across the context-loss boundary and the last summary survives the flush. Status
stays `active` (the session continues after compaction).

This is the direct file-based analog of the Claude Code `PreCompact` hook.
Contrary to an earlier reading of the OpenClaw docs, `session:compact:before`
**is** a file-based automation event (not Plugin-SDK-only), so this guarantee
ships in Track A — no plugin required. Fail-safe — any error returns cleanly and
never blocks compaction.
"""

COMPACT_HANDLER_TS = """\
// fulcra-coord session:compact:before handler — checkpoint the session's task.
//
// Validated against the OpenClaw file-based automation-hook API:
//   * Handler shape `export default async function handler(event)` and the
//     `execFileAsync` shell pattern: docs/automation/hooks.md (the
//     gateway:pre-restart example) and src/hooks/internal-hook-types.ts
//     (`InternalHookHandler = (event: InternalHookEvent) => Promise<void> | void`).
//   * Event fields `event.type` / `event.action` / `event.sessionKey` are
//     top-level on InternalHookEvent (src/hooks/internal-hook-types.ts L3-16).
//     For compaction, type is "session" and action is "compact:before".
//   * `session:compact:before` IS a real file-based automation event
//     (docs.openclaw.ai/automation/hooks); its context carries `messageCount`
//     and `tokenCount`. This corrects the earlier assumption that before-
//     compaction was reachable only via the Plugin-SDK — it is not.
// Like the other Track A handlers it cannot be RUN here (no ts runtime / no
// OpenClaw SDK in this repo); the installer that materializes it is what the
// Python suite tests. Needs live-SDK validation before shipping.
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

// Compaction is on the critical path of the turn; keep the write short. Well
// under any reasonable compaction budget.
const WRITE_TIMEOUT_MS = 4000;

// Resolved at install time (Gap 1) as a real JSON argv ARRAY (C1): an absolute
// `["/abs/fulcra-coord"]` or `["<python>", "-m", "fulcra_coord"]`. A JSON array
// — not a string we `.split()` — so a spaced argv[0] survives intact.
const FULCRA_COORD_CMD: string[] = __FULCRA_COORD_ARGV_JSON__;

export default async function handler(event: any): Promise<void> {
  try {
    // type "session" + action "compact:before" — mirror the shutdown handler's
    // top-level type/action guard against the documented event shape.
    if (event?.type !== "session" || event?.action !== "compact:before") return;

    // The stable conversation bucket — survives compaction/daily/idle resets.
    // Top-level on InternalHookEvent (NOT under event.context). Keying the
    // pointer on sessionKey is exactly what lets it survive THIS event.
    const sessionKey: string | undefined = event?.sessionKey;
    if (!sessionKey) return;

    // FULCRA_COORD_SESSION_KEY lets the CLI key the pointer on the OpenClaw
    // sessionKey (see fulcra_coord/session_link.py), same as the shutdown hook.
    const childEnv = { ...process.env, FULCRA_COORD_SESSION_KEY: sessionKey };
    const { stdout } = await execFileAsync(
      FULCRA_COORD_CMD[0],
      [...FULCRA_COORD_CMD.slice(1), "__session-task", sessionKey],
      { timeout: WRITE_TIMEOUT_MS, env: childEnv },
    );
    const taskId = (stdout || "").trim();
    if (!taskId) return;

    // ALWAYS checkpoint: stamp a fresh summary so the task is not stale across
    // the context-loss boundary. Status stays active (session continues).
    await execFileAsync(
      FULCRA_COORD_CMD[0],
      [
        ...FULCRA_COORD_CMD.slice(1),
        "update",
        taskId,
        "--summary",
        "Compaction checkpoint (context about to be summarized; detail may be lost).",
      ],
      { timeout: WRITE_TIMEOUT_MS, env: childEnv },
    );
  } catch {
    // Fail-safe: never block or fail compaction on a coordination error.
    return;
  }
}
"""

# Managed hook directory names (named so uninstall can find them surgically).
SHUTDOWN_DIRNAME = "fulcra-coord-shutdown"
BOOTSTRAP_DIRNAME = "fulcra-coord-bootstrap"
COMPACT_DIRNAME = "fulcra-coord-compact"

# Hook sub-dirs: dirname -> {filename: contents}. These are wholly ours, so
# uninstall removes the whole directory.
_HOOK_DIRS: dict[str, dict[str, str]] = {
    SHUTDOWN_DIRNAME: {"HOOK.md": SHUTDOWN_HOOK_MD, "handler.ts": SHUTDOWN_HANDLER_TS},
    BOOTSTRAP_DIRNAME: {"HOOK.md": BOOTSTRAP_HOOK_MD, "handler.ts": BOOTSTRAP_HANDLER_TS},
    COMPACT_DIRNAME: {"HOOK.md": COMPACT_HOOK_MD, "handler.ts": COMPACT_HANDLER_TS},
}

# Prompt files merged via a tagged marker block at the hooks-root level.
_PROMPT_FILES: dict[str, str] = {
    "BOOT.md": BOOT_MD_BODY,
    "HEARTBEAT.md": HEARTBEAT_MD_BODY,
}


def _default_hooks_root() -> Path:
    """Default OpenClaw automation-hooks dir, overridable via env for tests.

    FULCRA_OPENCLAW_HOOKS_ROOT lets tests (and unusual installs) point the
    installer at an arbitrary tree without touching the real ~/.openclaw/.
    """
    env = os.environ.get("FULCRA_OPENCLAW_HOOKS_ROOT", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".openclaw" / "hooks"


def _managed_block(body: str) -> str:
    return f"{_BEGIN}\n{body.rstrip()}\n{_END}\n"


# Match our marker block (and any trailing whitespace) for surgical strip.
_BLOCK_RE = re.compile(
    re.escape(_BEGIN) + r".*?" + re.escape(_END) + r"\n?",
    re.DOTALL,
)


def _strip_block(text: str) -> str:
    return _BLOCK_RE.sub("", text)


def install_openclaw(*, dry_run: bool = False, uninstall: bool = False,
                     hooks_root: "str | Path | None" = None) -> dict[str, Any]:
    """Install/uninstall the OpenClaw Track A coordination artifacts.

    Materializes BOOT.md/HEARTBEAT.md (marker-fenced merge) and the
    fulcra-coord-shutdown / fulcra-coord-bootstrap automation hooks into
    ``hooks_root`` (default ``~/.openclaw/hooks/``, overridable via the
    ``hooks_root`` arg or ``FULCRA_OPENCLAW_HOOKS_ROOT`` env).

    Idempotent; ``dry_run`` writes nothing but reports the plan; ``uninstall``
    surgically removes only the managed content.
    """
    root = Path(hooks_root) if hooks_root is not None else _default_hooks_root()
    plan: dict[str, Any] = {
        "hooks_root": str(root),
        "uninstall": uninstall,
        "dry_run": dry_run,
        "hook_dirs": [],
        "prompt_files": [],
        "writes": [],
        "removes": [],
    }

    # --- compute prompt-file (BOOT.md/HEARTBEAT.md) merges/strips -----------
    for fname, body in _PROMPT_FILES.items():
        path = root / fname
        existing = path.read_text() if path.is_file() else ""
        stripped = _strip_block(existing)
        if uninstall:
            new_text = stripped
            # Only a "remove" if there was actually a managed block to drop.
            if new_text != existing:
                plan["removes"].append(str(path))
            # If the file is now empty (was *only* our block), delete it so we
            # leave no empty husk behind; otherwise rewrite the user's content.
            plan.setdefault("_prompt_actions", []).append(
                ("delete" if new_text.strip() == "" else "write", path, new_text))
        else:
            block = _managed_block(body)
            if stripped.strip():
                # Preserve the user's own content, append our block after it.
                new_text = stripped.rstrip() + "\n\n" + block
            else:
                new_text = block
            plan["prompt_files"].append(str(path))
            plan["writes"].append(str(path))
            plan.setdefault("_prompt_actions", []).append(("write", path, new_text))

    # --- compute hook-dir (HOOK.md + handler.ts) writes/removes ------------
    for dirname, files in _HOOK_DIRS.items():
        hdir = root / dirname
        if uninstall:
            if hdir.exists():
                plan["removes"].append(str(hdir))
            plan.setdefault("_hook_actions", []).append(("rmtree", hdir, None))
        else:
            plan["hook_dirs"].append(str(hdir))
            for hfname, body in files.items():
                fpath = hdir / hfname
                plan["writes"].append(str(fpath))
                plan.setdefault("_hook_actions", []).append(("write", fpath, body))

    if dry_run:
        # Strip the private action lists from the returned plan: callers only
        # need the human-readable "what would happen" lists, not the closures.
        plan.pop("_prompt_actions", None)
        plan.pop("_hook_actions", None)
        return plan

    # --- materialize -------------------------------------------------------
    import json as _json
    import shutil as _shutil
    # Gap 1 + C1: bake a concretely-callable CLI invocation into the handler
    # sources at materialize time. The handler.ts templates carry a JSON-array
    # placeholder (__FULCRA_COORD_ARGV_JSON__) substituted with the resolved argv
    # as a JSON array literal, so a spaced argv[0] is preserved (no `.split()`).
    # The BOOT.md / HEARTBEAT.md prose files reference only the bare command name
    # in human instructions, so they need no substitution. The committed parity
    # copies (if any) keep the literal placeholder.
    from .cli_invocation import resolve_cli_argv, resolve_cli_command
    argv = resolve_cli_argv()
    argv_json = _json.dumps(argv)
    plan["resolved_cli"] = resolve_cli_command()  # display string only

    def _materialize(body: str) -> str:
        return body.replace(PLACEHOLDER_ARGV_JSON, argv_json)

    for action, path, body in plan.pop("_prompt_actions", []):
        if action == "delete":
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:  # write
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_materialize(body))

    for action, path, body in plan.pop("_hook_actions", []):
        if action == "rmtree":
            _shutil.rmtree(path, ignore_errors=True)
        else:  # write
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_materialize(body))

    return plan
