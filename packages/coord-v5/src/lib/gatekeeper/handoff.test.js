import { describe, expect, it } from 'vitest';
import backlog from '../../../tests/fixtures/work-backlog-synthetic.json';
import checkpointBody from '../../../tests/fixtures/work-handoff-synthetic.json';
import { buildHandoffPackage } from './checkpoint.js';
import { validateWorkEvent, workContentDigest } from './work-contract.js';
import { replayWorkEvents } from './work-projection.js';
import { assessHandoffReadiness, buildReadinessEvent } from './handoff.js';

const time = '2026-09-27T12:00:00.000Z';
const later = '2026-09-27T13:00:00.000Z';
const id = (/** @type {number} */ n) => `30000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const history = [...backlog.events, ...backlog.replay_events];
const base = history[0];
const source = base.actor;
const target = { ...source, logical_agent_id: 'receiver', instance_id: 'receiver-instance' };
/** @param {any[]} events @param {any[]} [verifications] @param {string} [asOf] */
function context(events, verifications = [], asOf = time) {
  return {
    events,
    trust: {
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
        { ...target, capabilities: ['assignment.accept', 'handoff.ready'] }
      ],
      event_evidence: events.flatMap((e, n) => {
        const v = validateWorkEvent(e);
        return v.ok
          ? [
              {
                event_id: e.event_id,
                event_digest: workContentDigest(v.event),
                record_id: `record-${n}`,
                source_principal_id: e.actor.principal_id,
                stream_id: e.stream_id,
                received_at: time
              }
            ]
          : [];
      }),
      handoff_verifications: verifications
    },
    observation: {
      coverage: 'complete',
      as_of: asOf,
      last_successful_observation_at: asOf,
      sources: [{ stream_id: base.stream_id, status: 'complete', pending_pages: 0 }],
      gaps: [],
      errors: [],
      completeness_evidence_id: 'complete'
    },
    asOf
  };
}
/** @param {number} n @param {string} kind @param {any} payload @param {any[]} parents @param {any} [actor] */
function event(n, kind, payload, parents, actor = source) {
  return {
    ...structuredClone(base),
    event_id: id(n),
    operation_id: id(n + 1000),
    kind,
    subject: { type: 'handoff', id: id(10) },
    actor,
    occurred_at: time,
    parents: parents.map((e) => (typeof e === 'string' ? e : e.event_id)),
    payload
  };
}
function setup() {
  const body = structuredClone(checkpointBody);
  const publication = event(
    1,
    'checkpoint.published',
    {
      work_id: body.work_id,
      checkpoint_id: body.checkpoint_id,
      artifact: {
        id: id(2),
        uri: 'https://example.invalid/checkpoint',
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
    [history[5]]
  );
  publication.subject = { type: 'checkpoint', id: body.checkpoint_id };
  const events = [...history, publication];
  const built = buildHandoffPackage({
    ...context(events),
    checkpoint: body,
    publicationEvent: publication,
    recipient: target,
    accessRequirements: [{ resource_id: id(2), action: 'read', scope_id: id(3) }]
  });
  expect(built.ok).toBe(true);
  if (!built.ok) throw new Error('fixture package failed');
  const pkg = built.package;
  const digest = workContentDigest(pkg);
  const offer = event(
    4,
    'handoff.offered',
    {
      work_id: body.work_id,
      expected_assignment_event_id: body.assignment_event_id,
      expected_version: body.assignment_version,
      checkpoint_event_id: publication.event_id,
      package_digest: digest,
      target
    },
    [publication, history[5]]
  );
  const checks = {
    checked_at: time,
    valid_until: later,
    receiver: target,
    package_digest: digest,
    publication: {
      event_id: publication.event_id,
      artifact_id: id(2),
      body_digest: publication.payload.body_digest,
      status: 'verified',
      receipt_id: 'receipt-pub'
    },
    resources: [
      {
        resource_id: id(2),
        scope_id: id(3),
        action: 'read',
        status: 'verified',
        receipt_id: 'receipt-access'
      }
    ]
  };
  const ready = event(
    5,
    'handoff.ready',
    {
      offer_event_id: offer.event_id,
      package_digest: digest,
      verification_receipt_id: 'receipt-pub'
    },
    [offer],
    target
  );
  const accept = event(
    6,
    'assignment.accepted',
    {
      work_id: body.work_id,
      offer_event_id: offer.event_id,
      expected_assignment_event_id: body.assignment_event_id,
      expected_version: body.assignment_version,
      ready_event_id: ready.event_id
    },
    [offer, ready, history[5]],
    target
  );
  accept.subject = { type: 'work', id: body.work_id };
  return { body, publication, pkg, digest, offer, checks, ready, accept, events };
}
/** @param {any} s */
const evidenceFor = (s) => [
  {
    ready_event_id: s.ready.event_id,
    ready_event_digest: workContentDigest(s.ready),
    offer_event_id: s.offer.event_id,
    package_digest: s.digest,
    checks: s.checks
  }
];
describe('explicit handoff readiness and transfer', () => {
  function distinctArtifact() {
    const s = setup();
    s.publication.payload.artifact.sha256 = 'b'.repeat(64);
    s.pkg.events.find((e) => e.event_id === s.publication.event_id).payload.artifact.sha256 =
      'b'.repeat(64);
    s.digest = workContentDigest(s.pkg);
    s.offer.payload.package_digest = s.digest;
    s.ready.payload.package_digest = s.digest;
    s.checks.package_digest = s.digest;
    return s;
  }
  it('requires independent raw artifact integrity when canonical body and upload hashes differ', () => {
    const s = distinctArtifact();
    const input = {
      package: s.pkg,
      projection: replayWorkEvents(context(s.events)),
      receiver: target,
      checks: s.checks,
      asOf: time
    };
    expect(assessHandoffReadiness(input).status).toBe('blocked');
    s.checks.publication.artifact_sha256 = 'a'.repeat(64);
    expect(assessHandoffReadiness(input).status).toBe('blocked');
    s.checks.publication.artifact_sha256 = 'b'.repeat(64);
    expect(assessHandoffReadiness(input).status).toBe('ready');
    s.checks.publication.body_digest = 'a'.repeat(64);
    expect(assessHandoffReadiness(input).status).toBe('blocked');
  });
  it('replay rejects missing or mismatched raw artifact proof before transfer', () => {
    const s = distinctArtifact(),
      events = [...s.events, s.offer, s.ready, s.accept];
    const missing = replayWorkEvents(context(events, evidenceFor(s)));
    expect(missing.rejected.some((d) => d.code === 'HANDOFF_VERIFICATION_INVALID')).toBe(true);
    s.checks.publication.artifact_sha256 = 'b'.repeat(64);
    const accepted = replayWorkEvents(context(events, evidenceFor(s)));
    expect(
      accepted.work.find((w) => w.work_id === s.body.work_id).assignment.accepted_actor
    ).toEqual(target);
    s.checks.publication.artifact_sha256 = 'a'.repeat(64);
    expect(
      replayWorkEvents(context(events, evidenceFor(s))).rejected.some(
        (d) => d.code === 'HANDOFF_VERIFICATION_INVALID'
      )
    ).toBe(true);
  });
  it('requires independent exact checks and keeps unknown operation effects visible', () => {
    const s = setup();
    const projection = replayWorkEvents(context(s.events));
    const input = { package: s.pkg, projection, receiver: target, checks: s.checks, asOf: time };
    const result = assessHandoffReadiness(input);
    expect(result.status).toBe('ready');
    expect(result.package_digest).toBe(s.digest);
    expect(result.blocking_operations).toEqual([s.body.external_operations[0].operation_id]);
    for (const changed of [
      { ...s.checks, package_digest: 'a'.repeat(64) },
      { ...s.checks, receiver: source },
      { ...s.checks, valid_until: time },
      { ...s.checks, resources: [] },
      { ...s.checks, resources: [{ ...s.checks.resources[0], status: 'denied' }] }
    ])
      expect(assessHandoffReadiness({ ...input, checks: changed }).status).not.toBe('ready');
    expect(assessHandoffReadiness({ ...input, receiver: source }).status).toBe('blocked');
    expect(assessHandoffReadiness({ ...input, checks: null }).status).toBe('unknown');
    expect(assessHandoffReadiness({ ...input, package: { ...s.pkg, grants: [] } }).status).toBe(
      'blocked'
    );
    expect(
      assessHandoffReadiness({
        ...input,
        checks: {
          ...s.checks,
          valid_until: '2026-09-27T12:00:00.0000001Z'
        }
      }).status
    ).toBe('ready');
    expect(
      assessHandoffReadiness({
        ...input,
        checks: {
          ...s.checks,
          checked_at: '2026-02-30T12:00:00.000Z'
        }
      }).status
    ).toBe('blocked');
  });
  it('proposes a ready event without embedding checks or authority', () => {
    const s = setup();
    const assessment = assessHandoffReadiness({
      package: s.pkg,
      projection: replayWorkEvents(context(s.events)),
      receiver: target,
      checks: s.checks,
      asOf: time
    });
    const proposal = buildReadinessEvent({
      assessment,
      offerEvent: s.offer,
      eventEnvelope: { ...s.ready, payload: undefined }
    });
    expect(validateWorkEvent(proposal).ok).toBe(true);
    expect(proposal).not.toHaveProperty('checks');
    expect(proposal.payload).not.toHaveProperty('grants');
  });
  it('blocks a fresh projection when task content changed after package assembly', () => {
    const s = setup();
    const changed = {
      ...structuredClone(history[6]),
      event_id: id(91),
      operation_id: id(1091),
      parents: [history[6].event_id],
      payload: {
        expected_event_id: history[6].event_id,
        item: { ...structuredClone(history[6].payload.item), next_action: 'Reconcile new state' },
        reason: null
      }
    };
    const projection = replayWorkEvents(context([...s.events, changed]));
    const r = assessHandoffReadiness({
      package: s.pkg,
      projection,
      receiver: target,
      checks: s.checks,
      asOf: time
    });
    expect(r.status).toBe('blocked');
    expect(r.errors).toContain('WORK_CHANGED_SINCE_PACKAGE');
  });
  it('retains old responsibility before acceptance and changes it only after independently evidenced acceptance', () => {
    const s = setup();
    const before = replayWorkEvents(context([...s.events, s.offer, s.ready]));
    expect(before.work[0].assignment.version).toBe(1);
    expect(before.work[0].assignment.accepted_actor).toEqual(source);
    expect(before.handoffs[0].state).toBe('offered');
    expect(before.pending.some((d) => d.code === 'HANDOFF_VERIFICATION_REQUIRED')).toBe(true);
    const evidence = [
      {
        ready_event_id: s.ready.event_id,
        ready_event_digest: workContentDigest(s.ready),
        offer_event_id: s.offer.event_id,
        package_digest: s.digest,
        checks: s.checks
      }
    ];
    const readied = replayWorkEvents(context([...s.events, s.offer, s.ready], evidence));
    expect(readied.work[0].assignment.version).toBe(1);
    expect(readied.handoffs[0].state).toBe('ready');
    const after = replayWorkEvents(context([...s.events, s.offer, s.ready, s.accept], evidence));
    expect(after.work[0].assignment).toMatchObject({ version: 2, accepted_actor: target });
    expect(after.handoffs[0].state).toBe('accepted');
    expect(
      replayWorkEvents(context([...s.events, s.offer, s.ready, s.accept])).work[0].assignment
        .version
    ).toBe(1);
  });
  it('quarantines competing accepted transfers and retains the uncontested predecessor', () => {
    const s = setup();
    const evidence = [
      {
        ready_event_id: s.ready.event_id,
        ready_event_digest: workContentDigest(s.ready),
        offer_event_id: s.offer.event_id,
        package_digest: s.digest,
        checks: s.checks
      }
    ];
    const another = { ...structuredClone(s.accept), event_id: id(7), operation_id: id(1007) };
    const p = replayWorkEvents(
      context([...s.events, s.offer, s.ready, s.accept, another], evidence)
    );
    expect(p.work[0].assignment).toMatchObject({
      version: 1,
      state: 'conflicted',
      accepted_actor: source
    });
    expect(p.conflicts.some((d) => d.code === 'ASSIGNMENT_CONFLICT')).toBe(true);
  });
  it('rejects stale offers and terminal work without transferring responsibility', () => {
    const s = setup();
    const stale = structuredClone(s.offer);
    stale.payload.expected_version = 2;
    expect(
      replayWorkEvents(context([...s.events, stale])).rejected.some(
        (d) => d.code === 'STALE_ASSIGNMENT_HEAD'
      )
    ).toBe(true);
    const changed = {
      ...structuredClone(history[6]),
      event_id: id(90),
      operation_id: id(1090),
      parents: [history[6].event_id],
      payload: {
        expected_event_id: history[6].event_id,
        item: { ...structuredClone(history[6].payload.item), status: 'cancelled', blocker: null },
        reason: 'Cancelled'
      }
    };
    const offer = { ...structuredClone(s.offer), parents: [...s.offer.parents, changed.event_id] };
    const p = replayWorkEvents(context([...s.events, changed, offer]));
    expect(p.work[0].assignment.version).toBe(1);
    expect(p.rejected.some((d) => d.code === 'HANDOFF_WORK_REQUIRED')).toBe(true);
  });
  it('permits an independently granted assignment manager to offer without taking assignment', () => {
    const s = setup();
    const admin = { ...source, logical_agent_id: 'manager', instance_id: 'manager-instance' };
    const offer = { ...structuredClone(s.offer), actor: admin };
    const input = context([...s.events, offer]);
    input.trust.grants.push({ ...admin, capabilities: ['assignment.manage'] });
    const p = replayWorkEvents(input);
    expect(p.handoffs[0].state).toBe('offered');
    expect(p.work[0].assignment).toMatchObject({ version: 1, accepted_actor: source });
  });
  it('packages a successor checkpoint after an accepted handoff with its offer and ready ancestry', () => {
    const s = setup();
    const body = {
      ...structuredClone(s.body),
      checkpoint_id: id(24),
      assignment_version: 2,
      assignment_event_id: s.accept.event_id,
      identity: target
    };
    const publication = {
      ...structuredClone(s.publication),
      event_id: id(22),
      operation_id: id(1022),
      subject: { type: 'checkpoint', id: body.checkpoint_id },
      actor: target,
      parents: [s.accept.event_id],
      payload: {
        ...structuredClone(s.publication.payload),
        checkpoint_id: body.checkpoint_id,
        artifact: {
          ...structuredClone(s.publication.payload.artifact),
          id: id(23),
          sha256: workContentDigest(body)
        },
        body_digest: workContentDigest(body),
        assignment_version: 2,
        assignment_event_id: s.accept.event_id
      }
    };
    const events = [...s.events, s.offer, s.ready, s.accept, publication];
    const input = context(events, evidenceFor(s));
    input.trust.grants[1].capabilities.push('checkpoint.publish');
    const result = buildHandoffPackage({
      ...input,
      checkpoint: body,
      publicationEvent: publication,
      recipient: source,
      accessRequirements: []
    });
    expect(result.ok).toBe(true);
    if (result.ok)
      expect(result.package.events.map((/** @type {any} */ e) => e.event_id)).toEqual(
        expect.arrayContaining([s.offer.event_id, s.ready.event_id, s.accept.event_id])
      );
  });
  it('blocks readiness when independent relevant answer history is omitted, changed, or conflicted', () => {
    const s = setup();
    const projection = replayWorkEvents(context(s.events));
    const original = s.pkg.events.find((/** @type {any} */ e) => e.kind === 'question.answered');
    expect(original).toBeDefined();
    const variants = [];
    const missing = structuredClone(s.pkg);
    missing.events = missing.events.filter(
      (/** @type {any} */ e) => !['question.answered', 'question.applied'].includes(e.kind)
    );
    missing.tail_event_ids = missing.tail_event_ids.filter((/** @type {string} */ id) =>
      missing.events.some((/** @type {any} */ e) => e.event_id === id)
    );
    variants.push({ pkg: missing, projection });
    const changed = structuredClone(s.pkg);
    changed.events.find((/** @type {any} */ e) => e.kind === 'question.answered').payload.text =
      'Different answer';
    variants.push({ pkg: changed, projection });
    const conflict = {
      ...structuredClone(original),
      payload: {
        ...structuredClone(original.payload),
        text: 'Conflicting answer'
      }
    };
    variants.push({ pkg: s.pkg, projection: replayWorkEvents(context([...s.events, conflict])) });
    for (const variant of variants) {
      const checks = { ...s.checks, package_digest: workContentDigest(variant.pkg) };
      expect(
        assessHandoffReadiness({
          package: variant.pkg,
          projection: variant.projection,
          receiver: target,
          checks,
          asOf: time
        }).status
      ).toBe('blocked');
    }
  });
  it('keeps historical transfer but demands recheck once adapter validity expires', () => {
    const s = setup();
    const events = [...s.events, s.offer, s.ready, s.accept];
    const historical = replayWorkEvents(context(events, evidenceFor(s)));
    expect(historical.work[0].assignment.version).toBe(2);
    const expired = replayWorkEvents(context(events, evidenceFor(s), later));
    expect(expired.work[0].assignment.version).toBe(2);
    expect(expired.work[0].execution_authority).toBe('blocked_validation');
    expect(expired.work[0].validation.some((d) => d.code === 'HANDOFF_RECHECK_REQUIRED')).toBe(
      true
    );
    expect(expired.handoffs[0].state).toBe('blocked');
  });
  it('deduplicates exact replay and rejects forged verification and package fields', () => {
    const s = setup();
    const events = [...s.events, s.offer, s.ready, s.accept];
    const p = replayWorkEvents(context([...events, s.offer, s.ready, s.accept], evidenceFor(s)));
    expect(p.work[0].assignment.version).toBe(2);
    const fake = structuredClone(s.ready);
    fake.payload.verified = true;
    expect(validateWorkEvent(fake).ok).toBe(false);
    const wrong = evidenceFor(s);
    wrong[0].ready_event_digest = 'a'.repeat(64);
    const untrusted = replayWorkEvents(context(events, wrong));
    expect(untrusted.work[0].assignment.version).toBe(1);
    expect(untrusted.pending.some((d) => d.code === 'HANDOFF_VERIFICATION_REQUIRED')).toBe(true);
  });
  it('quarantines competing accepted transfers from distinct valid offers', () => {
    const s = setup();
    const offer2 = {
      ...structuredClone(s.offer),
      event_id: id(8),
      operation_id: id(1008),
      subject: { type: 'handoff', id: id(11) }
    };
    const ready2 = {
      ...structuredClone(s.ready),
      event_id: id(9),
      operation_id: id(1009),
      subject: offer2.subject,
      parents: [offer2.event_id],
      payload: { ...s.ready.payload, offer_event_id: offer2.event_id }
    };
    const accept2 = {
      ...structuredClone(s.accept),
      event_id: id(10),
      operation_id: id(1010),
      parents: [offer2.event_id, ready2.event_id, history[5].event_id],
      payload: {
        ...s.accept.payload,
        offer_event_id: offer2.event_id,
        ready_event_id: ready2.event_id
      }
    };
    const proof2 = {
      ...evidenceFor(s)[0],
      ready_event_id: ready2.event_id,
      ready_event_digest: workContentDigest(ready2),
      offer_event_id: offer2.event_id
    };
    const p = replayWorkEvents(
      context(
        [...s.events, s.offer, s.ready, s.accept, offer2, ready2, accept2],
        [...evidenceFor(s), proof2]
      )
    );
    expect(p.work[0].assignment).toMatchObject({
      version: 1,
      state: 'conflicted',
      accepted_actor: source
    });
    expect(p.handoffs.map((h) => h.state)).toEqual(['conflicted', 'conflicted']);
  });
});
