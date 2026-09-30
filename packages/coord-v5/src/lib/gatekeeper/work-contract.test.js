import { describe, expect, it } from 'vitest';
import backlogFixture from '../../../tests/fixtures/work-backlog-synthetic.json';
import { validateEvent } from './protocol.js';
import {
  canonicalWorkJson,
  parseWorkNote,
  serializeWorkEvent,
  validateWorkEvent,
  workContentDigest
} from './work-contract.js';

/** @param {number} n */
const id = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const actor = {
  principal_id: 'principal-a',
  logical_agent_id: 'agent-a',
  instance_id: 'instance-a',
  session_id: 'session-a'
};
const artifact = {
  id: id(20),
  uri: 'https://example.com/result',
  sha256: 'a'.repeat(64),
  version: null,
  media_type: 'application/json',
  owner_principal_id: 'principal-a',
  audience: 'workspace',
  portable: true
};
const item = {
  title: 'Ship migration',
  intent: 'Keep clients compatible',
  type: 'task',
  status: 'proposed',
  owner_id: 'agent-a',
  next_action: 'Write compatibility tests',
  acceptance_criteria: [{ id: 'criterion-1', text: 'Tests pass' }],
  priority: 'normal',
  dependency_ids: [],
  due_at: null,
  revisit: null,
  blocker: null,
  result: null
};
/** @type {Record<string, any>} */
const payloads = {
  'work.opened': { item },
  'work.revised': { expected_event_id: id(1), item: { ...item, status: 'ready' }, reason: null },
  'assignment.offered': {
    work_id: id(10),
    expected_assignment_event_id: null,
    expected_version: 0,
    target: actor
  },
  'assignment.accepted': {
    work_id: id(10),
    offer_event_id: id(2),
    expected_assignment_event_id: null,
    expected_version: 0,
    ready_event_id: null
  },
  'assignment.released': {
    work_id: id(10),
    expected_assignment_event_id: id(3),
    expected_version: 1,
    reason: 'Unforeseen absence'
  },
  'question.opened': {
    work_ids: [id(10)],
    text: 'Which path?',
    decision_maker_id: 'agent-a',
    options: ['A', 'B'],
    recommendation: null,
    deadline_at: null,
    escalation_target_id: null
  },
  'question.acknowledged': { opened_event_id: id(6) },
  'question.answered': { opened_event_id: id(6), supersedes_answer_event_id: null, text: 'Use A' },
  'question.applied': { answer_event_id: id(8), work_id: id(10), condition_id: 'condition-1' }
};
/** @param {string} kind */
const subjectType = (kind) => (kind.startsWith('question.') ? 'question' : 'work');
/** @param {Record<string, any>} payload */
const referencedParents = (payload) =>
  Object.entries(payload)
    .filter(([key, value]) => key.endsWith('_event_id') && typeof value === 'string')
    .map(([, value]) => value);
/** @param {string} kind @param {any} [payload] @returns {any} */
const event = (kind, payload = payloads[kind]) => ({
  protocol: 'gatekeeper-work/1',
  event_id: id(30),
  operation_id: id(31),
  workspace_id: id(32),
  stream_id: id(33),
  workstream_id: id(34),
  actor,
  kind,
  subject: { type: subjectType(kind), id: id(kind.startsWith('question.') ? 11 : 10) },
  parents: referencedParents(payload),
  audience: 'workspace',
  occurred_at: '2026-09-26T10:00:00.000Z',
  payload: structuredClone(payload)
});

describe('strict opt-in gatekeeper-work/1 event contract', () => {
  it('validates each synthetic backlog record without treating fixture data as trust', () => {
    expect(backlogFixture.events).toHaveLength(3);
    for (const candidate of backlogFixture.events) {
      expect(validateWorkEvent(candidate)).toEqual({ ok: true, event: candidate });
    }
  });

  it.each(Object.keys(payloads))(
    'validates and locally round trips %s without activating v1',
    (kind) => {
      const valid = event(kind);
      expect(validateWorkEvent(valid)).toEqual({ ok: true, event: valid });
      expect(parseWorkNote(serializeWorkEvent(valid))).toEqual({ ok: true, event: valid });
      expect(validateEvent(valid).ok).toBe(false);
    }
  );

  it.each([
    ['unknown envelope key', { secret_token: 'do-not-echo' }],
    ['missing envelope field', { operation_id: undefined }],
    ['null required envelope field', { operation_id: null }],
    ['wrong audience', { audience: 'public' }],
    ['duplicate parent', { parents: [id(1), id(1)] }],
    ['timezone-free time', { occurred_at: '2026-09-26T10:00:00' }],
    ['impossible day', { occurred_at: '2026-02-30T10:00:00Z' }],
    ['wrong subject', { subject: { type: 'question', id: id(10) } }]
  ])('rejects %s', (_, change) => {
    expect(validateWorkEvent({ ...event('work.opened'), ...change }).ok).toBe(false);
  });

  it.each([
    ['unknown item field', { ...item, authorization: 'Bearer do-not-echo' }],
    ['missing criterion', { ...item, acceptance_criteria: [] }],
    [
      'duplicate criterion',
      { ...item, acceptance_criteria: [item.acceptance_criteria[0], item.acceptance_criteria[0]] }
    ],
    ['missing next action', { ...item, next_action: null }],
    ['terminal without result', { ...item, status: 'completed', result: null }],
    [
      'completion without evidence',
      { ...item, status: 'completed', result: { summary: 'Done', evidence: [] } }
    ],
    [
      'wrong evidence criterion',
      {
        ...item,
        status: 'completed',
        result: { summary: 'Done', evidence: [{ criterion_id: 'other', artifact }] }
      }
    ],
    ['blocked without blocker', { ...item, status: 'blocked' }],
    ['paused without revisit', { ...item, status: 'paused' }],
    [
      'deferred without revisit',
      { ...item, type: 'deferred', status: 'deferred', next_action: null, acceptance_criteria: [] }
    ]
  ])('rejects %s', (_, invalid) => {
    expect(validateWorkEvent(event('work.opened', { item: invalid })).ok).toBe(false);
  });

  it('accepts structurally complete completion without claiming replay authorization', () => {
    const completed = {
      ...item,
      status: 'completed',
      next_action: null,
      result: { summary: 'Tests passed', evidence: [{ criterion_id: 'criterion-1', artifact }] }
    };
    expect(
      validateWorkEvent(
        event('work.revised', { expected_event_id: id(1), item: completed, reason: null })
      ).ok
    ).toBe(true);
  });

  it('leaves transition-dependent reason checks for replay', () => {
    const blocked = {
      ...item,
      status: 'blocked',
      blocker: {
        condition_id: 'waiting-on-failure',
        kind: 'failure',
        ref_id: null,
        unlock_condition: 'Failure is diagnosed'
      }
    };
    expect(
      validateWorkEvent(
        event('work.revised', { expected_event_id: id(1), item: blocked, reason: null })
      ).ok
    ).toBe(true);
  });

  it('accepts non-handoff acceptance with a released assignment head for replay authorization', () => {
    const candidate = event('assignment.accepted', {
      ...payloads['assignment.accepted'],
      expected_assignment_event_id: id(3),
      expected_version: 2
    });
    expect(validateWorkEvent(candidate)).toEqual({ ok: true, event: candidate });
  });

  it.each([
    { expected_assignment_event_id: id(3), expected_version: 0 },
    { expected_assignment_event_id: null, expected_version: 2 }
  ])('rejects inconsistent assignment acceptance shape %j', (change) => {
    expect(
      validateWorkEvent(
        event('assignment.accepted', { ...payloads['assignment.accepted'], ...change })
      ).ok
    ).toBe(false);
  });

  it('requires the released assignment head as a direct acceptance parent', () => {
    const candidate = event('assignment.accepted', {
      ...payloads['assignment.accepted'],
      expected_assignment_event_id: id(3),
      expected_version: 2
    });
    candidate.parents = [id(2)];
    expect(validateWorkEvent(candidate)).toEqual({
      ok: false,
      error: { code: 'PARENT_REQUIRED', path: '$.payload.expected_assignment_event_id' }
    });
  });

  it.each([
    [
      'partial offer',
      'assignment.offered',
      { work_id: id(10), expected_version: 0, target: actor }
    ],
    [
      'null accepted offer',
      'assignment.accepted',
      { ...payloads['assignment.accepted'], offer_event_id: null }
    ],
    [
      'fractional version',
      'assignment.released',
      { ...payloads['assignment.released'], expected_version: 1.5 }
    ],
    [
      'extra password',
      'question.answered',
      { ...payloads['question.answered'], password: 'do-not-echo' }
    ],
    [
      'duplicate work ids',
      'question.opened',
      { ...payloads['question.opened'], work_ids: [id(10), id(10)] }
    ]
  ])('rejects %s', (_, kind, payload) => {
    expect(validateWorkEvent(event(kind, payload)).ok).toBe(false);
  });

  it('rejects non-plain and prototype-polluted inputs', () => {
    expect(
      validateWorkEvent(Object.assign(Object.create({ inherited: true }), event('work.opened'))).ok
    ).toBe(false);
    expect(
      validateWorkEvent({
        ...event('work.opened'),
        payload: Object.assign(Object.create(null), payloads['work.opened'])
      }).ok
    ).toBe(false);
    expect(
      validateWorkEvent(
        JSON.parse(
          serializeWorkEvent(event('work.opened')).replace(
            '"payload":',
            '"__proto__":{},"payload":'
          )
        )
      ).ok
    ).toBe(false);
  });

  it('rejects hidden and accessor properties that JSON serialization would omit or execute', () => {
    const hidden = event('work.opened');
    Object.defineProperty(hidden.payload, 'toJSON', { value: () => ({}) });
    expect(validateWorkEvent(hidden).ok).toBe(false);
    expect(() => canonicalWorkJson(hidden)).toThrow(TypeError);
    const accessed = event('work.opened');
    Object.defineProperty(accessed.payload, 'item', {
      get() {
        throw new Error('must not execute');
      }
    });
    expect(validateWorkEvent(accessed)).toEqual({
      ok: false,
      error: { code: expect.any(String), path: expect.any(String) }
    });
    const arrayExtra = event('work.opened');
    Object.defineProperty(arrayExtra.parents, 'extra', { value: 'hidden' });
    expect(validateWorkEvent(arrayExtra).ok).toBe(false);
  });

  it('rejects array subclasses that can bypass element validation', () => {
    class Sneaky extends Array {
      forEach() {}
    }
    const candidate = event('work.opened');
    candidate.parents = new Sneaky('not-a-uuid');
    expect(validateWorkEvent(candidate).ok).toBe(false);
    expect(() => canonicalWorkJson(candidate)).toThrow(TypeError);
  });

  it('normalizes time to UTC and deep copies the accepted value', () => {
    const input = event('work.opened');
    input.occurred_at = '2026-09-26T06:00:00-04:00';
    const result = validateWorkEvent(input);
    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error('Expected valid event');
    expect(result.event.occurred_at).toBe('2026-09-26T10:00:00.000Z');
    input.payload.item.title = 'Changed later';
    expect(result.event.payload.item).toMatchObject({ title: 'Ship migration' });
  });

  it('preserves sub-millisecond precision while normalizing the timezone', () => {
    const input = event('work.opened');
    input.occurred_at = '2026-09-26T06:00:00.123456-04:00';
    const result = validateWorkEvent(input);
    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error('Expected valid event');
    expect(result.event.occurred_at).toBe('2026-09-26T10:00:00.123456Z');
  });

  it.each(['9999-12-31T23:00:00-02:00', '0001-01-01T00:00:00+02:00'])(
    'rejects %s when UTC normalization leaves the four-digit year range',
    (occurred_at) => {
      expect(validateWorkEvent({ ...event('work.opened'), occurred_at })).toEqual({
        ok: false,
        error: { code: 'INVALID_TIME', path: '$.occurred_at' }
      });
    }
  );

  it('rejects excess UTF-8 note bytes and gives safe code/path diagnostics', () => {
    const oversized = parseWorkNote('x'.repeat(65_537));
    expect(oversized.ok).toBe(false);
    if (oversized.ok) throw new Error('Expected rejection');
    expect(oversized.error).toMatchObject({ code: expect.any(String), path: '$' });
    const bad = event('question.answered', {
      ...payloads['question.answered'],
      secret_key: 'do-not-echo'
    });
    const result = validateWorkEvent(bad);
    expect(JSON.stringify(result)).not.toContain('do-not-echo');
    expect(() => serializeWorkEvent(bad)).toThrow(TypeError);
    expect(() => serializeWorkEvent(bad)).not.toThrow(/do-not-echo/);
  });

  it('never echoes an untrusted unknown key as an error path', () => {
    const candidate = event('work.opened');
    candidate.payload['Bearer do-not-echo'] = true;
    expect(JSON.stringify(validateWorkEvent(candidate))).not.toContain('do-not-echo');
  });

  it('applies byte rather than character limits to a valid-looking event', () => {
    const options = Array.from({ length: 50 }, (_, index) => `Option ${index} ${'😀'.repeat(400)}`);
    const candidate = event('question.opened', { ...payloads['question.opened'], options });
    expect(validateWorkEvent(candidate)).toEqual({
      ok: false,
      error: { code: 'LIMIT_EXCEEDED', path: '$' }
    });
  });

  it('canonicalizes reordered fields, preserves arrays, and rejects non-JSON values', () => {
    const a = { z: [2, 1], a: { y: 'x', b: true } };
    const b = { a: { b: true, y: 'x' }, z: [2, 1] };
    expect(canonicalWorkJson(a)).toBe('{"a":{"b":true,"y":"x"},"z":[2,1]}');
    expect(workContentDigest(a)).toBe(workContentDigest(b));
    expect(workContentDigest(a)).not.toBe(workContentDigest({ ...b, z: [1, 2] }));
    expect(() => canonicalWorkJson({ a: NaN })).toThrow(TypeError);
    expect(() => canonicalWorkJson({ a: undefined })).toThrow(TypeError);
    const cyclic = {};
    cyclic.self = cyclic;
    expect(() => canonicalWorkJson(cyclic)).toThrow(TypeError);
  });
});
