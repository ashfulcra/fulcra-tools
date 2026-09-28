#!/usr/bin/env node
import { existsSync, statSync } from 'node:fs';
import { basename, dirname, isAbsolute, resolve } from 'node:path';
import { openListenerStore } from '../src/lib/server/gatekeeper/listener-store.js';
import {
  acknowledgeWake,
  configureRoutes,
  prepareWake,
  settleWake
} from '../src/lib/server/gatekeeper/listener-runtime.js';
import {
  correlation,
  exact,
  id,
  observation,
  routes
} from '../src/lib/server/gatekeeper/listener-validation.js';

const commands = new Set(['configure', 'prepare', 'settle', 'ack', 'inspect']);
const inputLimit = 256 * 1024;
const leaseTtlMs = 120000;

function badInput() {
  throw new TypeError('Invalid listener CLI input');
}

/** @param {string[]} args */
function parseArgs(args) {
  if (args.length !== 7 || !commands.has(args[0])) badInput();
  /** @type {Record<string,string>} */
  const flags = {};
  for (let i = 1; i < args.length; i += 2) {
    const key = args[i];
    if (!['--db', '--scope', '--holder'].includes(key) || Object.hasOwn(flags, key) || !args[i + 1])
      badInput();
    flags[key] = args[i + 1];
  }
  if (!flags['--db'] || !flags['--scope'] || !flags['--holder']) badInput();
  if (Buffer.byteLength(flags['--scope'], 'utf8') > 4096) badInput();
  const db = flags['--db'];
  if (
    !isAbsolute(db) ||
    resolve(db) !== db ||
    basename(db) === '.' ||
    db.includes('\0') ||
    !statSync(dirname(db)).isDirectory()
  )
    badInput();
  const scope = exact(JSON.parse(flags['--scope']), [
    'principalId',
    'workspaceId',
    'environmentId',
    'harness'
  ]);
  for (const value of Object.values(scope)) id(value);
  const holder = id(flags['--holder']);
  return { command: args[0], db, scope, holder };
}

async function readInput() {
  /** @type {Buffer[]} */
  const chunks = [];
  let total = 0;
  for await (const chunk of process.stdin) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    total += bytes.length;
    if (total > inputLimit) badInput();
    chunks.push(bytes);
  }
  if (!total) badInput();
  return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks)));
}

/** @param {string} command @param {unknown} input @param {number} now */
function checkedInput(command, input, now) {
  if (command === 'configure') return routes(input) && input;
  if (command === 'prepare') return observation(input, now, 0) && input;
  if (command === 'settle') return correlation(input, true);
  if (command === 'ack') return correlation(input);
  badInput();
}

/** @param {unknown} value */
function print(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

async function main() {
  let parsed;
  let input;
  let now;
  try {
    parsed = parseArgs(process.argv.slice(2));
    if (parsed.command !== 'inspect') {
      const raw = await readInput();
      now = Date.now();
      input = checkedInput(parsed.command, raw, now);
    }
  } catch {
    print({ error: { code: 'BAD_INPUT' } });
    process.exitCode = 1;
    return;
  }

  if (parsed.command === 'inspect' && !existsSync(parsed.db)) {
    print({ snapshot: null });
    return;
  }
  let store;
  try {
    store = openListenerStore(parsed.db);
    if (parsed.command === 'inspect') {
      print({ snapshot: store.inspect(parsed.scope) });
      return;
    }
    const lease = store.acquire(parsed.scope, parsed.holder, now, leaseTtlMs);
    if (!lease) {
      print({ error: { code: 'LEASE_CONFLICT' } });
      process.exitCode = 2;
      return;
    }
    const result =
      parsed.command === 'configure'
        ? configureRoutes(store, lease, input, now)
        : parsed.command === 'prepare'
          ? prepareWake(store, lease, input, now)
          : parsed.command === 'settle'
            ? settleWake(store, lease, input, now)
            : acknowledgeWake(store, lease, input, now);
    print(result);
  } catch (error) {
    print({
      error: {
        code:
          error instanceof TypeError || error instanceof RangeError
            ? 'BAD_INPUT'
            : 'OPERATION_FAILED'
      }
    });
    process.exitCode = 1;
  } finally {
    store?.close();
  }
}

await main();
