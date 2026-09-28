import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
const bin = fileURLToPath(new URL("../bin/coord-v5.mjs", import.meta.url));
const run = (verb, input) =>
  spawnSync(process.execPath, [bin, "enrollment", verb], {
    input: JSON.stringify(input),
    encoding: "utf8",
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
test("instruction CLI returns reversible local content, no filesystem mutation", () => {
  const original = "# User rules\n";
  const added = run("instructions", {
    content: original,
    workspace_id: "example",
    remove: false,
  });
  assert.equal(added.status, 0, added.stdout);
  const changed = JSON.parse(added.stdout).content;
  assert.notEqual(changed, original);
  const removed = run("instructions", {
    content: changed,
    workspace_id: "example",
    remove: true,
  });
  assert.equal(removed.status, 0, removed.stdout);
  assert.equal(JSON.parse(removed.stdout).content, original);
});
