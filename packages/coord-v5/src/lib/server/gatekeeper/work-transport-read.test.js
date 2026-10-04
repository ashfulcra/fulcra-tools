import { describe, expect, it, vi } from 'vitest';
import fixture from '../../../../tests/fixtures/work-backlog-synthetic.json';
import { serializeWorkEvent, workContentDigest } from '../../gatekeeper/work-contract.js';
import {
  assertTrustedWorkReadResult,
  readWorkWindow,
  verifyOwnedWorkStream
} from './work-transport-read.js';

const annotationId = '00000000-0000-4000-8000-000000000901';
const configValue = {
  baseUrl: 'https://api.fulcradynamics.com/',
  principalId: '00000000-0000-4000-8000-000000000900',
  channel: 'MomentAnnotation/00000000-0000-4000-8000-000000000901',
  workspaceId: '00000000-0000-4000-8000-000000000100',
  workstreamId: '00000000-0000-4000-8000-000000000300',
  actorBinding: {
    principal_id: '00000000-0000-4000-8000-000000000900',
    logical_agent_id: 'synthetic-agent',
    instance_id: 'synthetic-instance',
    session_id: 'synthetic-session'
  }
};
const streamId = annotationId;
const source = `com.fulcradynamics.annotation.${annotationId}`;
const recordId = '00000000-0000-4000-8000-0000000009f6';
const start = '2026-09-26T00:00:00Z';
const end = '2026-09-27T00:00:00Z';
const receivedAt = '2026-09-27T01:00:00.000Z';
const now = () => Date.parse(receivedAt);
const event = {
  ...fixture.events[0],
  stream_id: streamId,
  actor: { ...fixture.events[0].actor, principal_id: configValue.principalId }
};
const note = serializeWorkEvent(event);
const metadata = {
  id: annotationId,
  fulcra_userid: configValue.principalId,
  annotation_type: 'moment',
  fulcra_source_id: source,
  deleted_at: null
};
const row = (body = note) => ({ id: recordId, source_id: source, note: body, metadata });
const responses = (records = [row()]) => ({
  info: { userid: configValue.principalId },
  catalog: [
    {
      id: configValue.channel,
      api_version: 'v1alpha1',
      recordable: true,
      queryable: true,
      record_spec: { type: 'event' },
      fulcra_userid: configValue.principalId
    }
  ],
  annotation: [metadata],
  records
});
function transport(changes = {}) {
  const values = { ...responses(), ...changes };
  /** @type {{url:string,init:RequestInit}[]} */
  const calls = [];
  /** @type {typeof globalThis.fetch} */
  const fetch = async (url, init) => {
    calls.push({ url: String(url), init: init ?? {} });
    const key = String(url).includes('/info')
      ? 'info'
      : String(url).includes('/catalog')
        ? 'catalog'
        : String(url).includes('/annotation?')
          ? 'annotation'
          : 'records';
    const value = values[key];
    return value instanceof Response
      ? value
      : new Response(JSON.stringify(value), { headers: { 'content-type': 'application/json' } });
  };
  return { fetch, calls };
}
async function read(changes = {}, override = {}) {
  const t = transport(changes);
  return {
    result: await readWorkWindow({
      fetch: t.fetch,
      token: 'SENSITIVE_BEARER',
      config: configValue,
      start,
      end,
      now,
      ...override
    }),
    calls: t.calls
  };
}

describe('bounded synthetic work transport reader', () => {
  it('requests exact owner/catalog/annotation/scoped-window paths and returns only partial transport provenance', async () => {
    const { result, calls } = await read();
    expect(calls.map((call) => call.url)).toEqual([
      'https://api.fulcradynamics.com/user/v1alpha1/info',
      'https://api.fulcradynamics.com/data/v1/catalog?data_type=MomentAnnotation%2F00000000-0000-4000-8000-000000000901',
      `https://api.fulcradynamics.com/user/v1alpha1/annotation?fulcra_userid=${configValue.principalId}&annotation_type=moment`,
      `https://api.fulcradynamics.com/data/v1alpha1/event/${configValue.channel}?start_time=2026-09-26T00%3A00%3A00.000Z&end_time=2026-09-27T00%3A00%3A00.000Z`
    ]);
    expect(
      calls.every(
        ({ init }) =>
          init.method === 'GET' &&
          init.redirect === 'error' &&
          new Headers(init.headers).get('Authorization') === 'Bearer SENSITIVE_BEARER'
      )
    ).toBe(true);
    expect(result.scope).toEqual({
      principal_id: configValue.principalId,
      channel: configValue.channel,
      workspace_id: configValue.workspaceId,
      workstream_id: configValue.workstreamId,
      stream_id: streamId
    });
    expect(result.window).toEqual({ start, end });
    expect(result.observation).toEqual({
      coverage: 'partial',
      as_of: receivedAt,
      last_successful_observation_at: receivedAt,
      sources: [{ stream_id: streamId, status: 'partial', pending_pages: null }],
      gaps: [],
      errors: [],
      completeness_evidence_id: null
    });
    expect(result.records).toEqual([
      {
        record_id: recordId,
        event_id: event.event_id,
        event_digest: workContentDigest(event),
        note,
        event,
        source_binding: { source_id: source, metadata },
        received_at: receivedAt
      }
    ]);
    expect(result.candidates).toEqual([event]);
    expect(result.eventEvidence).toEqual([
      {
        event_id: event.event_id,
        event_digest: workContentDigest(event),
        record_id: recordId,
        source_principal_id: configValue.principalId,
        stream_id: streamId,
        received_at: receivedAt
      }
    ]);
    expect(assertTrustedWorkReadResult(result, configValue)).toBe(result);
    expect(() =>
      assertTrustedWorkReadResult(JSON.parse(JSON.stringify(result)), configValue)
    ).toThrow('UNTRUSTED_READ_RESULT');
  });

  it.each([
    [{ info: { userid: 'other' } }, 'PRINCIPAL_MISMATCH'],
    [{ catalog: [] }, 'CATALOG_MISMATCH'],
    [{ catalog: [...responses().catalog, ...responses().catalog] }, 'CATALOG_MISMATCH'],
    [{ catalog: [{ ...responses().catalog[0], recordable: false }] }, 'CATALOG_MISMATCH'],
    [{ annotation: [] }, 'ANNOTATION_MISMATCH'],
    [{ annotation: [{ ...metadata, deleted_at: '2026-09-26T12:00:00Z' }] }, 'ANNOTATION_MISMATCH']
  ])('fails closed on ownership preflight %#', async (change, code) => {
    const t = transport(change);
    expect(
      await verifyOwnedWorkStream({
        fetch: t.fetch,
        token: 'SENSITIVE_BEARER',
        config: configValue
      })
    ).toEqual({ status: 'unavailable', code });
    const result = await readWorkWindow({
      fetch: t.fetch,
      token: 'SENSITIVE_BEARER',
      config: configValue,
      start,
      end,
      now
    });
    expect(result.records).toEqual([]);
    expect(result.observation.coverage).toBe('unavailable');
    expect(result.observation.errors).toEqual([{ code, stream_id: streamId }]);
  });

  it('ignores a valid v1 note, gaps malformed work and wrong source, and retains a forged logical-agent candidate without a grant', async () => {
    const forged = { ...event, actor: { ...event.actor, logical_agent_id: 'forged-agent' } };
    const v1 = {
      protocol: 'gatekeeper/1',
      event_id: recordId,
      conversation_id: recordId,
      sender: 'test',
      kind: 'message',
      created_at: start,
      payload: { text: 'old', speaker: 'visitor' }
    };
    const rows = [
      row(JSON.stringify(v1)),
      { ...row('{"protocol":"gatekeeper-work/1",'), id: '00000000-0000-4000-8000-0000000009f7' },
      { ...row(), id: '00000000-0000-4000-8000-0000000009f8', source_id: 'foreign' },
      { ...row(serializeWorkEvent(forged)), id: '00000000-0000-4000-8000-0000000009f9' }
    ];
    const { result } = await read({ records: rows });
    expect(result.records).toHaveLength(1);
    expect(result.candidates[0].actor.logical_agent_id).toBe('forged-agent');
    expect(result.eventEvidence[0]).not.toHaveProperty('grant');
    expect(result.observation.gaps).toEqual([
      { code: 'NOTE_FORMAT', stream_id: streamId },
      { code: 'RECORD_SOURCE_MISMATCH', stream_id: streamId }
    ]);
  });

  it.each([
    [{ records: [{ ...row(), id: 'bad' }] }, 'RECORD_ID'],
    [{ records: [{ ...row(), note: 'x'.repeat(65537) }] }, 'NOTE_LIMIT'],
    [{ records: Array.from({ length: 1001 }, () => row()) }, 'ROW_LIMIT'],
    [{ records: new Response('x'.repeat(2 * 1024 * 1024 + 1)) }, 'BYTE_LIMIT'],
    [{ records: Response.redirect('https://other.example/') }, 'HTTP_ERROR']
  ])('bounds rows, notes and response bodies without leaking secrets %#', async (change, code) => {
    const { result } = await read(change);
    expect(JSON.stringify(result)).not.toContain('SENSITIVE_BEARER');
    expect(result.observation.coverage).toBe(
      code === 'ROW_LIMIT' || code === 'BYTE_LIMIT' || code === 'HTTP_ERROR'
        ? 'unavailable'
        : 'partial'
    );
    const diagnostics = [...result.observation.gaps, ...result.observation.errors];
    expect(diagnostics.some((item) => item.code === code)).toBe(true);
  });

  it('rejects overlong windows and times out a stalled response', async () => {
    const oversized = await read({}, { end: '2026-10-04T00:00:01Z' });
    expect(oversized.result.observation.errors).toEqual([
      { code: 'INVALID_WINDOW', stream_id: streamId }
    ]);
    vi.useFakeTimers();
    try {
      const fetch = () => new Promise(() => {});
      const pending = readWorkWindow({
        fetch,
        token: 'SENSITIVE_BEARER',
        config: configValue,
        start,
        end,
        now
      });
      await vi.advanceTimersByTimeAsync(15001);
      const result = await pending;
      expect(result.observation.errors).toEqual([{ code: 'TIMEOUT', stream_id: streamId }]);
    } finally {
      vi.useRealTimers();
    }
  });
});
