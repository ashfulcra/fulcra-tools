import { afterEach, describe, expect, it } from 'vitest';
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import fixture from '../../../../tests/fixtures/work-backlog-synthetic.json';
import { serializeWorkEvent } from '../../gatekeeper/work-contract.js';
import { openListenerStore } from './listener-store.js';
import { acknowledgeWake, configureRoutes, prepareWake, settleWake } from './listener-runtime.js';
import { observation as validateObservation } from './listener-validation.js';
import { buildWorkListenerObservation } from './work-listener.js';
import { readWorkWindow } from './work-transport-read.js';
import { openWorkTransportStore } from './work-transport-store.js';

const principal = '00000000-0000-4000-8000-000000000900';
const stream = '00000000-0000-4000-8000-000000000901';
const source = `com.fulcradynamics.annotation.${stream}`;
const workId = fixture.events[0].subject.id;
const questionId = fixture.events[2].subject.id;
const actor = { ...fixture.events[0].actor, principal_id: principal };
const config = {
  baseUrl: 'https://api.fulcradynamics.com/',
  principalId: principal,
  channel: `MomentAnnotation/${stream}`,
  workspaceId: fixture.events[0].workspace_id,
  workstreamId: fixture.events[0].workstream_id,
  actorBinding: actor
};
const policy = {
  principal_id: principal,
  workspace_id: config.workspaceId,
  stream_id: stream,
  grants: [{ ...actor, capabilities: ['work.write', 'question.ask', 'assignment.manage'] }],
  work_jobs: [{ work_id: workId, job_id: 'job-a' }]
};
const metadata = {
  id: stream,
  fulcra_userid: principal,
  annotation_type: 'moment',
  fulcra_source_id: source,
  deleted_at: null
};
/** @returns {any} */
const opened = () => ({ ...structuredClone(fixture.events[0]), stream_id: stream, actor });
/** @returns {any} */
const question = () => ({ ...structuredClone(fixture.events[2]), stream_id: stream, actor });
/** @param {number} n */
const uuid = (n) => `20000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
/** @param {number} n @param {any} event */
const row = (n, event) => ({
  id: uuid(n),
  source_id: source,
  note: serializeWorkEvent(event),
  metadata
});
/** @type {string[]} */
const dirs = [];
/** @type {Array<ReturnType<typeof openWorkTransportStore>>} */
const handles = [];
function setup() {
  const dir = mkdtempSync(join(tmpdir(), 'work-listener-'));
  chmodSync(dir, 0o700);
  dirs.push(dir);
  const store = openWorkTransportStore({ dbPath: join(dir, 'work.sqlite'), config });
  handles.push(store);
  return { dir, store };
}
/** @param {ReturnType<typeof openWorkTransportStore>} store @param {any[]} events @param {string} [stamp] @param {boolean} [malformed] */
async function append(store, events, stamp = '2026-09-27T01:00:00Z', malformed = false) {
  /** @type {typeof globalThis.fetch} */
  const fetch = async (url) => {
    const address = String(url);
    const response = address.includes('/info')
      ? { userid: principal }
      : address.includes('/catalog')
        ? [
            {
              id: config.channel,
              api_version: 'v1alpha1',
              recordable: true,
              queryable: true,
              record_spec: { type: 'event' },
              fulcra_userid: principal
            }
          ]
        : address.includes('/annotation?')
          ? [metadata]
          : malformed
            ? { malformed: true }
            : events.map((event, index) => row(100 + index + events.length * 10, event));
    return new Response(JSON.stringify(response), {
      headers: { 'content-type': 'application/json' }
    });
  };
  return store.appendWindow(
    await readWorkWindow({
      fetch,
      token: 'fixture-only',
      config,
      start: '2026-09-26T00:00:00Z',
      end: '2026-09-27T00:00:00Z',
      now: () => Date.parse(stamp)
    })
  );
}
/** @param {any} store @param {any} [selected] @returns {any} */
const bridge = (store, selected = policy) =>
  buildWorkListenerObservation({
    store,
    policy: selected,
    now: () => Date.parse('2026-09-30T00:00:00Z')
  });
afterEach(() => {
  for (const handle of handles.splice(0)) handle.close();
  for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

describe('retained work to native listener observation', () => {
  it('routes known authorized work through durable prepare, settle, ack and reopen without a second wake', async () => {
    const { dir, store } = setup();
    const open = opened();
    expect((await append(store, [open])).status).toBe('stored');
    const result = bridge(store);
    expect(result.observation).toBeDefined();
    validateObservation(result.observation, Date.parse('2026-09-30T00:00:00Z'), 0);
    expect(result.observation).toMatchObject({
      observedAt: '2026-09-27T01:00:00.000Z',
      eventObservation: {
        coverage: 'partial',
        items: [{ itemId: `work:${workId}`, jobId: 'job-a', revision: expect.any(String) }]
      },
      obligationObservation: {
        coverage: 'partial',
        items: [{ itemId: `work:${workId}`, jobId: 'job-a', revision: expect.any(String) }]
      }
    });
    expect(JSON.stringify(result)).not.toContain(open.payload.item.title);
    const db = join(dir, 'listener.sqlite');
    const listener = openListenerStore(db);
    const scope = {
      principalId: principal,
      workspaceId: config.workspaceId,
      environmentId: 'synthetic',
      harness: 'codex'
    };
    const lease = /** @type {any} */ (
      listener.acquire(scope, 'holder', Date.parse('2026-09-27T01:00:01Z'), 300000)
    );
    configureRoutes(
      listener,
      lease,
      [
        {
          jobId: 'job-a',
          logicalIdentity: 'agent-a',
          lifecycle: 'active',
          threadId: 'authorized-thread',
          hostId: 'local'
        }
      ],
      Date.parse('2026-09-27T01:00:02Z')
    );
    const wake = prepareWake(
      listener,
      lease,
      result.observation,
      Date.parse('2026-09-27T01:00:03Z')
    );
    expect(wake.actions).toHaveLength(1);
    expect(wake.actions[0].arguments.threadId).toBe('authorized-thread');
    expect(wake.actions[0].arguments.prompt).not.toContain(open.payload.item.title);
    const action = wake.actions[0];
    const target = { threadId: 'authorized-thread', hostId: 'local' };
    settleWake(
      listener,
      lease,
      { wakeId: action.wakeId, attemptId: action.attemptId, target, status: 'accepted' },
      Date.parse('2026-09-27T01:00:04Z')
    );
    acknowledgeWake(
      listener,
      lease,
      { wakeId: action.wakeId, attemptId: action.attemptId, target },
      Date.parse('2026-09-27T01:00:05Z')
    );
    listener.close();
    const reopened = openListenerStore(db);
    const next = /** @type {any} */ (
      reopened.acquire(scope, 'holder', Date.parse('2026-09-27T01:00:06Z'), 300000)
    );
    expect(
      prepareWake(reopened, next, result.observation, Date.parse('2026-09-27T01:00:07Z')).actions
    ).toEqual([]);
    reopened.close();
  });

  it('notifies a later linked question once, while duplicates and reorder keep stable revisions', async () => {
    const { store } = setup();
    const open = opened();
    await append(store, [open]);
    const first = bridge(store).observation;
    await append(store, [question(), open], '2026-09-28T01:00:00Z');
    const second = bridge(store).observation;
    expect(second.eventObservation.items).toEqual(
      expect.arrayContaining([
        { itemId: `question:${questionId}`, jobId: 'job-a', revision: expect.any(String) }
      ])
    );
    expect(
      second.eventObservation.items.find(
        (/** @type {any} */ item) => item.itemId === `work:${workId}`
      ).revision
    ).toBe(first.eventObservation.items[0].revision);
    await append(store, [open, question()], '2026-09-29T01:00:00Z');
    expect(bridge(store).observation.eventObservation.items).toEqual(second.eventObservation.items);
  });

  it('denies mismatched policy and excludes ungranted or unmapped work', async () => {
    const { store } = setup();
    await append(store, [opened()]);
    expect(bridge(store, { ...policy, workspace_id: uuid(9) }).status).toBe('blocked');
    expect(bridge(store, { ...policy, grants: [] }).observation.eventObservation.items).toEqual([]);
    const unmapped = bridge(store, { ...policy, work_jobs: [] });
    expect(unmapped.observation.eventObservation.items).toEqual([]);
    expect(unmapped.diagnostics).toEqual(
      expect.arrayContaining([expect.objectContaining({ code: 'UNMAPPED_WORK' })])
    );
  });

  it('does not invent a timestamp or clear work from cold, failed, or partial empty reads', async () => {
    const { store } = setup();
    expect(bridge(store)).toMatchObject({ status: 'unavailable' });
    await append(store, [], '2026-09-27T01:00:00Z', true);
    expect(bridge(store)).toMatchObject({ status: 'unavailable' });
    await append(store, [opened()], '2026-09-28T01:00:00Z');
    const first = bridge(store).observation;
    await append(store, [], '2026-09-29T01:00:00Z');
    const later = bridge(store).observation;
    expect(later.eventObservation.items).toEqual(first.eventObservation.items);
    expect(later.obligationObservation.items).toEqual(first.obligationObservation.items);
    expect(later.eventObservation.coverage).toBe('partial');
  });

  it('emits mapped conflict attention for source-valid changed variants without raw text', async () => {
    const { store } = setup();
    const open = opened();
    const changed = {
      ...opened(),
      payload: { item: { ...open.payload.item, title: 'Secret changed variant' } }
    };
    await append(store, [open]);
    await append(store, [changed], '2026-09-28T01:00:00Z');
    const result = bridge(store);
    expect(result.observation.eventObservation.items).toEqual(
      expect.arrayContaining([
        { itemId: `conflict:${workId}`, jobId: 'job-a', revision: expect.any(String) }
      ])
    );
    expect(JSON.stringify(result)).not.toContain('Secret changed variant');
  });

  it('changes one known-work revision for an authorized assignment offer, not a duplicate offer', async () => {
    const { store } = setup();
    const open = opened();
    await append(store, [open]);
    const first = bridge(store).observation.eventObservation.items[0].revision;
    const offer = {
      ...opened(),
      event_id: uuid(11),
      operation_id: uuid(12),
      kind: 'assignment.offered',
      parents: [open.event_id],
      payload: {
        work_id: workId,
        expected_assignment_event_id: null,
        expected_version: 0,
        target: actor
      }
    };
    await append(store, [offer], '2026-09-28T01:00:00Z');
    const second = bridge(store).observation.eventObservation.items[0].revision;
    expect(second).not.toBe(first);
    await append(store, [offer], '2026-09-29T01:00:00Z');
    expect(bridge(store).observation.eventObservation.items[0].revision).toBe(second);
  });

  it('changes one known-work revision on an authorized edit and ignores a denied candidate', async () => {
    const { store } = setup();
    const open = opened();
    await append(store, [open]);
    const first = bridge(store).observation.eventObservation.items[0].revision;
    const denied = {
      ...opened(),
      event_id: uuid(31),
      operation_id: uuid(32),
      subject: { type: 'work', id: uuid(33) }
    };
    await append(store, [denied], '2026-09-28T01:00:00Z');
    expect(bridge(store).observation.eventObservation.items[0].revision).toBe(first);
    const edit = {
      ...opened(),
      event_id: uuid(41),
      operation_id: uuid(42),
      kind: 'work.revised',
      parents: [open.event_id],
      payload: {
        expected_event_id: open.event_id,
        item: { ...open.payload.item, title: 'Changed private title' },
        reason: null
      }
    };
    await append(store, [edit], '2026-09-29T01:00:00Z');
    const result = bridge(store);
    expect(result.observation.eventObservation.items[0].revision).not.toBe(first);
    expect(JSON.stringify(result)).not.toContain('Changed private title');
  });

  it('reports a terminal transition once without asserting stale partial obligations are cleared', async () => {
    const { store } = setup();
    const open = opened();
    await append(store, [open]);
    const first = bridge(store).observation;
    const cancel = {
      ...opened(),
      event_id: uuid(51),
      operation_id: uuid(52),
      kind: 'work.revised',
      parents: [open.event_id],
      payload: {
        expected_event_id: open.event_id,
        item: { ...open.payload.item, status: 'cancelled', next_action: null },
        reason: 'Decision made'
      }
    };
    await append(store, [cancel], '2026-09-28T01:00:00Z');
    const terminal = bridge(store).observation;
    expect(terminal.eventObservation.items[0].revision).not.toBe(
      first.eventObservation.items[0].revision
    );
    expect(terminal.obligationObservation.coverage).toBe('partial');
    expect(terminal.obligationObservation.items).toEqual([]);
    await append(store, [cancel], '2026-09-29T01:00:00Z');
    expect(bridge(store).observation.eventObservation.items).toEqual(
      terminal.eventObservation.items
    );
  });

  it('links a source-valid conflicted question opening to an explicit mapped work ID', async () => {
    const { store } = setup();
    await append(store, [opened(), question()]);
    const changed = {
      ...question(),
      payload: { ...question().payload, text: 'Private changed question' }
    };
    await append(store, [changed], '2026-09-28T01:00:00Z');
    const result = bridge(store);
    expect(result.observation.eventObservation.items).toEqual(
      expect.arrayContaining([
        { itemId: `conflict:${questionId}`, jobId: 'job-a', revision: expect.any(String) }
      ])
    );
    expect(JSON.stringify(result)).not.toContain('Private changed question');
  });

  it('prints a normalized observation from private local files without network or note text', async () => {
    const { dir, store } = setup();
    await append(store, [opened()]);
    const configPath = join(dir, 'config.json');
    const policyPath = join(dir, 'policy.json');
    writeFileSync(configPath, JSON.stringify(config), { mode: 0o600 });
    writeFileSync(policyPath, JSON.stringify(policy), { mode: 0o600 });
    const run = spawnSync(
      process.execPath,
      [
        'scripts/work-listener-observation.mjs',
        '--config',
        configPath,
        '--policy',
        policyPath,
        '--db',
        join(dir, 'work.sqlite')
      ],
      { encoding: 'utf8' }
    );
    expect(run.status).toBe(0);
    const result = JSON.parse(run.stdout);
    expect(result.observation.eventObservation.items).toHaveLength(1);
    expect(result.observation.observedAt).toBe('2026-09-27T01:00:00.000Z');
    expect(run.stdout).not.toContain(opened().payload.item.title);
    chmodSync(policyPath, 0o644);
    const unsafe = spawnSync(
      process.execPath,
      [
        'scripts/work-listener-observation.mjs',
        '--config',
        configPath,
        '--policy',
        policyPath,
        '--db',
        join(dir, 'work.sqlite')
      ],
      { encoding: 'utf8' }
    );
    expect(unsafe.status).toBe(1);
    expect(JSON.parse(unsafe.stdout)).toMatchObject({ error: { code: 'UNSAFE_FILE' } });
  });
});
