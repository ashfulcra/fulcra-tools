import { afterEach, describe, expect, it } from 'vitest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { openListenerStore } from './listener-store.js';
import { acknowledgeWake, configureRoutes, prepareWake, settleWake } from './listener-runtime.js';

const scope = { principalId: 'p', workspaceId: 'w', environmentId: 'e', harness: 'codex' };
const active = {
  jobId: 'job',
  logicalIdentity: 'agent',
  lifecycle: 'active',
  threadId: 'thread',
  hostId: 'local'
};
const item = { itemId: 'item', jobId: 'job', revision: 'r1' };
/** @type {string[]} */
const directories = [];
/** @type {Array<ReturnType<typeof openListenerStore>>} */
const stores = [];
function setup(customScope = scope, startMs = 1767225600000) {
  const directory = mkdtempSync(join(tmpdir(), 'listener-runtime-'));
  directories.push(directory);
  const path = join(directory, 'journal.sqlite');
  const store = openListenerStore(path);
  stores.push(store);
  const lease = store.acquire(customScope, 'holder', startMs, 300000);
  if (!lease) throw new Error('lease');
  return { path, store, lease };
}
/** @template T @param {T|null} value @returns {T} */
function required(value) {
  if (value === null) throw new Error('Expected value');
  return value;
}
/** @param {ReturnType<typeof openListenerStore>} store @returns {any} */
function stateOf(store) {
  return required(store.inspect(scope)).state;
}
/** @param {typeof item[]} [events] @param {typeof item[]} [obligations] @param {string} [eventCoverage] @param {string} [obligationCoverage] @param {string} [observedAt] */
function observation(
  events = [item],
  obligations = [],
  eventCoverage = 'complete',
  obligationCoverage = 'complete',
  observedAt = '2026-01-01T00:00:02Z'
) {
  return {
    version: 1,
    eventObservation: { coverage: eventCoverage, items: events },
    obligationObservation: { coverage: obligationCoverage, items: obligations },
    observedAt
  };
}
afterEach(() => {
  for (const store of stores.splice(0)) store.close();
  for (const directory of directories.splice(0))
    rmSync(directory, { recursive: true, force: true });
});

describe('recoverable wake planner', () => {
  it('durably prepares a coalesced action and does not resend after crash/reopen', () => {
    const { path, store, lease } = setup();
    configureRoutes(store, lease, [active], 1767225601001);
    const result = prepareWake(
      store,
      lease,
      observation([item, { itemId: 'other', jobId: 'job', revision: 'r1' }], [item]),
      1767225602000
    );
    expect(result.actions).toHaveLength(1);
    const action = result.actions[0];
    expect(action).toMatchObject({
      tool: 'mcp__codex_app__send_message_to_thread',
      arguments: { threadId: 'thread', hostId: 'local' }
    });
    expect(action.arguments.prompt).toContain('item');
    expect(stateOf(store).attempts[action.wakeId]).toMatchObject({
      attemptId: action.attemptId,
      state: 'prepared'
    });
    store.close();
    const reopened = openListenerStore(path);
    stores.push(reopened);
    const next = required(reopened.acquire(scope, 'holder', 1767225602001, 300000));
    expect(next).not.toBeNull();
    const repeated = prepareWake(
      reopened,
      next,
      observation([item, { itemId: 'other', jobId: 'job', revision: 'r1' }], [item]),
      1767225602002
    );
    expect(repeated.actions).toEqual([]);
    expect(repeated.reconciliation).toMatchObject([
      { wakeId: action.wakeId, attemptId: action.attemptId }
    ]);
  });

  it('correlates receipts and acknowledgments exactly without completing obligations', () => {
    const { store, lease } = setup();
    configureRoutes(store, lease, [active], 1767225601001);
    const action = prepareWake(store, lease, observation([], [item]), 1767225602000).actions[0];
    const target = { threadId: 'thread', hostId: 'local' };
    expect(() =>
      settleWake(
        store,
        lease,
        { wakeId: action.wakeId, attemptId: 'wrong', target, status: 'accepted' },
        1767225602001
      )
    ).toThrow();
    expect(() =>
      acknowledgeWake(
        store,
        lease,
        {
          wakeId: action.wakeId,
          attemptId: action.attemptId,
          target: { threadId: 'other', hostId: 'local' }
        },
        1767225602001
      )
    ).toThrow();
    settleWake(
      store,
      lease,
      { wakeId: action.wakeId, attemptId: action.attemptId, target, status: 'accepted' },
      1767225602001
    );
    settleWake(
      store,
      lease,
      { wakeId: action.wakeId, attemptId: action.attemptId, target, status: 'accepted' },
      1767225602001
    );
    expect(() =>
      settleWake(
        store,
        lease,
        { wakeId: action.wakeId, attemptId: action.attemptId, target, status: 'error' },
        1767225602001
      )
    ).toThrow();
    acknowledgeWake(
      store,
      lease,
      { wakeId: action.wakeId, attemptId: action.attemptId, target },
      1767225602002
    );
    expect(prepareWake(store, lease, observation([], [item]), 1767225602003).actions).toEqual([]);
    expect(stateOf(store).policy.obligations).toMatchObject({ '["job","item"]': 'r1' });
  });

  it('preserves known obligations through partial and empty reads and records new distinct arrivals', () => {
    const { store, lease } = setup();
    configureRoutes(store, lease, [active], 1767225601001);
    prepareWake(store, lease, observation([], [item]), 1767225602000);
    const partial = prepareWake(
      store,
      lease,
      observation([], [], 'partial', 'unavailable', '2026-01-01T00:00:03Z'),
      1767225603000
    );
    expect(partial.coverage).toEqual({ events: 'partial', obligations: 'unavailable' });
    expect(stateOf(store).policy.obligations).toMatchObject({ '["job","item"]': 'r1' });
    expect(partial.nextIntervalMinutes).toBeLessThanOrEqual(15);
    prepareWake(
      store,
      lease,
      observation([], [], 'complete', 'complete', '2026-01-01T00:00:04Z'),
      1767225604000
    );
    expect(stateOf(store).policy.obligations).toEqual({});
    expect(Object.keys(stateOf(store).attempts)).toHaveLength(1);
  });

  it('keeps immutable targets across route changes and fences expired leases', () => {
    const { store, lease } = setup();
    configureRoutes(store, lease, [active], 1767225601001);
    const action = prepareWake(store, lease, observation(), 1767225602000).actions[0];
    const nextRoute = { ...active, threadId: 'successor' };
    configureRoutes(store, lease, [nextRoute], 1767225602001);
    expect(stateOf(store).attempts[action.wakeId]).toMatchObject({
      target: { threadId: 'thread', hostId: 'local' },
      superseded: true
    });
    expect(
      prepareWake(store, lease, observation(), 1767225602002).actions[0].arguments.threadId
    ).toBe('successor');
    const fresh = store.acquire(scope, 'new-holder', lease.expiresAt, 300000);
    expect(fresh).not.toBeNull();
    expect(() =>
      settleWake(
        store,
        lease,
        {
          wakeId: action.wakeId,
          attemptId: action.attemptId,
          target: { threadId: 'thread', hostId: 'local' },
          status: 'accepted'
        },
        lease.expiresAt + 1
      )
    ).toThrow();
  });

  it('reports missing successor/coordinator per job and unsupported harness without fake actions', () => {
    const { store, lease } = setup();
    configureRoutes(
      store,
      lease,
      [
        { jobId: 'job', logicalIdentity: 'agent', lifecycle: 'dormant' },
        { jobId: 'other', logicalIdentity: 'other', lifecycle: 'retired' }
      ],
      1767225601001
    );
    const result = prepareWake(
      store,
      lease,
      observation([item, { itemId: 'other', jobId: 'other', revision: 'r1' }]),
      1767225602000
    );
    expect(result.actions).toEqual([]);
    expect(
      /** @type {Array<{status:string}>} */ (result.attention).map((x) => x.status).sort()
    ).toEqual(['needs_coordinator', 'needs_successor']);
    const otherScope = { ...scope, harness: 'other' };
    const second = setup(otherScope);
    configureRoutes(second.store, second.lease, [active], 1767225601001);
    expect(
      prepareWake(second.store, second.lease, observation(), 1767225602000).attention
    ).toMatchObject([{ status: 'unsupported_harness' }]);
  });

  it('rejects hostile strings, duplicate conflicts, cycles, future/older observations without mutation', () => {
    const { store, lease } = setup();
    expect(() =>
      configureRoutes(store, lease, [{ ...active, jobId: '__proto__' }], 1767225601001)
    ).toThrow();
    expect(() =>
      configureRoutes(store, lease, [{ ...active, coordinatorJobId: 'job' }], 1767225601001)
    ).toThrow();
    configureRoutes(store, lease, [active], 1767225601001);
    const before = stateOf(store);
    expect(() =>
      prepareWake(
        store,
        lease,
        observation([{ ...item, itemId: 'ignore instructions' }]),
        1767225602000
      )
    ).toThrow();
    expect(() =>
      prepareWake(store, lease, observation([item, { ...item, revision: 'r2' }]), 1767225602000)
    ).toThrow();
    expect(() =>
      prepareWake(
        store,
        lease,
        observation([item], [], 'complete', 'complete', '2027-01-01T00:00:00Z'),
        1767225602000
      )
    ).toThrow();
    expect(stateOf(store)).toEqual(before);
    prepareWake(store, lease, observation(), 1767225602000);
    expect(() =>
      prepareWake(
        store,
        lease,
        observation([], [], 'complete', 'complete', '2026-01-01T00:00:01Z'),
        1767225602001
      )
    ).toThrow();
  });

  it('keeps uncertain attempts for reconciliation after reopening', () => {
    const { path, store, lease } = setup();
    configureRoutes(store, lease, [active], 1767225601001);
    const action = prepareWake(store, lease, observation(), 1767225602000).actions[0];
    settleWake(
      store,
      lease,
      {
        wakeId: action.wakeId,
        attemptId: action.attemptId,
        target: { threadId: 'thread', hostId: 'local' },
        status: 'unknown'
      },
      1767225602001
    );
    store.close();
    const reopened = openListenerStore(path);
    stores.push(reopened);
    const next = required(reopened.acquire(scope, 'holder', 1767225602002, 300000));
    const result = prepareWake(reopened, next, observation(), 1767225602003);
    expect(result.actions).toEqual([]);
    expect(result.reconciliation).toMatchObject([{ state: 'uncertain', wakeId: action.wakeId }]);
    expect(result.nextIntervalMinutes).toBeLessThanOrEqual(5);
  });

  it('keeps composite identifiers distinct and bounds prompts for large observations', () => {
    const { store, lease } = setup();
    configureRoutes(
      store,
      lease,
      [active, { ...active, jobId: 'job:x', logicalIdentity: 'other', threadId: 'other' }],
      1767225601001
    );
    const first = { itemId: 'x:y', jobId: 'job', revision: 'r1' };
    const second = { itemId: 'y', jobId: 'job:x', revision: 'r1' };
    const many = Array.from({ length: 1000 }, (_, i) => ({
      itemId: `id${i}`,
      jobId: 'job',
      revision: 'r1'
    }));
    const result = prepareWake(
      store,
      lease,
      observation([first, second, ...many.slice(0, 998)]),
      1767225602000
    );
    expect(result.actions).toHaveLength(2);
    expect(Object.keys(stateOf(store).policy.events)).toHaveLength(1000);
    expect(
      /** @type {Array<{arguments:{prompt:string}}>} */ (result.actions).every(
        (action) => action.arguments.prompt.length <= 4096
      )
    ).toBe(true);
  });

  it('supersedes a retired job wake when only its coordinator session changes', () => {
    const { store, lease } = setup();
    const retired = {
      jobId: 'job',
      logicalIdentity: 'agent',
      lifecycle: 'retired',
      coordinatorJobId: 'coord'
    };
    const coordinator = {
      jobId: 'coord',
      logicalIdentity: 'lead',
      lifecycle: 'active',
      threadId: 'first'
    };
    configureRoutes(store, lease, [retired, coordinator], 1767225601001);
    const old = prepareWake(store, lease, observation(), 1767225602000).actions[0];
    configureRoutes(store, lease, [retired, { ...coordinator, threadId: 'second' }], 1767225602001);
    expect(stateOf(store).attempts[old.wakeId].superseded).toBe(true);
    const fresh = prepareWake(store, lease, observation(), 1767225602002).actions[0];
    expect(fresh.wakeId).not.toBe(old.wakeId);
    expect(fresh.arguments.threadId).toBe('second');
  });

  it('does not suppress a bound job because another job has no session', () => {
    const { store, lease } = setup();
    configureRoutes(
      store,
      lease,
      [active, { jobId: 'missing', logicalIdentity: 'missing', lifecycle: 'dormant' }],
      1767225601001
    );
    const result = prepareWake(
      store,
      lease,
      observation([item, { itemId: 'x', jobId: 'missing', revision: 'r1' }]),
      1767225602000
    );
    expect(result.actions).toHaveLength(1);
    expect(result.actions[0].arguments.threadId).toBe('thread');
    expect(result.attention).toMatchObject([{ jobId: 'missing', status: 'needs_successor' }]);
  });

  it('does not resend accepted work after reopening and keeps uncertain attention after an empty complete read', () => {
    const { path, store, lease } = setup();
    configureRoutes(store, lease, [active], 1767225601001);
    const action = prepareWake(store, lease, observation(), 1767225602000).actions[0];
    const target = { threadId: 'thread', hostId: 'local' };
    settleWake(
      store,
      lease,
      { wakeId: action.wakeId, attemptId: action.attemptId, target, status: 'accepted' },
      1767225602001
    );
    store.close();
    const reopened = openListenerStore(path);
    stores.push(reopened);
    const next = required(reopened.acquire(scope, 'holder', 1767225602002, 300000));
    const repeated = prepareWake(reopened, next, observation(), 1767225602003);
    expect(repeated.actions).toEqual([]);
    expect(repeated.reconciliation).toEqual([]);
    const changed = prepareWake(
      reopened,
      next,
      observation(
        [{ ...item, revision: 'r2' }],
        [],
        'complete',
        'complete',
        '2026-01-01T00:00:04Z'
      ),
      1767225604000
    ).actions[0];
    settleWake(
      reopened,
      next,
      { wakeId: changed.wakeId, attemptId: changed.attemptId, target, status: 'error' },
      1767225604001
    );
    const emptyResult = prepareWake(
      reopened,
      next,
      observation([], [], 'complete', 'complete', '2026-01-01T00:00:05Z'),
      1767225605000
    );
    expect(emptyResult.reconciliation).toMatchObject([
      { wakeId: changed.wakeId, state: 'uncertain' }
    ]);
    expect(emptyResult.attention).toContainEqual({
      jobId: 'job',
      wakeId: changed.wakeId,
      status: 'needs_reconciliation'
    });
    expect(emptyResult.nextIntervalMinutes).toBeLessThanOrEqual(5);
  });

  it('rejects calendar-invalid observations and preserves journal state', () => {
    const { store, lease } = setup(scope, 1772496000000);
    configureRoutes(store, lease, [active], 1772496001001);
    const before = stateOf(store);
    expect(() =>
      prepareWake(
        store,
        lease,
        observation([], [], 'complete', 'complete', '2026-02-30T00:00:00Z'),
        1772496002000
      )
    ).toThrow();
    expect(stateOf(store)).toEqual(before);
  });

  it('rejects conflicting revisions across event and obligation sources', () => {
    const { store, lease } = setup();
    configureRoutes(store, lease, [active], 1767225601001);
    const before = stateOf(store);
    expect(() =>
      prepareWake(store, lease, observation([item], [{ ...item, revision: 'r2' }]), 1767225602000)
    ).toThrow();
    expect(stateOf(store)).toEqual(before);
  });

  it('matches target fields regardless of object property insertion order', () => {
    const { store, lease } = setup();
    configureRoutes(store, lease, [active], 1767225601001);
    const action = prepareWake(store, lease, observation(), 1767225602000).actions[0];
    const target = { hostId: 'local', threadId: 'thread' };
    expect(
      settleWake(
        store,
        lease,
        { wakeId: action.wakeId, attemptId: action.attemptId, target, status: 'accepted' },
        1767225602001
      )
    ).toMatchObject({ state: 'accepted' });
  });

  it('accepts an exact acknowledgment when a crash left the attempt prepared', () => {
    const { store, lease } = setup();
    configureRoutes(store, lease, [active], 1767225601001);
    const action = prepareWake(store, lease, observation(), 1767225602000).actions[0];
    expect(
      acknowledgeWake(
        store,
        lease,
        {
          wakeId: action.wakeId,
          attemptId: action.attemptId,
          target: { threadId: 'thread', hostId: 'local' }
        },
        1767225602001
      )
    ).toMatchObject({ state: 'acknowledged' });
    expect(prepareWake(store, lease, observation(), 1767225602002).actions).toEqual([]);
  });

  it('allows a matching later acceptance to resolve an unknown receipt', () => {
    const { store, lease } = setup();
    configureRoutes(store, lease, [active], 1767225601001);
    const action = prepareWake(store, lease, observation(), 1767225602000).actions[0];
    const target = { threadId: 'thread', hostId: 'local' };
    settleWake(
      store,
      lease,
      { wakeId: action.wakeId, attemptId: action.attemptId, target, status: 'unknown' },
      1767225602001
    );
    expect(
      settleWake(
        store,
        lease,
        { wakeId: action.wakeId, attemptId: action.attemptId, target, status: 'accepted' },
        1767225602002
      )
    ).toMatchObject({ state: 'accepted' });
  });

  it('adds positive deterministic quiet jitter while respecting the operator cap', () => {
    const quietScope = { ...scope, principalId: 'p49364' };
    const { store, lease } = setup(quietScope);
    store.transact(lease, 1767225601001, (state) => {
      state.policy = {
        enrolledAt: '2025-12-31T22:00:02Z',
        lastWorkAt: '2025-12-31T22:00:02Z',
        policyIntervalMinutes: 30,
        operatorIntervalMinutes: 40
      };
      return state;
    });
    const result = prepareWake(store, lease, observation([], []), 1767225602000);
    expect(result.nextIntervalMinutes).toBeGreaterThan(30);
    expect(result.nextIntervalMinutes).toBeLessThanOrEqual(33);
    const repeated = prepareWake(store, lease, observation([], []), 1767225602001);
    expect(repeated.nextIntervalMinutes).toBe(result.nextIntervalMinutes);
  });

  it('prepares a new wake after removing and rebinding a job', () => {
    const { store, lease } = setup();
    configureRoutes(store, lease, [active], 1767225601001);
    const old = prepareWake(store, lease, observation(), 1767225602000).actions[0];
    settleWake(
      store,
      lease,
      {
        wakeId: old.wakeId,
        attemptId: old.attemptId,
        target: { threadId: 'thread', hostId: 'local' },
        status: 'accepted'
      },
      1767225602001
    );
    configureRoutes(store, lease, [], 1767225602002);
    configureRoutes(store, lease, [{ ...active, threadId: 'successor' }], 1767225602003);
    const result = prepareWake(store, lease, observation(), 1767225602004);
    expect(result.actions).toHaveLength(1);
    expect(result.actions[0].wakeId).not.toBe(old.wakeId);
    expect(result.actions[0].arguments.threadId).toBe('successor');
  });

  it('does not re-wake accepted work when identical route fields are reordered', () => {
    const { store, lease } = setup();
    configureRoutes(store, lease, [active], 1767225601001);
    const old = prepareWake(store, lease, observation(), 1767225602000).actions[0];
    settleWake(
      store,
      lease,
      {
        wakeId: old.wakeId,
        attemptId: old.attemptId,
        target: { threadId: 'thread', hostId: 'local' },
        status: 'accepted'
      },
      1767225602001
    );
    configureRoutes(
      store,
      lease,
      [
        {
          hostId: 'local',
          threadId: 'thread',
          lifecycle: 'active',
          logicalIdentity: 'agent',
          jobId: 'job'
        }
      ],
      1767225602002
    );
    const result = prepareWake(store, lease, observation(), 1767225602003);
    expect(result.actions).toEqual([]);
    expect(stateOf(store).attempts[old.wakeId].superseded).toBe(false);
  });

  it('accepts distinct colon-bearing identities with different revisions in one source', () => {
    const { store, lease } = setup();
    configureRoutes(
      store,
      lease,
      [active, { ...active, jobId: 'job:x', logicalIdentity: 'other', threadId: 'other' }],
      1767225601001
    );
    const result = prepareWake(
      store,
      lease,
      observation([
        { jobId: 'job', itemId: 'x:y', revision: 'r1' },
        { jobId: 'job:x', itemId: 'y', revision: 'r2' }
      ]),
      1767225602000
    );
    expect(result.actions).toHaveLength(2);
    expect(
      /** @type {Array<{arguments:{threadId:string}}>} */ (result.actions)
        .map((action) => action.arguments.threadId)
        .sort()
    ).toEqual(['other', 'thread']);
  });
});
