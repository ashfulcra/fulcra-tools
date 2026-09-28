import { describe, expect, it } from 'vitest';
import backlog from '../../../tests/fixtures/work-backlog-synthetic.json';
import checkpointBody from '../../../tests/fixtures/work-handoff-synthetic.json';
import { buildHandoffPackage, validateHandoffPackage } from './checkpoint.js';
import { buildWorkDigest } from './work-digest.js';
import { assessHandoffReadiness } from './handoff.js';
import {
  parseWorkNote,
  serializeWorkEvent,
  validateWorkEvent,
  workContentDigest
} from './work-contract.js';
import { replayWorkEvents } from './work-projection.js';
import { parseAnnotationNote } from './protocol.js';

const asOf = '2026-09-27T12:00:00.000Z';
const validUntil = '2026-09-27T13:00:00.000Z';
const id = (/** @type {number} */ n) => `40000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const base = backlog.events[0];
const source = base.actor;
const receiver = { ...source, logical_agent_id: 'cold-receiver', instance_id: 'cold-instance' };
/** @param {any[]} events @param {any[]} [verifications] */
function trusted(events, verifications = []) {
  return {
    workspace_id: base.workspace_id,
    allowed_stream_ids: [base.stream_id],
    grants: [
      {
        ...source,
        capabilities: [
          'work.write',
          'assignment.manage',
          'assignment.accept',
          'question.ask',
          'question.answer',
          'question.apply',
          'checkpoint.publish',
          'handoff.offer'
        ]
      },
      { ...receiver, capabilities: ['handoff.ready', 'assignment.accept'] }
    ],
    event_evidence: events.map((e, n) => {
      const valid = validateWorkEvent(e);
      if (!valid.ok) throw new Error(`invalid cold event ${n}`);
      return {
        event_id: e.event_id,
        event_digest: workContentDigest(valid.event),
        record_id: `independent-receipt-${n}`,
        source_principal_id: e.actor.principal_id,
        stream_id: e.stream_id,
        received_at: asOf
      };
    }),
    handoff_verifications: verifications
  };
}
function observation() {
  return {
    coverage: 'complete',
    as_of: asOf,
    last_successful_observation_at: asOf,
    sources: [{ stream_id: base.stream_id, status: 'complete', pending_pages: 0 }],
    gaps: [],
    errors: [],
    completeness_evidence_id: 'independent-complete-read'
  };
}
/** @param {any[]} events @param {any[]} [verifications] @param {any} [trust] */
const replay = (events, verifications = [], trust = trusted(events, verifications)) =>
  replayWorkEvents({ events, trust, observation: observation(), asOf });
/** @param {number} n @param {string} kind @param {any} payload @param {any[]} parents @param {any} [actor] */
function event(n, kind, payload, parents, actor = source) {
  return {
    ...structuredClone(base),
    event_id: id(n),
    operation_id: id(n + 1000),
    kind,
    subject: { type: 'handoff', id: id(10) },
    actor,
    occurred_at: asOf,
    parents: parents.map((p) => (typeof p === 'string' ? p : p.event_id)),
    payload
  };
}
function setup() {
  /** @type {any[]} */
  const events = [...structuredClone(backlog.events), ...structuredClone(backlog.replay_events)];
  events[1].payload.item.revisit = { at: '2026-09-26T08:00:00.000Z', condition: null };
  const exploratory = {
    ...structuredClone(base),
    event_id: id(40),
    operation_id: id(1040),
    subject: { type: 'work', id: id(41) },
    payload: {
      item: {
        ...structuredClone(events[1].payload.item),
        type: 'open_thread',
        status: 'open',
        title: 'Explore recovery edge cases',
        next_action: 'Inspect edge cases',
        revisit: null
      }
    }
  };
  events.push(exploratory);
  const body = structuredClone(checkpointBody);
  const publication = event(
    1,
    'checkpoint.published',
    {
      work_id: body.work_id,
      checkpoint_id: body.checkpoint_id,
      artifact: {
        id: id(2),
        uri: 'https://example.invalid/cold-checkpoint',
        sha256: workContentDigest(body),
        version: null,
        media_type: 'application/json',
        owner_principal_id: 'artifact-owner',
        audience: 'workspace',
        portable: true
      },
      body_digest: workContentDigest(body),
      assignment_version: body.assignment_version,
      assignment_event_id: body.assignment_event_id
    },
    [events[5]]
  );
  publication.subject = { type: 'checkpoint', id: body.checkpoint_id };
  events.push(publication);
  const built = buildHandoffPackage({
    checkpoint: body,
    publicationEvent: publication,
    events,
    trust: trusted(events),
    observation: observation(),
    recipient: receiver,
    accessRequirements: [{ resource_id: id(2), scope_id: id(3), action: 'read' }],
    asOf
  });
  expect(built.ok).toBe(true);
  if (!built.ok) throw new Error('cold fixture package did not build');
  const pkg = built.package;
  const packageDigest = workContentDigest(pkg);
  const offer = event(
    4,
    'handoff.offered',
    {
      work_id: body.work_id,
      expected_assignment_event_id: body.assignment_event_id,
      expected_version: body.assignment_version,
      checkpoint_event_id: publication.event_id,
      package_digest: packageDigest,
      target: receiver
    },
    [publication, events[5]]
  );
  const ready = event(
    5,
    'handoff.ready',
    {
      offer_event_id: offer.event_id,
      package_digest: packageDigest,
      verification_receipt_id: 'independent-artifact-receipt'
    },
    [offer],
    receiver
  );
  const accepted = event(
    6,
    'assignment.accepted',
    {
      work_id: body.work_id,
      offer_event_id: offer.event_id,
      expected_assignment_event_id: body.assignment_event_id,
      expected_version: body.assignment_version,
      ready_event_id: ready.event_id
    },
    [offer, ready, events[5]],
    receiver
  );
  accepted.subject = { type: 'work', id: body.work_id };
  const checks = {
    checked_at: asOf,
    valid_until: validUntil,
    receiver,
    package_digest: packageDigest,
    publication: {
      event_id: publication.event_id,
      artifact_id: id(2),
      body_digest: publication.payload.body_digest,
      status: 'verified',
      receipt_id: 'independent-artifact-receipt'
    },
    resources: [
      {
        resource_id: id(2),
        scope_id: id(3),
        action: 'read',
        status: 'verified',
        receipt_id: 'independent-access-receipt'
      }
    ]
  };
  const verification = {
    ready_event_id: ready.event_id,
    ready_event_digest: workContentDigest(ready),
    offer_event_id: offer.event_id,
    package_digest: packageDigest,
    checks
  };
  return {
    events: [...events, offer, ready, accepted],
    pkg,
    checks,
    verification,
    publication,
    offer,
    ready,
    accepted,
    body,
    exploratory
  };
}
/** @param {any} projection @param {import('./work-digest.js').WorkQuery} [query] */
const digest = (projection, query = 'everything_owed') =>
  buildWorkDigest({
    projection,
    viewerId: source.logical_agent_id,
    viewerRole: 'member',
    query,
    asOf,
    presence: []
  });

describe('cross-slice cold replay acceptance', () => {
  it('reconstructs assignment, answers and open obligations from only serialized notes/package plus independent evidence', () => {
    const s = setup();
    const before = replay(s.events, [s.verification]);
    const bytes = JSON.stringify({ notes: s.events.map(serializeWorkEvent), package: s.pkg });
    const cold = JSON.parse(bytes);
    expect(parseAnnotationNote(cold.notes[0]).ok).toBe(false);
    const parsed = cold.notes.map((/** @type {string} */ note) => {
      const v = parseWorkNote(note);
      expect(v.ok).toBe(true);
      if (!v.ok) throw new Error('cold parse failed');
      return v.event;
    });
    expect(validateHandoffPackage(cold.package).ok).toBe(true);
    expect(workContentDigest(cold.package)).toBe(workContentDigest(s.pkg));
    const rebuiltVerification = {
      ...s.verification,
      ready_event_digest: workContentDigest(
        parsed.find((/** @type {any} */ e) => e.event_id === s.ready.event_id)
      )
    };
    const after = replay(parsed, [rebuiltVerification]);
    expect(workContentDigest(after)).toBe(workContentDigest(before));
    const task = after.work.find((w) => w.work_id === s.body.work_id);
    expect(task?.assignment).toMatchObject({ version: 2, accepted_actor: receiver });
    expect(after.handoffs[0].state).toBe('accepted');
    expect(after.questions[0].current_answer?.payload.text).toBe('Current target');
    expect(after.questions[0].applications).toHaveLength(1);
    const obligations = digest(after).items;
    expect(
      obligations.some((x) => x.type === 'deferred' && x.reasons.includes('revisit_due'))
    ).toBe(true);
    expect(obligations.some((x) => x.type === 'open_thread' && x.status === 'open')).toBe(true);
    expect(cold.package.checkpoint.external_operations[0].status).toBe('unknown');
    expect(
      assessHandoffReadiness({
        package: cold.package,
        projection: replay(
          parsed.filter(
            (/** @type {any} */ e) =>
              !['handoff.offered', 'handoff.ready'].includes(e.kind) &&
              e.event_id !== s.accepted.event_id
          )
        ),
        receiver,
        checks: s.checks,
        asOf
      }).status
    ).toBe('ready');
    const noTrust = replayWorkEvents({ events: parsed, observation: observation(), asOf });
    expect(noTrust.work).toEqual([]);
    expect(
      assessHandoffReadiness({
        package: cold.package,
        projection: noTrust,
        receiver,
        checks: s.checks,
        asOf
      }).status
    ).not.toBe('ready');
    const noVerification = replay(parsed, []);
    expect(noVerification.work.find((w) => w.work_id === s.body.work_id)?.assignment.version).toBe(
      1
    );
    expect(noVerification.pending.some((d) => d.code === 'HANDOFF_VERIFICATION_REQUIRED')).toBe(
      true
    );
    const partialEvents = parsed.slice(-1);
    const partialObservation = {
      ...observation(),
      coverage: 'partial',
      sources: [{ stream_id: base.stream_id, status: 'partial', pending_pages: null }],
      completeness_evidence_id: null
    };
    const partial = replayWorkEvents({
      events: partialEvents,
      trust: trusted(partialEvents),
      observation: partialObservation,
      asOf
    });
    expect(partial.observation.coverage).toBe('partial');
    expect(digest(partial).clear).toBe(false);
  });
  it('keeps valid application routing when only acknowledgment variants conflict after an answer', () => {
    const events = [
      ...structuredClone(backlog.events),
      ...structuredClone(backlog.replay_events)
    ].filter((e) => e.kind !== 'question.applied');
    const opened = /** @type {any} */ (events.find((e) => e.kind === 'question.opened'));
    const ack = {
      ...structuredClone(opened),
      event_id: id(60),
      operation_id: id(1060),
      kind: 'question.acknowledged',
      parents: [opened.event_id],
      payload: { opened_event_id: opened.event_id }
    };
    const variant = { ...structuredClone(ack), occurred_at: '2026-09-26T10:03:00.000Z' };
    const projection = replay([...events, ack, variant]);
    expect(projection.questions[0].current_answer?.payload.text).toBe('Current target');
    expect(projection.questions[0].state).toBe('conflicted');
    const needs = digest(projection, 'needs_me').items;
    expect(needs.some((x) => x.type === 'awaiting_application')).toBe(true);
    expect(needs.some((x) => x.type === 'question' && x.status === 'awaiting_decision')).toBe(
      false
    );
    expect(
      digest(projection, 'lost_track').items.some((x) => x.type === 'conflicted_question')
    ).toBe(true);
  });
});
