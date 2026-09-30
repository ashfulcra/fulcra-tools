import { expect, it } from 'vitest';
import backlog from '../../../tests/fixtures/work-backlog-synthetic.json';
import { validateWorkEvent, workContentDigest } from './work-contract.js';
import { replayWorkEvents } from './work-projection.js';
import * as presenceApi from './work-presence.js';
import * as rolesApi from './work-roles.js';
import { buildAuthorizedWorkView } from '../server/gatekeeper/work-view.js';
import { buildWorkDigest } from './work-digest.js';

const base = backlog.events[0];
const actor = base.actor;
const other = { ...actor, session_id: 'successor-session' };
const at = '2026-09-27T12:00:00.000Z';
const id = (n) => `60000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const caps = [
  'presence.publish',
  'role.manage',
  'role.claim',
  'role.checkpoint',
  'work.write',
  'checkpoint.publish'
];
function event(n, kind, payload, parents = [], who = actor) {
  return {
    ...base,
    event_id: id(n),
    operation_id: id(n + 1000),
    kind,
    subject: {
      type: kind.startsWith('presence.') ? 'presence' : 'role',
      id: id(kind.startsWith('presence.') ? 900 : 901)
    },
    actor: who,
    occurred_at: at,
    payload,
    parents: parents.map((e) => (typeof e === 'string' ? e : e.event_id))
  };
}
const presence = () =>
  event(1, 'presence.observed', {
    actor,
    contact_at: at,
    inbox_observed_at: at,
    inbox_coverage: 'partial',
    progress_at: null,
    checkpoint_event_id: null,
    contact_due_at: '2026-09-27T12:30:00.000Z',
    progress_due_at: null,
    checkpoint_due_at: null,
    engagement: { mode: 'session', until: '2026-09-27T14:00:00.000Z' },
    work_ids: [base.subject.id]
  });
const definition = (policy = 'exclusive') => event(2, 'role.defined', { name: 'reviewer', policy });
const claim = (n, def, who = actor, previous = null) =>
  event(
    n,
    'role.claimed',
    {
      expected_role_event_id: def.event_id,
      previous_claim_event_id: previous?.event_id ?? null,
      expires_at: '2026-09-27T13:00:00.000Z'
    },
    [def, ...(previous ? [previous] : [])],
    who
  );
function context(events) {
  return {
    events,
    trust: {
      workspace_id: base.workspace_id,
      allowed_stream_ids: [base.stream_id],
      grants: [actor, other].map((a) => ({ ...a, capabilities: [...caps] })),
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
                received_at: at
              }
            ]
          : [];
      })
    },
    observation: {
      coverage: 'complete',
      as_of: at,
      last_successful_observation_at: at,
      sources: [{ stream_id: base.stream_id, status: 'complete', pending_pages: 0 }],
      gaps: [],
      errors: [],
      completeness_evidence_id: 'complete'
    },
    asOf: at
  };
}
it('validates additive source event shapes without accepting self-spoofed presence', () => {
  expect(validateWorkEvent(presence()).ok).toBe(true);
  expect(validateWorkEvent(definition()).ok).toBe(true);
  expect(validateWorkEvent(claim(3, definition())).ok).toBe(true);
  const spoof = presence();
  spoof.payload.actor = other;
  expect(validateWorkEvent(spoof).ok).toBe(false);
});

export { event, presence, definition, claim, context, actor, other, at, id, base };

it('replays authorized role identity and self presence without creating work authority', () => {
  const def = definition(),
    c = claim(3, def);
  const p = replayWorkEvents(context([presence(), def, c]));
  expect(p.roles).toEqual([
    expect.objectContaining({
      role_id: id(901),
      name: 'reviewer',
      policy: 'exclusive',
      claim_event_ids: [c.event_id]
    })
  ]);
  expect(p.presence).toEqual([
    expect.objectContaining({ actor, contact_at: at, progress_at: null })
  ]);
  expect(p.work).toEqual([]);
});
it('does not accept presence or claims without exact capabilities and source receipts', () => {
  const input = context([presence(), definition()]);
  input.trust.grants = [];
  const p = replayWorkEvents(input);
  expect(p.presence).toEqual([]);
  expect(p.roles).toEqual([]);
  expect(p.rejected.some((d) => d.code === 'UNTRUSTED_EVENT')).toBe(true);
});
it('requires causal role definition and exact session for claim renewal', () => {
  const def = definition(),
    c = claim(3, def),
    renewal = claim(4, def, other, c);
  const p = replayWorkEvents(context([def, c, renewal]));
  expect(p.rejected.some((d) => d.code === 'ROLE_CLAIMANT_REQUIRED')).toBe(true);
  const missing = claim(5, def);
  const absent = replayWorkEvents(context([missing]));
  expect(absent.pending.some((d) => d.code === 'MISSING_PARENT')).toBe(true);
});
it('retains competing exclusive claims until explicit resolution binds all heads', () => {
  const def = definition(),
    a = claim(3, def),
    b = claim(4, def, other);
  const p = replayWorkEvents(context([def, a, b]));
  expect(p.roles[0].claim_event_ids).toEqual([a.event_id, b.event_id]);
  const resolve = event(
    5,
    'role.resolved',
    {
      expected_role_event_id: def.event_id,
      claim_event_ids: [a.event_id, b.event_id],
      retained_claim_event_ids: [b.event_id]
    },
    [def, a, b]
  );
  const done = replayWorkEvents(context([def, a, b, resolve]));
  expect(done.roles[0].claim_event_ids).toEqual([b.event_id]);
  expect(replayWorkEvents(context([resolve, b, a, def]))).toEqual(done);
  resolve.payload.claim_event_ids = [a.event_id];
  resolve.payload.retained_claim_event_ids = [];
  expect(
    replayWorkEvents(context([def, a, b, resolve])).rejected.some(
      (d) => d.code === 'ROLE_RESOLUTION_HEAD_MISMATCH'
    )
  ).toBe(true);
});
it('release binds the exact claim and rejects another session without management grant', () => {
  const def = definition(),
    a = claim(3, def),
    r = event(4, 'role.released', { claim_event_id: a.event_id, reason: null }, [a], other);
  const input = context([def, a, r]);
  input.trust.grants[1].capabilities = ['role.claim'];
  expect(replayWorkEvents(input).rejected.some((d) => d.code === 'ROLE_CLAIMANT_REQUIRED')).toBe(
    true
  );
  r.actor = actor;
  expect(replayWorkEvents(context([def, a, r])).roles[0].claim_event_ids).toEqual([]);
});

it('evaluates distinct presence clocks without polling becoming progress', () => {
  expect(typeof presenceApi.evaluateWorkPresence).toBe('function');
  const p = replayWorkEvents(context([presence()]));
  const evaluated = presenceApi.evaluateWorkPresence({
    projection: p,
    evaluated_at: '2026-09-27T12:31:00.000Z',
    max_source_age_ms: 3600000
  });
  expect(evaluated.rows[0].clocks.contact.state).toBe('overdue');
  expect(evaluated.rows[0].clocks.progress.state).toBe('unknown');
  expect(evaluated.rows[0].clocks.checkpoint.state).toBe('unknown');
  expect(evaluated.rows[0].inbox_coverage).toBe('partial');
  expect(p.as_of).toBe(at);
});
it('evaluates expired role claims as lapsed and stale sources as unknown, retaining evidence', () => {
  expect(typeof rolesApi.evaluateWorkRoles).toBe('function');
  const def = definition(),
    c = claim(3, def),
    p = replayWorkEvents(context([def, c]));
  expect(
    rolesApi.evaluateWorkRoles({ projection: p, evaluated_at: at, max_source_age_ms: 3600000 })
      .rows[0]
  ).toMatchObject({ state: 'held', routing_allowed: true });
  expect(
    rolesApi.evaluateWorkRoles({
      projection: p,
      evaluated_at: '2026-09-27T13:00:00.000Z',
      max_source_age_ms: 7200000
    }).rows[0]
  ).toMatchObject({ state: 'lapsed', routing_allowed: false });
  const stale = rolesApi.evaluateWorkRoles({
    projection: p,
    evaluated_at: '2026-09-27T13:00:00.000Z',
    max_source_age_ms: 1000
  });
  expect(stale.rows[0]).toMatchObject({
    state: 'unknown',
    claim_event_ids: [c.event_id],
    routing_allowed: false
  });
  p.observation.errors = [{ code: 'SOURCE_FAILED' }];
  expect(
    rolesApi.evaluateWorkRoles({ projection: p, evaluated_at: at, max_source_age_ms: 3600000 })
      .rows[0].state
  ).toBe('unknown');
});
it('exclusive live claims are contested while shared roles coexist', () => {
  expect(typeof rolesApi.evaluateWorkRoles).toBe('function');
  for (const [policy, state, allowed] of [
    ['exclusive', 'contested', false],
    ['shared', 'held', true]
  ]) {
    const def = definition(policy),
      p = replayWorkEvents(context([def, claim(3, def), claim(4, def, other)]));
    expect(
      rolesApi.evaluateWorkRoles({ projection: p, evaluated_at: at, max_source_age_ms: 1000 })
        .rows[0]
    ).toMatchObject({ state, routing_allowed: allowed });
  }
});
it('partial source history cannot establish global vacancy or automatic role routing', () => {
  expect(typeof rolesApi.evaluateWorkRoles).toBe('function');
  const def = definition(),
    input = context([def]);
  input.observation.coverage = 'partial';
  input.observation.completeness_evidence_id = null;
  expect(
    rolesApi.evaluateWorkRoles({
      projection: replayWorkEvents(input),
      evaluated_at: at,
      max_source_age_ms: 1000
    }).rows[0]
  ).toMatchObject({ state: 'unknown', routing_allowed: false });
  input.events.push(claim(3, def));
  const c = context(input.events);
  c.observation = input.observation;
  expect(
    rolesApi.evaluateWorkRoles({
      projection: replayWorkEvents(c),
      evaluated_at: at,
      max_source_age_ms: 1000
    }).rows[0]
  ).toMatchObject({ state: 'held', routing_allowed: false, provisional: true });
});

function published(n = 20, digest = 'a'.repeat(64)) {
  return {
    ...event(
      n,
      'checkpoint.published',
      {
        work_id: base.subject.id,
        checkpoint_id: id(800),
        artifact: {
          id: id(801),
          uri: 'https://example.invalid/checkpoint',
          sha256: digest,
          version: null,
          media_type: 'application/json',
          owner_principal_id: actor.principal_id,
          audience: 'workspace',
          portable: true
        },
        body_digest: digest,
        assignment_version: 0,
        assignment_event_id: null
      },
      [base]
    ),
    subject: { type: 'checkpoint', id: id(800) }
  };
}
it('durable role checkpoint references survive release and successor claim without transferring access', () => {
  const def = definition(),
    c = claim(3, def),
    pub = published(),
    ref = event(
      4,
      'role.checkpoint',
      {
        claim_event_id: c.event_id,
        checkpoint_event_id: pub.event_id,
        body_digest: 'a'.repeat(64)
      },
      [c, pub]
    );
  const release = event(5, 'role.released', { claim_event_id: c.event_id, reason: 'handoff' }, [
    c,
    ref
  ]);
  const next = claim(6, def, other);
  next.parents.push(release.event_id);
  const projection = replayWorkEvents(context([base, def, c, pub, ref, release, next]));
  const row = rolesApi.evaluateWorkRoles({ projection, evaluated_at: at, max_source_age_ms: 1000 })
    .rows[0];
  expect(row.checkpoint_reference).toMatchObject({
    checkpoint_event_id: pub.event_id,
    body_digest: 'a'.repeat(64),
    actor
  });
  expect(row.live_claims[0].actor).toEqual(other);
  expect(projection.work[0].assignment.accepted_actor).toBeNull();
  expect(projection.handoffs).toEqual([]);
  ref.payload.body_digest = 'b'.repeat(64);
  expect(
    replayWorkEvents(context([base, def, c, pub, ref])).rejected.some(
      (d) => d.code === 'ROLE_CHECKPOINT_DIGEST_MISMATCH'
    )
  ).toBe(true);
});
it('checkpoint reference publication must be authorized and current claimant bound', () => {
  const def = definition(),
    c = claim(3, def),
    pub = published(),
    ref = event(
      4,
      'role.checkpoint',
      {
        claim_event_id: c.event_id,
        checkpoint_event_id: pub.event_id,
        body_digest: 'a'.repeat(64)
      },
      [c, pub],
      other
    );
  expect(
    replayWorkEvents(context([base, def, c, pub, ref])).rejected.some(
      (d) => d.code === 'ROLE_CLAIMANT_REQUIRED'
    )
  ).toBe(true);
  ref.actor = actor;
  ref.occurred_at = '2026-09-27T13:00:00.000Z';
  const input = context([base, def, c, pub, ref]);
  input.asOf = ref.occurred_at;
  input.observation.as_of = ref.occurred_at;
  expect(replayWorkEvents(input).rejected.some((d) => d.code === 'ROLE_CLAIM_EXPIRED')).toBe(true);
});
it('work view evaluates authorized presence with explicit age while preserving source observation', () => {
  const principal = id(999),
    e = presence();
  e.actor = { ...actor, principal_id: principal };
  e.payload.actor = e.actor;
  const input = context([e]);
  const policy = {
    principal_id: principal,
    workspace_id: base.workspace_id,
    stream_id: base.stream_id,
    grants: [{ ...e.actor, capabilities: ['presence.publish'] }],
    work_jobs: []
  };
  const store = {
    inspect: () => ({ scope: policy }),
    accumulated: () => ({
      status: 'ready',
      events: input.events,
      event_evidence: input.trust.event_evidence,
      observation: input.observation
    })
  };
  const view = buildAuthorizedWorkView({
    store,
    policy,
    now: () => Date.parse('2026-09-27T12:31:00.000Z'),
    max_source_age_ms: 3600000
  });
  expect(view.status).toBe('ready');
  expect(view.presence).toEqual([
    expect.objectContaining({
      clocks: expect.objectContaining({ contact: expect.objectContaining({ state: 'overdue' }) })
    })
  ]);
  expect(view.evaluated_at).toBe('2026-09-27T12:31:00.000Z');
  expect(view.projection.as_of).toBe(at);
});
it('digest can evaluate positive partial presence clocks at a separate evaluation time', () => {
  const events = [...backlog.events, ...backlog.replay_events, presence()];
  const input = context(events);
  input.trust.grants[0].capabilities.push('assignment.manage', 'assignment.accept');
  input.observation.coverage = 'partial';
  input.observation.completeness_evidence_id = null;
  const projection = replayWorkEvents(input);
  const evaluation = presenceApi.evaluateWorkPresence({
    projection,
    evaluated_at: '2026-09-27T12:31:00.000Z',
    max_source_age_ms: 3600000
  });
  const digest = buildWorkDigest({
    projection,
    viewerId: actor.logical_agent_id,
    viewerRole: 'coordinator',
    query: 'lost_track',
    asOf: at,
    evaluatedAt: evaluation.evaluated_at,
    presence: presenceApi.presenceRowsForDigest(evaluation)
  });
  expect(digest.items.some((row) => row.reasons.includes('contact_overdue'))).toBe(true);
  expect(digest.clear).toBe(false);
  expect(digest.as_of).toBe(at);
});
it('rejects null required role references and future-dated role/presence facts', () => {
  const def = definition(),
    c = claim(3, def);
  c.payload.expected_role_event_id = null;
  expect(validateWorkEvent(c).ok).toBe(false);
  const future = presence();
  future.occurred_at = '2026-09-27T14:00:00.000Z';
  expect(replayWorkEvents(context([future])).presence).toEqual([]);
});
it('concurrent renewal and release remain contested until causally resolved', () => {
  const def = definition(),
    c = claim(3, def),
    renew = claim(4, def, actor, c),
    release = event(5, 'role.released', { claim_event_id: c.event_id, reason: null }, [c]);
  const p = replayWorkEvents(context([def, c, renew, release]));
  expect(
    rolesApi.evaluateWorkRoles({ projection: p, evaluated_at: at, max_source_age_ms: 1000 }).rows[0]
      .state
  ).toBe('contested');
  const resolved = event(
    6,
    'role.resolved',
    {
      expected_role_event_id: def.event_id,
      claim_event_ids: [renew.event_id],
      retained_claim_event_ids: [renew.event_id]
    },
    [def, renew, release]
  );
  expect(
    rolesApi.evaluateWorkRoles({
      projection: replayWorkEvents(context([def, c, renew, release, resolved])),
      evaluated_at: at,
      max_source_age_ms: 1000
    }).rows[0].state
  ).toBe('held');
});
it('invalid evaluation dates and age policy are blocked', () => {
  const p = replayWorkEvents(context([presence()]));
  expect(
    presenceApi.evaluateWorkPresence({
      projection: p,
      evaluated_at: '2026-09-31T12:00:00.000Z',
      max_source_age_ms: 1000
    }).status
  ).toBe('blocked');
  expect(
    rolesApi.evaluateWorkRoles({ projection: p, evaluated_at: at, max_source_age_ms: 0 }).status
  ).toBe('blocked');
});
it('malformed projection arrays block and future source success is unknown', () => {
  const p = replayWorkEvents(context([presence()]));
  delete p.presence;
  expect(
    presenceApi.evaluateWorkPresence({ projection: p, evaluated_at: at, max_source_age_ms: 1000 })
      .status
  ).toBe('blocked');
  p.observation.last_successful_observation_at = '2026-09-27T13:00:00.000Z';
  expect(
    rolesApi.evaluateWorkRoles({ projection: p, evaluated_at: at, max_source_age_ms: 1000 })
      .source_freshness
  ).toBe('unknown');
});
it('concurrent presence cannot be selected by timestamps, and role identity is immutable', () => {
  const a = presence(),
    b = presence();
  b.event_id = id(40);
  b.operation_id = id(1040);
  b.occurred_at = '2026-09-27T11:59:00.000Z';
  b.payload.contact_at = b.occurred_at;
  b.payload.inbox_observed_at = b.occurred_at;
  const p = replayWorkEvents(context([a, b]));
  expect(p.presence[0]).toMatchObject({ conflicted: true, contact_at: null });
  const def = definition(),
    changed = event(41, 'role.defined', { name: 'maintainer', policy: 'shared' }, [def]);
  expect(
    replayWorkEvents(context([def, changed])).rejected.some(
      (d) => d.code === 'ROLE_IDENTITY_IMMUTABLE'
    )
  ).toBe(true);
});
it('source receipt absence and denied checkpoint publication cannot create role resume authority', () => {
  const input = context([presence()]);
  input.trust.event_evidence = [];
  expect(replayWorkEvents(input).presence).toEqual([]);
  const def = definition(),
    c = claim(3, def),
    pub = published(),
    ref = event(
      4,
      'role.checkpoint',
      {
        claim_event_id: c.event_id,
        checkpoint_event_id: pub.event_id,
        body_digest: 'a'.repeat(64)
      },
      [c, pub]
    );
  const denied = context([base, def, c, pub, ref]);
  denied.trust.grants[0].capabilities = denied.trust.grants[0].capabilities.filter(
    (c) => c !== 'checkpoint.publish'
  );
  const p = replayWorkEvents(denied);
  expect(p.roles[0].checkpoint_references).toEqual([]);
  expect(p.pending.some((d) => d.code === 'MISSING_PARENT')).toBe(true);
});
it('substantive progress is not applied to unrelated work owned by the same actor', () => {
  const heartbeat = presence();
  heartbeat.payload.progress_at = at;
  heartbeat.payload.progress_due_at = '2026-09-27T13:00:00.000Z';
  heartbeat.payload.work_ids = [id(700)];
  const input = context([...backlog.events, ...backlog.replay_events, heartbeat]);
  input.trust.grants[0].capabilities.push('assignment.manage', 'assignment.accept');
  const projection = replayWorkEvents(input),
    evaluation = presenceApi.evaluateWorkPresence({
      projection,
      evaluated_at: at,
      max_source_age_ms: 1000
    });
  const digest = buildWorkDigest({
    projection,
    viewerId: actor.logical_agent_id,
    viewerRole: 'coordinator',
    query: 'lost_track',
    asOf: at,
    presence: presenceApi.presenceRowsForDigest(evaluation)
  });
  expect(digest.items.find((row) => row.work_id === base.subject.id).clocks.progress.state).toBe(
    'unknown'
  );
});
it('role expiry retains sub-millisecond precision', () => {
  const def = definition(),
    c = claim(3, def);
  c.payload.expires_at = '2026-09-27T12:00:00.0001Z';
  expect(validateWorkEvent(c).ok).toBe(true);
  const projection = replayWorkEvents(context([def, c]));
  expect(
    rolesApi.evaluateWorkRoles({ projection, evaluated_at: at, max_source_age_ms: 1000 }).rows[0]
      .state
  ).toBe('held');
  expect(
    rolesApi.evaluateWorkRoles({
      projection,
      evaluated_at: '2026-09-27T12:00:00.0001Z',
      max_source_age_ms: 1000
    }).rows[0].state
  ).toBe('lapsed');
});
