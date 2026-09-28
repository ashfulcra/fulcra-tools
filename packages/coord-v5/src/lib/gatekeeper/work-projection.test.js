import { describe, expect, it } from 'vitest';
import fixture from '../../../tests/fixtures/work-backlog-synthetic.json';
import { validateWorkEvent, workContentDigest } from './work-contract.js';
import { replayWorkEvents } from './work-projection.js';

const time = '2026-09-27T12:00:00.000Z';
const id = (/** @type {number} */ n) => `10000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const actor = fixture.events[0].actor;
const other = { ...actor, logical_agent_id: 'worker-b', instance_id: 'instance-b' };
const capabilities = [
  'work.write',
  'question.ask',
  'question.answer',
  'question.apply',
  'assignment.manage',
  'assignment.accept'
];
const base = fixture.events[0];
const workId = base.subject.id;
const questionId = fixture.events[2].subject.id;
/** @param {number} n @param {string} kind @param {any} payload @param {any[]} [parents] @param {any} [extra] */
function event(n, kind, payload, parents = [], extra = {}) {
  return {
    ...structuredClone(base),
    event_id: id(n),
    operation_id: id(n + 1000),
    kind,
    subject: {
      type: kind.startsWith('question.') ? 'question' : 'work',
      id: kind.startsWith('question.') ? questionId : workId
    },
    parents: parents.map((p) => (typeof p === 'string' ? p : p.event_id)),
    payload,
    ...extra
  };
}
const opened = () => event(1, 'work.opened', structuredClone(base.payload));
/** @param {number} n @param {any} previous @param {any} changes @param {any[]} [parents] @param {string|null} [reason] */
const revision = (n, previous, changes, parents = [], reason = null) =>
  event(
    n,
    'work.revised',
    {
      expected_event_id: previous.event_id,
      item: { ...structuredClone(previous.payload.item), ...changes },
      reason
    },
    [previous, ...parents]
  );
/** @param {any} open */
function assignment(open) {
  const offer = event(
    3,
    'assignment.offered',
    { work_id: workId, expected_assignment_event_id: null, expected_version: 0, target: actor },
    [open]
  );
  const accept = event(
    4,
    'assignment.accepted',
    {
      work_id: workId,
      offer_event_id: offer.event_id,
      expected_assignment_event_id: null,
      expected_version: 0,
      ready_event_id: null
    },
    [offer]
  );
  return [offer, accept];
}
/** @param {any[]} events */
function context(events) {
  return {
    events,
    trust: {
      workspace_id: base.workspace_id,
      allowed_stream_ids: [base.stream_id, id(900)],
      event_evidence: events.flatMap((e, index) => {
        const v = validateWorkEvent(e);
        return v.ok
          ? [
              {
                event_id: e.event_id,
                event_digest: workContentDigest(v.event),
                record_id: `record-${index}`,
                source_principal_id: e.actor.principal_id,
                stream_id: e.stream_id,
                received_at: time
              }
            ]
          : [];
      }),
      grants: [actor, other].map((a) => ({ ...a, capabilities }))
    },
    observation: {
      coverage: 'complete',
      as_of: time,
      last_successful_observation_at: time,
      sources: [
        { stream_id: base.stream_id, status: 'complete', pending_pages: 0 },
        { stream_id: id(900), status: 'complete', pending_pages: 0 }
      ],
      gaps: [],
      errors: [],
      completeness_evidence_id: 'receipt-complete'
    },
    asOf: time
  };
}
/** @param {any[]} events */
const replay = (events) => replayWorkEvents(context(events));
/** @param {any} projection */
const codes = (projection) =>
  [...projection.rejected, ...projection.pending, ...projection.conflicts].map((d) => d.code);

describe('authorized durable work replay', () => {
  it('keeps three distinct item types, revision history, and independent streams', () => {
    const open = opened();
    const ready = revision(2, open, { status: 'ready' });
    const thread = event(
      20,
      'work.opened',
      {
        item: {
          ...base.payload.item,
          type: 'open_thread',
          status: 'open',
          owner_id: null,
          next_action: null,
          acceptance_criteria: []
        }
      },
      [],
      { subject: { type: 'work', id: id(500) }, stream_id: id(900) }
    );
    const result = replay([open, ready, thread, fixture.events[1]]);
    expect(result.work.map((w) => w.item?.type).sort()).toEqual([
      'deferred',
      'open_thread',
      'task'
    ]);
    expect(result.work.find((w) => w.work_id === workId)).toMatchObject({
      item: { status: 'ready' },
      head_event_id: ready.event_id,
      event_ids: [open.event_id, ready.event_id],
      state: 'current'
    });
    expect(result.observation.coverage).toBe('complete');
    expect(result.checkpoints).toEqual([]);
    expect(result.handoffs).toEqual([]);
  });

  it('an offer is not acceptance; acceptance binds the exact actor and causal assignment', () => {
    const open = opened();
    const [offer, accept] = assignment(open);
    expect(replay([open, offer]).work[0].assignment.state).toBe('unassigned');
    expect(replay([open, offer, accept]).work[0].assignment).toMatchObject({
      version: 1,
      accepted_actor: actor,
      head_event_id: accept.event_id,
      state: 'accepted'
    });
    expect(replay([open, offer, { ...accept, actor: other }]).work[0].assignment.state).toBe(
      'unassigned'
    );
  });

  it('requires causal acceptance for active even when acceptance sorts first', () => {
    const open = opened();
    const ready = revision(2, open, { status: 'ready' });
    const [offer, accept] = assignment(open);
    const active = revision(10, ready, { status: 'active' });
    expect(replay([open, ready, offer, accept, active]).work[0].item?.status).toBe('ready');
    const valid = revision(10, ready, { status: 'active' }, [accept]);
    expect(replay([open, ready, offer, accept, valid]).work[0].item?.status).toBe('active');
  });

  it('release clears effective ownership but never completes or cancels work', () => {
    const open = opened();
    const [offer, accept] = assignment(open);
    const release = event(
      5,
      'assignment.released',
      {
        work_id: workId,
        expected_assignment_event_id: accept.event_id,
        expected_version: 1,
        reason: 'Unable to continue'
      },
      [accept]
    );
    expect(replay([open, offer, accept, release]).work[0]).toMatchObject({
      item: { status: 'proposed', owner_id: null },
      assignment: { version: 2, state: 'unassigned', accepted_actor: null, owner_id: null }
    });
  });

  it('only the accepted actor or an assignment manager may release', () => {
    const open = opened();
    const [offer, accept] = assignment(open);
    const release = event(
      5,
      'assignment.released',
      {
        work_id: workId,
        expected_assignment_event_id: accept.event_id,
        expected_version: 1,
        reason: 'release'
      },
      [accept],
      { actor: other }
    );
    const input = context([open, offer, accept, release]);
    input.trust.grants[1].capabilities = ['assignment.accept'];
    expect(replayWorkEvents(input).work[0].assignment.state).toBe('accepted');
  });

  it('accepts a fresh offer after release at the exact released head and version', () => {
    const open = opened();
    const [offer, accept] = assignment(open);
    const release = event(
      5,
      'assignment.released',
      {
        work_id: workId,
        expected_assignment_event_id: accept.event_id,
        expected_version: 1,
        reason: 'Release responsibility'
      },
      [accept]
    );
    const nextOffer = event(
      6,
      'assignment.offered',
      {
        work_id: workId,
        expected_assignment_event_id: release.event_id,
        expected_version: 2,
        target: other
      },
      [release]
    );
    const nextAccept = event(
      7,
      'assignment.accepted',
      {
        work_id: workId,
        offer_event_id: nextOffer.event_id,
        expected_assignment_event_id: release.event_id,
        expected_version: 2,
        ready_event_id: null
      },
      [nextOffer, release],
      { actor: other }
    );
    const events = [open, offer, accept, release, nextOffer, nextAccept];
    const result = replay(events);
    expect(result.work[0]).toMatchObject({
      item: { status: 'proposed', owner_id: other.logical_agent_id },
      assignment: {
        state: 'accepted',
        version: 3,
        head_event_id: nextAccept.event_id,
        accepted_actor: other
      }
    });
    expect(result.rejected).toEqual([]);
    expect(replay([...events].reverse())).toEqual(result);
    expect(replay(events.filter((e) => e !== release)).work[0].assignment).toMatchObject({
      state: 'accepted',
      version: 1,
      accepted_actor: actor
    });
    const staleOffer = { ...nextOffer, payload: { ...nextOffer.payload, expected_version: 1 } };
    const staleAccept = { ...nextAccept, payload: { ...nextAccept.payload, expected_version: 1 } };
    expect(
      replay([open, offer, accept, release, staleOffer, staleAccept]).work[0].assignment
    ).toMatchObject({ state: 'unassigned', version: 2, accepted_actor: null });
  });

  it('rejects non-handoff acceptance that attempts to replace an existing accepted assignment', () => {
    const open = opened();
    const [offer, accept] = assignment(open);
    const transferOffer = event(
      6,
      'assignment.offered',
      {
        work_id: workId,
        expected_assignment_event_id: accept.event_id,
        expected_version: 1,
        target: other
      },
      [accept]
    );
    const transferAccept = event(
      7,
      'assignment.accepted',
      {
        work_id: workId,
        offer_event_id: transferOffer.event_id,
        expected_assignment_event_id: accept.event_id,
        expected_version: 1,
        ready_event_id: null
      },
      [transferOffer, accept],
      { actor: other }
    );
    const result = replay([open, offer, accept, transferOffer, transferAccept]);
    expect(result.work[0].assignment).toMatchObject({
      state: 'accepted',
      version: 1,
      accepted_actor: actor
    });
    expect(codes(result)).toContain('HANDOFF_REQUIRED');
  });

  it('does not let revisions edit an accepted owner', () => {
    const open = opened();
    const [offer, accept] = assignment(open);
    const change = revision(6, open, { owner_id: other.logical_agent_id }, [accept]);
    expect(replay([open, offer, accept, change]).work[0].item?.owner_id).toBe(
      actor.logical_agent_id
    );
  });

  it('rejects invalid lifecycle, missing transition reasons, and identity changes', () => {
    const open = opened();
    for (const changes of [
      { status: 'active' },
      { status: 'cancelled' },
      { type: 'open_thread', status: 'open' }
    ]) {
      expect(replay([open, revision(2, open, changes)]).work[0].item?.status).toBe('proposed');
    }
    const foreign = revision(2, open, { status: 'ready' });
    foreign.workstream_id = id(999);
    expect(replay([open, foreign]).work[0].item?.status).toBe('proposed');
  });

  it('preserves completion results and rejects terminal mutation', () => {
    const open = opened();
    const ready = revision(2, open, { status: 'ready' });
    const [offer, accept] = assignment(open);
    const active = revision(5, ready, { status: 'active' }, [accept]);
    const result = {
      summary: 'Compatibility passed',
      evidence: [
        {
          criterion_id: 'criterion-1',
          artifact: {
            id: id(800),
            uri: '/tmp/report.txt',
            sha256: 'a'.repeat(64),
            version: null,
            media_type: 'text/plain',
            owner_principal_id: actor.principal_id,
            audience: 'workspace',
            portable: false
          }
        }
      ]
    };
    const done = revision(6, active, { status: 'completed', result, next_action: null });
    const changed = revision(7, done, { title: 'Changed terminal task' });
    const row = replay([open, ready, offer, accept, active, done, changed]).work[0];
    expect(row.item?.result).toEqual(result);
    expect(row.head_event_id).toBe(done.event_id);
    expect(row.validation.map((d) => d.code)).toContain('NONPORTABLE_RESULT');
  });

  it('separates acknowledgment, answer, exact-condition application and explicit unblocking', () => {
    const open = opened();
    const ready = revision(2, open, { status: 'ready' });
    const [offer, accept] = assignment(open);
    const question = structuredClone(fixture.events[2]);
    const blocked = revision(
      5,
      ready,
      {
        status: 'blocked',
        blocker: {
          condition_id: 'choice',
          kind: 'question',
          ref_id: questionId,
          unlock_condition: 'Apply target choice'
        }
      },
      [question],
      'Need decision'
    );
    const ack = event(6, 'question.acknowledged', { opened_event_id: question.event_id }, [
      question
    ]);
    const answer = event(
      7,
      'question.answered',
      {
        opened_event_id: question.event_id,
        supersedes_answer_event_id: null,
        text: 'Current target'
      },
      [question]
    );
    const apply = event(
      8,
      'question.applied',
      { answer_event_id: answer.event_id, work_id: workId, condition_id: 'choice' },
      [answer, blocked, accept]
    );
    const events = [open, ready, offer, accept, question, blocked, ack, answer];
    expect(replay(events).questions[0]).toMatchObject({
      current_answer: { event_id: answer.event_id },
      acknowledgments: [{ event_id: ack.event_id }],
      applications: []
    });
    expect(replay([...events, apply]).work[0].item?.status).toBe('blocked');
    const resume = revision(
      9,
      blocked,
      { status: 'active', blocker: null },
      [apply],
      'Decision applied'
    );
    expect(replay([...events, apply, resume]).work[0].item?.status).toBe('active');
    expect(
      replay([
        ...events,
        { ...apply, payload: { ...apply.payload, condition_id: 'wrong' } },
        resume
      ]).work[0].item?.status
    ).toBe('blocked');
    expect(
      replay([
        ...events,
        revision(9, blocked, { status: 'ready', blocker: null }, [answer], 'Answered')
      ]).work[0].item?.status
    ).toBe('blocked');
  });

  it('rejects question answers by a different decision maker and unlinked blockers', () => {
    const question = structuredClone(fixture.events[2]);
    const answer = event(
      7,
      'question.answered',
      { opened_event_id: question.event_id, supersedes_answer_event_id: null, text: 'Choice' },
      [question],
      { actor: other }
    );
    expect(replay([question, answer]).questions[0].current_answer).toBeNull();
    const open = opened();
    const ready = revision(2, open, { status: 'ready' });
    question.payload.work_ids = [];
    const blocked = revision(
      5,
      ready,
      {
        status: 'blocked',
        blocker: {
          condition_id: 'choice',
          kind: 'question',
          ref_id: questionId,
          unlock_condition: 'answer'
        }
      },
      [question],
      'Need answer'
    );
    expect(replay([open, ready, question, blocked]).work[0].item?.status).toBe('ready');
  });

  it('requires application authorization at the historical causal point, not the final assignee', () => {
    const open = opened();
    const ready = revision(2, open, { status: 'ready' });
    const [offer, accept] = assignment(open);
    const question = structuredClone(fixture.events[2]);
    const blocked = revision(
      5,
      ready,
      {
        status: 'blocked',
        blocker: {
          condition_id: 'choice',
          kind: 'question',
          ref_id: questionId,
          unlock_condition: 'answer'
        }
      },
      [question],
      'Need answer'
    );
    const answer = event(
      7,
      'question.answered',
      { opened_event_id: question.event_id, supersedes_answer_event_id: null, text: 'Choice' },
      [question]
    );
    const apply = event(
      8,
      'question.applied',
      { answer_event_id: answer.event_id, work_id: workId, condition_id: 'choice' },
      [answer, blocked, accept]
    );
    const release = event(
      9,
      'assignment.released',
      {
        work_id: workId,
        expected_assignment_event_id: accept.event_id,
        expected_version: 1,
        reason: 'Done applying'
      },
      [accept, apply]
    );
    expect(
      replay([open, ready, offer, accept, question, blocked, answer, apply, release]).questions[0]
        .applications
    ).toHaveLength(1);
    expect(
      replay([
        open,
        ready,
        offer,
        accept,
        question,
        blocked,
        answer,
        { ...apply, parents: [answer.event_id, blocked.event_id] }
      ]).questions[0].applications
    ).toEqual([]);
  });

  it.each([
    'missing evidence',
    'digest mismatch',
    'source mismatch',
    'actor mismatch',
    'missing capability',
    'unknown capability'
  ])('fails closed for %s without leaking unauthorized subject IDs', (fault) => {
    const open = opened();
    const input = context([open]);
    if (fault === 'missing evidence') input.trust.event_evidence = [];
    if (fault === 'digest mismatch') input.trust.event_evidence[0].event_digest = '0'.repeat(64);
    if (fault === 'source mismatch') input.trust.event_evidence[0].source_principal_id = 'forged';
    if (fault === 'actor mismatch') input.trust.grants[0].session_id = 'other-session';
    if (fault === 'missing capability') input.trust.grants[0].capabilities = [];
    if (fault === 'unknown capability') input.trust.grants[0].capabilities = ['superuser'];
    const result = replayWorkEvents(input);
    expect(result.work).toEqual([]);
    expect(result.observation.coverage).toBe('partial');
    expect(JSON.stringify(result)).not.toContain(workId);
  });

  it('does not trust names or absent context', () => {
    const result = replayWorkEvents({ events: [opened()], asOf: time });
    expect(result.work).toEqual([]);
    expect(result.observation.coverage).toBe('unavailable');
  });

  it('scope checks happen before event and operation identity conflict grouping', () => {
    const open = opened();
    for (const change of [{ workspace_id: id(999) }, { stream_id: id(999) }]) {
      const foreign = {
        ...open,
        ...change,
        payload: { item: { ...open.payload.item, title: 'Poison' } }
      };
      const result = replay([foreign, open]);
      expect(result.work[0].item?.title).toBe('Ship migration');
      expect(result.conflicts).toEqual([]);
    }
  });

  it('deduplicates exact retries but quarantines conflicting IDs, operations, and descendants', () => {
    const open = opened();
    const ready = revision(2, open, { status: 'ready' });
    expect(replay([open, open, ready]).work[0].history).toHaveLength(2);
    for (const changes of [{}, { event_id: id(30) }]) {
      const collision = {
        ...ready,
        ...changes,
        payload: { ...ready.payload, item: { ...ready.payload.item, title: 'Different bytes' } }
      };
      const child = revision(10, ready, { title: 'Descendant' });
      const result = replay([open, ready, collision, child, fixture.events[1]]);
      expect(result.work.find((w) => w.work_id === workId)).toMatchObject({
        state: 'conflicted',
        head_event_id: open.event_id,
        item: { status: 'proposed' }
      });
      expect(result.work.find((w) => w.work_id === fixture.events[1].subject.id)?.state).toBe(
        'current'
      );
      expect(result.conflicts.length).toBeGreaterThan(0);
    }
  });

  it('preserves last uncontested state for sibling revisions and competing acceptances', () => {
    const open = opened();
    const ready = revision(2, open, { status: 'ready' });
    const a = revision(10, ready, { title: 'A' });
    const b = revision(11, ready, { title: 'B' });
    expect(replay([open, ready, a, b]).work[0]).toMatchObject({
      state: 'conflicted',
      head_event_id: ready.event_id,
      item: { title: 'Ship migration' }
    });
    const [offer, accept] = assignment(open);
    const second = { ...accept, event_id: id(12), operation_id: id(1012) };
    expect(replay([open, offer, accept, second]).work[0]).toMatchObject({
      state: 'conflicted',
      execution_authority: 'blocked_conflict',
      assignment: { state: 'conflicted', version: 0, accepted_actor: null }
    });
  });

  it('retains multiple openings and competing answers as visible conflicts', () => {
    const open = opened();
    const duplicate = { ...open, event_id: id(20), operation_id: id(1020) };
    expect(replay([open, duplicate]).work[0]).toMatchObject({
      state: 'conflicted',
      item: null,
      head_event_id: null
    });
    const question = structuredClone(fixture.events[2]);
    const a = event(
      7,
      'question.answered',
      { opened_event_id: question.event_id, supersedes_answer_event_id: null, text: 'A' },
      [question]
    );
    const b = {
      ...a,
      event_id: id(8),
      operation_id: id(1008),
      payload: { ...a.payload, text: 'B' }
    };
    expect(replay([question, a, b]).questions[0]).toMatchObject({
      state: 'conflicted',
      current_answer: null
    });
  });

  it('does not let unrelated event ID order authorize concurrent owner changes', () => {
    const open = opened();
    const [offer, accept] = assignment(open);
    const change = revision(10, open, { owner_id: other.logical_agent_id });
    expect(replay([open, offer, accept, change]).work[0]).toMatchObject({
      state: 'conflicted',
      execution_authority: 'blocked_conflict',
      assignment: { accepted_actor: null }
    });
  });

  it('allows concurrent acceptance and a revision that inherits an unowned predecessor', () => {
    const open = opened();
    open.payload.item.owner_id = null;
    const [offer, accept] = assignment(open);
    const ready = revision(10, open, { status: 'ready' });
    const events = [open, offer, accept, ready];
    const result = replay(events);
    expect(result.conflicts).toEqual([]);
    expect(result.work[0]).toMatchObject({
      state: 'current',
      item: { status: 'ready', owner_id: actor.logical_agent_id },
      assignment: { state: 'accepted', version: 1, accepted_actor: actor }
    });
    expect(replay([...events].reverse())).toEqual(result);
  });

  it('keeps missing parents and cycles pending, without losing unrelated work', () => {
    const open = opened();
    const missing = revision(2, open, { status: 'ready' });
    missing.parents.push(id(99));
    const cycleA = {
      ...opened(),
      event_id: id(50),
      operation_id: id(1050),
      parents: [id(51)],
      subject: { type: 'work', id: id(500) }
    };
    const cycleB = { ...cycleA, event_id: id(51), operation_id: id(1051), parents: [id(50)] };
    const result = replay([open, missing, cycleA, cycleB]);
    expect(result.work.find((w) => w.work_id === workId)?.item?.status).toBe('proposed');
    expect(codes(result)).toContain('MISSING_PARENT');
    expect(codes(result)).toContain('CAUSAL_CYCLE');
    expect(result.observation.coverage).toBe('partial');
  });

  it('flags missing dependencies and dependency cycles locally', () => {
    const a = opened();
    a.payload.item.dependency_ids = [id(500)];
    expect(replay([a]).work[0].validation.map((d) => d.code)).toContain('MISSING_DEPENDENCY');
    const b = {
      ...opened(),
      event_id: id(30),
      operation_id: id(1030),
      subject: { type: 'work', id: id(500) }
    };
    b.payload.item.dependency_ids = [workId];
    expect(
      replay([a, b]).work.every((w) => w.validation.some((d) => d.code === 'DEPENDENCY_CYCLE'))
    ).toBe(true);
  });

  it('never invents completeness or clears accumulated history from a partial empty window', () => {
    const input = context([opened()]);
    input.observation.completeness_evidence_id = '';
    expect(replayWorkEvents(input).observation.coverage).toBe('partial');
    input.observation.coverage = 'unavailable';
    expect(replayWorkEvents(input).observation.coverage).toBe('unavailable');
    expect(replayWorkEvents(input).work).toHaveLength(1);
    input.observation.coverage = 'partial';
    expect(replayWorkEvents(input).work[0].provisional).toBe(true);
  });

  it('rejects malformed trust, over-limit batches, and unsafe input without throwing', () => {
    const input = context([opened()]);
    expect(replayWorkEvents({ ...input, events: Array(1001).fill(opened()) }).work).toEqual([]);
    const dangerous = JSON.parse('{"__proto__":{"capabilities":["work.write"]}}');
    expect(replayWorkEvents({ ...input, trust: dangerous }).work).toEqual([]);
    expect(() =>
      replayWorkEvents({ ...input, events: [null, { kind: 'constructor' }] })
    ).not.toThrow();
  });

  it('isolates a malformed non-JSON candidate instead of erasing independent valid history', () => {
    const input = context([opened()]);
    const cyclic = /** @type {any} */ ({});
    cyclic.self = cyclic;
    const result = replayWorkEvents({ ...input, events: [opened(), cyclic] });
    expect(result.work[0]?.item?.title).toBe('Ship migration');
    expect(codes(result)).toContain('MALFORMED_EVENT');
  });

  it('does not certify future successful observation timestamps', () => {
    const input = context([opened()]);
    input.observation.last_successful_observation_at = '2027-01-01T00:00:00Z';
    expect(replayWorkEvents(input).observation.coverage).not.toBe('complete');
  });

  it('does not round future sub-millisecond receipts into the permitted as-of instant', () => {
    const input = context([opened()]);
    input.trust.event_evidence[0].received_at = '2026-09-27T12:00:00.000001Z';
    expect(replayWorkEvents(input).work).toEqual([]);
  });

  it('replays the combined synthetic fixture with an applied decision and a still-blocked task', () => {
    const result = replay([...fixture.events, ...fixture.replay_events]);
    expect(result.work.find((w) => w.work_id === workId)).toMatchObject({
      item: { status: 'blocked' },
      assignment: { state: 'accepted', version: 1 }
    });
    expect(result.questions[0].applications).toHaveLength(1);
    expect(result.pending).toEqual([]);
    expect(result.rejected).toEqual([]);
  });

  it('makes pending descendants distinguishable from actual causal cycles', () => {
    const a = opened();
    a.parents = [id(2)];
    const b = revision(2, a, { status: 'ready' });
    const child = revision(3, b, { title: 'After cycle' });
    const result = replay([a, b, child]);
    expect(result.pending.find((d) => d.event_ids?.includes(child.event_id))?.code).toBe(
      'UNAVAILABLE_PARENT'
    );
  });

  it('is byte-equivalent across reverse order and twenty fixed seeded shuffles', () => {
    const open = opened();
    const ready = revision(2, open, { status: 'ready' });
    const [offer, accept] = assignment(open);
    const a = revision(10, ready, { title: 'A' }, [accept]);
    const b = revision(11, ready, { title: 'B' });
    const events = [open, ready, offer, accept, a, b, ...fixture.events, ...fixture.replay_events];
    const input = context(events);
    const expected = replayWorkEvents(input);
    expect(replayWorkEvents({ ...input, events: [...events].reverse() })).toEqual(expected);
    for (let seed = 1; seed <= 20; seed++) {
      let state = seed;
      const shuffled = [...events];
      for (let i = shuffled.length - 1; i > 0; i--) {
        state = (state * 1664525 + 1013904223) >>> 0;
        const j = state % (i + 1);
        [shuffled[i], shuffled[j]] = [shuffled[j], shuffled[i]];
      }
      expect(replayWorkEvents({ ...input, events: shuffled })).toEqual(expected);
    }
  });
});
