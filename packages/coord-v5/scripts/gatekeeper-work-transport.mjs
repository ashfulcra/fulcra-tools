#!/usr/bin/env node
import { lstatSync, readFileSync } from 'node:fs';
import { basename, dirname, isAbsolute, resolve } from 'node:path';
import { validateWorkTransportConfig } from '../src/lib/server/gatekeeper/work-transport-config.js';
import { readWorkWindow } from '../src/lib/server/gatekeeper/work-transport-read.js';
import { openWorkTransportStore } from '../src/lib/server/gatekeeper/work-transport-store.js';
import {
  publishWorkOnce,
  reconcileWorkReadback
} from '../src/lib/server/gatekeeper/work-transport-publish.js';
import { replayWorkEvents } from '../src/lib/gatekeeper/work-projection.js';

const MAX_FILE = 64 * 1024;
const MAX_STDIN = 16 * 1024;
const COMMANDS = {
  read: ['config', 'db', 'start', 'end'],
  publish: ['config', 'db', 'event'],
  inspect: ['config', 'db'],
  replay: ['config', 'db']
};

/** @param {string[]} argv */
function argumentsFor(argv) {
  const [command, ...rest] = argv;
  const expected = COMMANDS[command];
  if (!expected || rest.length !== expected.length * 2) throw new Error('INVALID_ARGUMENTS');
  const args = {};
  for (let index = 0; index < rest.length; index += 2) {
    const flag = rest[index];
    const value = rest[index + 1];
    if (
      !flag?.startsWith('--') ||
      !expected.includes(flag.slice(2)) ||
      Object.hasOwn(args, flag.slice(2)) ||
      typeof value !== 'string' ||
      value.startsWith('--')
    )
      throw new Error('INVALID_ARGUMENTS');
    args[flag.slice(2)] = value;
  }
  if (!expected.every((key) => Object.hasOwn(args, key))) throw new Error('INVALID_ARGUMENTS');
  return { command, args };
}
/** @param {string} path */
function privateFile(path) {
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
    if (file.size > MAX_FILE) throw new Error('FILE_LIMIT');
    const bytes = readFileSync(path);
    if (bytes.byteLength > MAX_FILE) throw new Error('FILE_LIMIT');
    try {
      return JSON.parse(bytes.toString('utf8'));
    } catch {
      throw new Error('MALFORMED_FILE');
    }
  } catch (error) {
    if (
      error instanceof Error &&
      ['UNSAFE_FILE', 'FILE_LIMIT', 'MALFORMED_FILE'].includes(error.message)
    )
      throw error;
    throw new Error('UNSAFE_FILE');
  }
}
/** Stdin is consumed only by read/publish, never echoed or persisted.
 * @returns {Promise<string>} */
async function bearer() {
  let bytes = 0;
  const chunks = [];
  for await (const chunk of process.stdin) {
    const data = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    bytes += data.byteLength;
    if (bytes > MAX_STDIN) throw new Error('TOKEN_LIMIT');
    chunks.push(data);
  }
  const token = Buffer.concat(chunks)
    .toString('utf8')
    .replace(/\r?\n$/, '');
  if (!token || /[\r\n]/.test(token)) throw new Error('INVALID_TOKEN');
  return token;
}
/** @param {unknown} error */
function safeCode(error) {
  if (
    error instanceof Error &&
    [
      'INVALID_ARGUMENTS',
      'UNSAFE_FILE',
      'FILE_LIMIT',
      'MALFORMED_FILE',
      'TOKEN_LIMIT',
      'INVALID_TOKEN',
      'INVALID_CONFIG',
      'UNSAFE_DB_PATH',
      'STORE_SCOPE_MISMATCH',
      'STORE_CORRUPT'
    ].includes(error.message)
  )
    return error.message;
  return 'WORK_TRANSPORT_FAILURE';
}
/** @param {unknown} value */
function output(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

async function main() {
  const { command, args } = argumentsFor(process.argv.slice(2));
  const config = validateWorkTransportConfig(privateFile(args.config));
  const event = command === 'publish' ? privateFile(args.event) : null;
  const token = command === 'read' || command === 'publish' ? await bearer() : null;
  const store = openWorkTransportStore({ dbPath: args.db, config });
  try {
    if (command === 'inspect') return { status: 'ready', ...store.inspect() };
    if (command === 'publish')
      return await publishWorkOnce({
        fetch: globalThis.fetch,
        token,
        config,
        store,
        event,
        now: Date.now
      });
    if (command === 'read') {
      const readResult = await readWorkWindow({
        fetch: globalThis.fetch,
        token,
        config,
        start: args.start,
        end: args.end,
        now: Date.now
      });
      const stored = store.appendWindow(readResult);
      if (stored.status !== 'stored')
        return {
          status: 'blocked',
          code: stored.code,
          coverage: readResult.observation.coverage,
          added_records: 0
        };
      const reconciled = [];
      for (const intent of store.inspect().intents) {
        if (
          !['PREPARED', 'UNKNOWN_IN_FLIGHT', 'UNKNOWN', 'UPLOAD_ACCEPTED', 'VERIFIED'].includes(
            intent.state
          )
        )
          continue;
        if (readResult.records.some((record) => record.event_id === intent.event_id))
          reconciled.push(reconcileWorkReadback({ store, readResult, eventId: intent.event_id }));
      }
      return {
        status: 'stored',
        added_records: stored.added_records,
        coverage: readResult.observation.coverage,
        candidate_count: readResult.candidates.length,
        gaps: readResult.observation.gaps,
        errors: readResult.observation.errors,
        reconciled: reconciled.map((entry) => ({ status: entry.status, event_id: entry.event_id }))
      };
    }
    const accumulated = store.accumulated();
    if (accumulated.status !== 'ready')
      return {
        status: accumulated.status,
        coverage: accumulated.observation.coverage,
        candidate_count: accumulated.event_count,
        conflict_count: accumulated.conflicts.length,
        gaps: accumulated.observation.gaps,
        errors: accumulated.observation.errors
      };
    const trust = {
      workspace_id: config.workspaceId,
      allowed_stream_ids: [],
      event_evidence: [],
      grants: []
    };
    const projection = replayWorkEvents({
      events: accumulated.events,
      trust,
      observation: accumulated.observation,
      asOf: accumulated.observation.as_of
    });
    return {
      status: 'trust_withheld',
      coverage: projection.observation.coverage,
      candidate_count: accumulated.events.length,
      authorized_work_count: projection.work.length,
      rejected_count: projection.rejected.length,
      pending_count: projection.pending.length,
      conflict_count: accumulated.conflicts.length + projection.conflicts.length,
      gaps: projection.observation.gaps,
      errors: projection.observation.errors
    };
  } finally {
    store.close();
  }
}

try {
  const result = await main();
  output(result);
  if (
    result.status === 'blocked' ||
    result.status === 'unavailable' ||
    result.status === 'blocked_limit' ||
    result.coverage === 'unavailable' ||
    (result.coverage === 'partial' && result.errors?.length > 0)
  )
    process.exitCode = 2;
} catch (error) {
  output({ status: 'blocked', code: safeCode(error) });
  process.exitCode = 2;
}
