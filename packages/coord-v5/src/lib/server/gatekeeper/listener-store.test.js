import { afterEach, describe, expect, it } from 'vitest';
import { mkdtempSync, rmSync, symlinkSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { openListenerStore } from './listener-store.js';

const scope = {
  principalId: 'principal-a',
  workspaceId: 'workspace',
  environmentId: 'production',
  harness: 'codex'
};
/** @type {string[]} */
const directories = [];
/** @type {Array<ReturnType<typeof openListenerStore>>} */
const stores = [];

function databasePath() {
  const directory = mkdtempSync(join(tmpdir(), 'gatekeeper-listener-'));
  directories.push(directory);
  return join(directory, 'journal.sqlite');
}

/** @param {string} path */
function open(path) {
  const store = openListenerStore(path);
  stores.push(store);
  return store;
}

/** @template T @param {T|null} value @returns {T} */
function required(value) {
  if (value === null) throw new Error('Expected a listener lease');
  return value;
}

afterEach(() => {
  for (const store of stores.splice(0)) store.close();
  for (const directory of directories.splice(0))
    rmSync(directory, { recursive: true, force: true });
});

describe('atomic local listener journal', () => {
  it('permits one holder per exact scope across independent connections', () => {
    const path = databasePath();
    const first = open(path);
    const second = open(path);
    expect(first.acquire(scope, 'a', 100, 50)).toMatchObject({
      holder: 'a',
      epoch: 1,
      expiresAt: 150
    });
    expect(second.acquire(scope, 'b', 100, 50)).toBeNull();
    expect(second.acquire({ ...scope, principalId: 'principal-b' }, 'b', 100, 50)).toMatchObject({
      epoch: 1
    });
    expect(second.acquire({ ...scope, environmentId: 'staging' }, 'b', 100, 50)).toMatchObject({
      epoch: 1
    });
  });

  it('fences an old handle after expiry takeover', () => {
    const path = databasePath();
    const first = open(path);
    const second = open(path);
    const old = required(first.acquire(scope, 'a', 100, 50));
    expect(old).not.toBeNull();
    const next = second.acquire(scope, 'b', 150, 50);
    expect(next).toMatchObject({ holder: 'b', epoch: 2, expiresAt: 200 });
    expect(() => first.transact(old, 151, (state) => state)).toThrow();
    expect(second.inspect(scope)?.state).toEqual({
      version: 1,
      routes: {},
      attempts: {},
      policy: {}
    });
  });

  it('preserves epoch on early renewal and increments it after expiry', () => {
    const store = open(databasePath());
    expect(store.acquire(scope, 'a', 100, 50)).toMatchObject({ epoch: 1, expiresAt: 150 });
    expect(store.acquire(scope, 'a', 120, 50)).toMatchObject({ epoch: 1, expiresAt: 170 });
    expect(store.acquire(scope, 'a', 170, 50)).toMatchObject({ epoch: 2, expiresAt: 220 });
  });

  it('rejects backward clocks and transactions at or after lease expiry', () => {
    const store = open(databasePath());
    const lease = required(store.acquire(scope, 'a', 100, 50));
    expect(lease).not.toBeNull();
    expect(() => store.acquire(scope, 'a', 99, 50)).toThrow();
    expect(() => store.transact(lease, 99, (state) => state)).toThrow();
    expect(() => store.transact(lease, 150, (state) => state)).toThrow();
    expect(store.inspect(scope)?.state).toEqual({
      version: 1,
      routes: {},
      attempts: {},
      policy: {}
    });
  });

  it('rolls back a throwing, asynchronous, or invalid-JSON update', () => {
    const store = open(databasePath());
    const lease = required(store.acquire(scope, 'a', 100, 50));
    const initial = { version: 1, routes: {}, attempts: {}, policy: {} };
    expect(() =>
      store.transact(lease, 101, (state) => {
        state.routes.job = 'mutated';
        throw new Error('abort');
      })
    ).toThrow('abort');
    expect(() => store.transact(lease, 101, async (state) => state)).toThrow();
    expect(() =>
      store.transact(lease, 101, (state) => {
        state.routes.bad = undefined;
        return state;
      })
    ).toThrow();
    expect(store.inspect(scope)?.state).toEqual(initial);
    const committed = store.transact(lease, 101, (state) => {
      state.routes.job = 'target';
      return state;
    });
    expect(committed).toEqual({ ...initial, routes: { job: 'target' } });
    committed.routes.job = 'local-only';
    expect(store.inspect(scope)?.state).toEqual({ ...initial, routes: { job: 'target' } });
  });

  it('rejects state whose serialized form violates the operational schema', () => {
    const store = open(databasePath());
    const lease = required(store.acquire(scope, 'a', 100, 50));
    expect(() =>
      store.transact(lease, 101, (state) => {
        Object.defineProperty(state, 'toJSON', {
          value: () => ({ unexpected: 'invalid persisted shape' })
        });
        return state;
      })
    ).toThrow();
    expect(store.inspect(scope)?.state).toEqual({
      version: 1,
      routes: {},
      attempts: {},
      policy: {}
    });
  });

  it('consumes a rejected async update while rolling back synchronously', async () => {
    const store = open(databasePath());
    const lease = required(store.acquire(scope, 'a', 100, 50));
    /** @type {unknown[]} */
    const unhandled = [];
    /** @param {unknown} reason */
    const onUnhandled = (reason) => unhandled.push(reason);
    process.on('unhandledRejection', onUnhandled);
    try {
      expect(() =>
        store.transact(lease, 101, async () => {
          throw new Error('async callback rejected');
        })
      ).toThrow('Listener update must be synchronous');
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(unhandled).toEqual([]);
      expect(store.inspect(scope)?.state).toEqual({
        version: 1,
        routes: {},
        attempts: {},
        policy: {}
      });
    } finally {
      process.off('unhandledRejection', onUnhandled);
    }
  });

  it('persists state and epoch after reopening', () => {
    const path = databasePath();
    const first = open(path);
    const lease = required(first.acquire(scope, 'a', 100, 50));
    first.transact(lease, 110, (state) => {
      state.policy.mode = 'test';
      return state;
    });
    first.close();
    const reopened = open(path);
    expect(reopened.inspect(scope)).toMatchObject({
      lease: { holder: 'a', epoch: 1, expiresAt: 150 },
      state: { policy: { mode: 'test' } }
    });
    expect(reopened.acquire(scope, 'b', 150, 50)).toMatchObject({ epoch: 2 });
  });

  it('rejects malformed scope, TTL, clock, and holder without mutation', () => {
    const store = open(databasePath());
    const invalidScopes = [
      { ...scope, principalId: '' },
      { ...scope, extra: 'x' },
      { ...scope, harness: 5 },
      null
    ];
    for (const invalid of invalidScopes)
      expect(() =>
        store.acquire(
          /** @type {Parameters<typeof store.acquire>[0]} */ (/** @type {unknown} */ (invalid)),
          'a',
          100,
          50
        )
      ).toThrow();
    for (const ttl of [0, 300001, 1.5, NaN])
      expect(() => store.acquire(scope, 'a', 100, ttl)).toThrow();
    for (const now of [-1, 1.5, Infinity])
      expect(() => store.acquire(scope, 'a', now, 50)).toThrow();
    expect(() => store.acquire(scope, '', 100, 50)).toThrow();
    expect(store.inspect(scope)).toBeNull();
  });

  it('restricts new database mode and refuses symlink or pre-existing unknown schema', () => {
    const path = databasePath();
    const store = open(path);
    expect(statSync(path).mode & 0o777).toBe(0o600);
    store.close();
    const linked = `${path}.link`;
    symlinkSync(path, linked);
    expect(() => openListenerStore(linked)).toThrow();
    const unknown = databasePath();
    writeFileSync(unknown, 'not a database');
    expect(() => openListenerStore(unknown)).toThrow();
  });

  it('fails safely on a corrupted persisted operational state', () => {
    const path = databasePath();
    const first = open(path);
    first.acquire(scope, 'a', 100, 50);
    first.close();
    const db = new DatabaseSync(path);
    db.prepare('UPDATE journal SET state_json = ?').run('{broken');
    db.close();
    const reopened = open(path);
    expect(() => reopened.inspect(scope)).toThrow('Corrupt listener state');
  });
});
