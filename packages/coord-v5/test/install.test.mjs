import { test } from "node:test";
import assert from "node:assert/strict";
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";

// Catches omitted runtime files/exports, prototype-only imports, a broken installed
// bin, and accidental shipping of private/app content. No registry is contacted.
test("packed install runs independently and ships only the public runtime", () => {
  const directory = mkdtempSync(join(tmpdir(), "coord-v5-install-"));
  try {
    const packed = spawnSync(
      "npm",
      ["pack", "--json", "--pack-destination", directory],
      {
        cwd: resolve(import.meta.dirname, ".."),
        encoding: "utf8",
      },
    );
    assert.equal(packed.status, 0, packed.stderr);
    const packageInfo = JSON.parse(packed.stdout)[0];
    const paths = packageInfo.files.map((file) => file.path);
    assert.ok(paths.includes("bin/coord-v5.mjs"));
    assert.ok(paths.includes("src/lib/gatekeeper/work-contract.js"));
    assert.ok(
      paths.every(
        (path) =>
          !/(node_modules|\.env|\.sqlite|\.db|routes\/|svelte|\.test\.|test\/)/i.test(
            path,
          ),
      ),
    );
    const installed = spawnSync(
      "npm",
      [
        "install",
        "--offline",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        join(directory, packageInfo.filename),
      ],
      {
        cwd: directory,
        encoding: "utf8",
      },
    );
    assert.equal(installed.status, 0, installed.stderr);
    const executable = join(directory, "node_modules", ".bin", "coord-v5");
    const help = spawnSync(executable, ["help"], {
      cwd: directory,
      encoding: "utf8",
    });
    assert.equal(help.status, 0, help.stderr);
    assert.match(help.stdout, /coord-v5/);
    assert.match(help.stdout, /explicit enrollment/i);
    const unknown = spawnSync(executable, ["not-a-command"], {
      cwd: directory,
      encoding: "utf8",
    });
    assert.notEqual(unknown.status, 0);
    const scope = JSON.stringify({
      principalId: "synthetic",
      workspaceId: "synthetic",
      environmentId: "test",
      harness: "codex",
    });
    const listenerDb = join(directory, "listener.sqlite");
    const inspectListener = spawnSync(
      executable,
      [
        "listener",
        "inspect",
        "--db",
        listenerDb,
        "--scope",
        scope,
        "--holder",
        "test",
      ],
      { cwd: directory, encoding: "utf8" },
    );
    assert.equal(inspectListener.status, 0, inspectListener.stderr);
    assert.deepEqual(JSON.parse(inspectListener.stdout), { snapshot: null });
    assert.equal(existsSync(listenerDb), false);
    const principal = "00000000-0000-4000-8000-000000000900";
    const config = {
      baseUrl: "https://api.fulcradynamics.com/",
      principalId: principal,
      channel: "MomentAnnotation/00000000-0000-4000-8000-000000000901",
      workspaceId: "00000000-0000-4000-8000-000000000100",
      workstreamId: "00000000-0000-4000-8000-000000000300",
      actorBinding: {
        principal_id: principal,
        logical_agent_id: "test",
        instance_id: "test",
        session_id: "test",
      },
    };
    const configPath = join(directory, "config.json");
    writeFileSync(configPath, JSON.stringify(config), { mode: 0o600 });
    const cold = spawnSync(
      executable,
      [
        "transport",
        "replay",
        "--config",
        configPath,
        "--db",
        join(directory, "work.sqlite"),
      ],
      { cwd: directory, encoding: "utf8" },
    );
    assert.equal(cold.status, 2, cold.stderr);
    assert.equal(JSON.parse(cold.stdout).coverage, "unavailable");
    assert.notEqual(JSON.parse(cold.stdout).clear, true);
    const observation = spawnSync(
      executable,
      [
        "observation",
        "--config",
        "relative.json",
        "--policy",
        "relative.json",
        "--db",
        "relative.sqlite",
      ],
      { cwd: directory, encoding: "utf8" },
    );
    assert.notEqual(observation.status, 0);
    assert.equal(JSON.parse(observation.stdout).error.code, "UNSAFE_FILE");
    const imported = spawnSync(
      process.execPath,
      [
        "--input-type=module",
        "-e",
        `
      import { contract, replay } from '@fulcra/coord-v5';
      import { validateWorkEvent } from '@fulcra/coord-v5/work-contract';
      if (contract.parseWorkNote('not-json').ok !== false || validateWorkEvent({}).ok !== false) process.exit(2);
      const view = replay.replayWorkEvents({events:[]});
      if (view.observation.coverage !== 'unavailable' || view.work.length !== 0) process.exit(3);
      for (const module of ['work-digest','checkpoint','handoff','protocol','projection','listener','work-transport-config','work-transport-read','work-transport-store','work-transport-publish','listener-validation','listener-store','listener-runtime','work-listener','work-view']) await import('@fulcra/coord-v5/' + module);
      console.log('independent-runtime-ok');
    `,
      ],
      { cwd: directory, encoding: "utf8" },
    );
    assert.equal(imported.status, 0, imported.stderr);
    assert.match(imported.stdout, /independent-runtime-ok/);
    const root = join(directory, "node_modules", "@fulcra", "coord-v5");
    function inspect(path) {
      for (const entry of readdirSync(path, { withFileTypes: true })) {
        const target = join(path, entry.name);
        if (entry.isDirectory()) inspect(target);
        else
          assert.doesNotMatch(
            readFileSync(target, "utf8"),
            /\/Users\/|gatekeeper-v5\/node_modules/,
          );
      }
    }
    inspect(root);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
