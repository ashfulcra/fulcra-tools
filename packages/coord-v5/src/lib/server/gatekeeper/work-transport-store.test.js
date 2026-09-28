import { afterEach, describe, expect, it } from 'vitest';
import { chmodSync, mkdtempSync, rmSync, symlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import fixture from '../../../../tests/fixtures/work-backlog-synthetic.json';
import { serializeWorkEvent, workContentDigest } from '../../gatekeeper/work-contract.js';
import { replayWorkEvents } from '../../gatekeeper/work-projection.js';
import { readWorkWindow } from './work-transport-read.js';
import { openWorkTransportStore } from './work-transport-store.js';

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
const start = '2026-09-26T00:00:00Z';
const end = '2026-09-27T00:00:00Z';
const at = '2026-09-27T01:00:00.000Z';
/** @param {number} n */
const id = (n) => `20000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
/** @param {number} n @param {Record<string,any>} [changes] */
const event = (n, changes = {}) => ({
  ...fixture.events[0],
  event_id: id(n),
  operation_id: id(n + 10000),
  stream_id: streamId,
  actor: { ...fixture.events[0].actor, principal_id: principalId },
  subject: { type: 'work', id: id(n + 20000) },
  ...changes
});
const metadata = {
  id: streamId,
  fulcra_userid: principalId,
  annotation_type: 'moment',
  fulcra_source_id: sourceId,
  deleted_at: null
};
/** @param {number} n @param {any} [value] @param {Record<string,any>} [changes] */
const row = (n, value = event(n), changes = {}) => ({
  id: id(n + 30000),
  source_id: sourceId,
  note: serializeWorkEvent(value),
  metadata,
  ...changes
});
/** @type {string[]} */
const dirs = [];
/** @type {Array<ReturnType<typeof openWorkTransportStore>>} */
const handles = [];
function path() {
  const dir = mkdtempSync(join(tmpdir(), 'gatekeeper-work-'));
  chmodSync(dir, 0o700);
  dirs.push(dir);
  return join(dir, 'work.sqlite');
}
/** @param {string} dbPath @param {any} [selected] */
function open(dbPath, selected = config) {
  const store = openWorkTransportStore({ dbPath, config: selected });
  handles.push(store);
  return store;
}
/** @param {any[]} rows @param {{start:string,end:string}} [window] @param {string} [stamp] @param {any} [responseOverride] */
async function read(rows, window = { start, end }, stamp = at, responseOverride) {
  /** @type {typeof globalThis.fetch} */
  const fetch = async (url) => {
    const address = String(url);
    const response = address.includes('/info')
      ? { userid: principalId }
      : address.includes('/catalog')
        ? [
            {
              id: config.channel,
              api_version: 'v1alpha1',
              recordable: true,
              queryable: true,
              record_spec: { type: 'event' },
              fulcra_userid: principalId
            }
          ]
        : address.includes('/annotation?')
          ? [metadata]
          : (responseOverride ?? rows);
    return new Response(JSON.stringify(response), {
      headers: { 'content-type': 'application/json' }
    });
  };
  return readWorkWindow({
    fetch,
    token: 'test-bearer',
    config,
    ...window,
    now: () => Date.parse(stamp)
  });
}
afterEach(() => {
  for (const handle of handles.splice(0)) handle.close();
  for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

describe('atomic synthetic observed-work and intent journal', () => {
  it('blocks fabricated read-back until the exact variant was appended from a trusted read, without pinning receipt time', async () => {
    const dbPath = path();
    const store = open(dbPath);
    const candidate = event(50);
    store.reserveIntent({
      event: candidate,
      note: serializeWorkEvent(candidate),
      digest: workContentDigest(candidate)
    });
    store.startPost(candidate.event_id);
    const first = await read([row(50, candidate)]);
    const fabricated = structuredClone(first.records[0]);
    expect(store.recordReadback(candidate.event_id, fabricated)).toEqual({ status: 'blocked' });
    expect(store.inspect().intents[0].state).toBe('UNKNOWN_IN_FLIGHT');
    expect(store.appendWindow(first)).toEqual({ status: 'stored', added_records: 1 });
    const later = await read([row(50, candidate)], { start, end }, '2026-09-28T01:00:00Z');
    expect(store.recordReadback(candidate.event_id, later.records[0])).toEqual({
      status: 'verified',
      record_id: fabricated.record_id
    });
    const db = new DatabaseSync(dbPath);
    db.exec("UPDATE record_variants SET digest='tampered'");
    db.close();
    expect(store.recordReadback(candidate.event_id, later.records[0])).toEqual({
      status: 'blocked'
    });
    expect(store.inspect().intents[0].state).toBe('VERIFIED');
  });

  it('keeps failed-only history unavailable and retains prior success through a later failure', async () => {
    const store = open(path());
    const failed = await read([], { start, end }, at, { malformed: true });
    expect(store.appendWindow(failed)).toEqual({ status: 'stored', added_records: 0 });
    const before = store.accumulated();
    expect(before.status).toBe('unavailable');
    expect(before.events).toBeNull();
    expect(before.observation.coverage).toBe('unavailable');
    expect(before.observation.last_successful_observation_at).toBeNull();
    expect(before.observation.errors).toEqual([
      { code: 'MALFORMED_RESPONSE', stream_id: streamId }
    ]);
    expect(
      store.appendWindow(await read([row(1)], { start, end }, '2026-09-28T01:00:00Z')).added_records
    ).toBe(1);
    expect(
      store.appendWindow(
        await read([], { start, end }, '2026-09-29T01:00:00Z', { malformed: true })
      ).status
    ).toBe('stored');
    const after = store.accumulated();
    expect(after.status).toBe('ready');
    expect(after.events).toHaveLength(1);
    expect(after.observation).toMatchObject({
      coverage: 'partial',
      as_of: '2026-09-29T01:00:00.000Z',
      last_successful_observation_at: '2026-09-28T01:00:00.000Z'
    });
    expect(after.observation.errors).toHaveLength(2);
  });

  it('treats a cold empty cache as unavailable, never a complete empty ledger', () => {
    const state = open(path()).accumulated();
    expect(state.status).toBe('unavailable');
    expect(state.events).toBeNull();
    expect(state.observation.coverage).toBe('unavailable');
    expect(state.observation.completeness_evidence_id).toBeNull();
  });

  it('retains two disjoint partial windows across reopen; later empty and unavailable windows never clear work', async () => {
    const dbPath = path();
    const first = open(dbPath);
    expect(first.appendWindow(await read([row(1)]))).toEqual({
      status: 'stored',
      added_records: 1
    });
    expect(
      first.appendWindow(
        await read(
          [row(2)],
          { start: '2026-09-27T00:00:00Z', end: '2026-09-28T00:00:00Z' },
          '2026-09-28T01:00:00Z'
        )
      )
    ).toEqual({ status: 'stored', added_records: 1 });
    first.close();
    handles.splice(handles.indexOf(first), 1);
    const second = open(dbPath);
    expect(second.appendWindow(await read([])).added_records).toBe(0);
    expect(
      second.appendWindow(await read([], { start, end }, at, { malformed: true })).status
    ).toBe('stored');
    const accumulated = second.accumulated();
    expect(accumulated.status).toBe('ready');
    expect((accumulated.events ?? []).map((entry) => entry.event_id)).toEqual([id(1), id(2)]);
    expect(accumulated.event_evidence).toHaveLength(2);
    expect(accumulated.observation.coverage).toBe('partial');
    expect(accumulated.observation.completeness_evidence_id).toBeNull();
    expect(accumulated.observation.last_successful_observation_at).toBe('2026-09-28T01:00:00.000Z');
  });

  it('rejects copied results, mismatched scope and impossible as-of atomically', async () => {
    const store = open(path());
    const result = await read([row(1)]);
    expect(store.appendWindow(JSON.parse(JSON.stringify(result)))).toEqual({
      status: 'blocked',
      added_records: 0,
      code: 'UNTRUSTED_READ_RESULT'
    });
    const other = open(path(), { ...config, workspaceId: id(900) });
    expect(other.appendWindow(result).status).toBe('blocked');
    const early = await read([row(2)], { start, end }, '2026-09-25T00:00:00Z');
    expect(store.appendWindow(early).status).toBe('blocked');
    expect(store.accumulated().events).toBeNull();
  });

  it('is idempotent for identical bytes but retains changed event, operation and source-record variants after reopen', async () => {
    const dbPath = path();
    const store = open(dbPath);
    const baseline = event(1);
    const changed = event(1, { payload: { item: { ...baseline.payload.item, title: 'Changed' } } });
    const sameOperation = event(3, { operation_id: baseline.operation_id });
    const unrelated = event(4);
    expect(store.appendWindow(await read([row(1, baseline)])).added_records).toBe(1);
    expect(store.appendWindow(await read([row(1, baseline)])).added_records).toBe(0);
    expect(
      store.appendWindow(await read([row(1, changed), row(3, sameOperation), row(4, unrelated)]))
        .added_records
    ).toBe(3);
    store.close();
    handles.splice(handles.indexOf(store), 1);
    const reopened = open(dbPath);
    const all = reopened.accumulated();
    expect(all.status).toBe('ready');
    expect(all.records).toHaveLength(4);
    expect((all.events ?? []).filter((entry) => entry.event_id === id(1))).toHaveLength(2);
    expect(all.event_evidence).toHaveLength(4);
    expect(all.conflicts.map((item) => item.code)).toEqual(
      expect.arrayContaining(['EVENT_ID_CONFLICT', 'OPERATION_ID_CONFLICT', 'RECORD_ID_CONFLICT'])
    );
    const trust = {
      workspace_id: config.workspaceId,
      allowed_stream_ids: [streamId],
      event_evidence: all.event_evidence,
      grants: [{ ...baseline.actor, capabilities: ['work.write'] }]
    };
    const projection = replayWorkEvents({
      events: all.events,
      trust,
      observation: all.observation,
      asOf: all.observation.as_of
    });
    expect(projection.conflicts.length).toBeGreaterThan(0);
    expect((all.events ?? []).some((entry) => entry.event_id === unrelated.event_id)).toBe(true);
  });

  it('stores only redacted gap diagnostics for unauthorized-source rows', async () => {
    const store = open(path());
    const foreign = row(1, event(1), { source_id: 'SENSITIVE_FOREIGN_SOURCE' });
    const result = await read([foreign]);
    expect(store.appendWindow(result)).toEqual({ status: 'stored', added_records: 0 });
    const accumulated = store.accumulated();
    expect(accumulated.records).toEqual([]);
    expect(accumulated.observation.gaps).toEqual([
      { code: 'RECORD_SOURCE_MISMATCH', stream_id: streamId }
    ]);
    expect(JSON.stringify(accumulated)).not.toContain('SENSITIVE_FOREIGN_SOURCE');
  });

  it('retains 1,001 variants but blocks replay without truncating its evidence cache', async () => {
    const store = open(path());
    const first = Array.from({ length: 1000 }, (_, index) => row(index + 1));
    expect(store.appendWindow(await read(first)).added_records).toBe(1000);
    expect(store.appendWindow(await read([row(1001)])).added_records).toBe(1);
    const state = store.accumulated();
    expect(state.status).toBe('blocked_limit');
    expect(state.event_count).toBe(1001);
    expect(state.events).toBeNull();
    expect(state.event_evidence).toBeNull();
    expect(state.observation.gaps.some((entry) => entry.code === 'EVENT_LIMIT')).toBe(true);
  });

  it('journals POST_STARTED before any network action and never makes it resendable on reopen', () => {
    const dbPath = path();
    const store = open(dbPath);
    const candidate = event(1);
    const note = serializeWorkEvent(candidate);
    const digest = workContentDigest(candidate);
    expect(store.reserveIntent({ event: candidate, note, digest })).toEqual({
      status: 'new',
      state: 'PREPARED'
    });
    expect(store.reserveIntent({ event: candidate, note, digest })).toEqual({
      status: 'same',
      state: 'PREPARED'
    });
    expect(store.startPost(candidate.event_id)).toEqual({ status: 'send_once' });
    store.close();
    handles.splice(handles.indexOf(store), 1);
    const reopened = open(dbPath);
    expect(reopened.inspect().intents[0].state).toBe('UNKNOWN_IN_FLIGHT');
    expect(reopened.startPost(candidate.event_id)).toEqual({ status: 'already_started' });
    const changed = event(1, {
      payload: { item: { ...candidate.payload.item, title: 'Changed content' } }
    });
    expect(
      reopened.reserveIntent({
        event: changed,
        note: serializeWorkEvent(changed),
        digest: workContentDigest(changed)
      }).status
    ).toBe('conflict');
    expect(JSON.stringify(reopened.inspect())).not.toContain('test-bearer');
  });

  it('rejects noncanonical intent bytes before creating a resendable reservation', () => {
    const store = open(path());
    const candidate = event(1);
    expect(() =>
      store.reserveIntent({
        event: candidate,
        note: JSON.stringify(candidate),
        digest: workContentDigest(candidate)
      })
    ).toThrow('INVALID_INTENT');
    expect(store.inspect().intents).toEqual([]);
  });

  it('requires a full exact source-bound read-back for verification; changed content remains a conflict', async () => {
    const store = open(path());
    const candidate = event(1);
    const note = serializeWorkEvent(candidate);
    const digest = workContentDigest(candidate);
    store.reserveIntent({ event: candidate, note, digest });
    expect(store.recordReadback(candidate.event_id, { status: 'verified' })).toEqual({
      status: 'blocked'
    });
    expect(store.startPost(candidate.event_id)).toEqual({ status: 'send_once' });
    expect(
      store.recordPostOutcome(candidate.event_id, {
        status: 'upload_accepted',
        upload_id: 'pending-1'
      })
    ).toEqual({ status: 'upload_accepted' });
    expect(store.inspect().intents[0].state).toBe('UPLOAD_ACCEPTED');
    const exact = (await read([row(1, candidate)])).records[0];
    expect(store.appendWindow(await read([row(1, candidate)])).status).toBe('stored');
    expect(store.recordReadback(candidate.event_id, exact)).toEqual({
      status: 'verified',
      record_id: exact.record_id
    });
    expect(store.inspect().intents[0].state).toBe('VERIFIED');

    const second = event(2);
    store.reserveIntent({
      event: second,
      note: serializeWorkEvent(second),
      digest: workContentDigest(second)
    });
    store.startPost(second.event_id);
    expect(
      store.recordPostOutcome(second.event_id, { status: 'unknown', code: 'TIMEOUT' })
    ).toEqual({ status: 'unknown', code: 'TIMEOUT' });
    const changed = event(2, {
      payload: { item: { ...second.payload.item, title: 'Changed content' } }
    });
    const conflicting = (await read([row(2, changed)])).records[0];
    expect(store.appendWindow(await read([row(2, changed)])).status).toBe('stored');
    expect(store.recordReadback(second.event_id, conflicting)).toEqual({ status: 'conflict' });
    expect(store.inspect().intents.find((entry) => entry.event_id === second.event_id)?.state).toBe(
      'CONFLICT'
    );
    expect(store.startPost(second.event_id)).toEqual({ status: 'already_started' });
  });

  it('does not turn a corrupt stored record into an empty-success accumulated view', async () => {
    const dbPath = path();
    const store = open(dbPath);
    store.appendWindow(await read([row(1)]));
    store.close();
    handles.splice(handles.indexOf(store), 1);
    const db = new DatabaseSync(dbPath);
    db.exec("UPDATE record_variants SET record_json='{}'");
    db.close();
    const reopened = open(dbPath);
    const state = reopened.accumulated();
    expect(state.status).toBe('unavailable');
    expect(state.events).toBeNull();
    expect(state.observation.errors).toEqual([{ code: 'STORE_CORRUPT', stream_id: streamId }]);
  });

  it('rejects tampered record columns and a fabricated complete observation on reopen', async () => {
    const dbPath = path();
    const store = open(dbPath);
    store.appendWindow(await read([row(1)]));
    store.close();
    handles.splice(handles.indexOf(store), 1);
    const db = new DatabaseSync(dbPath);
    db.exec("UPDATE record_variants SET digest='forged'");
    db.close();
    const reopened = open(dbPath);
    expect(reopened.accumulated().status).toBe('unavailable');
    reopened.close();
    handles.splice(handles.indexOf(reopened), 1);
    const editing = new DatabaseSync(dbPath);
    editing.exec(`UPDATE record_variants SET digest='${workContentDigest(event(1))}'`);
    const forged = {
      coverage: 'complete',
      as_of: at,
      last_successful_observation_at: at,
      sources: [{ stream_id: streamId, status: 'complete', pending_pages: 0 }],
      gaps: [],
      errors: [],
      completeness_evidence_id: 'forged'
    };
    editing.prepare('UPDATE observations SET observation_json=?').run(JSON.stringify(forged));
    editing.close();
    const reopenedAgain = open(dbPath);
    expect(reopenedAgain.accumulated().status).toBe('unavailable');
  });

  it('rejects unsafe path permissions and symlinks', () => {
    const dbPath = path();
    expect(() => openWorkTransportStore({ dbPath: 'relative.sqlite', config })).toThrow(
      'UNSAFE_DB_PATH'
    );
    const dir = join(dbPath, '..');
    chmodSync(dir, 0o755);
    expect(() => openWorkTransportStore({ dbPath, config })).toThrow('UNSAFE_DB_PATH');
    chmodSync(dir, 0o700);
    const link = join(dir, 'link.sqlite');
    symlinkSync(dbPath, link);
    expect(() => openWorkTransportStore({ dbPath: link, config })).toThrow('UNSAFE_DB_PATH');
    const db = new DatabaseSync(dbPath);
    const schema = db
      .prepare("SELECT sql FROM sqlite_master WHERE type='table'")
      .all()
      .map((entry) => entry.sql)
      .join(' ');
    expect(schema.toLowerCase()).not.toContain('token');
    db.close();
  });
});
