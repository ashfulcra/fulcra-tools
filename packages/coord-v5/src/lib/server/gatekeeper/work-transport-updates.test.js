import { afterEach, describe, expect, it } from 'vitest';
import { chmodSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import fixture from '../../../../tests/fixtures/work-backlog-synthetic.json';
import { serializeWorkEvent, workContentDigest } from '../../gatekeeper/work-contract.js';
import { openWorkTransportStore } from './work-transport-store.js';

const module = await import('./work-transport-updates.js').catch(() => ({}));
const config = {
  baseUrl: 'https://api.fulcradynamics.com/',
  principalId: '00000000-0000-4000-8000-000000000900',
  channel: 'MomentAnnotation/00000000-0000-4000-8000-000000000901',
  workspaceId: '00000000-0000-4000-8000-000000000100',
  workstreamId: '00000000-0000-4000-8000-000000000300',
  actorBinding: { principal_id: '00000000-0000-4000-8000-000000000900', logical_agent_id: 'a', instance_id: 'i', session_id: 's' }
};
const stream = config.channel.split('/')[1];
const metadata = { id: stream, fulcra_userid: config.principalId, annotation_type: 'moment', fulcra_source_id: `com.fulcradynamics.annotation.${stream}`, deleted_at: null };
const event = { ...fixture.events[0], stream_id: stream, actor: config.actorBinding };
const row = { id: '91fe3dd7-8f9f-5267-b5d7-eb8187c349f6', source_id: metadata.fulcra_source_id, metadata, note: serializeWorkEvent(event) };
const dirs = [];
const stores = [];
const start = '2026-09-26T00:00:00Z';
const end = '2026-09-27T00:00:00Z';
function setup() {
  const dir = mkdtempSync(join(tmpdir(), 'work-updates-'));
  chmodSync(dir, 0o700); dirs.push(dir);
  const dbPath = join(dir, 'transport.sqlite');
  const store = openWorkTransportStore({ dbPath, config }); stores.push(store);
  return { store, dbPath };
}
function network(dataTypes = {}, records = [], updatesStatus = 200) {
  const calls = [];
  const fetch = async (url, init) => {
    const u = new URL(url); calls.push(u);
    expect(init.redirect).toBe('error');
    const value = u.pathname.endsWith('/info') ? { userid: config.principalId }
      : u.pathname.endsWith('/updates') ? { data_types: dataTypes, file_changes: [] }
      : u.pathname.endsWith('/catalog') ? [{ id: config.channel, api_version: 'v1alpha1', recordable: true, queryable: true, record_spec: { type: 'event' }, fulcra_userid: config.principalId }]
      : u.pathname.endsWith('/annotation') ? [metadata] : records;
    return new Response(JSON.stringify(value), { status: u.pathname.endsWith('/updates') ? updatesStatus : 200 });
  };
  return { fetch, calls };
}
async function run(store, net, overrides = {}) {
  expect(module.readWorkUpdates).toBeTypeOf('function');
  return module.readWorkUpdates({ config, store, fetch: net.fetch, token: 'PRIVATE_TOKEN', start, end, updatesStart: start, mode: 'shadow', now: () => Date.parse(overrides.end ?? end), ...overrides });
}
afterEach(() => { for (const store of stores.splice(0)) store.close(); for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true }); });
describe('parallel updates reader: real retained transport and externally simulated HTTP', () => {
  it('a failed direct read survives reopen and requires clean direct recovery before quiet gating', async () => {
    const { store, dbPath } = setup(); await run(store, network());
    const failed = network({ [config.channel]: 1 });
    const fetch = async (url, init) => String(url).includes('/event/')
      ? new Response('{}', { status: 503 }) : failed.fetch(url, init);
    await run(store, { fetch }, { mode: 'gated', end: '2026-09-27T00:05:00Z' });
    store.close(); stores.splice(stores.indexOf(store), 1);
    const reopened = openWorkTransportStore({ dbPath, config }); stores.push(reopened);
    const recovery = await run(reopened, network(), { mode: 'gated', end: '2026-09-27T00:10:00Z' });
    expect(recovery.direct_read).toBe(true);
    const quiet = await run(reopened, network(), { mode: 'gated', end: '2026-09-27T00:15:00Z' });
    expect(quiet.direct_read).toBe(false);
  });
  it('a stale shadow cursor cannot discard a detected hint miss', async () => {
    const { store } = setup(); await run(store, network());
    let release, entered;
    const ready = new Promise(resolve => { entered = resolve; });
    const wait = new Promise(resolve => { release = resolve; });
    const net = network({}, [row]);
    const fetch = async (url, init) => {
      if (String(url).includes('/event/')) { entered(); await wait; }
      return net.fetch(url, init);
    };
    const shadow = run(store, { fetch }, { end: '2026-09-27T00:05:00Z' });
    await ready;
    await run(store, network(), { mode: 'gated', end: '2026-09-27T00:10:00Z' });
    release();
    const stale = await shadow;
    expect(stale.code).toBe('UPDATE_CURSOR_CONFLICT');
    expect(stale.hint_miss).toBe(true);
    const following = await run(store, network({}, [row]), { mode: 'gated', end: '2026-09-27T00:15:00Z' });
    expect(following.direct_read).toBe(true);
  });
  it('a clean concurrent read cannot clear a failure that occurred after it began', async () => {
    const { store } = setup(); await run(store, network());
    let release, entered;
    const ready = new Promise(resolve => { entered = resolve; });
    const wait = new Promise(resolve => { release = resolve; });
    const net = network();
    const fetch = async (url, init) => {
      if (String(url).includes('/event/')) { entered(); await wait; }
      return net.fetch(url, init);
    };
    const clean = run(store, { fetch }, { end: '2026-09-27T00:10:00Z' });
    await ready;
    const failed = network({ [config.channel]: 1 });
    await run(store, { fetch: (url, init) => String(url).includes('/event/')
      ? Promise.resolve(new Response('{}', { status: 503 })) : failed.fetch(url, init) },
    { mode: 'gated', end: '2026-09-27T00:05:00Z' });
    release(); await clean;
    const recovery = await run(store, network(), { mode: 'gated', end: '2026-09-27T00:15:00Z' });
    expect(recovery.direct_read).toBe(true);
  });
  it('catches up a long summary interval in bounded windows before advancing the cursor', async () => {
    const { store } = setup(); await run(store, network());
    const net = network();
    const result = await run(store, net, { mode: 'gated', start: '2026-10-04T00:00:00Z', end: '2026-10-05T00:00:00Z' });
    expect(result.update_status).toBe('available');
    expect(result.update_cursor).toBe('2026-10-05T00:00:00Z');
    const windows = net.calls.filter(u => u.pathname.endsWith('/updates')).map(u => [u.searchParams.get('start_time'), u.searchParams.get('end_time')]);
    expect(windows).toEqual([
      ['2026-09-26T23:58:00.000Z', '2026-10-03T23:58:00.000Z'],
      ['2026-10-03T23:58:00.000Z', '2026-10-05T00:00:00.000Z']
    ]);
  });
  it('a failed catch-up segment preserves the entire summary interval for retry', async () => {
    const { store } = setup(); await run(store, network());
    const net = network(); let segments = 0;
    const fetch = (url, init) => String(url).includes('/updates?') && ++segments === 2
      ? Promise.resolve(new Response('{}', { status: 503 })) : net.fetch(url, init);
    const result = await run(store, { fetch }, { mode: 'gated', start: '2026-10-04T00:00:00Z', end: '2026-10-05T00:00:00Z' });
    expect(result.update_status).toBe('unavailable'); expect(result.update_cursor).toBe(end);
    expect(result.direct_read).toBe(true);
    expect(segments).toBe(2);
  });
  it('does not turn an invalid calendar end into a valid quiet summary through normalization', async () => {
    const { store } = setup();
    await run(store, network(), { start: '2026-02-27T00:00:00Z', end: '2026-02-28T00:00:00Z', updatesStart: '2026-02-27T00:00:00Z', now: () => Date.parse('2026-03-02T00:00:00Z') });
    const r = await run(store, network(), { mode: 'gated', start: '2026-02-27T00:00:00Z', end: '2026-02-30T00:00:00Z', updatesStart: '2026-02-27T00:00:00Z' });
    expect(r.status).toBe('blocked');
    expect(r.update_cursor).toBe('2026-02-28T00:00:00Z');
  });
  it('caps ancient catch-up work and preserves the entire cursor without summary requests', async () => {
    const { store } = setup(); await run(store, network());
    const net = network();
    const result = await run(store, net, { mode: 'gated', start: '2026-12-01T00:00:00Z', end: '2026-12-02T00:00:00Z' });
    expect(result.update_error).toBe('SUMMARY_CATCHUP_LIMIT');
    expect(result.update_cursor).toBe(end);
    expect(net.calls.filter(u => u.pathname.endsWith('/updates'))).toHaveLength(0);
    expect(result.direct_read).toBe(true);
  });
  it('does not use another source hint to advance this store cursor', async () => {
    const { store } = setup(); await run(store, network());
    const r = await run(store, network(), { mode: 'gated', end: '2026-09-27T00:05:00Z', config: { ...config, workspaceId: '00000000-0000-4000-8000-000000000101' } });
    expect(r.status).toBe('blocked'); expect(r.update_cursor).toBe(end);
  });
  it('a detected hint miss disables later quiet gating across persisted state', async () => {
    const { store } = setup(); await run(store, network());
    await run(store, network({}, [row]), { end: '2026-09-27T00:05:00Z' });
    const r = await run(store, network({}, [row]), { mode: 'gated', end: '2026-09-27T00:10:00Z' });
    expect(r.direct_read).toBe(true); expect(r.would_skip).toBe(false);
  });
  it('a quiet hint cannot bypass an unresolved publication intent', async () => {
    const { store } = setup(); await run(store, network());
    expect(store.reserveIntent({ event, note: serializeWorkEvent(event), digest: workContentDigest(event) }).status).toBe('new');
    const r = await run(store, network({}, [row]), { mode: 'gated', end: '2026-09-27T00:05:00Z' });
    expect(r.direct_read).toBe(true); expect(r.reconciled).toHaveLength(1);
    // A locally prepared, never-sent intent cannot become a publication claim.
    expect(store.inspect().intents[0].state).toBe('PREPARED');
  });
  it('compare-and-swap refuses a stale cursor commit without altering the newer cursor', async () => {
    const { store } = setup(); await run(store, network());
    const r = store.commitUpdateCursor(null, { ...store.updateCursor(), cursor: '2026-09-27T00:05:00Z' });
    expect(r).toEqual({ status: 'blocked', code: 'UPDATE_CURSOR_CONFLICT' });
    expect(store.updateCursor().cursor).toBe(end);
  });
  it('shadow catches a quiet-hint miss while preserving the actual source record', async () => {
    const { store } = setup();
    await run(store, network());
    const r = await run(store, network({}, [row]), { end: '2026-09-27T00:05:00Z' });
    expect(r.status).toBe('stored');
    expect(r.hint_miss).toBe(true);
    expect(store.accumulated().events.map(e => e.event_id)).toEqual([event.event_id]);
    expect(r.update_cursor).toBe('2026-09-27T00:05:00Z');
  });
  it('gated quiet reads reduce HTTP requests after bootstrap without fabricating a fresh record observation', async () => {
    const { store } = setup(); await run(store, network({}, [row]));
    const before = store.inspect(); const net = network();
    const r = await run(store, net, { mode: 'gated', end: '2026-09-27T00:05:00Z', now: () => Date.parse('2026-09-27T00:05:00Z') });
    expect(r.status).toBe('unchanged_hint');
    expect(net.calls.map(u => u.pathname)).toEqual(['/user/v1alpha1/info', '/data/v1/updates']);
    expect(store.inspect().observation_count).toBe(before.observation_count);
    expect(store.accumulated().events).toHaveLength(1);
    expect(net.calls[1].searchParams.get('start_time')).toBe('2026-09-26T23:58:00.000Z');
  });
  it.each([{}, { MomentAnnotation: 1 }])('bootstrap and aggregate annotation hints cannot suppress a direct read: %j', async hints => {
    const { store } = setup();
    const r = await run(store, network(hints, [row]), { mode: 'gated' });
    expect(r.status).toBe('stored'); expect(r.direct_read).toBe(true);
  });
  it('a changed exact source is read even when its event timestamp predates the update window', async () => {
    const { store } = setup(); await run(store, network());
    const net = network({ [config.channel]: 1 }, [row]);
    const r = await run(store, net, { mode: 'gated', end: '2026-09-27T00:05:00Z' });
    expect(r.added_records).toBe(1);
    expect(net.calls.at(-1).searchParams.get('start_time')).toBe('2026-09-26T00:00:00.000Z');
  });
  it.each([['malformed', { [config.channel]: -1 }, 200], ['failure', {}, 503]])('falls back on %s updates without advancing its update cursor', async (_, hints, status) => {
    const { store } = setup(); await run(store, network());
    const r = await run(store, network(hints, [row], status), { mode: 'gated', end: '2026-09-27T00:05:00Z' });
    expect(r.status).toBe('stored'); expect(r.update_cursor).toBe(end);
    expect(r.update_status).toBe('unavailable'); expect(r.added_records).toBe(1);
  });
  it('retains cursor when a source record is malformed and forces periodic record audits', async () => {
    const { store } = setup(); await run(store, network());
    const r = await run(store, network({ [config.channel]: 1 }, [{ ...row, note: 'invalid' }]), { mode: 'gated', end: '2026-09-27T00:05:00Z' });
    expect(r.update_cursor).toBe(end); expect(r.gaps.length).toBeGreaterThan(0);
    const audit = await run(store, network({}, [row]), { mode: 'gated', end: '2026-09-27T01:05:00Z', now: () => Date.parse('2026-09-27T01:05:00Z') });
    expect(audit.direct_read).toBe(true); expect(audit.added_records).toBe(1);
  });
  it('persists the update cursor across reopen but never advances it on invalid windows', async () => {
    const { store, dbPath } = setup(); await run(store, network());
    store.close(); stores.splice(stores.indexOf(store), 1);
    const reopened = openWorkTransportStore({ dbPath, config }); stores.push(reopened);
    const net = network();
    const r = await run(reopened, net, { end: start });
    expect(r.status).toBe('blocked'); expect(r.update_cursor).toBe(end); expect(net.calls).toHaveLength(0);
  });
});
