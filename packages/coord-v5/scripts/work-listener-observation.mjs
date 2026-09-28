#!/usr/bin/env node
import { lstatSync, readFileSync } from 'node:fs';
import { basename, dirname, isAbsolute, resolve } from 'node:path';
import { validateWorkTransportConfig } from '../src/lib/server/gatekeeper/work-transport-config.js';
import { openWorkTransportStore } from '../src/lib/server/gatekeeper/work-transport-store.js';
import { buildWorkListenerObservation } from '../src/lib/server/gatekeeper/work-listener.js';

const MAX_FILE = 64 * 1024;
/** @param {string[]} argv */
function argsOf(argv) {
  if (argv.length !== 6) throw new Error('INVALID_ARGUMENTS');
  const args = {};
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i]?.slice(2);
    if (
      !['config', 'policy', 'db'].includes(key) ||
      argv[i] !== `--${key}` ||
      Object.hasOwn(args, key) ||
      !argv[i + 1]
    )
      throw new Error('INVALID_ARGUMENTS');
    args[key] = argv[i + 1];
  }
  return args;
}
/** @param {string} path */
function privatePath(path) {
  if (
    typeof path !== 'string' ||
    !isAbsolute(path) ||
    resolve(path) !== path ||
    basename(path) === '.' ||
    path.includes('\0')
  )
    throw new Error('UNSAFE_FILE');
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
      throw new Error('UNSAFE_FILE');
    return file;
  } catch {
    throw new Error('UNSAFE_FILE');
  }
}
/** @param {string} path */
function privateJson(path) {
  const file = privatePath(path);
  if (file.size > MAX_FILE) throw new Error('FILE_LIMIT');
  const bytes = readFileSync(path);
  if (bytes.byteLength > MAX_FILE) throw new Error('FILE_LIMIT');
  try {
    return JSON.parse(bytes.toString('utf8'));
  } catch {
    throw new Error('MALFORMED_FILE');
  }
}
function print(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}
try {
  const args = argsOf(process.argv.slice(2));
  const config = validateWorkTransportConfig(privateJson(args.config));
  const policy = privateJson(args.policy);
  privatePath(args.db);
  const store = openWorkTransportStore({ dbPath: args.db, config });
  try {
    print(buildWorkListenerObservation({ store, policy, now: Date.now }));
  } finally {
    store.close();
  }
} catch (error) {
  const code =
    error instanceof Error &&
    ['INVALID_ARGUMENTS', 'UNSAFE_FILE', 'FILE_LIMIT', 'MALFORMED_FILE', 'INVALID_CONFIG'].includes(
      error.message
    )
      ? error.message
      : 'STORE_UNAVAILABLE';
  print({ error: { code } });
  process.exitCode = 1;
}
