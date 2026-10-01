#!/usr/bin/env node
import {
  planEnrollment,
  managedInstructions,
} from "../src/lib/server/gatekeeper/enrollment.js";
import { exact } from "../src/lib/server/gatekeeper/listener-validation.js";

try {
  const [action, ...args] = process.argv.slice(2);
  if (!["plan", "instructions"].includes(action) || args.length)
    throw new Error("Invalid action");
  let size = 0;
  const chunks = [];
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > 1024 * 1024) throw new Error("Input limit");
    chunks.push(chunk);
  }
  const input = JSON.parse(
    new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)),
  );
  let result;
  if (action === "plan") result = planEnrollment(input);
  else {
    exact(input, ["content", "workspace_id", "remove"], ["instruction_mode"]);
    result = {
      content: managedInstructions(
        input.content,
        input.workspace_id,
        input.remove,
        input.instruction_mode,
      ),
    };
  }
  process.stdout.write(JSON.stringify(result) + "\n");
} catch {
  process.stdout.write(
    JSON.stringify({ error: { code: "INVALID_ENROLLMENT_INPUT" } }) + "\n",
  );
  process.exitCode = 1;
}
