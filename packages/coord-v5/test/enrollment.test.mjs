import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { managedInstructions } from "../src/lib/server/gatekeeper/enrollment.js";
const bin = fileURLToPath(new URL("../bin/coord-v5.mjs", import.meta.url));
const run = (verb, input, cwd) =>
  spawnSync(process.execPath, [bin, "enrollment", verb], {
    input: JSON.stringify(input),
    encoding: "utf8",
    cwd,
  });
test("enrollment CLI plans native events without scheduling and retains authorization boundaries", () => {
  const observed_at = "2026-09-28T12:00:00Z";
  const capability = { status: "verified", observed_at, evidence_ref: "receipt:synthetic" };
  const result = run("plan", {
    version: 1,
    principal_id: "example",
    workspace_id: "example",
    environment_id: "local",
    harness: "codex-desktop",
    harness_version: "test",
    unattended: true,
    evaluated_at: observed_at,
    capabilities: Object.fromEntries(["read", "publish", "checkpoint", "dispatch", "event_ingress"].map(name => [name, capability])),
  });
  assert.equal(result.status, 0, result.stdout);
  const plan = JSON.parse(result.stdout);
  assert.equal(plan.mode, "native-event");
  assert.equal(plan.membership, "requires_authorized_grant");
  assert.equal(plan.service_level, "trial");
  assert.equal(plan.deployment_status, "not_installed");
  assert.doesNotMatch(plan.next_steps.join(" "), /Keep the schedule alive/);
});
test("enrollment CLI selects interactive and never echoes unknown secret fields", () => {
  const input = {
    version: 1,
    principal_id: "example",
    workspace_id: "example",
    environment_id: "local",
    harness: "codex-desktop",
    harness_version: "test",
    unattended: false,
    evaluated_at: "2026-09-28T12:00:00Z",
    capabilities: {},
  };
  const result = run("plan", input);
  assert.equal(result.status, 0, result.stdout);
  assert.equal(JSON.parse(result.stdout).mode, "inquiry-only");
  input.secret = "never-echo-this";
  const invalid = run("plan", input);
  assert.equal(invalid.status, 1);
  assert.equal(invalid.stdout.includes(input.secret), false);
});
// Catches optional-key rejection, wrong forwarding, envelope drift and accidental file writes.
test("instruction CLI converts every mode and removes without touching actual local instructions", () => {
  const directory = mkdtempSync(join(tmpdir(), "coord-v5-instructions-"));
  try {
    const original = "# Synthetic user rules without newline";
    const file = join(directory, "AGENTS.md");
    writeFileSync(file, original);
    const convert = (content, mode, remove = false) => {
      const result = run("instructions", { content, workspace_id: "example", remove,
        ...(mode === undefined ? {} : { instruction_mode: mode }) }, directory);
      assert.equal(result.status, 0, result.stdout);
      assert.equal(result.stderr, "");
      const output = JSON.parse(result.stdout);
      assert.deepEqual(Object.keys(output), ["content"]);
      assert.equal(readFileSync(file, "utf8"), original);
      assert.deepEqual(readdirSync(directory), ["AGENTS.md"]);
      return output.content;
    };
    const worker = managedInstructions(original, "example"); // API golden-byte contract is tested separately.
    assert.equal(convert(original), worker);
    assert.equal(convert(original, "worker"), worker);
    for (const source of ["worker", "listener", "executor"]) {
      const added = convert(original, source);
      if (source !== "worker") assert.notEqual(added, worker);
      for (const target of ["worker", "listener", "executor"]) {
        const changed = convert(added + "suffix", target);
        assert.equal(changed, managedInstructions(original, "example", false, target) + "suffix");
        assert.equal(convert(changed, target), changed);
        for (const removeMode of ["worker", "listener", "executor"])
          assert.equal(convert(changed, removeMode, true), original + "suffix");
      }
    }
    const rejected = run("instructions", { content: original, workspace_id: "example", remove: true,
      instruction_mode: "invalid-mode" }, directory);
    assert.equal(rejected.status, 1);
    assert.deepEqual(JSON.parse(rejected.stdout), { error: { code: "INVALID_ENROLLMENT_INPUT" } });
    assert.equal(readFileSync(file, "utf8"), original);
    assert.deepEqual(readdirSync(directory), ["AGENTS.md"]);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
// Catches mode validation bypass on add, replace and remove, and private input echo.
test("instruction CLI rejects mode enums and types with one sanitized failure envelope", () => {
  const sentinel = "synthetic-private-content-never-echo";
  for (const instruction_mode of ["unknown", "", null, 0, false, {}, [], "Listener"])
    for (const content of [sentinel, managedInstructions(sentinel, "example")])
      for (const remove of [false, true]) {
        const result = run("instructions", { content, workspace_id: "example", remove, instruction_mode });
        assert.equal(result.status, 1);
        assert.deepEqual(JSON.parse(result.stdout), { error: { code: "INVALID_ENROLLMENT_INPUT" } });
        assert.equal(result.stderr, "");
        assert.equal((result.stdout + result.stderr).includes(sentinel), false);
      }
});
