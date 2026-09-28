#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const [major, minor] = process.versions.node.split(".").map(Number);
if (major < 22 || (major === 22 && minor < 16)) {
  process.stdout.write(
    JSON.stringify({ error: { code: "NODE_22_16_REQUIRED" } }) + "\n",
  );
  process.exitCode = 1;
} else {
  const [command, ...args] = process.argv.slice(2);
  if (!command || ["help", "--help", "-h"].includes(command)) {
    process.stdout.write(`coord-v5 — opt-in coordination alpha\n
Usage: coord-v5 transport <read|publish|inspect|replay> [flags]
       coord-v5 listener <configure|prepare|settle|ack|inspect> [flags]
       coord-v5 observation --config ABS --policy ABS --db ABS

Transport is pinned synthetic in this release, not general production configuration.
Transport read/publish receive the bearer on stdin; config/event files stay private.
Listener prepares action descriptors only; it never invokes a harness or scheduler.
Replay without grants cannot establish authorized work or a clear result.
Core digest/checkpoint/package/readiness APIs are exports, not CLI verbs yet.
See the installed README for exact arguments and limits.\n`);
  } else {
    const scripts = {
      transport: "../scripts/gatekeeper-work-transport.mjs",
      listener: "../scripts/gatekeeper-listener.mjs",
      observation: "../scripts/work-listener-observation.mjs",
    };
    if (!Object.hasOwn(scripts, command)) {
      process.stdout.write(
        JSON.stringify({ error: { code: "UNKNOWN_COMMAND" } }) + "\n",
      );
      process.exitCode = 1;
    } else {
      const result = spawnSync(
        process.execPath,
        [fileURLToPath(new URL(scripts[command], import.meta.url)), ...args],
        { stdio: "inherit" },
      );
      if (result.error)
        process.stdout.write(
          JSON.stringify({ error: { code: "COMMAND_UNAVAILABLE" } }) + "\n",
        );
      process.exitCode = result.status ?? 1;
    }
  }
}
