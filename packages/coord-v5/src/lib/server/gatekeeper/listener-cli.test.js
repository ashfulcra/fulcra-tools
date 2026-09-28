import { afterEach, expect, it } from 'vitest';
import { mkdtempSync, readFileSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';

const script = resolve('scripts/gatekeeper-listener.mjs');
const scope = {
  principalId: 'principal-a',
  workspaceId: 'workspace-a',
  environmentId: 'local',
  harness: 'codex'
};
/** @type {string[]} */
const directories = [];

function databasePath() {
  const directory = mkdtempSync(join(tmpdir(), 'listener-cli-'));
  directories.push(directory);
  return join(directory, 'journal.sqlite');
}

/** @param {string} command @param {string} db @param {unknown} [input] @param {string[]} [extra] */
function run(command, db, input, extra = []) {
  const result = spawnSync(
    process.execPath,
    [
      script,
      command,
      '--db',
      db,
      '--scope',
      JSON.stringify(scope),
      '--holder',
      'holder-a',
      ...extra
    ],
    {
      input:
        input === undefined
          ? ''
          : typeof input === 'string' || Buffer.isBuffer(input)
            ? input
            : JSON.stringify(input),
      encoding: 'utf8'
    }
  );
  return {
    status: result.status,
    stdout: result.stdout ? JSON.parse(result.stdout) : null,
    stderr: result.stderr
  };
}

afterEach(() => {
  for (const directory of directories.splice(0))
    rmSync(directory, { recursive: true, force: true });
});

it('inspects without creating a journal or taking a lease', () => {
  const db = databasePath();
  expect(run('inspect', db)).toMatchObject({ status: 0, stdout: { snapshot: null }, stderr: '' });
  expect(() => statSync(db)).toThrow();
  expect(run('configure', db, [])).toMatchObject({ status: 0, stdout: { routes: {} }, stderr: '' });
  const first = run('inspect', db);
  expect(first.status).toBe(0);
  expect(first.stdout.snapshot.lease).toMatchObject({ holder: 'holder-a', epoch: 1 });
  expect(first.stdout.snapshot.state.routes).toEqual({});
  expect(run('inspect', db).stdout.snapshot).toEqual(first.stdout.snapshot);
});

it('configures, prepares, settles and acknowledges across separate CLI processes', () => {
  const db = databasePath();
  const route = {
    jobId: 'job-a',
    logicalIdentity: 'logical-a',
    lifecycle: 'active',
    threadId: 'test-thread',
    hostId: 'local'
  };
  expect(run('configure', db, [route]).stdout.routes).toEqual({ 'job-a': route });
  const observation = {
    version: 1,
    eventObservation: {
      coverage: 'complete',
      items: [{ itemId: 'event-a', jobId: 'job-a', revision: 'rev-1' }]
    },
    obligationObservation: {
      coverage: 'complete',
      items: [{ itemId: 'work-a', jobId: 'job-a', revision: 'rev-1' }]
    },
    observedAt: new Date(Date.now() - 1000).toISOString()
  };
  const prepared = run('prepare', db, observation);
  expect(prepared.status).toBe(0);
  expect(prepared.stdout.coverage).toEqual({ events: 'complete', obligations: 'complete' });
  expect(prepared.stdout.actions).toHaveLength(1);
  const action = prepared.stdout.actions[0];
  expect(action).toMatchObject({
    tool: 'mcp__codex_app__send_message_to_thread',
    arguments: { threadId: 'test-thread', hostId: 'local' }
  });
  expect(run('prepare', db, observation).stdout).toMatchObject({
    actions: [],
    reconciliation: [{ wakeId: action.wakeId, attemptId: action.attemptId, state: 'prepared' }]
  });
  const target = { threadId: 'test-thread', hostId: 'local' };
  expect(
    run('settle', db, {
      wakeId: action.wakeId,
      attemptId: action.attemptId,
      target,
      status: 'accepted'
    }).stdout.state
  ).toBe('accepted');
  expect(
    run('ack', db, { wakeId: action.wakeId, attemptId: action.attemptId, target }).stdout.state
  ).toBe('acknowledged');
  expect(run('prepare', db, observation).stdout.actions).toEqual([]);
  const snapshot = run('inspect', db).stdout.snapshot;
  expect(snapshot.state.attempts[action.wakeId].state).toBe('acknowledged');
  expect(snapshot.state.policy.obligations).toEqual({ '["job-a","work-a"]': 'rev-1' });
});

it('reports lease contention with exit 2 and permits read-only inspection', () => {
  const db = databasePath();
  expect(run('configure', db, []).status).toBe(0);
  const second = spawnSync(
    process.execPath,
    [script, 'configure', '--db', db, '--scope', JSON.stringify(scope), '--holder', 'holder-b'],
    { input: '[]', encoding: 'utf8' }
  );
  expect(second.status).toBe(2);
  expect(JSON.parse(second.stdout)).toEqual({ error: { code: 'LEASE_CONFLICT' } });
  expect(second.stderr).toBe('');
  expect(run('inspect', db).status).toBe(0);
});

it('rejects malformed flags and payloads without reflecting them or mutating state', () => {
  const db = databasePath();
  const raw = 'SENSITIVE_TOKEN_DO_NOT_ECHO';
  for (const extra of [
    ['--mystery', raw],
    ['--db', raw],
    ['--holder', raw]
  ]) {
    const outcome = run('configure', db, [], extra);
    expect(outcome.status).toBe(1);
    expect(JSON.stringify(outcome)).not.toContain(raw);
  }
  const malformed = run('configure', db, `{ "not-json": ${raw}`);
  expect(malformed).toMatchObject({ status: 1, stdout: { error: { code: 'BAD_INPUT' } } });
  expect(JSON.stringify(malformed)).not.toContain(raw);
  const oversizedScope = spawnSync(
    process.execPath,
    [
      script,
      'inspect',
      '--db',
      db,
      '--scope',
      `${JSON.stringify(scope)}${' '.repeat(5000)}`,
      '--holder',
      'holder-a'
    ],
    { encoding: 'utf8' }
  );
  expect(oversizedScope.status).toBe(1);
  expect(JSON.parse(oversizedScope.stdout)).toEqual({ error: { code: 'BAD_INPUT' } });
  expect(() => statSync(db)).toThrow();
});

it('caps stdin by UTF-8 bytes, rejects malformed UTF-8 and raw native tool responses', () => {
  const db = databasePath();
  const excessive = run('configure', db, `"${'é'.repeat(131072)}"`);
  expect(excessive).toMatchObject({ status: 1, stdout: { error: { code: 'BAD_INPUT' } } });
  expect(run('configure', db, Buffer.from([0xff]))).toMatchObject({
    status: 1,
    stdout: { error: { code: 'BAD_INPUT' } }
  });
  expect(
    run('settle', db, {
      wakeId: 'wake',
      attemptId: 'attempt',
      target: { threadId: 'thread' },
      status: 'accepted',
      nativeResult: 'SECRET'
    })
  ).toMatchObject({ status: 1, stdout: { error: { code: 'BAD_INPUT' } } });
  expect(() => statSync(db)).toThrow();
});

it('supports the committed synthetic observation fixture', () => {
  const db = databasePath();
  const fixture = JSON.parse(
    readFileSync(resolve('tests/fixtures/listener-observation-synthetic.json'), 'utf8')
  );
  expect(
    run('configure', db, [
      {
        jobId: 'synthetic-job',
        logicalIdentity: 'synthetic-job',
        lifecycle: 'active',
        threadId: 'synthetic-thread'
      }
    ]).status
  ).toBe(0);
  expect(run('prepare', db, fixture).stdout).toMatchObject({
    coverage: { events: 'partial', obligations: 'partial' },
    actions: [{ arguments: { threadId: 'synthetic-thread' } }]
  });
});

it('classifies stale observation as safe bad input after reopening the journal', () => {
  const db = databasePath();
  const observation = {
    version: 1,
    eventObservation: { coverage: 'complete', items: [] },
    obligationObservation: { coverage: 'complete', items: [] },
    observedAt: new Date(Date.now() - 1000).toISOString()
  };
  expect(run('prepare', db, observation).status).toBe(0);
  const stale = run('prepare', db, { ...observation, observedAt: '2026-09-01T00:00:00Z' });
  expect(stale).toMatchObject({ status: 1, stdout: { error: { code: 'BAD_INPUT' } }, stderr: '' });
  expect(run('inspect', db).stdout.snapshot.state.policy.lastObservedMs).toBe(
    Date.parse(observation.observedAt)
  );
});

it('timestamps the lease and observation check after bounded stdin arrives', async () => {
  const db = databasePath();
  const child = spawn(process.execPath, [
    script,
    'prepare',
    '--db',
    db,
    '--scope',
    JSON.stringify(scope),
    '--holder',
    'holder-a'
  ]);
  let stdout = '';
  child.stdout.setEncoding('utf8').on('data', (chunk) => {
    stdout += chunk;
  });
  const observedAt = new Date(Date.now() + 300).toISOString();
  await new Promise((resolve) => setTimeout(resolve, 500));
  child.stdin.end(
    JSON.stringify({
      version: 1,
      eventObservation: { coverage: 'partial', items: [] },
      obligationObservation: { coverage: 'partial', items: [] },
      observedAt
    })
  );
  const exit = await new Promise((resolve) => child.on('close', resolve));
  expect(exit).toBe(0);
  expect(JSON.parse(stdout).coverage).toEqual({ events: 'partial', obligations: 'partial' });
  const lease = run('inspect', db).stdout.snapshot.lease;
  expect(lease.expiresAt).toBeGreaterThanOrEqual(Date.parse(observedAt) + 120000);
});
