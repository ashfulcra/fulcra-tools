import { afterEach, expect, it } from 'vitest';
import { chmodSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { openListenerStore } from './listener-store.js';
import * as runtime from './listener-runtime.js';

// Invented identities and responses, never captured provider data.
const sessionId = '11111111-1111-4111-8111-111111111111';
const target = { kind: 'claude-code', sessionId, cwd: '/tmp/synthetic-workspace' };
const scope = { principalId: 'p', workspaceId: 'w', environmentId: 'e', harness: 'claude-code' };
const start = 1767225600000;
const resources = [];
const successful = { type: 'result', subtype: 'success', is_error: false, session_id: sessionId, result: 'synthetic reply' };
function setup(behavior = { response: successful }) {
  const cwd = realpathSync(mkdtempSync(join(tmpdir(), 'claude-dispatch-')));
  const db = join(cwd, 'journal.sqlite');
  const store = openListenerStore(db);
  resources.push({ cwd, store });
  const lease = store.acquire(scope, 'holder', start, 300000);
  const binding = { ...target, cwd };
  const route = { jobId: 'job', logicalIdentity: 'agent', lifecycle: 'active', target: binding };
  runtime.configureRoutes(store, lease, [route], start + 1);
  const prepare = (revision = 'r1', ms = start + 2) => runtime.prepareWake(store, lease, {
    version: 1, observedAt: new Date(ms).toISOString(),
    eventObservation: { coverage: 'complete', items: [{ jobId: 'job', itemId: 'item', revision }] },
    obligationObservation: { coverage: 'complete', items: [] }
  }, ms);
  const action = prepare().actions[0];
  writeFileSync(join(cwd, 'behavior.json'), JSON.stringify(behavior));
  const executable = resolve('tests/fixtures/claude-cli-synthetic.mjs');
  chmodSync(executable, 0o755);
  const input = { wakeId: action.wakeId, attemptId: action.attemptId, target: binding };
  const dispatch = (options = {}, value = input) => runtime.dispatchClaudeWake(store, lease, value, {
    executable, now: () => start + 10, ...options
  });
  return { cwd, db, store, lease, route, action, input, dispatch, prepare };
}
afterEach(() => {
  for (const { store, cwd } of resources.splice(0)) { store.close(); rmSync(cwd, { recursive: true, force: true }); }
});

it('prepares an explicit Claude target without marking delivery accepted', () => {
  const { action, store, input } = setup();
  expect(action).toMatchObject({ tool: 'coord-v5 listener dispatch-claude', arguments: input });
  expect(store.inspect(scope).state.attempts[input.wakeId].state).toBe('prepared');
});

it.each([
  { ...target, sessionId: 'not-a-uuid' },
  { ...target, cwd: 'relative' },
  { ...target, cwd: '/tmp/../tmp/work' },
  { ...target, cwd: '/tmp/secret\nvalue' },
  { ...target, executable: '/bin/sh' },
  { ...target, kind: 'claude-desktop' }
])('rejects invalid or observation-controllable Claude route targets %#', (badTarget) => {
  const { store, lease, route } = setup();
  expect(() => runtime.configureRoutes(store, lease, [{ ...route, target: badTarget }], start + 3)).toThrow();
});

it('rejects mixing Codex routing with a Claude target', () => {
  const { store, lease, route } = setup();
  expect(() => runtime.configureRoutes(store, lease, [{ ...route, threadId: 'thread' }], start + 3)).toThrow();
});

it('accepts only native success and launches fixed safe argv with journal prompt on stdin', async () => {
  const { dispatch, cwd, store, input } = setup({ response: successful, inspectClaim: true });
  expect(await dispatch()).toMatchObject({ state: 'accepted', code: 'NATIVE_ACCEPTED' });
  const calls = JSON.parse(readFileSync(join(cwd, 'calls.json'), 'utf8'));
  expect(calls.argv).toEqual(['--print', '--resume', sessionId, '--model', 'haiku', '--max-budget-usd', '0.25', '--output-format', 'json', '--safe-mode', '--tools', '', '--disable-slash-commands', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--permission-mode', 'plan', '--no-chrome']);
  expect(calls.cwd).toBe(cwd);
  expect(calls.claimState).toBe('claimed');
  expect(calls.prompt).toContain(`Wake ${input.wakeId}; attempt ${input.attemptId}`);
  expect(store.inspect(scope).state.attempts[input.wakeId]).toMatchObject({ state: 'accepted', dispatchStatus: 'accepted' });
  expect(JSON.stringify(store.inspect(scope))).not.toContain('synthetic reply');
  expect(await dispatch()).toMatchObject({ code: 'NOT_DISPATCHABLE' });
});

it.each([
  { response: { ...successful, session_id: '22222222-2222-4222-8222-222222222222' } },
  { response: { session_id: sessionId } },
  { response: { ...successful, subtype: 'error_during_execution' } },
  { response: { ...successful, is_error: true } },
  { response: { ...successful, type: 'assistant' } },
  { raw: 'secret-output-not-json' },
  { response: successful, exitCode: 1 },
  { stderr: 'secret-stderr', exitCode: 2 }
])('records ambiguous native outcome without sensitive diagnostics or retry %#', async (behavior) => {
  const { dispatch, store, input } = setup(behavior);
  const result = await dispatch();
  expect(result).toMatchObject({ state: 'uncertain' });
  expect(JSON.stringify(result)).not.toContain('secret');
  expect(JSON.stringify(store.inspect(scope))).not.toContain('secret');
  expect(await dispatch()).toMatchObject({ code: 'NOT_DISPATCHABLE' });
  expect(store.inspect(scope).state.attempts[input.wakeId].dispatchStatus).toBe('uncertain');
});

it('claims spawn failures and never retries them', async () => {
  const { dispatch, cwd, store, input } = setup();
  const broken = join(cwd, 'broken');
  writeFileSync(broken, '#!/nonexistent-interpreter\n'); chmodSync(broken, 0o755);
  expect(await dispatch({ executable: broken })).toMatchObject({ state: 'uncertain', code: 'SPAWN_FAILED' });
  expect(await dispatch()).toMatchObject({ code: 'NOT_DISPATCHABLE' });
  expect(store.inspect(scope).state.attempts[input.wakeId].dispatchStatus).toBe('uncertain');
});

it.each([{ hang: true }, { stdoutBytes: 100000 }, { stderrBytes: 100000 }])('bounds child runtime and combined output %#', async (behavior) => {
  const { dispatch } = setup(behavior);
  const result = await dispatch({ timeoutMs: behavior.hang ? 100 : 5000 });
  expect(result).toMatchObject({ state: 'uncertain', code: behavior.hang ? 'TIMEOUT' : 'OUTPUT_LIMIT' });
  expect(await dispatch()).toMatchObject({ code: 'NOT_DISPATCHABLE' });
});

it('does not wait for inherited descendant pipes after killing only the owned child', async () => {
  const { dispatch } = setup({ hang: true, descendantPipes: true });
  const began = Date.now();
  expect(await dispatch({ timeoutMs: 150 })).toMatchObject({ state: 'uncertain', code: 'TIMEOUT' });
  expect(Date.now() - began).toBeLessThan(1000);
});

it('refuses untrusted executor paths and excessive timeout before claiming', async () => {
  const { dispatch, store, input } = setup();
  await expect(dispatch({ executable: 'claude' })).rejects.toThrow();
  await expect(dispatch({ timeoutMs: 60001 })).rejects.toThrow();
  await expect(dispatch({}, { ...input, executable: '/bin/sh' })).rejects.toThrow();
  expect(store.inspect(scope).state.attempts[input.wakeId].state).toBe('prepared');
});

it('rejects stale bindings and mismatched exact native targets before launch', async () => {
  const { dispatch, store, lease, route, input } = setup();
  await expect(dispatch({}, { ...input, target: { ...input.target, cwd: '/tmp/other' } })).rejects.toThrow();
  runtime.configureRoutes(store, lease, [{ ...route, target: { ...route.target, cwd: '/tmp/other' } }], start + 3);
  expect(await dispatch()).toMatchObject({ code: 'STALE_BINDING' });
});

it('atomically fences concurrent dispatch through independent SQLite connections', async () => {
  const { dispatch, db, lease, input, cwd } = setup({ response: successful, delayMs: 100 });
  const second = openListenerStore(db); resources.push({ store: second, cwd: mkdtempSync(join(tmpdir(), 'claude-cleanup-')) });
  const running = dispatch();
  const other = await runtime.dispatchClaudeWake(second, lease, input, { executable: resolve('tests/fixtures/claude-cli-synthetic.mjs'), now: () => start + 10 });
  expect(other).toMatchObject({ code: 'NOT_DISPATCHABLE' });
  expect(await running).toMatchObject({ state: 'accepted' });
  expect(JSON.parse(readFileSync(join(cwd, 'calls.json'), 'utf8')).count).toBe(1);
});

it('fences different wake attempts targeting the same session during and after ambiguous launch', async () => {
  const { dispatch, prepare, input, store, lease } = setup({ raw: 'not-json', delayMs: 100 });
  const next = prepare('r2', start + 3).actions[0];
  const another = { wakeId: next.wakeId, attemptId: next.attemptId, target: input.target };
  const running = dispatch();
  const options = { executable: resolve('tests/fixtures/claude-cli-synthetic.mjs'), now: () => start + 10 };
  expect(await runtime.dispatchClaudeWake(store, lease, another, options)).toMatchObject({ code: 'SESSION_RECONCILIATION_REQUIRED' });
  expect(await running).toMatchObject({ state: 'uncertain' });
  expect(await runtime.dispatchClaudeWake(store, lease, another, options)).toMatchObject({ code: 'SESSION_RECONCILIATION_REQUIRED' });
});

it('retains a crash-left claim after reopen and reports reconciliation without resending', async () => {
  const { store, db, lease, prepare, input } = setup();
  store.transact(lease, start + 3, state => { state.attempts[input.wakeId].state = 'dispatching'; state.attempts[input.wakeId].dispatchStatus = 'claimed'; return state; });
  store.close();
  const reopened = openListenerStore(db); resources.push({ store: reopened, cwd: mkdtempSync(join(tmpdir(), 'claude-cleanup-')) });
  expect(await runtime.dispatchClaudeWake(reopened, lease, input, { executable: resolve('tests/fixtures/claude-cli-synthetic.mjs'), now: () => start + 10 })).toMatchObject({ code: 'NOT_DISPATCHABLE' });
  const result = runtime.prepareWake(reopened, lease, { version: 1, observedAt: new Date(start + 11).toISOString(), eventObservation: { coverage: 'complete', items: [] }, obligationObservation: { coverage: 'complete', items: [] } }, start + 11);
  expect(result.reconciliation).toMatchObject([{ state: 'dispatching' }]);
  expect(result.actions).toEqual([]);
});

it('correlates Claude acknowledgments without inventing one from native acceptance', async () => {
  const { dispatch, store, lease, input } = setup();
  await dispatch();
  expect(store.inspect(scope).state.attempts[input.wakeId].state).toBe('accepted');
  expect(() => runtime.acknowledgeWake(store, lease, { ...input, target: { ...input.target, sessionId: '22222222-2222-4222-8222-222222222222' } }, start + 11)).toThrow();
  expect(runtime.acknowledgeWake(store, lease, input, start + 11)).toMatchObject({ state: 'acknowledged' });
});

it('allows explicit later acceptance to reconcile an uncertain native claim', async () => {
  const { dispatch, store, lease, input, prepare, cwd } = setup({ raw: 'not-json' });
  const next = prepare('r2', start + 3).actions[0];
  await dispatch();
  runtime.settleWake(store, lease, { ...input, status: 'accepted' }, start + 11);
  writeFileSync(join(cwd, 'behavior.json'), JSON.stringify({ response: successful }));
  expect(await dispatch({ now: () => start + 12 }, next.arguments)).toMatchObject({ state: 'accepted', code: 'NATIVE_ACCEPTED' });
});

it('does not downgrade recorded native acceptance with a conflicting later receipt', async () => {
  const { dispatch, store, lease, input } = setup();
  await dispatch();
  expect(() => runtime.settleWake(store, lease, { ...input, status: 'error' }, start + 11)).toThrow();
});
