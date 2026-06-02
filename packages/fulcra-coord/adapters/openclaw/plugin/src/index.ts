// fulcra-coord OpenClaw plugin (Track B) — deterministic per-session +
// before-compaction checkpoints to the fulcra-coord coordination bus.
//
// This is the higher-fidelity upgrade over Track A's file-based automation
// hooks. It registers three in-process Plugin-SDK lifecycle hooks that Track A
// cannot reach as file-based events — most importantly `before_compaction`,
// the guarantee Track A lacks.
//
// EVERY SDK call below is validated against the published OpenClaw source
// (github.com/openclaw/openclaw) and docs (docs.openclaw.ai/plugins/hooks).
// The plugin cannot be RUN in the fulcra-coord repo (no OpenClaw runtime here);
// the install glue that materializes it is what the Python suite tests.
//
// Source citations (openclaw/openclaw @ main):
//   * Entry point `definePluginEntry({ id, name, description, register(api) })`
//     — src/plugin-sdk/plugin-entry.ts; real-world example
//     extensions/memory-lancedb/index.ts L1410-1417.
//   * Hook registration `api.on(hookName, (event, ctx) => {...})` — same
//     example, L2007 (`api.on("session_end", (event, ctx) => {...})`).
//   * session_start: (PluginHookSessionStartEvent { sessionId, sessionKey?,
//     resumedFrom? }, PluginHookSessionContext { agentId?, sessionId,
//     sessionKey? }) — src/plugins/hook-types.ts L609-613, L603-607, L1090-1093.
//   * before_compaction: (PluginHookBeforeCompactionEvent { messageCount,
//     compactingCount?, tokenCount?, messages?, sessionFile? },
//     PluginHookAgentContext { agentId?, sessionKey?, sessionId?, ... }) —
//     hook-types.ts L381-387, L228+, L1031-1034. NOTE: the event carries NO
//     sessionKey; it is read from ctx.
//   * session_end: (PluginHookSessionEndEvent { sessionId, sessionKey?,
//     messageCount, durationMs?, reason?, sessionFile?, nextSessionId?,
//     nextSessionKey? }, PluginHookSessionContext) — hook-types.ts L626-636,
//     L1094-1097. reason enum: new|reset|idle|daily|compaction|deleted|
//     shutdown|restart|unknown (L615-624).
//
// The same-CLI bridge: each hook shells `fulcra-coord` exactly like the Track A
// handlers and the docs' execFileAsync example — the one CLI every adapter
// drives. Hooks are fully fail-safe: any error is swallowed so a coordination
// failure never blocks a session, compaction, or shutdown.

import { execFile } from "node:child_process";
import { promisify } from "node:util";
// Subpath import per the SDK guidance ("always import from a specific subpath").
// docs.openclaw.ai/plugins/sdk-overview; src/plugin-sdk/plugin-entry.ts.
import {
  definePluginEntry,
  type OpenClawPluginApi,
} from "openclaw/plugin-sdk/plugin-entry";

const execFileAsync = promisify(execFile);

// Reads are off the critical path (CLI's normal read budget); writes must stay
// well under OpenClaw's bounded session_end drain on gateway shutdown.
const READ_TIMEOUT_MS = 8000;
const WRITE_TIMEOUT_MS = 4000;

// session_end reasons that mean "this session is really going away" → park the
// task as waiting. We deliberately SKIP `compaction` (the session continues;
// before_compaction already checkpointed it) and `new`/`unknown` (not a real
// teardown of in-flight work). Mirrors the spec's Checkpoint 3.
const PARK_REASONS = new Set<string>(["idle", "daily", "reset", "deleted", "shutdown", "restart"]);

/** Build the child env carrying the stable sessionKey so the CLI keys its
 *  session→task pointer on the OpenClaw bucket (FULCRA_COORD_SESSION_KEY is the
 *  generic fallback Track A added; see fulcra_coord/session_link.py). */
function childEnv(sessionKey: string | undefined): NodeJS.ProcessEnv {
  return sessionKey
    ? { ...process.env, FULCRA_COORD_SESSION_KEY: sessionKey }
    : process.env;
}

/** Resolve the task this session owns via the CLI's session pointer. Returns ""
 *  when there is no pointer (no claimed task) — callers then no-op. */
async function resolveSessionTask(
  sessionKey: string,
  env: NodeJS.ProcessEnv,
): Promise<string> {
  const { stdout } = await execFileAsync(
    "fulcra-coord",
    ["__session-task", sessionKey],
    { timeout: WRITE_TIMEOUT_MS, env },
  );
  return (stdout || "").trim();
}

export default definePluginEntry({
  id: "fulcra-coord",
  name: "Fulcra Coordination",
  description:
    "Deterministic per-session and before-compaction checkpoints to the fulcra-coord bus.",
  register(api: OpenClawPluginApi) {
    // ----------------------------------------------------------------------
    // Checkpoint 1 — session_start: surface in-flight + stale work.
    // Reads `fulcra-coord status` and logs/announces it so a fresh session sees
    // what may have been left running. sessionKey: prefer ctx then event
    // (mirrors the memory-lancedb example's `ctx.sessionKey ?? event.sessionKey`).
    // ----------------------------------------------------------------------
    api.on("session_start", async (event, ctx) => {
      try {
        const sessionKey = ctx?.sessionKey ?? event?.sessionKey;
        const { stdout } = await execFileAsync(
          "fulcra-coord",
          ["status"],
          { timeout: READ_TIMEOUT_MS, env: childEnv(sessionKey) },
        );
        const summary = (stdout || "").trim();
        if (summary) {
          api.logger.info(
            `fulcra-coord: in-flight / possibly-forgotten work at session start:\n${summary}`,
          );
        }
      } catch {
        // Fail-safe: surfacing is best-effort; never block session start.
      }
    });

    // ----------------------------------------------------------------------
    // Checkpoint 2 — before_compaction: ALWAYS checkpoint the session's task.
    // This is the guarantee Track A lacks. The event carries no sessionKey, so
    // we read it from ctx (PluginHookAgentContext). Stamp a fresh timestamp via
    // `update` so the task does not look stale across the context-loss boundary;
    // status stays `active`.
    // ----------------------------------------------------------------------
    api.on("before_compaction", async (_event, ctx) => {
      try {
        const sessionKey = ctx?.sessionKey;
        if (!sessionKey) return;
        const env = childEnv(sessionKey);
        const taskId = await resolveSessionTask(sessionKey, env);
        if (!taskId) return;
        await execFileAsync(
          "fulcra-coord",
          [
            "update",
            taskId,
            "--summary",
            "Pre-compaction checkpoint (context about to be summarized).",
          ],
          { timeout: WRITE_TIMEOUT_MS, env },
        );
      } catch {
        // Fail-safe: never block or fail compaction on a coordination error.
      }
    });

    // ----------------------------------------------------------------------
    // Checkpoint 3 — session_end: park active→waiting on a real teardown.
    // Skip `compaction` (session continues) and reasons that are not a teardown
    // of in-flight work. Keyed on sessionKey (ctx then event).
    // ----------------------------------------------------------------------
    api.on("session_end", async (event, ctx) => {
      try {
        const reason = event?.reason ?? "unknown";
        if (!PARK_REASONS.has(reason)) return;
        const sessionKey = ctx?.sessionKey ?? event?.sessionKey;
        if (!sessionKey) return;
        const env = childEnv(sessionKey);
        const taskId = await resolveSessionTask(sessionKey, env);
        if (!taskId) return;
        await execFileAsync(
          "fulcra-coord",
          [
            "pause",
            taskId,
            "--next",
            `Session ended (${reason}); resume from last next_action.`,
          ],
          { timeout: WRITE_TIMEOUT_MS, env },
        );
      } catch {
        // Fail-safe: never block or fail session teardown on a coordination error.
      }
    });
  },
});
