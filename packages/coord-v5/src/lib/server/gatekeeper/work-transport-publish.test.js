import { afterEach, describe, expect, it, vi } from 'vitest';
import { chmodSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import fixture from '../../../../tests/fixtures/work-backlog-synthetic.json';
import { serializeWorkEvent, workContentDigest } from '../../gatekeeper/work-contract.js';
import { readWorkWindow } from './work-transport-read.js';
import { openWorkTransportStore } from './work-transport-store.js';
import { publishWorkOnce, reconcileWorkReadback } from './work-transport-publish.js';

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
/** @type {any} */
const event = {
  ...fixture.events[0],
  stream_id: streamId,
  actor: { ...fixture.events[0].actor, principal_id: principalId }
};
const note = serializeWorkEvent(event);
const metadata = {
  id: streamId,
  fulcra_userid: principalId,
  annotation_type: 'moment',
  fulcra_source_id: sourceId,
  deleted_at: null
};
/** @param {any} [value] */
const row = (value = event) => ({
  id: '91fe3dd7-8f9f-5267-b5d7-eb8187c349f6',
  source_id: sourceId,
  note: serializeWorkEvent(value),
  metadata
});
const start = '2026-09-26T00:00:00Z';
const end = '2026-09-27T00:00:00Z';
const now = () => Date.parse('2026-09-27T01:00:00.000Z');
/** @type {string[]} */
const dirs = [];
/** @type {Array<ReturnType<typeof openWorkTransportStore>>} */
const stores = [];
function database() {
  const dir = mkdtempSync(join(tmpdir(), 'gatekeeper-publish-'));
  chmodSync(dir, 0o700);
  dirs.push(dir);
  return join(dir, 'work.sqlite');
}
/** @param {string} dbPath */
function open(dbPath) {
  const store = openWorkTransportStore({ dbPath, config });
  stores.push(store);
  return store;
}
/** @param {Record<string,any>} [overrides] */
function transport(overrides = {}) {
  /** @type {{url:string,init:RequestInit}[]} */
  const calls = [];
  const values = {
    info: { userid: principalId },
    catalog: [
      {
        id: config.channel,
        api_version: 'v1alpha1',
        recordable: true,
        queryable: true,
        record_spec: { type: 'event' },
        fulcra_userid: principalId
      }
    ],
    annotation: [metadata],
    post: { upload_id: 'pending-1' },
    records: [row()],
    ...overrides
  };
  /** @type {typeof globalThis.fetch} */
  const fetch = async (url, init) => {
    calls.push({ url: String(url), init: init ?? {} });
    const key = String(url).includes('/info')
      ? 'info'
      : String(url).includes('/catalog')
        ? 'catalog'
        : String(url).includes('/annotation?')
          ? 'annotation'
          : init?.method === 'POST'
            ? 'post'
            : 'records';
    const value = values[key];
    return value instanceof Response
      ? value
      : new Response(JSON.stringify(value), {
          status: key === 'post' ? 201 : 200,
          headers: { 'content-type': 'application/json' }
        });
  };
  return { fetch, calls };
}
/** @param {typeof globalThis.fetch|undefined} [fetch] @param {any[]} [records] */
async function trustedRead(fetch, records = [row()]) {
  const t = transport({ records });
  return readWorkWindow({
    fetch: fetch ?? t.fetch,
    token: 'SENSITIVE_BEARER',
    config,
    start,
    end,
    now
  });
}
afterEach(() => {
  for (const store of stores.splice(0)) store.close();
  for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true });
  vi.useRealTimers();
});

describe('single-attempt synthetic work publisher', () => {
  it('posts exact canonical JSONL bytes and treats upload receipt as pending, not verified', async () => {
    const store = open(database());
    const t = transport();
    const result = await publishWorkOnce({
      fetch: t.fetch,
      token: 'SENSITIVE_BEARER',
      config,
      store,
      event,
      now
    });
    expect(result).toEqual({
      status: 'pending_readback',
      event_id: event.event_id,
      upload_id: 'pending-1'
    });
    expect(t.calls.map((call) => call.url)).toEqual([
      'https://api.fulcradynamics.com/user/v1alpha1/info',
      'https://api.fulcradynamics.com/data/v1/catalog?data_type=MomentAnnotation%2F00000000-0000-4000-8000-000000000901',
      `https://api.fulcradynamics.com/user/v1alpha1/annotation?fulcra_userid=${principalId}&annotation_type=moment`,
      'https://api.fulcradynamics.com/ingest/v1/record/MomentAnnotation?api_version=v1alpha1'
    ]);
    const post = t.calls[3];
    const body = `${JSON.stringify({ note, sources: ['com.gatekeeper.frontdoor', sourceId] })}\n`;
    expect(post.init.body).toBe(body);
    expect(post.init.headers).toMatchObject({
      Authorization: 'Bearer SENSITIVE_BEARER',
      'Content-Type': 'application/x-jsonl',
      'Content-Length': String(Buffer.byteLength(body, 'utf8'))
    });
    expect(post.init.redirect).toBe('error');
    expect(store.inspect().intents[0].state).toBe('UPLOAD_ACCEPTED');
  });

  it.each([
    [{ actor: { ...event.actor, logical_agent_id: 'forged' } }, 'INVALID_EVENT_SCOPE'],
    [{ workspace_id: '00000000-0000-4000-8000-000000000999' }, 'INVALID_EVENT_SCOPE'],
    [{ stream_id: '00000000-0000-4000-8000-000000000999' }, 'INVALID_EVENT_SCOPE']
  ])(
    'rejects changed actor/workspace/stream before any network or store write %#',
    async (changes, code) => {
      const store = open(database());
      const t = transport();
      expect(
        await publishWorkOnce({
          fetch: t.fetch,
          token: 'SENSITIVE_BEARER',
          config,
          store,
          event: { ...event, ...changes },
          now
        })
      ).toEqual({ status: 'blocked', event_id: event.event_id, code });
      expect(t.calls).toEqual([]);
      expect(store.inspect().intents).toEqual([]);
    }
  );

  it.each([
    [{ info: { userid: 'other' } }, 'PRINCIPAL_MISMATCH'],
    [{ catalog: [] }, 'CATALOG_MISMATCH']
  ])('denies owner/catalog preflight without posting %#', async (changes, code) => {
    const store = open(database());
    const t = transport(changes);
    expect(
      await publishWorkOnce({
        fetch: t.fetch,
        token: 'SENSITIVE_BEARER',
        config,
        store,
        event,
        now
      })
    ).toEqual({ status: 'blocked', event_id: event.event_id, code });
    expect(t.calls.some((call) => call.init.method === 'POST')).toBe(false);
    expect(store.inspect().intents).toEqual([]);
  });

  it('verifies only exact read-back and treats same event ID with changed bytes as conflict', async () => {
    const store = open(database());
    const t = transport();
    await publishWorkOnce({ fetch: t.fetch, token: 'SENSITIVE_BEARER', config, store, event, now });
    expect(
      reconcileWorkReadback({
        store,
        readResult: JSON.parse(JSON.stringify(await trustedRead(t.fetch))),
        eventId: event.event_id
      }).status
    ).toBe('unknown');
    const changed = { ...event, payload: { item: { ...event.payload.item, title: 'Changed' } } };
    const result = await trustedRead(undefined, [
      row(),
      { ...row(changed), id: '91fe3dd7-8f9f-5267-b5d7-eb8187c349f7' }
    ]);
    expect(reconcileWorkReadback({ store, readResult: result, eventId: event.event_id })).toEqual({
      status: 'conflict',
      event_id: event.event_id
    });
    expect(store.inspect().intents[0].state).toBe('CONFLICT');
    expect(store.accumulated().records).toHaveLength(2);
  });

  it('moves accepted upload to verified only after exact source-bound read-back', async () => {
    const store = open(database());
    const t = transport();
    await publishWorkOnce({ fetch: t.fetch, token: 'SENSITIVE_BEARER', config, store, event, now });
    const result = await trustedRead(t.fetch);
    expect(reconcileWorkReadback({ store, readResult: result, eventId: event.event_id })).toEqual({
      status: 'verified',
      event_id: event.event_id,
      record_id: row().id
    });
    expect(store.inspect().intents[0].state).toBe('VERIFIED');
  });

  it.each([
    [new Response(JSON.stringify({ error: 'SENSITIVE_BODY' }), { status: 500 }), 'HTTP_ERROR'],
    [new Response('{not-json', { status: 201 }), 'MALFORMED_RESPONSE'],
    [Response.redirect('https://other.example/'), 'HTTP_ERROR'],
    [new Response('x'.repeat(2 * 1024 * 1024 + 1), { status: 201 }), 'BYTE_LIMIT']
  ])(
    'marks ambiguous POST outcomes unknown without leaking response bodies %#',
    async (post, code) => {
      const store = open(database());
      const t = transport({ post });
      const result = await publishWorkOnce({
        fetch: t.fetch,
        token: 'SENSITIVE_BEARER',
        config,
        store,
        event,
        now
      });
      expect(result).toEqual({ status: 'unknown', event_id: event.event_id, code });
      expect(store.inspect().intents[0].state).toBe('UNKNOWN');
      expect(JSON.stringify(result)).not.toContain('SENSITIVE_BODY');
      expect(t.calls.filter((call) => call.init.method === 'POST')).toHaveLength(1);
    }
  );

  it('times out a stalled POST and does not issue a second POST after restart', async () => {
    const dbPath = database();
    const store = open(dbPath);
    const t = transport();
    /** @type {typeof globalThis.fetch} */
    const fetch = (url, init) =>
      init?.method === 'POST' ? new Promise(() => {}) : t.fetch(url, init);
    vi.useFakeTimers();
    const pending = publishWorkOnce({
      fetch,
      token: 'SENSITIVE_BEARER',
      config,
      store,
      event,
      now
    });
    await vi.advanceTimersByTimeAsync(15001);
    const first = await pending;
    expect(first).toEqual({ status: 'unknown', event_id: event.event_id, code: 'TIMEOUT' });
    store.close();
    stores.splice(stores.indexOf(store), 1);
    const reopened = open(dbPath);
    const retry = await publishWorkOnce({
      fetch: t.fetch,
      token: 'SENSITIVE_BEARER',
      config,
      store: reopened,
      event,
      now
    });
    expect(retry.status).toBe('unknown');
    expect(t.calls.some((call) => call.init.method === 'POST')).toBe(false);
  });

  it('treats a crash after POST_STARTED as unknown in flight and never resends on reopen', async () => {
    const dbPath = database();
    const store = open(dbPath);
    store.reserveIntent({ event, note, digest: workContentDigest(event) });
    store.startPost(event.event_id);
    store.close();
    stores.splice(stores.indexOf(store), 1);
    const reopened = open(dbPath);
    const t = transport();
    expect(
      (
        await publishWorkOnce({
          fetch: t.fetch,
          token: 'SENSITIVE_BEARER',
          config,
          store: reopened,
          event,
          now
        })
      ).status
    ).toBe('unknown');
    expect(t.calls.some((call) => call.init.method === 'POST')).toBe(false);
  });
});
