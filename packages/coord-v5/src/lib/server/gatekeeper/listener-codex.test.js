import { afterEach, expect, it } from 'vitest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { openListenerStore } from './listener-store.js';
import * as runtime from './listener-runtime.js';

// Invented targets and raw native envelopes; no captured user/provider data.
const start = 1767225600000;
const scope = { principalId: 'p', workspaceId: 'w', environmentId: 'e', harness: 'codex' };
const target = { threadId: 'synthetic-thread', hostId: 'local' };
const route = { jobId: 'job', logicalIdentity: 'agent', lifecycle: 'active', ...target };
const directories = [];
const stores = [];
const rawRead = (t = target, type = 'idle') => ({ schemaVersion: 1, thread: { id: t.threadId, hostId: t.hostId, status: { type } } });
const wrapped = value => ({ content: [{ type: 'text', text: JSON.stringify(value) }] });
const observation = (revision = 'r1') => ({ version: 1, observedAt: new Date(start + 2).toISOString(), eventObservation: { coverage: 'complete', items: [] }, obligationObservation: { coverage: 'complete', items: [{ jobId: 'job', itemId: 'item', revision }] } });
function setup(customTarget = target, harness = 'codex') {
  const directory = mkdtempSync(join(tmpdir(), 'codex-dispatch-'));
  directories.push(directory);
  const path = join(directory, 'journal.sqlite');
  const store = openListenerStore(path);
  stores.push(store);
  const lease = store.acquire({ ...scope, harness }, 'holder', start, 300000);
  runtime.configureRoutes(store, lease, [{ ...route, hostId: undefined, ...customTarget }].map(r => Object.fromEntries(Object.entries(r).filter(([, v]) => v !== undefined))), start + 1);
  const action = runtime.prepareWake(store, lease, observation(), start + 2).actions[0];
  const input = action && { wakeId: action.wakeId, attemptId: action.attemptId, target: customTarget };
  return { path, store, lease, action, input, now: () => start + 3 };
}
const stateOf = f => f.store.inspect(f.lease.scope).state;
const attemptOf = f => stateOf(f).attempts[f.input.wakeId];
const options = f => ({ now: f.now, readThread: async () => wrapped(rawRead(f.input.target)), sendMessage: async () => wrapped({ threadId: f.input.target.threadId }) });
afterEach(() => {
  for (const store of stores.splice(0)) store.close();
  for (const directory of directories.splice(0)) rmSync(directory, { recursive: true, force: true });
});

it('commits a claim visible from a fresh SQLite connection before exact native send', async () => {
  // Moving the claim after invocation must fail this independent-store assertion.
  const f = setup();
  let calls = 0;
  const result = await runtime.dispatchCodexWake(f.store, f.lease, f.input, {
    ...options(f),
    readThread: async args => { expect(args).toEqual(target); return wrapped(rawRead()); },
    sendMessage: async args => {
      calls++;
      const independent = openListenerStore(f.path);
      try {
        const attempt = independent.inspect(scope).state.attempts[f.input.wakeId];
        expect(attempt).toMatchObject({ state: 'dispatching', dispatchStatus: 'claimed', attemptId: f.input.attemptId });
        expect(attempt.dispatchClaimId).toMatch(/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/);
      } finally { independent.close(); }
      expect(args).toEqual({ ...target, prompt: `Listener notification for logical job agent (job). Known item IDs/revisions: obligation item@r1 Wake ${f.input.wakeId}; attempt ${f.input.attemptId}. Please acknowledge this notification using those IDs and your session target. This is not a completion request.` });
      return wrapped({ threadId: target.threadId });
    }
  });
  expect(calls).toBe(1);
  expect(result).toMatchObject({ state: 'accepted', code: 'NATIVE_ACCEPTED' });
  expect(attemptOf(f)).toMatchObject({ state: 'accepted', dispatchStatus: 'accepted', receiptStatus: 'accepted' });
  expect(stateOf(f).policy.obligations).toEqual({ '["job","item"]': 'r1' });
});

it.each([
  ['active', rawRead(target, 'active'), 'THREAD_NOT_IDLE'],
  ['busy', rawRead(target, 'busy'), 'THREAD_NOT_IDLE'],
  ['unknown', rawRead(target, 'unknown'), 'THREAD_NOT_IDLE'],
  ['missing status', { schemaVersion: 1, thread: { id: target.threadId, hostId: 'local' } }, 'THREAD_NOT_IDLE'],
  ['wrong thread', rawRead({ ...target, threadId: 'other' }), 'INVALID_THREAD_RESULT'],
  ['wrong host', rawRead({ ...target, hostId: 'other' }), 'INVALID_THREAD_RESULT'],
  ['missing host', { schemaVersion: 1, thread: { id: target.threadId, status: { type: 'idle' } } }, 'INVALID_THREAD_RESULT'],
  ['missing schema', { thread: rawRead().thread }, 'INVALID_THREAD_RESULT'],
  ['error', { isError: true, ...wrapped(rawRead()) }, 'INVALID_THREAD_RESULT'],
  ['payload error', wrapped({ ...rawRead(), error: { message: 'private-target' } }), 'INVALID_THREAD_RESULT'],
  ['malformed', { content: [{ type: 'text', text: 'not-json' }] }, 'INVALID_THREAD_RESULT'],
  ['empty', null, 'INVALID_THREAD_RESULT'],
  ['cloud kind', { ...rawRead(), thread: { ...rawRead().thread, kind: 'codex-cloud' } }, 'INVALID_THREAD_RESULT'],
  ['chatgpt kind', { ...rawRead(), thread: { ...rawRead().thread, kind: 'chatgpt' } }, 'INVALID_THREAD_RESULT'],
  ['contradictory wrapper', { ...wrapped(rawRead()), structuredContent: rawRead(target, 'active') }, 'INVALID_THREAD_RESULT'],
  ['contradictory direct/wrapper', { ...rawRead(target, 'active'), ...wrapped(rawRead()) }, 'INVALID_THREAD_RESULT'],
])('retains prepared obligation without sending on %s preflight', async (_name, response, code) => {
  // Treating anything except exact idle evidence as permission to send must fail.
  const f = setup();
  let sends = 0;
  const result = await runtime.dispatchCodexWake(f.store, f.lease, f.input, {
    ...options(f), readThread: async () => response,
    sendMessage: async () => { sends++; return { threadId: target.threadId }; }
  });
  expect(result).toEqual({ wakeId: f.input.wakeId, attemptId: f.input.attemptId, state: 'prepared', code });
  expect(sends).toBe(0);
  expect(attemptOf(f)).toMatchObject({ state: 'prepared' });
  expect(attemptOf(f).dispatchClaimId).toBeUndefined();
  f.store.close();
  const reopened = openListenerStore(f.path);
  stores.push(reopened);
  expect(reopened.inspect(scope).state.policy.obligations).toEqual({ '["job","item"]': 'r1' });
});

it.each([
  ['direct', { threadId: target.threadId }],
  ['text', wrapped({ threadId: target.threadId })],
  ['structured', { structuredContent: { threadId: target.threadId } }],
])('accepts exact %s native result only as tool acceptance', async (_name, response) => {
  const f = setup();
  const result = await runtime.dispatchCodexWake(f.store, f.lease, f.input, {
    ...options(f), readThread: async () => ({ structuredContent: rawRead() }), sendMessage: async () => response
  });
  expect(result.state).toBe('accepted');
  expect(stateOf(f).policy.obligations).toEqual({ '["job","item"]': 'r1' });
});

it.each([
  ['empty', undefined], ['boolean', true], ['asserted acceptance', { accepted: true }],
  ['wrong target', { threadId: 'other-private-target' }],
  ['tool error', { isError: true, ...wrapped({ threadId: target.threadId }) }],
  ['payload error', { threadId: target.threadId, error: 'private-diagnostics' }],
  ['malformed', { content: [{ type: 'text', text: '{' }] }],
  ['contradictory wrapper', { ...wrapped({ threadId: target.threadId }), structuredContent: { threadId: 'other' } }],
  ['contradictory direct/wrapper', { threadId: 'other', ...wrapped({ threadId: target.threadId }) }],
])('retains uncertain claim on %s send response', async (_name, response) => {
  const f = setup();
  let sends = 0;
  const opts = { ...options(f), sendMessage: async () => { sends++; return response; } };
  expect(await runtime.dispatchCodexWake(f.store, f.lease, f.input, opts)).toMatchObject({ state: 'uncertain', code: 'INVALID_NATIVE_RESULT' });
  const claimId = attemptOf(f).dispatchClaimId;
  expect(claimId).toBeTruthy();
  expect(attemptOf(f)).toMatchObject({ dispatchStatus: 'uncertain', receiptStatus: 'unknown' });
  expect(await runtime.dispatchCodexWake(f.store, f.lease, f.input, opts)).toMatchObject({ code: 'NOT_DISPATCHABLE' });
  expect(sends).toBe(1);
  expect(JSON.stringify(attemptOf(f))).not.toContain('private-diagnostics');
});

it('refuses repeat acceptance without a second native read or send', async () => {
  const f = setup();
  await runtime.dispatchCodexWake(f.store, f.lease, f.input, options(f));
  const result = await runtime.dispatchCodexWake(f.store, f.lease, f.input, {
    ...options(f), readThread: () => { throw new Error('must not read'); }, sendMessage: () => { throw new Error('must not send'); }
  });
  expect(result).toMatchObject({ state: 'accepted', code: 'NOT_DISPATCHABLE' });
});

it('resolves omitted route host to exact local native target without changing descriptors', async () => {
  const f = setup({ threadId: target.threadId });
  expect(f.action.arguments.hostId).toBeUndefined();
  const result = await runtime.dispatchCodexWake(f.store, f.lease, f.input, {
    ...options(f),
    readThread: async args => { expect(args).toEqual(target); return rawRead(); },
    sendMessage: async args => { expect(args).toEqual({ ...target, prompt: f.action.arguments.prompt }); return { threadId: target.threadId }; }
  });
  expect(result.state).toBe('accepted');
});

it('fences same resolved thread/host while another invocation remains claimed', async () => {
  const f = setup({ threadId: target.threadId });
  let clock = start + 3;
  let resolveSend;
  const pending = runtime.dispatchCodexWake(f.store, f.lease, f.input, {
    ...options(f), now: () => clock, readThread: async () => rawRead(),
    sendMessage: () => new Promise(resolve => { resolveSend = resolve; })
  });
  await new Promise(resolve => setTimeout(resolve, 1));
  runtime.configureRoutes(f.store, f.lease, [{ ...route, hostId: 'local' }], start + 4);
  const next = runtime.prepareWake(f.store, f.lease, observation('r2'), start + 5).actions[0];
  const result = await runtime.dispatchCodexWake(f.store, f.lease, { wakeId: next.wakeId, attemptId: next.attemptId, target }, {
    ...options(f), now: () => start + 6, readThread: () => { throw new Error('must not read'); }
  });
  expect(result.code).toBe('THREAD_RECONCILIATION_REQUIRED');
  clock = start + 6;
  resolveSend({ threadId: target.threadId });
  expect(await pending).toMatchObject({ state: 'accepted', code: 'NATIVE_ACCEPTED' });
  expect(attemptOf(f).dispatchStatus).toBe('accepted');
});

it('rechecks lease even when native preflight refused a busy target', async () => {
  const f = setup();
  let clock = start + 3;
  await expect(runtime.dispatchCodexWake(f.store, f.lease, f.input, {
    ...options(f), now: () => clock,
    readThread: async () => { clock = f.lease.expiresAt; return rawRead(target, 'active'); }
  })).rejects.toThrow('lease expired or fenced');
  expect(attemptOf(f).dispatchClaimId).toBeUndefined();
});

it.each(['read', 'send'])('refuses explicit native is_error at %s boundary', async boundary => {
  const f = setup();
  const result = await runtime.dispatchCodexWake(f.store, f.lease, f.input, {
    ...options(f),
    [boundary === 'read' ? 'readThread' : 'sendMessage']: async () => boundary === 'read'
      ? { ...rawRead(), is_error: true } : { threadId: target.threadId, is_error: true }
  });
  expect(result).toMatchObject({ state: boundary === 'read' ? 'prepared' : 'uncertain', code: boundary === 'read' ? 'INVALID_THREAD_RESULT' : 'INVALID_NATIVE_RESULT' });
});

it.each(['route', 'lease', 'correlation', 'prepared state'])('revalidates %s after preflight before claiming', async change => {
  const f = setup();
  let sends = 0;
  let clock = start + 3;
  const promise = runtime.dispatchCodexWake(f.store, f.lease, f.input, {
    ...options(f), now: () => clock,
    readThread: async () => {
      clock++;
      if (change === 'route') runtime.configureRoutes(f.store, f.lease, [{ ...route, threadId: 'other' }], clock);
      else if (change === 'lease') clock = f.lease.expiresAt;
      else f.store.transact(f.lease, clock, state => {
        const attempt = state.attempts[f.input.wakeId];
        if (change === 'correlation') attempt.attemptId = 'other';
        else attempt.state = 'acknowledged';
        return state;
      });
      return rawRead();
    }, sendMessage: async () => { sends++; return { threadId: target.threadId }; }
  });
  if (change === 'lease') await expect(promise).rejects.toThrow('lease expired or fenced');
  else if (change === 'correlation') await expect(promise).rejects.toThrow('correlation mismatch');
  else expect(await promise).toMatchObject({ code: change === 'route' ? 'STALE_BINDING' : 'NOT_DISPATCHABLE' });
  expect(sends).toBe(0);
  expect(attemptOf(f).dispatchClaimId).toBeUndefined();
});

it.each(['lease', 'claim', 'settled receipt'])('fails closed on changed %s after native invocation', async change => {
  const f = setup();
  let clock = start + 3;
  const promise = runtime.dispatchCodexWake(f.store, f.lease, f.input, {
    ...options(f), now: () => clock,
    sendMessage: async () => {
      if (change === 'lease') clock = f.lease.expiresAt;
      else f.store.transact(f.lease, ++clock, state => {
        const attempt = state.attempts[f.input.wakeId];
        if (change === 'claim') attempt.dispatchClaimId = 'changed-claim';
        else { attempt.state = 'accepted'; attempt.dispatchStatus = 'accepted'; attempt.receiptStatus = 'accepted'; }
        return state;
      });
      return { threadId: target.threadId };
    }
  });
  await expect(promise).rejects.toThrow(change === 'lease' ? 'lease expired or fenced' : 'claim mismatch');
  expect(stateOf(f).policy.obligations).toEqual({ '["job","item"]': 'r1' });
  if (change === 'lease') expect(attemptOf(f)).toMatchObject({ state: 'dispatching', dispatchStatus: 'claimed' });
});

it.each([true, false])('preserves receiver acknowledgment and refuses inconsistent settlement (accepted=%s)', async accepted => {
  const f = setup();
  const promise = runtime.dispatchCodexWake(f.store, f.lease, f.input, {
    ...options(f), sendMessage: async () => {
      runtime.acknowledgeWake(f.store, f.lease, f.input, start + 3);
      return accepted ? { threadId: target.threadId } : null;
    }
  });
  if (accepted) expect(await promise).toMatchObject({ state: 'acknowledged', code: 'NATIVE_ACCEPTED' });
  else await expect(promise).rejects.toThrow('Conflicting listener receipt');
  expect(attemptOf(f)).toMatchObject({ state: 'acknowledged', dispatchStatus: accepted ? 'accepted' : 'claimed' });
});

it('times out observation, retains uncertain obligation across reopen, and ignores late resolution', async () => {
  const f = setup();
  let complete;
  let sends = 0;
  const result = await runtime.dispatchCodexWake(f.store, f.lease, f.input, {
    ...options(f), timeoutMs: 5,
    sendMessage: () => { sends++; return new Promise(resolve => { complete = resolve; }); }
  });
  expect(result).toMatchObject({ state: 'uncertain', code: 'TIMEOUT' });
  const before = attemptOf(f);
  f.store.close();
  complete({ threadId: target.threadId });
  await new Promise(resolve => setTimeout(resolve, 10));
  const reopened = openListenerStore(f.path);
  stores.push(reopened);
  expect(reopened.inspect(scope).state.attempts[f.input.wakeId]).toEqual(before);
  const lease = reopened.acquire(scope, 'holder', start + 4, 300000);
  const prepared = runtime.prepareWake(reopened, lease, observation('r2'), start + 5);
  expect(prepared.reconciliation).toMatchObject([{ wakeId: f.input.wakeId, state: 'uncertain' }]);
  const next = prepared.actions[0];
  expect(await runtime.dispatchCodexWake(reopened, lease, { wakeId: next.wakeId, attemptId: next.attemptId, target }, {
    ...options(f), now: () => start + 6, sendMessage: () => { sends++; }
  })).toMatchObject({ code: 'THREAD_RECONCILIATION_REQUIRED' });
  expect(sends).toBe(1);
});

it.each(['throw', 'timeout'])('refuses %s preflight without a claim and allows later reconsideration', async kind => {
  const f = setup();
  const result = await runtime.dispatchCodexWake(f.store, f.lease, f.input, {
    ...options(f), timeoutMs: 5,
    readThread: () => { if (kind === 'throw') throw new Error('private-target'); return new Promise(() => {}); }
  });
  expect(result).toMatchObject({ state: 'prepared', code: kind === 'throw' ? 'PREFLIGHT_UNAVAILABLE' : 'PREFLIGHT_TIMEOUT' });
  expect((await runtime.dispatchCodexWake(f.store, f.lease, f.input, options(f))).state).toBe('accepted');
});

it('journals a thrown native send as uncertain without private diagnostics', async () => {
  const f = setup();
  const result = await runtime.dispatchCodexWake(f.store, f.lease, f.input, {
    ...options(f), sendMessage: () => { throw new Error('private-target'); }
  });
  expect(result).toMatchObject({ state: 'uncertain', code: 'NATIVE_FAILED' });
  expect(JSON.stringify(stateOf(f))).not.toContain('private-target');
});

it.each([0, -1, 1.5, 60001, Infinity, '5'])('rejects invalid timeout %s before native calls', async timeoutMs => {
  const f = setup();
  await expect(runtime.dispatchCodexWake(f.store, f.lease, f.input, { ...options(f), timeoutMs })).rejects.toThrow('Invalid Codex executor');
  expect(attemptOf(f).dispatchClaimId).toBeUndefined();
});

it('requires both callbacks, exact scope and correlation, and rejects caller prompt', async () => {
  const f = setup();
  for (const key of ['readThread', 'sendMessage']) await expect(runtime.dispatchCodexWake(f.store, f.lease, f.input, { ...options(f), [key]: undefined })).rejects.toThrow('Invalid Codex executor');
  for (const harness of ['codex-cloud', 'claude-code']) await expect(runtime.dispatchCodexWake(f.store, { ...f.lease, scope: { ...scope, harness } }, f.input, options(f))).rejects.toThrow('Invalid Codex dispatch target');
  await expect(runtime.dispatchCodexWake(f.store, f.lease, { ...f.input, target: { kind: 'claude-code', sessionId: '11111111-1111-4111-8111-111111111111', cwd: '/tmp/synthetic' } }, options(f))).rejects.toThrow('Invalid Codex dispatch target');
  await expect(runtime.dispatchCodexWake(f.store, f.lease, { ...f.input, attemptId: 'wrong' }, options(f))).rejects.toThrow('correlation mismatch');
  await expect(runtime.dispatchCodexWake(f.store, f.lease, { ...f.input, target: { ...target, hostId: 'wrong' } }, options(f))).rejects.toThrow('correlation mismatch');
  await expect(runtime.dispatchCodexWake(f.store, f.lease, { ...f.input, prompt: 'caller text' }, options(f))).rejects.toThrow('Invalid listener input');
  expect(attemptOf(f)).toMatchObject({ state: 'prepared' });
});

it('allows a separate host while an identical thread ID has an uncertain local invocation', async () => {
  const f = setup();
  await runtime.dispatchCodexWake(f.store, f.lease, f.input, { ...options(f), sendMessage: async () => null });
  const remote = { ...target, hostId: 'synthetic-remote' };
  runtime.configureRoutes(f.store, f.lease, [{ ...route, ...remote }], start + 4);
  const action = runtime.prepareWake(f.store, f.lease, observation(), start + 5).actions[0];
  const result = await runtime.dispatchCodexWake(f.store, f.lease, { wakeId: action.wakeId, attemptId: action.attemptId, target: remote }, {
    now: () => start + 6, readThread: async () => rawRead(remote),
    sendMessage: async args => { expect(args.hostId).toBe('synthetic-remote'); return { threadId: remote.threadId }; }
  });
  expect(result.state).toBe('accepted');
  expect(attemptOf(f).dispatchStatus).toBe('uncertain');
});

it('allows one claim when concurrent preflights observe the same prepared attempt', async () => {
  const f = setup();
  let sends = 0;
  let release;
  const preflight = new Promise(resolve => { release = resolve; });
  const opts = { ...options(f), readThread: () => preflight, sendMessage: async () => { sends++; return { threadId: target.threadId }; } };
  const first = runtime.dispatchCodexWake(f.store, f.lease, f.input, opts);
  const second = runtime.dispatchCodexWake(f.store, f.lease, f.input, opts);
  release(rawRead());
  const results = await Promise.all([first, second]);
  expect(results.map(r => r.code).sort()).toEqual(['NATIVE_ACCEPTED', 'NOT_DISPATCHABLE']);
  expect(sends).toBe(1);
});

it('fences an old lease epoch replaced during native preflight', async () => {
  const f = setup();
  let clock = start + 3;
  await expect(runtime.dispatchCodexWake(f.store, f.lease, f.input, {
    ...options(f), now: () => clock, readThread: async () => {
      clock = f.lease.expiresAt;
      const next = f.store.acquire(scope, 'new-holder', clock, 300000);
      expect(next.epoch).toBe(f.lease.epoch + 1);
      return rawRead();
    }
  })).rejects.toThrow('lease expired or fenced');
  expect(attemptOf(f).state).toBe('prepared');
});

it.each([1, 60000])('accepts supported timeout boundary %s', async timeoutMs => {
  const f = setup();
  expect((await runtime.dispatchCodexWake(f.store, f.lease, f.input, { ...options(f), timeoutMs })).state).toBe('accepted');
});

it('freezes original correlation against caller mutation during preflight', async () => {
  const f = setup();
  const input = { ...f.input, target: { ...target } };
  const result = await runtime.dispatchCodexWake(f.store, f.lease, input, {
    ...options(f), readThread: async () => { input.attemptId = 'changed'; input.target.threadId = 'changed'; return rawRead(); }
  });
  expect(result).toMatchObject({ attemptId: f.input.attemptId, state: 'accepted' });
});

it('classifies non-JSON structured send evidence as uncertain instead of throwing', async () => {
  const f = setup();
  const cyclic = { threadId: target.threadId };
  cyclic.self = cyclic;
  const result = await runtime.dispatchCodexWake(f.store, f.lease, f.input, {
    ...options(f), sendMessage: async () => ({ structuredContent: cyclic })
  });
  expect(result).toMatchObject({ state: 'uncertain', code: 'INVALID_NATIVE_RESULT' });
});

it.each(['cyclic', 'bigint', 'undefined serialization'])('retains an uncertain claim without retry for malformed direct %s send evidence', async kind => {
  // Removing direct-payload serialization validation would promote this to acceptance.
  const f = setup();
  const response = { threadId: target.threadId };
  if (kind === 'undefined serialization') response.toJSON = () => undefined;
  else response.invalid = kind === 'cyclic' ? response : 1n;
  let sends = 0;
  const opts = { ...options(f), sendMessage: async () => { sends++; return response; } };
  const result = await runtime.dispatchCodexWake(f.store, f.lease, f.input, opts);
  expect(result).toMatchObject({ state: 'uncertain', code: 'INVALID_NATIVE_RESULT' });
  expect(attemptOf(f)).toMatchObject({ dispatchStatus: 'uncertain', receiptStatus: 'unknown' });
  expect(attemptOf(f).dispatchClaimId).toBeTruthy();
  expect(await runtime.dispatchCodexWake(f.store, f.lease, f.input, opts)).toMatchObject({ code: 'NOT_DISPATCHABLE' });
  expect(sends).toBe(1);
});

it.each(['cyclic', 'bigint', 'undefined serialization'])('refuses malformed direct %s read evidence without send or claim', async kind => {
  // Moving direct return before validation would authorize sending on malformed read evidence.
  const f = setup();
  const response = rawRead();
  if (kind === 'undefined serialization') response.toJSON = () => undefined;
  else response.invalid = kind === 'cyclic' ? response : 1n;
  let sends = 0;
  const result = await runtime.dispatchCodexWake(f.store, f.lease, f.input, {
    ...options(f), readThread: async () => response,
    sendMessage: async () => { sends++; return { threadId: target.threadId }; }
  });
  expect(result).toMatchObject({ state: 'prepared', code: 'INVALID_THREAD_RESULT' });
  expect(sends).toBe(0);
  expect(attemptOf(f).dispatchClaimId).toBeUndefined();
  expect(stateOf(f).policy.obligations).toEqual({ '["job","item"]': 'r1' });
});

it('refuses structured send evidence whose serialization produces no JSON value', async () => {
  const f = setup();
  expect(await runtime.dispatchCodexWake(f.store, f.lease, f.input, {
    ...options(f), sendMessage: async () => ({ structuredContent: { threadId: target.threadId, toJSON: () => undefined } })
  })).toMatchObject({ state: 'uncertain', code: 'INVALID_NATIVE_RESULT' });
});
