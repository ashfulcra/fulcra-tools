import { afterEach, describe, expect, it } from 'vitest';
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import { DatabaseSync } from 'node:sqlite';
import fixture from '../tests/fixtures/work-backlog-synthetic.json';
import { serializeWorkEvent } from '../src/lib/gatekeeper/work-contract.js';

const script = resolve('scripts/gatekeeper-work-transport.mjs');
const principalId = '00000000-0000-4000-8000-000000000900';
const streamId = '00000000-0000-4000-8000-000000000901';
const sourceId = `com.fulcradynamics.annotation.${streamId}`;
const config = {
  baseUrl: 'https://api.fulcradynamics.com/',
  principalId,
  channel: `MomentAnnotation/${streamId}`,
  workspaceId: '00000000-0000-4000-8000-000000000100',
  workstreamId: '00000000-0000-4000-8000-000000000300',
  actorBinding: {
    principal_id: principalId,
    logical_agent_id: 'agent-a',
    instance_id: 'instance-a',
    session_id: 'session-a'
  }
};
const event = (n, changes = {}) => ({
  ...fixture.events[0],
  event_id: `10000000-0000-4000-8000-${String(n).padStart(12, '0')}`,
  operation_id: `10000000-0000-4000-8000-${String(n + 100).padStart(12, '0')}`,
  stream_id: streamId,
  actor: { ...fixture.events[0].actor, principal_id: principalId },
  subject: { type: 'work', id: `10000000-0000-4000-8000-${String(n + 200).padStart(12, '0')}` },
  ...changes
});
const metadata = {
  id: streamId,
  fulcra_userid: principalId,
  annotation_type: 'moment',
  fulcra_source_id: sourceId,
  deleted_at: null
};
const row = (n, value = event(n)) => ({
  id: `30000000-0000-4000-8000-${String(n).padStart(12, '0')}`,
  source_id: sourceId,
  note: serializeWorkEvent(value),
  metadata
});
const dirs = [];
function fixturePaths() {
  const dir = mkdtempSync(join(tmpdir(), 'gatekeeper-work-cli-'));
  chmodSync(dir, 0o700);
  dirs.push(dir);
  const configPath = join(dir, 'config.json');
  const eventPath = join(dir, 'event.json');
  const dbPath = join(dir, 'work.sqlite');
  const logPath = join(dir, 'fetch.log');
  writeFileSync(configPath, JSON.stringify(config), { mode: 0o600 });
  writeFileSync(eventPath, JSON.stringify(event(1)), { mode: 0o600 });
  return { dir, configPath, eventPath, dbPath, logPath };
}
const mockSource = `
  import { appendFileSync } from 'node:fs';
  import { pathToFileURL } from 'node:url';
  const values = JSON.parse(process.env.MOCK_VALUES || '{}');
  globalThis.fetch = async (url, init = {}) => {
    const address = String(url);
    const key = address.includes('/info') ? 'info' : address.includes('/catalog') ? 'catalog' : address.includes('/annotation?') ? 'annotation' : init.method === 'POST' ? 'post' : 'records';
    appendFileSync(process.env.MOCK_LOG, JSON.stringify({ key, method: init.method, url: address, body: init.body ?? null }) + '\\n');
    const value = Object.hasOwn(values, key) ? values[key] : key === 'info' ? { userid: '${principalId}' } : key === 'catalog' ? [{ id: '${config.channel}', api_version: 'v1alpha1', recordable: true, queryable: true, record_spec: { type: 'event' }, fulcra_userid: '${principalId}' }] : key === 'annotation' ? [${JSON.stringify(metadata)}] : key === 'post' ? { upload_id: 'pending-1' } : [];
    return new Response(JSON.stringify(value), { status: key === 'post' ? 201 : 200, headers: { 'content-type': 'application/json' } });
  };
  process.argv = [process.execPath, process.env.WORK_SCRIPT, ...JSON.parse(process.env.WORK_ARGS)];
  await import(pathToFileURL(process.env.WORK_SCRIPT).href);
`;
/** @param {ReturnType<typeof fixturePaths>} paths @param {string[]} args @param {string} [input] @param {Record<string,any>} [values] */
function run(paths, args, input = '', values = {}) {
  const child = spawnSync(process.execPath, ['--input-type=module', '-e', mockSource], {
    input,
    encoding: 'utf8',
    timeout: 30000,
    env: {
      ...process.env,
      WORK_SCRIPT: script,
      WORK_ARGS: JSON.stringify(args),
      MOCK_VALUES: JSON.stringify(values),
      MOCK_LOG: paths.logPath
    }
  });
  let output;
  try {
    output = JSON.parse(child.stdout.trim());
  } catch {
    output = null;
  }
  return { ...child, output };
}
/** @param {ReturnType<typeof fixturePaths>} paths */
function calls(paths) {
  try {
    return readFileSync(paths.logPath, 'utf8')
      .trim()
      .split('\n')
      .filter(Boolean)
      .map((line) => JSON.parse(line));
  } catch {
    return [];
  }
}
afterEach(() => {
  for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

describe('opt-in synthetic work transport CLI in fresh processes', () => {
  it('rejects unknown flags, relative files, oversized files and overlong stdin with safe codes', () => {
    const p = fixturePaths();
    expect(
      run(p, ['inspect', '--config', p.configPath, '--db', p.dbPath, '--unknown', 'x']).output
    ).toEqual({ status: 'blocked', code: 'INVALID_ARGUMENTS' });
    expect(run(p, ['inspect', '--config', 'relative.json', '--db', p.dbPath]).output).toEqual({
      status: 'blocked',
      code: 'UNSAFE_FILE'
    });
    expect(
      run(
        p,
        [
          'read',
          '--config',
          p.configPath,
          '--db',
          p.dbPath,
          '--start',
          '2026-09-26T00:00:00Z',
          '--end',
          '2026-09-27T00:00:00Z'
        ],
        'x'.repeat(16 * 1024 + 1)
      ).output
    ).toEqual({ status: 'blocked', code: 'TOKEN_LIMIT' });
    writeFileSync(p.eventPath, 'x'.repeat(64 * 1024 + 1), { mode: 0o600 });
    expect(
      run(
        p,
        ['publish', '--config', p.configPath, '--db', p.dbPath, '--event', p.eventPath],
        'fixture'
      ).output
    ).toEqual({ status: 'blocked', code: 'FILE_LIMIT' });
    writeFileSync(p.configPath, 'x'.repeat(64 * 1024 + 1), { mode: 0o600 });
    expect(run(p, ['inspect', '--config', p.configPath, '--db', p.dbPath]).output).toEqual({
      status: 'blocked',
      code: 'FILE_LIMIT'
    });
    expect(calls(p)).toEqual([]);
  });

  it('blocks actor mismatch before POST and never stores or prints the bearer', () => {
    const p = fixturePaths();
    writeFileSync(
      p.eventPath,
      JSON.stringify(event(1, { actor: { ...event(1).actor, logical_agent_id: 'forged' } })),
      { mode: 0o600 }
    );
    const result = run(
      p,
      ['publish', '--config', p.configPath, '--db', p.dbPath, '--event', p.eventPath],
      'fixture'
    );
    expect(result.output).toEqual({
      status: 'blocked',
      event_id: event(1).event_id,
      code: 'INVALID_EVENT_SCOPE'
    });
    expect(calls(p).some((call) => call.method === 'POST')).toBe(false);
    expect(result.stdout + result.stderr).not.toContain('fixture');
    expect(readFileSync(p.dbPath).includes(Buffer.from('fixture'))).toBe(false);
  });

  it('publishes once and a later process never resends the same intent', () => {
    const p = fixturePaths();
    const args = ['publish', '--config', p.configPath, '--db', p.dbPath, '--event', p.eventPath];
    expect(run(p, args, 'fixture').output.status).toBe('pending_readback');
    expect(run(p, args, 'fixture').output.status).toBe('unknown');
    expect(calls(p).filter((call) => call.method === 'POST')).toHaveLength(1);
    expect(readFileSync(p.dbPath).includes(Buffer.from('fixture'))).toBe(false);
  });

  it('does not resend after a separate process crashes with POST_STARTED persisted', () => {
    const p = fixturePaths();
    const setup = `import { pathToFileURL } from 'node:url'; const { openWorkTransportStore } = await import(pathToFileURL(${JSON.stringify(resolve('src/lib/server/gatekeeper/work-transport-store.js'))}).href); const { serializeWorkEvent, workContentDigest } = await import(pathToFileURL(${JSON.stringify(resolve('src/lib/gatekeeper/work-contract.js'))}).href); const config = ${JSON.stringify(config)}; const event = ${JSON.stringify(event(1))}; const store = openWorkTransportStore({ dbPath: ${JSON.stringify(p.dbPath)}, config }); store.reserveIntent({ event, note: serializeWorkEvent(event), digest: workContentDigest(event) }); store.startPost(event.event_id); store.close();`;
    const crashed = spawnSync(process.execPath, ['--input-type=module', '-e', setup], {
      encoding: 'utf8'
    });
    expect(crashed.status).toBe(0);
    const result = run(
      p,
      ['publish', '--config', p.configPath, '--db', p.dbPath, '--event', p.eventPath],
      'fixture'
    );
    expect(result.output.status).toBe('unknown');
    expect(calls(p).filter((call) => call.method === 'POST')).toHaveLength(0);
  });

  it('reconciles a VERIFIED intent again when a later scoped read finds changed same-ID bytes', () => {
    const p = fixturePaths();
    const publishArgs = [
      'publish',
      '--config',
      p.configPath,
      '--db',
      p.dbPath,
      '--event',
      p.eventPath
    ];
    const readArgs = [
      'read',
      '--config',
      p.configPath,
      '--db',
      p.dbPath,
      '--start',
      '2026-09-26T00:00:00Z',
      '--end',
      '2026-09-27T00:00:00Z'
    ];
    expect(run(p, publishArgs, 'fixture').output.status).toBe('pending_readback');
    expect(run(p, readArgs, 'fixture', { records: [row(1)] }).output.reconciled).toEqual([
      { status: 'verified', event_id: event(1).event_id }
    ]);
    expect(
      run(p, ['inspect', '--config', p.configPath, '--db', p.dbPath]).output.intents[0].state
    ).toBe('VERIFIED');
    const changed = event(1, {
      payload: { item: { ...event(1).payload.item, title: 'Changed after verification' } }
    });
    const changedRow = { ...row(1, changed), id: '30000000-0000-4000-8000-000000000099' };
    expect(run(p, readArgs, 'fixture', { records: [changedRow] }).output.reconciled).toEqual([
      { status: 'conflict', event_id: event(1).event_id }
    ]);
    expect(
      run(p, ['inspect', '--config', p.configPath, '--db', p.dbPath]).output.intents[0].state
    ).toBe('CONFLICT');
    const replay = run(p, ['replay', '--config', p.configPath, '--db', p.dbPath]);
    expect(replay.output).toMatchObject({
      status: 'trust_withheld',
      coverage: 'partial',
      candidate_count: 2,
      authorized_work_count: 0
    });
    expect(replay.output.conflict_count).toBeGreaterThan(0);
  });

  it('retains disjoint work and all conflicting variants after reopen; trust-withheld replay never authorizes them', () => {
    const p = fixturePaths();
    const base = event(1);
    const changed = event(1, {
      actor: { ...base.actor, logical_agent_id: 'forged-agent' },
      payload: { item: { ...base.payload.item, title: 'Changed' } }
    });
    const readArgs = [
      'read',
      '--config',
      p.configPath,
      '--db',
      p.dbPath,
      '--start',
      '2026-09-26T00:00:00Z',
      '--end',
      '2026-09-27T00:00:00Z'
    ];
    expect(
      run(p, readArgs, 'fixture', {
        records: [
          row(1, base),
          { ...row(1, changed), id: '30000000-0000-4000-8000-000000000099' },
          row(2)
        ]
      }).output
    ).toMatchObject({ status: 'stored', added_records: 3, coverage: 'partial' });
    expect(run(p, readArgs, 'fixture', { records: [] }).output).toMatchObject({
      status: 'stored',
      added_records: 0,
      coverage: 'partial'
    });
    const inspect = run(p, ['inspect', '--config', p.configPath, '--db', p.dbPath]);
    expect(inspect.output).toMatchObject({ status: 'ready', record_count: 3 });
    const replay = run(p, ['replay', '--config', p.configPath, '--db', p.dbPath]);
    expect(replay.output).toMatchObject({
      status: 'trust_withheld',
      coverage: 'partial',
      candidate_count: 3,
      authorized_work_count: 0
    });
    expect(replay.output.conflict_count).toBeGreaterThan(0);
    expect(replay.output).not.toHaveProperty('clear', true);
    const db = new DatabaseSync(p.dbPath);
    expect(db.prepare('SELECT COUNT(*) AS n FROM record_variants').get().n).toBe(3);
    db.close();
  });
});
