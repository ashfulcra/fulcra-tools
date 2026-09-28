import { DatabaseSync } from 'node:sqlite';
import { closeSync, constants, lstatSync, openSync, statSync } from 'node:fs';
import { basename, dirname, isAbsolute, resolve } from 'node:path';

const scopeFields = ['principalId', 'workspaceId', 'environmentId', 'harness'];
const initialState = { version: 1, routes: {}, attempts: {}, policy: {} };

/** @param {unknown} value */
function validId(value) {
  return (
    typeof value === 'string' &&
    value.length > 0 &&
    value.length <= 256 &&
    !Array.from(value).some((character) => character.charCodeAt(0) < 32)
  );
}

/** @typedef {{principalId:string,workspaceId:string,environmentId:string,harness:string}} Scope */
/** @typedef {{scope:Scope,holder:string,epoch:number,expiresAt:number}} Lease */
/** @typedef {{version:number,routes:Record<string,unknown>,attempts:Record<string,unknown>,policy:Record<string,unknown>}} State */
/** @typedef {{scope_key:string,scope_json:string,holder:string,epoch:number,expires_at:number,last_clock:number,state_json:string}} JournalRow */

/** @param {unknown} value @returns {Scope} */
function validScope(value) {
  if (
    !value ||
    typeof value !== 'object' ||
    Array.isArray(value) ||
    Object.getPrototypeOf(value) !== Object.prototype
  )
    throw new TypeError('Invalid listener scope');
  const keys = Object.keys(value);
  if (keys.length !== scopeFields.length || !scopeFields.every((field) => keys.includes(field)))
    throw new TypeError('Invalid listener scope');
  for (const field of scopeFields) {
    const part = /** @type {Record<string,unknown>} */ (value)[field];
    if (!validId(part)) throw new TypeError('Invalid listener scope');
  }
  return /** @type {Scope} */ (value);
}

/** @param {unknown} value @param {string} label */
function validClock(value, label) {
  if (!Number.isSafeInteger(value) || /** @type {number} */ (value) < 0)
    throw new RangeError(`Invalid ${label}`);
  return /** @type {number} */ (value);
}

/** @param {unknown} value */
function validHolder(value) {
  if (!validId(value)) throw new TypeError('Invalid listener holder');
  return value;
}

/** @param {unknown} value */
function validLease(value) {
  if (
    !value ||
    typeof value !== 'object' ||
    Array.isArray(value) ||
    Object.getPrototypeOf(value) !== Object.prototype
  )
    throw new TypeError('Invalid listener lease');
  const keys = Object.keys(value);
  if (
    keys.length !== 4 ||
    !['scope', 'holder', 'epoch', 'expiresAt'].every((key) => keys.includes(key))
  )
    throw new TypeError('Invalid listener lease');
  const lease = /** @type {Lease} */ (value);
  validScope(lease.scope);
  validHolder(lease.holder);
  if (!Number.isSafeInteger(lease.epoch) || lease.epoch < 1)
    throw new TypeError('Invalid listener lease');
  validClock(lease.expiresAt, 'lease expiry');
  return lease;
}

/** @param {unknown} value @param {number} depth */
function validJson(value, depth = 0) {
  if (depth > 64) throw new TypeError('Invalid listener state');
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return;
  if (typeof value === 'number' && Number.isFinite(value)) return;
  if (Array.isArray(value)) {
    for (let i = 0; i < value.length; i += 1) {
      if (!Object.hasOwn(value, i)) throw new TypeError('Invalid listener state');
      validJson(value[i], depth + 1);
    }
    return;
  }
  if (value && typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype) {
    for (const [key, entry] of Object.entries(value)) {
      if (key === '__proto__' || key === 'constructor' || key === 'prototype')
        throw new TypeError('Invalid listener state');
      validJson(entry, depth + 1);
    }
    return;
  }
  throw new TypeError('Invalid listener state');
}

/** @param {unknown} value */
function validState(value) {
  validJson(value);
  if (!value || typeof value !== 'object' || Array.isArray(value))
    throw new TypeError('Invalid listener state');
  const state = /** @type {Record<string,unknown>} */ (value);
  if (
    Object.keys(state).length !== 4 ||
    state.version !== 1 ||
    !['routes', 'attempts', 'policy'].every(
      (key) => state[key] && typeof state[key] === 'object' && !Array.isArray(state[key])
    )
  )
    throw new TypeError('Invalid listener state');
}

/** @param {unknown} value @returns {string} */
function stateJson(value) {
  validState(value);
  const json = JSON.stringify(value);
  validState(JSON.parse(json));
  if (Buffer.byteLength(json, 'utf8') > 1024 * 1024)
    throw new RangeError('Listener state too large');
  return json;
}

/** @param {string} json @returns {State} */
function parseState(json) {
  try {
    const state = JSON.parse(json);
    stateJson(state);
    return state;
  } catch {
    throw new Error('Corrupt listener state');
  }
}

/** @param {JournalRow} row @returns {Lease} */
function rowLease(row) {
  return {
    scope: JSON.parse(row.scope_json),
    holder: row.holder,
    epoch: row.epoch,
    expiresAt: row.expires_at
  };
}

/** @param {string} path */
function ensurePath(path) {
  if (
    typeof path !== 'string' ||
    !isAbsolute(path) ||
    resolve(path) !== path ||
    basename(path) === '.' ||
    path.includes('\0')
  )
    throw new TypeError('Unsafe listener database path');
  if (!statSync(dirname(path)).isDirectory()) throw new TypeError('Unsafe listener database path');
  try {
    const entry = lstatSync(path);
    if (!entry.isFile() || entry.isSymbolicLink())
      throw new TypeError('Unsafe listener database path');
    return false;
  } catch (error) {
    if (/** @type {NodeJS.ErrnoException} */ (error).code !== 'ENOENT') throw error;
  }
  try {
    const fd = openSync(
      path,
      constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW,
      0o600
    );
    closeSync(fd);
    return true;
  } catch (error) {
    if (/** @type {NodeJS.ErrnoException} */ (error).code !== 'EEXIST') throw error;
    const entry = lstatSync(path);
    if (!entry.isFile() || entry.isSymbolicLink())
      throw new TypeError('Unsafe listener database path');
    return false;
  }
}

/**
 * Open one local operational journal. It is not an annotation authority or a
 * distributed fencing service; callers must provide clocks and lease TTLs.
 * @param {string} path Absolute path on a local filesystem.
 */
export function openListenerStore(path) {
  const fresh = ensurePath(path);
  const db = new DatabaseSync(path, { timeout: 5000 });
  try {
    db.exec('PRAGMA busy_timeout = 5000');
    const version = /** @type {{user_version:number}} */ (db.prepare('PRAGMA user_version').get());
    if (fresh) {
      if (version.user_version !== 0) throw new Error('Unsupported listener schema');
      db.exec('BEGIN IMMEDIATE');
      try {
        db.exec(
          'CREATE TABLE journal (scope_key TEXT PRIMARY KEY, scope_json TEXT NOT NULL, holder TEXT NOT NULL, epoch INTEGER NOT NULL, expires_at INTEGER NOT NULL, last_clock INTEGER NOT NULL, state_json TEXT NOT NULL)'
        );
        db.exec('PRAGMA user_version = 1');
        db.exec('COMMIT');
      } catch (error) {
        db.exec('ROLLBACK');
        throw error;
      }
    } else if (
      version.user_version !== 1 ||
      !db.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'journal'").get()
    ) {
      throw new Error('Unsupported listener schema');
    }
  } catch (error) {
    db.close();
    throw error;
  }

  const select = db.prepare(
    'SELECT scope_key,scope_json,holder,epoch,expires_at,last_clock,state_json FROM journal WHERE scope_key = ?'
  );
  const insert = db.prepare(
    'INSERT INTO journal (scope_key,scope_json,holder,epoch,expires_at,last_clock,state_json) VALUES (?,?,?,?,?,?,?)'
  );
  const renew = db.prepare(
    'UPDATE journal SET holder = ?,epoch = ?,expires_at = ?,last_clock = ? WHERE scope_key = ?'
  );
  const updateState = db.prepare(
    'UPDATE journal SET state_json = ?,last_clock = ? WHERE scope_key = ?'
  );
  let closed = false;

  /** @param {Scope} scope */
  function keyFor(scope) {
    const valid = validScope(scope);
    return JSON.stringify(scopeFields.map((field) => valid[/** @type {keyof Scope} */ (field)]));
  }

  /** @param {() => unknown} operation */
  function immediate(operation) {
    if (closed) throw new Error('Listener store closed');
    db.exec('BEGIN IMMEDIATE');
    try {
      const result = operation();
      db.exec('COMMIT');
      return result;
    } catch (error) {
      db.exec('ROLLBACK');
      throw error;
    }
  }

  return {
    /** @param {Scope} scope @param {string} holder @param {number} nowMs @param {number} ttlMs @returns {Lease|null} */
    acquire(scope, holder, nowMs, ttlMs) {
      const key = keyFor(scope);
      validHolder(holder);
      validClock(nowMs, 'listener clock');
      if (!Number.isSafeInteger(ttlMs) || ttlMs < 1 || ttlMs > 300000)
        throw new RangeError('Invalid listener TTL');
      const expiry = validClock(nowMs + ttlMs, 'lease expiry');
      return /** @type {Lease|null} */ (
        immediate(() => {
          const row = /** @type {JournalRow|undefined} */ (select.get(key));
          if (row && nowMs < row.last_clock) throw new RangeError('Listener clock moved backwards');
          if (row && row.holder !== holder && nowMs < row.expires_at) return null;
          const epoch = row ? (nowMs < row.expires_at ? row.epoch : row.epoch + 1) : 1;
          if (!Number.isSafeInteger(epoch)) throw new RangeError('Listener epoch exhausted');
          const expiresAt =
            row && nowMs < row.expires_at ? Math.max(row.expires_at, expiry) : expiry;
          const scopeJson = JSON.stringify(scope);
          if (row) renew.run(holder, epoch, expiresAt, nowMs, key);
          else insert.run(key, scopeJson, holder, epoch, expiresAt, nowMs, stateJson(initialState));
          return { scope: JSON.parse(scopeJson), holder, epoch, expiresAt };
        })
      );
    },

    /** @param {Lease} lease @param {number} nowMs @param {(state:State)=>unknown} update @returns {State} */
    transact(lease, nowMs, update) {
      validLease(lease);
      const key = keyFor(lease.scope);
      validClock(nowMs, 'listener clock');
      if (typeof update !== 'function') throw new TypeError('Invalid listener update');
      return /** @type {State} */ (
        immediate(() => {
          const row = /** @type {JournalRow|undefined} */ (select.get(key));
          if (
            !row ||
            row.holder !== lease.holder ||
            row.epoch !== lease.epoch ||
            nowMs >= row.expires_at
          )
            throw new Error('Listener lease expired or fenced');
          if (nowMs < row.last_clock) throw new RangeError('Listener clock moved backwards');
          const state = parseState(row.state_json);
          const result = update(state);
          if (
            result &&
            (typeof result === 'object' || typeof result === 'function') &&
            'then' in result
          ) {
            void Promise.resolve(result).catch(() => {});
            throw new TypeError('Listener update must be synchronous');
          }
          const json = stateJson(result);
          updateState.run(json, nowMs, key);
          return JSON.parse(json);
        })
      );
    },

    /** @param {Scope} scope @returns {{lease:Lease,state:State}|null} */
    inspect(scope) {
      const key = keyFor(scope);
      if (closed) throw new Error('Listener store closed');
      const row = /** @type {JournalRow|undefined} */ (select.get(key));
      return row ? { lease: rowLease(row), state: parseState(row.state_json) } : null;
    },

    close() {
      if (!closed) {
        db.close();
        closed = true;
      }
    }
  };
}
