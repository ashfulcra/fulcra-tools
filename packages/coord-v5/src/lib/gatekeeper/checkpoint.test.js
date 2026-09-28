import { describe, expect, it } from 'vitest';
import backlog from '../../../tests/fixtures/work-backlog-synthetic.json';
import { validateWorkEvent, workContentDigest } from './work-contract.js';
import { replayWorkEvents } from './work-projection.js';
import checkpointFixture from '../../../tests/fixtures/work-handoff-synthetic.json';
import * as checkpointApi from './checkpoint.js';

/** @returns {any} */
function packageInput() {
  const checkpoint = structuredClone(checkpointFixture);
  const e = publication();
  e.payload.body_digest = workContentDigest(checkpoint);
  e.payload.artifact.sha256 = e.payload.body_digest;
  return {
    ...context([...structuredClone(history), e]),
    checkpoint,
    publicationEvent: e,
    recipient: { ...actor, logical_agent_id: 'receiver' },
    accessRequirements: []
  };
}

describe('portable checkpoint bodies and replay packages', () => {
  it('rejects an unrelated frontier without widening the exported scope', () => {
    const i = packageInput();
    i.checkpoint.source_frontier.event_ids.push(history[1].event_id);
    i.publicationEvent.payload.body_digest = workContentDigest(i.checkpoint);
    i.publicationEvent.payload.artifact.sha256 = i.publicationEvent.payload.body_digest;
    Object.assign(i, context(i.events));
    expect(checkpointApi.buildHandoffPackage(i)).toMatchObject({
      ok: false,
      error: { code: 'FRONTIER_OUTSIDE_CLOSURE' }
    });
  });
  it('preserves operation-ID conflicts even when the other candidate belongs to another entity', () => {
    const i = packageInput();
    const collision = structuredClone(history[1]);
    collision.operation_id = history[7].operation_id;
    i.events = i.events.filter((/** @type {any} */ e) => e.event_id !== collision.event_id);
    i.events.push(collision);
    Object.assign(i, context(i.events));
    const r = checkpointApi.buildHandoffPackage(i);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.package.events.some((/** @type {any} */ e) => e.event_id === collision.event_id)).toBe(
      true
    );
    expect(
      replayWorkEvents(context(r.package.events)).conflicts.some(
        (d) => d.code === 'OPERATION_ID_CONFLICT'
      )
    ).toBe(true);
  });
  it('uses a total content tie-break even for canonically equivalent Unicode text', () => {
    const i = packageInput();
    const a = structuredClone(history[7]),
      b = structuredClone(history[7]);
    a.payload.text = '\u00e9';
    b.payload.text = 'e\u0301';
    i.events = i.events.filter((/** @type {any} */ e) => e.event_id !== a.event_id);
    i.events.push(a, b);
    Object.assign(i, context(i.events));
    const x = checkpointApi.buildHandoffPackage(i),
      y = checkpointApi.buildHandoffPackage({ ...i, events: [...i.events].reverse() });
    expect(x.ok).toBe(true);
    expect(y.ok).toBe(true);
    if (x.ok && y.ok) expect(workContentDigest(x.package)).toBe(workContentDigest(y.package));
  });
  it.each(['missing', 'foreign', 'untrusted'])(
    'rejects %s semantic work dependency closure',
    (mode) => {
      const i = packageInput();
      const dependent = i.events.find((/** @type {any} */ e) => e.event_id === history[6].event_id);
      dependent.payload.item.dependency_ids = [id(95)];
      const dep = {
        ...structuredClone(base),
        event_id: id(96),
        operation_id: id(97),
        subject: { type: 'work', id: id(95) }
      };
      if (mode === 'foreign') dep.workstream_id = id(98);
      if (mode !== 'missing') i.events.push(dep);
      Object.assign(i, context(i.events));
      if (mode === 'untrusted')
        i.trust.event_evidence = i.trust.event_evidence.filter(
          (/** @type {any} */ r) => r.event_id !== dep.event_id
        );
      expect(checkpointApi.buildHandoffPackage(i).ok).toBe(false);
    }
  );
  it('includes recursive dependency history and a closed causal cycle as pending, without claiming progress', () => {
    const i = packageInput();
    const dependent = i.events.find((/** @type {any} */ e) => e.event_id === history[6].event_id);
    dependent.payload.item.dependency_ids = [history[1].subject.id];
    const pending = {
      ...structuredClone(history[7]),
      event_id: id(91),
      operation_id: id(92),
      parents: [history[2].event_id, id(93)]
    };
    const pending2 = {
      ...structuredClone(pending),
      event_id: id(93),
      operation_id: id(94),
      parents: [history[2].event_id, id(91)]
    };
    i.events.push(pending, pending2);
    Object.assign(i, context(i.events));
    const r = checkpointApi.buildHandoffPackage(i);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.package.events).toHaveLength(12);
    expect(
      replayWorkEvents(context(r.package.events)).pending.filter((d) => d.code === 'CAUSAL_CYCLE')
    ).toHaveLength(2);
    expect(r.package.observation.coverage).toBe('partial');
  });
  it.each(['trust', 'grants', 'readiness', 'verified', 'full_transcript'])(
    'rejects package extra field %s',
    (key) => {
      const r = checkpointApi.buildHandoffPackage(packageInput());
      expect(r.ok).toBe(true);
      if (!r.ok) return;
      r.package[key] = {};
      expect(checkpointApi.validateHandoffPackage(r.package).ok).toBe(false);
    }
  );
  it('rejects mismatched tail IDs and required resources absent from checkpoint references', () => {
    const i = packageInput();
    i.accessRequirements = [{ resource_id: id(999), scope_id: base.workspace_id, action: 'read' }];
    expect(checkpointApi.buildHandoffPackage(i).ok).toBe(false);
    const r = checkpointApi.buildHandoffPackage(packageInput());
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    r.package.tail_event_ids = [];
    expect(checkpointApi.validateHandoffPackage(r.package).ok).toBe(false);
  });
  it('accepts an opaque access scope and an artifact owned by someone other than the publisher', () => {
    const i = packageInput();
    i.publicationEvent.payload.artifact.owner_principal_id = 'artifact-owner';
    i.accessRequirements = [
      { resource_id: i.publicationEvent.payload.artifact.id, action: 'read', scope_id: id(98) }
    ];
    Object.assign(i, context(i.events));
    const r = checkpointApi.buildHandoffPackage(i);
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.package.access_requirements).toEqual(i.accessRequirements);
  });
  it('blocks an integrity-conflict counterpart in another workstream', () => {
    const i = packageInput();
    const foreign = structuredClone(history[1]);
    foreign.event_id = id(97);
    foreign.operation_id = history[7].operation_id;
    foreign.workstream_id = id(98);
    i.events.push(foreign);
    Object.assign(i, context(i.events));
    expect(checkpointApi.buildHandoffPackage(i)).toMatchObject({
      ok: false,
      error: { code: 'CROSS_SCOPE_CLOSURE' }
    });
  });
  it('round-trips full relevant history and ID-based tail without grants or verified claims', () => {
    expect(checkpointApi).not.toBeNull();
    const input = packageInput();
    const r = checkpointApi.buildHandoffPackage(input);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(checkpointApi.validateCheckpoint(input.checkpoint).ok).toBe(true);
    expect(checkpointApi.validateHandoffPackage(JSON.parse(JSON.stringify(r.package))).ok).toBe(
      true
    );
    expect(r.package.events).toHaveLength(9); // eight relevant records plus publication; unrelated deferred excluded
    expect(r.package.tail_event_ids).toEqual([
      history[2].event_id,
      history[6].event_id,
      history[7].event_id,
      history[8].event_id,
      id(1)
    ]);
    expect(r.package.checkpoint.external_operations[0].status).toBe('unknown');
    expect(r.package).not.toHaveProperty('trust');
  });
  it.each(['verified', 'trust', 'grants', 'credentials', 'hidden_reasoning', 'full_transcript'])(
    'rejects extra body field %s',
    (key) => {
      expect(checkpointApi).not.toBeNull();
      const c = /** @type {any} */ (structuredClone(checkpointFixture));
      c[key] = 'forbidden';
      expect(checkpointApi.validateCheckpoint(c).ok).toBe(false);
    }
  );
  it.each([
    '-----BEGIN PRIVATE KEY-----',
    'Bearer private-credential-123',
    'https://user:pass@example.invalid/x',
    'https://example.invalid/x?token=abc',
    'https://example.invalid/x#credential'
  ])('rejects recognizable credential-bearing free text without echoing it: %s', (value) => {
    expect(checkpointApi).not.toBeNull();
    const c = structuredClone(checkpointFixture);
    c.objective = value;
    const r = checkpointApi.validateCheckpoint(c);
    expect(r.ok).toBe(false);
    expect(JSON.stringify(r)).not.toContain(value);
  });
  it.each(['ToKeN', 'PASSWORD', 'Secret', 'Authorization', 'cookie', 'PRIVATE_KEY'])(
    'rejects sensitive key %s safely',
    (key) => {
      expect(checkpointApi).not.toBeNull();
      const c = /** @type {any} */ (structuredClone(checkpointFixture));
      c[key] = 'sensitive-value';
      const r = checkpointApi.validateCheckpoint(c);
      expect(r.ok).toBe(false);
      expect(JSON.stringify(r)).not.toContain('sensitive-value');
      expect(JSON.stringify(r)).not.toContain(key);
    }
  );
  it('rejects 128 KiB bodies without truncating valid-sized text entries', () => {
    expect(checkpointApi).not.toBeNull();
    const c = structuredClone(checkpointFixture);
    c.completed_actions = Array(40).fill('a'.repeat(4096));
    expect(checkpointApi.validateCheckpoint(c).ok).toBe(false);
  });
  it.each(['publication', 'digest', 'workspace', 'assignment', 'frontier', 'question', 'parent'])(
    'blocks missing or mismatched %s',
    (what) => {
      expect(checkpointApi).not.toBeNull();
      const i = packageInput();
      if (what === 'publication')
        i.events = i.events.filter((/** @type {any} */ e) => e.kind !== 'checkpoint.published');
      if (what === 'digest') i.checkpoint.objective = 'Changed body';
      if (what === 'workspace') i.checkpoint.workspace_id = id(90);
      if (what === 'assignment') i.checkpoint.assignment_version = 4;
      if (what === 'frontier') i.checkpoint.source_frontier.event_ids.push(id(99));
      if (what === 'question')
        i.events = i.events.filter((/** @type {any} */ e) => e.kind !== 'question.opened');
      if (what === 'parent')
        i.events = i.events.filter((/** @type {any} */ e) => e.kind !== 'work.opened');
      expect(checkpointApi.buildHandoffPackage(i).ok).toBe(false);
    }
  );
  it('keeps partial observations partial and rejects local required resources while preserving optional local ones', () => {
    expect(checkpointApi).not.toBeNull();
    const i = packageInput();
    i.observation.coverage = 'partial';
    const local = {
      ...i.publicationEvent.payload.artifact,
      id: id(70),
      uri: '/tmp/local-result',
      portable: false
    };
    i.checkpoint.artifacts.push(local);
    i.publicationEvent.payload.body_digest = workContentDigest(i.checkpoint);
    i.publicationEvent.payload.artifact.sha256 = i.publicationEvent.payload.body_digest;
    Object.assign(i, context(i.events));
    i.observation.coverage = 'partial';
    const r = checkpointApi.buildHandoffPackage(i);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.package.observation.coverage).toBe('partial');
    expect(r.package.checkpoint.artifacts[0].portable).toBe(false);
    i.accessRequirements = [{ resource_id: local.id, action: 'read', scope_id: base.workspace_id }];
    expect(checkpointApi.buildHandoffPackage(i).ok).toBe(false);
  });
  it('retains conflicting variants with identical event IDs deterministically under permutations', () => {
    expect(checkpointApi).not.toBeNull();
    const i = packageInput();
    const variant = structuredClone(history[7]);
    variant.payload.text = 'Another target';
    i.events.push(variant);
    Object.assign(i, context(i.events));
    const a = checkpointApi.buildHandoffPackage(i);
    const b = checkpointApi.buildHandoffPackage({ ...i, events: [...i.events].reverse() });
    expect(a.ok).toBe(true);
    expect(b.ok).toBe(true);
    if (!a.ok || !b.ok) return;
    expect(
      a.package.events.filter((/** @type {any} */ e) => e.event_id === variant.event_id)
    ).toHaveLength(2);
    expect(workContentDigest(a.package)).toBe(workContentDigest(b.package));
    expect(replayWorkEvents({ ...context(a.package.events) }).questions[0].state).toBe(
      'conflicted'
    );
  });
  it('retains a cancelled tail as authoritative despite checkpoint next-action prose', () => {
    expect(checkpointApi).not.toBeNull();
    const i = packageInput();
    const prev = history[6];
    const tail = {
      ...structuredClone(prev),
      event_id: id(80),
      operation_id: id(81),
      parents: [prev.event_id],
      payload: {
        ...structuredClone(prev.payload),
        expected_event_id: prev.event_id,
        reason: 'Stopped',
        item: { ...structuredClone(prev.payload.item), status: 'cancelled', blocker: null }
      }
    };
    i.events.push(tail);
    Object.assign(i, context(i.events));
    const r = checkpointApi.buildHandoffPackage(i);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.package.tail_event_ids).toContain(tail.event_id);
    expect(replayWorkEvents(context(r.package.events)).work[0].item?.status).toBe('cancelled');
  });
  it('blocks more than 1000 events and packages above 1 MiB explicitly', () => {
    expect(checkpointApi).not.toBeNull();
    const i = packageInput();
    i.events = Array(1001).fill(base);
    expect(checkpointApi.buildHandoffPackage(i).ok).toBe(false);
    const normal = checkpointApi.buildHandoffPackage(packageInput());
    expect(normal.ok).toBe(true);
    if (!normal.ok) return;
    const p = normal.package;
    p.events = Array.from({ length: 300 }, (_, n) => ({
      ...structuredClone(base),
      event_id: id(100 + n),
      operation_id: id(500 + n),
      payload: { item: { ...structuredClone(base.payload.item), intent: 'a'.repeat(4096) } }
    }));
    expect(checkpointApi.validateHandoffPackage(p).ok).toBe(false);
  });
});

const time = '2026-09-27T12:00:00.000Z';
const id = (/** @type {number} */ n) => `20000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const base = backlog.events[0];
const actor = base.actor;
const history = [...backlog.events, ...backlog.replay_events];
/** @returns {any} */
const publication = () => ({
  ...structuredClone(base),
  event_id: id(1),
  operation_id: id(2),
  kind: 'checkpoint.published',
  subject: { type: 'checkpoint', id: id(3) },
  parents: [history[5].event_id],
  payload: {
    work_id: base.subject.id,
    checkpoint_id: id(3),
    artifact: {
      id: id(3),
      uri: 'https://example.invalid/checkpoint.json',
      sha256: 'a'.repeat(64),
      version: null,
      media_type: 'application/json',
      owner_principal_id: actor.principal_id,
      audience: 'workspace',
      portable: true
    },
    body_digest: 'a'.repeat(64),
    assignment_version: 1,
    assignment_event_id: history[5].event_id
  }
});
/** @param {any[]} events */
function context(events) {
  return {
    events,
    asOf: time,
    trust: {
      workspace_id: base.workspace_id,
      allowed_stream_ids: [base.stream_id],
      grants: [
        {
          ...actor,
          capabilities: [
            'work.write',
            'assignment.manage',
            'assignment.accept',
            'question.ask',
            'question.answer',
            'question.apply',
            'checkpoint.publish'
          ]
        }
      ],
      event_evidence: events.flatMap((e, i) => {
        const result = validateWorkEvent(e);
        return result.ok
          ? [
              {
                event_id: e.event_id,
                event_digest: workContentDigest(result.event),
                record_id: `receipt-${i}`,
                source_principal_id: e.actor.principal_id,
                stream_id: e.stream_id,
                received_at: time
              }
            ]
          : [];
      })
    },
    observation: {
      coverage: 'complete',
      as_of: time,
      last_successful_observation_at: time,
      sources: [{ stream_id: base.stream_id, status: 'complete', pending_pages: 0 }],
      gaps: [],
      errors: [],
      completeness_evidence_id: 'full-history'
    }
  };
}

describe('checkpoint publication', () => {
  it('registers strict immutable pointers without treating publication as verification', () => {
    const e = publication();
    expect(validateWorkEvent(e).ok).toBe(true);
    const projection = replayWorkEvents(context([...history, e]));
    expect(projection.checkpoints).toHaveLength(1);
    expect(projection.checkpoints[0]).toMatchObject({
      checkpoint_id: id(3),
      work_id: base.subject.id,
      state: 'current',
      publication_event: e
    });
    expect(projection.checkpoints[0]).not.toHaveProperty('verified');
  });
  it.each(['verified', 'trust', 'credentials'])('rejects extra publication field %s', (key) => {
    const e = publication();
    e.payload[key] = true;
    expect(validateWorkEvent(e).ok).toBe(false);
  });
  it('rejects mismatched pointer/checkpoint identity and digest', () => {
    for (const edit of [
      (/** @type {any} */ e) => (e.payload.artifact.sha256 = 'b'.repeat(64)),
      (/** @type {any} */ e) => (e.payload.checkpoint_id = id(9)),
      (/** @type {any} */ e) => (e.payload.artifact.portable = false)
    ]) {
      const e = publication();
      edit(e);
      expect(validateWorkEvent(e).ok).toBe(false);
    }
  });
  it('allows independent resource IDs and version-only pointers', () => {
    const e = publication();
    e.payload.artifact.id = id(9);
    e.payload.artifact.sha256 = null;
    e.payload.artifact.version = 'v1';
    expect(validateWorkEvent(e).ok).toBe(true);
  });
  it('allows an explicitly unassigned causal snapshot without granting execution authority', () => {
    const e = publication();
    e.parents = [base.event_id];
    e.payload.assignment_version = 0;
    e.payload.assignment_event_id = null;
    const p = replayWorkEvents(context([base, e]));
    expect(p.checkpoints[0]?.publication_event).toEqual(e);
    expect(p.work[0].execution_authority).toBe('unassigned');
  });
  it('authorizes exact accepted identity and assignment in causal ancestry only', () => {
    for (const edit of [
      (/** @type {any} */ e) => (e.payload.assignment_version = 2),
      (/** @type {any} */ e) => {
        e.parents = [];
        e.payload.assignment_event_id = null;
        e.payload.assignment_version = 0;
      },
      (/** @type {any} */ e) => (e.workstream_id = id(9)),
      (/** @type {any} */ e) => (e.actor = { ...actor, instance_id: 'other' })
    ]) {
      const e = publication();
      edit(e);
      const c = context([...history, e]);
      c.trust.grants.push({ ...e.actor, capabilities: ['checkpoint.publish'] });
      expect(replayWorkEvents(c).checkpoints[0]?.publication_event ?? null).toBeNull();
    }
  });
});
