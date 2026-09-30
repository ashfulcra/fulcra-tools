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
       coord-v5 work view --config ABS --db ABS --policy ABS
       coord-v5 work digest --config ABS --db ABS --policy ABS --viewer ID --role ROLE --query QUERY
       coord-v5 event validate < event.json
       coord-v5 checkpoint <validate|prepare|package> < input.json
       coord-v5 checkpoint verify --artifact ABS --publication ABS
       coord-v5 handoff readiness < input.json
       coord-v5 handoff verification import --config ABS --db ABS --receipt ABS
       coord-v5 enrollment <plan|instructions> < input.json

Transport requires explicit enrollment and independent owned-source verification.
Transport read/publish receive the bearer on stdin; config/event files stay private.
Listener prepares action descriptors only; it never invokes a harness or scheduler.
Replay without grants cannot establish authorized work or a clear result.
Pure stdin commands do not publish or independently verify caller-supplied checks.
Handoff verification import retains an operator-trusted local attestation, not remote proof.
See the installed README for exact arguments and limits.\n`);
  } else {
    const scripts = {
      transport: "../scripts/gatekeeper-work-transport.mjs",
      listener: "../scripts/gatekeeper-listener.mjs",
      observation: "../scripts/work-listener-observation.mjs",
      work: "../scripts/coord-v5-operations.mjs",
      event: "../scripts/coord-v5-operations.mjs",
      checkpoint: "../scripts/coord-v5-operations.mjs",
      handoff: "../scripts/coord-v5-operations.mjs",
      enrollment: "../scripts/coord-v5-enrollment.mjs",
    };
    if (!Object.hasOwn(scripts, command)) {
      process.stdout.write(
        JSON.stringify({ error: { code: "UNKNOWN_COMMAND" } }) + "\n",
      );
      process.exitCode = 1;
    } else {
      const result = spawnSync(
        process.execPath,
        [
          fileURLToPath(new URL(scripts[command], import.meta.url)),
          ...(["work", "event", "checkpoint", "handoff"].includes(command)
            ? [command]
            : []),
          ...args,
        ],
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
