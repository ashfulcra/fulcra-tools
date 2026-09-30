#!/usr/bin/env node
import { lstatSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { validateWorkEvent } from "../src/lib/gatekeeper/work-contract.js";
import {
  validateCheckpoint,
  prepareCheckpoint,
  verifyCheckpointArtifact,
  buildHandoffPackage,
} from "../src/lib/gatekeeper/checkpoint.js";
import { assessHandoffReadiness } from "../src/lib/gatekeeper/handoff.js";
import { buildWorkDigest } from "../src/lib/gatekeeper/work-digest.js";
import { openWorkTransportStore } from "../src/lib/server/gatekeeper/work-transport-store.js";
import { validateWorkTransportConfig } from "../src/lib/server/gatekeeper/work-transport-config.js";
import { buildAuthorizedWorkView } from "../src/lib/server/gatekeeper/work-view.js";
import { presenceRowsForDigest } from "../src/lib/gatekeeper/work-presence.js";

const INPUT_LIMIT = 1024 * 1024;
const FILE_LIMIT = 64 * 1024;
const RECEIPT_FILE_LIMIT = 2 * 1024 * 1024;
const CHECKPOINT_ARTIFACT_LIMIT = 256 * 1024;
/** @param {string} path */
function privatePath(path) {
  if (
    typeof path !== "string" ||
    !isAbsolute(path) ||
    resolve(path) !== path ||
    path.includes("\0")
  )
    throw new Error("UNSAFE_FILE");
  try {
    const parent = lstatSync(dirname(path));
    const file = lstatSync(path);
    if (
      !parent.isDirectory() ||
      parent.isSymbolicLink() ||
      (parent.mode & 0o077) !== 0 ||
      !file.isFile() ||
      file.isSymbolicLink() ||
      (file.mode & 0o077) !== 0
    )
      throw new Error("UNSAFE_FILE");
    return file;
  } catch {
    throw new Error("UNSAFE_FILE");
  }
}
/** @param {string} path */
function privateJson(path, limit = FILE_LIMIT) {
  const bytes = privateBytes(path, limit);
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    throw new Error("INVALID_JSON");
  }
}
/** @param {string} path @param {number} limit */
function privateBytes(path, limit) {
  if (privatePath(path).size > limit) throw new Error("FILE_LIMIT");
  const bytes = readFileSync(path);
  if (bytes.byteLength > limit) throw new Error("FILE_LIMIT");
  return bytes;
}
async function stdinJson() {
  const chunks = [];
  let count = 0;
  for await (const chunk of process.stdin) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    count += bytes.byteLength;
    if (count > INPUT_LIMIT) throw new Error("INPUT_LIMIT");
    chunks.push(bytes);
  }
  try {
    return JSON.parse(
      new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)),
    );
  } catch {
    throw new Error("INVALID_JSON");
  }
}
/** @param {string[]} args @param {string[]} keys */
function flags(args, keys) {
  if (args.length !== keys.length * 2) throw new Error("INVALID_ARGUMENTS");
  const values = {};
  for (let i = 0; i < args.length; i += 2) {
    const key = args[i]?.slice(2);
    if (
      args[i] !== `--${key}` ||
      !keys.includes(key) ||
      Object.hasOwn(values, key) ||
      !args[i + 1]
    )
      throw new Error("INVALID_ARGUMENTS");
    values[key] = args[i + 1];
  }
  return values;
}
async function main() {
  const [group, action, ...args] = process.argv.slice(2);
  if (group === "checkpoint" && action === "verify") {
    const values = flags(args, ["artifact", "publication"]);
    const bytes = privateBytes(values.artifact, CHECKPOINT_ARTIFACT_LIMIT);
    const publication = privateJson(values.publication);
    return verifyCheckpointArtifact(bytes, publication);
  }
  if (group === "handoff" && action === "verification" && args[0] === "import") {
    const values = flags(args.slice(1), ["config", "db", "receipt"]);
    const config = validateWorkTransportConfig(privateJson(values.config));
    const receipt = privateJson(values.receipt, RECEIPT_FILE_LIMIT);
    privatePath(values.db);
    const store = openWorkTransportStore({ config, dbPath: values.db });
    try {
      return store.importHandoffVerification(receipt, { now: Date.now() });
    } finally {
      store.close();
    }
  }
  if (group === "work" && ["view", "digest"].includes(action)) {
    const keys = [
      "config",
      "db",
      "policy",
      ...(action === "digest" ? ["viewer", "role", "query"] : []),
    ];
    const values = flags(args, keys);
    if (
      action === "digest" &&
      (!/^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$/.test(values.viewer) ||
        !["member", "owner", "coordinator"].includes(values.role) ||
        ![
          "everything_owed",
          "needs_me",
          "completed_history",
          "lost_track",
        ].includes(values.query))
    )
      throw new Error("INVALID_ARGUMENTS");
    const config = validateWorkTransportConfig(privateJson(values.config));
    const policy = privateJson(values.policy);
    privatePath(values.db);
    const store = openWorkTransportStore({ config, dbPath: values.db });
    try {
      const view = buildAuthorizedWorkView({ store, policy, now: Date.now });
      if (view.status !== "ready") return view;
      if (action === "view") return view;
      return buildWorkDigest({
        projection: view.projection,
        viewerId: values.viewer,
        viewerRole: values.role,
        query: values.query,
        asOf: view.projection.as_of,
        evaluatedAt: view.evaluated_at,
        presence: presenceRowsForDigest({ rows: view.presence }),
      });
    } finally {
      store.close();
    }
  }
  if (args.length !== 0) throw new Error("INVALID_ARGUMENTS");
  const command = `${group} ${action}`;
  if (
    ![
      "event validate",
      "checkpoint validate",
      "checkpoint prepare",
      "checkpoint package",
      "handoff readiness",
    ].includes(command)
  )
    throw new Error("INVALID_ARGUMENTS");
  const input = await stdinJson();
  if (command === "event validate") return validateWorkEvent(input);
  if (command === "checkpoint validate") return validateCheckpoint(input);
  if (command === "checkpoint prepare") return prepareCheckpoint(input);
  if (command === "checkpoint package") return buildHandoffPackage(input);
  return assessHandoffReadiness(input);
}
try {
  const result = await main();
  process.stdout.write(`${JSON.stringify(result)}\n`);
  if (
    result.ok === false ||
    ["blocked", "unavailable", "unknown"].includes(result.status)
  )
    process.exitCode = 2;
} catch (error) {
  const code =
    error instanceof Error &&
    [
      "UNSAFE_FILE",
      "FILE_LIMIT",
      "INVALID_JSON",
      "INPUT_LIMIT",
      "INVALID_ARGUMENTS",
      "INVALID_CONFIG",
    ].includes(error.message)
      ? error.message
      : "OPERATION_UNAVAILABLE";
  process.stdout.write(`${JSON.stringify({ error: { code } })}\n`);
  process.exitCode = 2;
}
