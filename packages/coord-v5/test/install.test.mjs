import { test } from "node:test";
import assert from "node:assert/strict";
import {
  chmodSync,
  existsSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { workContentDigest } from "../src/lib/gatekeeper/work-contract.js";

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
    assert.match(help.stdout, /checkpoint verify --artifact ABS --publication ABS/);
    const checkpoint = JSON.parse(readFileSync(resolve(import.meta.dirname, "../tests/fixtures/work-handoff-synthetic.json"), "utf8"));
    const backlog = JSON.parse(readFileSync(resolve(import.meta.dirname, "../tests/fixtures/work-backlog-synthetic.json"), "utf8"));
    const baseEvent = backlog.events[0];
    const artifactBytes = Buffer.from(JSON.stringify(checkpoint));
    const artifactPath = join(directory, "checkpoint.json");
    const publicationPath = join(directory, "publication.json");
    const digest = createHash("sha256").update(artifactBytes).digest("hex");
    const publication = {
      ...baseEvent,
      event_id: "20000000-0000-4000-8000-000000000001",
      operation_id: "20000000-0000-4000-8000-000000000002",
      kind: "checkpoint.published",
      subject: {type:"checkpoint", id:checkpoint.checkpoint_id},
      parents: [checkpoint.assignment_event_id],
      payload: {
        work_id: checkpoint.work_id,
        checkpoint_id: checkpoint.checkpoint_id,
        artifact: {id:checkpoint.checkpoint_id, uri:"https://example.invalid/checkpoint.json", sha256:digest, version:null, media_type:"application/json", owner_principal_id:checkpoint.identity.principal_id, audience:"workspace", portable:true},
        body_digest:workContentDigest(checkpoint),
        assignment_version:checkpoint.assignment_version,
        assignment_event_id:checkpoint.assignment_event_id,
      },
    };
    writeFileSync(artifactPath, artifactBytes, {mode:0o600});
    writeFileSync(publicationPath, JSON.stringify(publication), {mode:0o600});
    const verify = (artifact = artifactPath, pub = publicationPath) => spawnSync(executable, ["checkpoint", "verify", "--artifact", artifact, "--publication", pub], {cwd:directory, encoding:"utf8"});
    const good = verify();
    assert.equal(good.status, 0, good.stdout);
    assert.equal(JSON.parse(good.stdout).verification.artifact_sha256, digest);
    writeFileSync(artifactPath, Buffer.concat([artifactBytes, Buffer.from("\n")]), {mode:0o600});
    const badDigest = verify();
    assert.equal(badDigest.status, 2);
    assert.deepEqual(JSON.parse(badDigest.stdout), {ok:false, errors:["ARTIFACT_DIGEST_MISMATCH"]});
    writeFileSync(artifactPath, artifactBytes, {mode:0o600});
    chmodSync(artifactPath, 0o644);
    assert.equal(JSON.parse(verify().stdout).error.code, "UNSAFE_FILE");
    chmodSync(artifactPath, 0o600);
    symlinkSync(artifactPath, join(directory, "linked.json"));
    assert.equal(JSON.parse(verify(join(directory, "linked.json")).stdout).error.code, "UNSAFE_FILE");
    assert.equal(JSON.parse(verify(join(directory, "missing.json")).stdout).error.code, "UNSAFE_FILE");
    chmodSync(publicationPath, 0o644);
    assert.equal(JSON.parse(verify().stdout).error.code, "UNSAFE_FILE");
    chmodSync(publicationPath, 0o600);
    writeFileSync(publicationPath, Buffer.alloc(64 * 1024 + 1), {mode:0o600});
    assert.equal(JSON.parse(verify().stdout).error.code, "FILE_LIMIT");
    writeFileSync(publicationPath, JSON.stringify(publication), {mode:0o600});
    writeFileSync(artifactPath, Buffer.alloc(256 * 1024 + 1), {mode:0o600});
    assert.equal(JSON.parse(verify().stdout).error.code, "FILE_LIMIT");
    assert.doesNotMatch(verify().stdout, /checkpoint\.json|example\.invalid/);
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
      for (const module of ['work-digest','checkpoint','handoff','protocol','projection','listener','work-transport-config','work-transport-read','work-transport-store','work-transport-publish','listener-validation','listener-store','listener-runtime','work-listener','work-view','work-presence','work-roles','enrollment']) await import('@fulcra/coord-v5/' + module);
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
