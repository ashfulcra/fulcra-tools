import { describe, expect, it } from 'vitest';
import fixture from '../../../tests/fixtures/work-backlog-synthetic.json';
import { validateWorkEvent, workContentDigest } from './work-contract.js';
import { buildWorkDigest } from './work-digest.js';
import { replayWorkEvents } from './work-projection.js';

const asOf = '2026-09-27T12:00:00.000Z';
const actor = fixture.events[0].actor;
/** @param {number} n */
const uid = (n) => `20000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
/** @param {string} type @param {string} status @param {any} [overrides] */
const item = (type, status, overrides = {}) => ({
  title: type,
  intent: 'Keep the commitment visible',
  type,
  status,
  owner_id: null,
  next_action: null,
  acceptance_criteria: [],
  priority: 'normal',
  dependency_ids: [],
  due_at: null,
  revisit: null,
  blocker: null,
  result: null,
  ...overrides
});
/** @param {number} n @param {any} value @param {any} [overrides] */
const row = (n, value, overrides = {}) => ({
  work_id: uid(n),
  workstream_id: uid(90),
  item: value,
  head_event_id: uid(n + 100),
  event_ids: [uid(n + 100)],
  history: [],
  assignment: {
    version: 0,
    head_event_id: null,
    owner_id: null,
    accepted_actor: null,
    state: 'unassigned'
  },
  state: 'current',
  execution_authority: 'unassigned',
  provisional: false,
  validation: [],
  branch_event_ids: [],
  ...overrides
});
const observation = (coverage = 'complete') => ({
  coverage,
  as_of: asOf,
  last_successful_observation_at: asOf,
  sources: [
    { stream_id: uid(80), status: coverage, pending_pages: coverage === 'complete' ? 0 : null }
  ],
  gaps: [],
  errors: [],
  completeness_evidence_id: coverage === 'complete' ? 'complete-receipt' : null
});
/** @param {any[]} [work] @param {any[]} [questions] @param {string} [coverage] */
const projection = (work = [], questions = [], coverage = 'complete') => ({
  schema: 'gatekeeper-work-view/1',
  workspace_id: fixture.workspace_id,
  as_of: asOf,
  observation: observation(coverage),
  work,
  questions,
  checkpoints: [],
  handoffs: [],
  conflicts: [],
  pending: [],
  rejected: []
});
/** @param {number} n @param {string[]} workIds @param {any} [answer] @param {any[]} [applications] */
const question = (n, workIds, answer = null, applications = []) => ({
  question_id: uid(n),
  workstream_id: uid(90),
  opened_event: {
    event_id: uid(n + 100),
    payload: {
      text: 'Choose a target',
      work_ids: workIds,
      decision_maker_id: 'agent-a',
      deadline_at: null
    }
  },
  event_ids: [],
  history: [],
  acknowledgments: [],
  answers: answer ? [answer] : [],
  current_answer: answer,
  applications,
  state: 'current',
  provisional: false,
  branch_event_ids: []
});
/** @param {any} p @param {import('./work-digest.js').WorkQuery} [query] @param {any} [extra] */
const run = (p, query = 'everything_owed', extra = {}) =>
  buildWorkDigest({
    projection: p,
    viewerId: 'agent-a',
    viewerRole: 'member',
    query,
    asOf,
    presence: [],
    ...extra
  });

/** @param {any[]} events */
function replayAuthorized(events) {
  return replayWorkEvents({
    events,
    trust: {
      workspace_id: fixture.workspace_id,
      allowed_stream_ids: [fixture.events[0].stream_id],
      event_evidence: events.map((event, index) => {
        const validated = validateWorkEvent(event);
        if (!validated.ok) throw new Error(`invalid fixture event ${index}`);
        return {
          event_id: event.event_id,
          event_digest: workContentDigest(validated.event),
          record_id: `receipt-${index}`,
          source_principal_id: actor.principal_id,
          stream_id: event.stream_id,
          received_at: asOf
        };
      }),
      grants: [
        {
          ...actor,
          capabilities: [
            'work.write',
            'question.ask',
            'question.answer',
            'question.apply',
            'assignment.manage',
            'assignment.accept'
          ]
        }
      ]
    },
    observation: {
      ...observation(),
      sources: [{ stream_id: fixture.events[0].stream_id, status: 'complete', pending_pages: 0 }]
    },
    asOf
  });
}

describe('pure owner-attention digest', () => {
  it('keeps the synthetic presence evidence separate from durable event candidates', () => {
    expect(fixture.digest_presence[0]).toMatchObject({
      actor,
      progress_at: null,
      coverage: 'complete'
    });
    expect(fixture.events).toHaveLength(3);
    expect(fixture.replay_events).toHaveLength(6);
  });

  it('reads applied-question and blocked-work facts from authorized replay, not raw event claims', () => {
    const events = [...fixture.events, ...fixture.replay_events];
    const p = replayAuthorized(events);
    const d = run(p, 'everything_owed', { presence: fixture.digest_presence });
    expect(d.items.map((x) => x.type)).toEqual(['task', 'deferred']);
    expect(d.items[0].reasons).toContain('blocked');
    expect(d.items[0].reasons).toContain('progress_unknown');
    expect(d.items.some((x) => x.type === 'awaiting_application')).toBe(false);
    expect(d.coverage).toBe('complete');
  });

  it('keeps competing question openings visible as unresolved unknown attention', () => {
    const first = structuredClone(fixture.events[2]);
    const second = { ...structuredClone(first), event_id: uid(800), operation_id: uid(801) };
    const p = replayAuthorized([first, second]);
    expect(p.questions[0]).toMatchObject({ state: 'conflicted', opened_event: null });
    const all = run(p);
    expect(all.items).toEqual([
      expect.objectContaining({
        id: `question:${first.subject.id}`,
        type: 'unknown_question',
        title: null,
        owner_id: null,
        next_action: null,
        reasons: ['conflicted']
      })
    ]);
    expect(all.counts).toMatchObject({ count_scope: 'observed_authorized', total: 1 });
    expect(run(p, 'lost_track').items).toHaveLength(1);
    expect(run(p, 'needs_me').items).toEqual([]);
    expect(run(p, 'needs_me', { viewerRole: 'owner' }).items).toHaveLength(1);
    expect(run(p, 'needs_me', { viewerRole: 'coordinator' }).items).toHaveLength(1);
  });

  it('keeps a missing-predecessor question pending without inventing a decision maker', () => {
    const orphan = {
      ...structuredClone(fixture.events[2]),
      event_id: uid(810),
      operation_id: uid(811),
      kind: 'question.acknowledged',
      parents: [uid(812)],
      payload: { opened_event_id: uid(812) }
    };
    const p = replayAuthorized([orphan]);
    expect(p.questions[0]).toMatchObject({ state: 'pending', opened_event: null });
    expect(run(p).items[0]).toMatchObject({
      id: `question:${orphan.subject.id}`,
      type: 'unknown_question',
      owner_id: null,
      next_action: null,
      reasons: ['pending']
    });
    expect(run(p, 'lost_track').items).toHaveLength(1);
    expect(run(p, 'needs_me', { viewerRole: 'owner' }).items).toHaveLength(1);
  });

  it('does not ask for a new answer when initial answers conflict', () => {
    const opened = structuredClone(fixture.events[2]);
    const answer = structuredClone(fixture.replay_events[4]);
    const competing = {
      ...structuredClone(answer),
      event_id: uid(820),
      operation_id: uid(821),
      payload: { ...answer.payload, text: 'Other target' }
    };
    const p = replayAuthorized([opened, answer, competing]);
    expect(p.questions[0]).toMatchObject({ state: 'conflicted', current_answer: null });
    const conflict = run(p).items[0];
    expect(conflict).toMatchObject({
      id: `question:${opened.subject.id}`,
      type: 'conflicted_question',
      status: 'conflicted',
      owner_id: null,
      next_action: null,
      reasons: ['conflicted']
    });
    expect(run(p, 'lost_track').items).toHaveLength(1);
    expect(run(p, 'needs_me').items).toEqual([]);
    expect(run(p, 'needs_me', { viewerRole: 'owner' }).items).toHaveLength(1);
  });

  it('keeps an unanswered decision actionable when only acknowledgment bytes conflict', () => {
    const opened = structuredClone(fixture.events[2]);
    const ack = {
      ...structuredClone(opened),
      event_id: uid(830),
      operation_id: uid(831),
      kind: 'question.acknowledged',
      parents: [opened.event_id],
      payload: { opened_event_id: opened.event_id }
    };
    const variant = { ...structuredClone(ack), occurred_at: '2026-09-26T10:03:00.000Z' };
    const p = replayAuthorized([opened, ack, variant]);
    expect(p.questions[0]).toMatchObject({ state: 'conflicted', current_answer: null });
    expect(p.questions[0].branch_event_ids).toEqual([ack.event_id]);
    expect(
      p.questions[0].history.filter((h) => h.disposition === 'conflicted').map((h) => h.kind)
    ).toEqual(['question.acknowledged', 'question.acknowledged']);
    const decision = run(p, 'needs_me').items[0];
    expect(decision).toMatchObject({
      id: `question:${opened.subject.id}`,
      type: 'question',
      status: 'awaiting_decision',
      owner_id: 'agent-a',
      next_action: 'Answer question'
    });
    expect(decision.reasons).toEqual(
      expect.arrayContaining(['awaiting_decision', 'conflicted', 'conflict_resolution_required'])
    );
    expect(run(p, 'lost_track').items).toHaveLength(1);
  });

  it('keeps an unanswered decision actionable when an extra acknowledgment parent is pending', () => {
    const opened = structuredClone(fixture.events[2]);
    const pending = {
      ...structuredClone(opened),
      event_id: uid(840),
      operation_id: uid(841),
      kind: 'question.acknowledged',
      parents: [opened.event_id, uid(842)],
      payload: { opened_event_id: opened.event_id }
    };
    const p = replayAuthorized([opened, pending]);
    expect(p.questions[0].opened_event?.event_id).toBe(opened.event_id);
    expect(p.questions[0].history.find((h) => h.event_id === pending.event_id)?.disposition).toBe(
      'pending'
    );
    const decision = run(p, 'needs_me').items[0];
    expect(decision).toMatchObject({
      type: 'question',
      owner_id: 'agent-a',
      next_action: 'Answer question'
    });
    expect(decision.reasons).toContain('acknowledgment_pending');
  });

  it('rejects non-string viewer IDs instead of coercing them into identities', () => {
    for (const viewerId of [42, ['agent-a'], null]) {
      const d = run(projection(), 'needs_me', { viewerId });
      expect(d.errors).toContainEqual({ code: 'INVALID_VIEWER' });
      expect(d.clear).toBe(false);
    }
  });
  it('keeps exploratory, deferred, unassigned, and blocked work distinct and sorted by priority then ID', () => {
    const thread = row(1, item('open_thread', 'open'));
    const deferred = row(
      2,
      item('deferred', 'deferred', { revisit: { at: '2026-09-26T00:00:00.000Z', condition: null } })
    );
    const unowned = row(
      3,
      item('task', 'ready', {
        next_action: 'Accept it',
        acceptance_criteria: [{ id: 'done', text: 'Done' }]
      })
    );
    const blocked = row(
      4,
      item('task', 'blocked', {
        owner_id: 'agent-a',
        next_action: 'Apply decision',
        acceptance_criteria: [{ id: 'done', text: 'Done' }],
        priority: 'urgent',
        blocker: {
          condition_id: 'choice',
          kind: 'question',
          ref_id: uid(9),
          unlock_condition: 'Apply answer'
        }
      })
    );
    const d = run(projection([unowned, deferred, thread, blocked]));
    expect(d.items.map((x) => [x.id, x.type, x.next_action])).toEqual([
      [`work:${uid(4)}`, 'task', 'Apply decision'],
      [`work:${uid(1)}`, 'open_thread', null],
      [`work:${uid(2)}`, 'deferred', null],
      [`work:${uid(3)}`, 'task', 'Accept it']
    ]);
    expect(d.items.find((x) => x.id === `work:${uid(2)}`).reasons).toContain('revisit_due');
    expect(d.items.find((x) => x.id === `work:${uid(3)}`).reasons).toContain('unassigned');
    expect(d.items.find((x) => x.id === `work:${uid(4)}`).reasons).toContain('blocked');
    expect(d.counts).toMatchObject({ count_scope: 'observed_authorized', total: 4 });
    expect(d.clear).toBe(false);
  });

  it('routes decisions separately from answers awaiting exact per-work application', () => {
    const blocked = row(
      4,
      item('task', 'blocked', {
        owner_id: 'worker-b',
        next_action: 'Apply answer',
        acceptance_criteria: [{ id: 'done', text: 'Done' }],
        blocker: {
          condition_id: 'choice',
          kind: 'question',
          ref_id: uid(9),
          unlock_condition: 'Apply answer'
        }
      }),
      {
        assignment: {
          version: 1,
          head_event_id: uid(60),
          owner_id: 'worker-b',
          accepted_actor: { ...actor, logical_agent_id: 'worker-b' },
          state: 'accepted'
        }
      }
    );
    const open = question(9, [uid(4)]);
    expect(run(projection([blocked], [open]), 'needs_me').items.map((x) => x.type)).toEqual([
      'question'
    ]);
    const answer = { event_id: uid(70), payload: { text: 'Current target' } };
    const answered = question(9, [uid(4)], answer);
    expect(
      run(projection([blocked], [answered]), 'everything_owed').items.find(
        (x) => x.type === 'awaiting_application'
      )
    ).toMatchObject({ id: `application:${uid(9)}:${uid(4)}`, next_action: 'Apply answer' });
    expect(run(projection([blocked], [answered]), 'needs_me').items).toEqual([]);
    expect(
      run(projection([blocked], [answered]), 'needs_me', { viewerId: 'worker-b' }).items.map(
        (x) => x.type
      )
    ).toEqual(['awaiting_application', 'task']);
    const applied = question(9, [uid(4)], answer, [
      { payload: { answer_event_id: answer.event_id, work_id: uid(4), condition_id: 'choice' } }
    ]);
    expect(
      run(projection([blocked], [applied]), 'everything_owed').items.some(
        (x) => x.type === 'awaiting_application'
      )
    ).toBe(false);
  });

  it('does not invent an application for a linked work item without the matching blocker', () => {
    const plain = row(
      4,
      item('task', 'ready', {
        owner_id: 'worker-b',
        next_action: 'Build',
        acceptance_criteria: [{ id: 'done', text: 'Done' }]
      })
    );
    const answered = question(9, [uid(4)], {
      event_id: uid(70),
      payload: { text: 'Current target' }
    });
    expect(run(projection([plain], [answered])).items.map((x) => x.type)).toEqual(['task']);
  });

  it('acknowledging or reading a question leaves its decision open', () => {
    const open = question(9, []);
    /** @type {any} */ (open).acknowledgments = [{ event_id: uid(70) }];
    expect(run(projection([], [open]), 'needs_me').items[0]).toMatchObject({
      type: 'question',
      status: 'awaiting_decision'
    });
    expect(run(projection([], [open]), 'lost_track').items).toEqual([]);
    /** @type {any} */ (open.opened_event.payload).deadline_at = '2026-09-26T12:00:00.000Z';
    expect(run(projection([], [open]), 'lost_track').items[0].reasons).toContain(
      'decision_overdue'
    );
  });

  it('shows unowned/conflicted work to owner or coordinator only on trusted caller role', () => {
    const unowned = row(
      2,
      item('task', 'ready', {
        next_action: 'Assign',
        acceptance_criteria: [{ id: 'done', text: 'Done' }]
      })
    );
    const conflicted = row(
      3,
      item('task', 'ready', {
        owner_id: 'worker-b',
        next_action: 'Resolve',
        acceptance_criteria: [{ id: 'done', text: 'Done' }]
      }),
      { state: 'conflicted', execution_authority: 'blocked_conflict' }
    );
    const p = projection([unowned, conflicted]);
    expect(run(p, 'needs_me').items).toEqual([]);
    expect(run(p, 'needs_me', { viewerRole: 'owner' }).items.map((x) => x.id)).toEqual([
      `work:${uid(2)}`,
      `work:${uid(3)}`
    ]);
    expect(run(p, 'needs_me', { viewerRole: 'coordinator' }).items).toHaveLength(2);
  });

  it('keeps a conflicted opening visible without inventing its missing item fields', () => {
    const conflict = row(7, null, {
      state: 'conflicted',
      execution_authority: 'blocked_conflict',
      head_event_id: null,
      branch_event_ids: [uid(107), uid(108)]
    });
    const p = projection([conflict]);
    expect(run(p).items[0]).toMatchObject({
      id: `work:${uid(7)}`,
      type: 'unknown_work',
      title: null,
      next_action: null,
      reasons: ['conflicted']
    });
    expect(run(p, 'needs_me', { viewerRole: 'coordinator' }).items).toHaveLength(1);
  });

  it('uses separately observed contact, progress, and checkpoint clocks without treating contact as progress', () => {
    const assigned = row(
      5,
      item('task', 'active', {
        owner_id: 'agent-a',
        next_action: 'Ship',
        acceptance_criteria: [{ id: 'done', text: 'Done' }]
      }),
      {
        assignment: {
          version: 1,
          head_event_id: uid(61),
          owner_id: 'agent-a',
          accepted_actor: actor,
          state: 'accepted'
        }
      }
    );
    const presence = [
      {
        actor,
        contact_at: asOf,
        inbox_observed_at: asOf,
        progress_at: null,
        checkpoint_at: null,
        contact_due_at: '2026-09-26T12:00:00.000Z',
        progress_due_at: null,
        checkpoint_due_at: null,
        coverage: 'complete'
      }
    ];
    const x = run(projection([assigned]), 'lost_track', { presence }).items[0];
    expect(x.reasons).toContain('progress_unknown');
    expect(x.reasons).not.toContain('contact_overdue');
    expect(x.clocks.contact.state).toBe('current');
    expect(x.clocks.progress.state).toBe('unknown');
    const silent = { ...presence[0], contact_at: '2026-09-25T12:00:00.000Z' };
    expect(
      run(projection([assigned]), 'lost_track', { presence: [silent] }).items[0].reasons
    ).toContain('contact_overdue');
    const future = { ...presence[0], contact_at: '2026-09-28T12:00:00.000Z' };
    const invalid = run(projection([assigned]), 'lost_track', { presence: [future] });
    expect(invalid.gaps).toContainEqual({ code: 'INVALID_PRESENCE' });
    expect(invalid.items[0].clocks.contact.state).toBe('unknown');
    const missing = { ...presence[0], contact_at: null, progress_at: asOf, progress_due_at: null };
    const unknown = run(projection([assigned]), 'lost_track', { presence: [missing] }).items[0];
    expect(unknown.reasons).toContain('contact_unknown');
    expect(unknown.reasons).toContain('progress_unknown');
    expect(unknown.reasons).not.toContain('contact_overdue');
    const duplicate = run(projection([assigned]), 'lost_track', {
      presence: [presence[0], future, presence[0]]
    });
    expect(duplicate.gaps).toContainEqual({ code: 'INVALID_PRESENCE' });
    expect(duplicate.items[0].clocks.contact.state).toBe('unknown');
  });

  it('uses explicit completion event time for half-open history and retains result evidence', () => {
    const artifact = {
      id: uid(50),
      uri: 'https://example.com/result',
      sha256: 'a'.repeat(64),
      version: null,
      media_type: 'text/plain',
      owner_principal_id: 'principal-a',
      audience: 'workspace',
      portable: true
    };
    const completed = row(
      6,
      item('task', 'completed', {
        owner_id: 'agent-a',
        result: { summary: 'Delivered', evidence: [{ criterion_id: 'done', artifact }] },
        acceptance_criteria: [{ id: 'done', text: 'Done' }]
      }),
      {
        head_event_id: uid(106),
        history: [
          {
            event_id: uid(106),
            kind: 'work.revised',
            disposition: 'applied',
            event: {
              event_id: uid(106),
              occurred_at: '2026-09-27T10:00:00.000Z',
              payload: { item: { status: 'completed' } }
            }
          }
        ]
      }
    );
    const p = projection([completed]);
    expect(run(p, 'everything_owed').items).toEqual([]);
    expect(
      run(p, 'completed_history', {
        from: '2026-09-27T10:00:00.000Z',
        to: '2026-09-27T11:00:00.000Z'
      }).items[0]
    ).toMatchObject({
      type: 'completed_task',
      completed_at: '2026-09-27T10:00:00.000Z',
      result: { summary: 'Delivered', evidence: [{ criterion_id: 'done', artifact }] }
    });
    expect(run(p, 'completed_history', { to: '2026-09-27T10:00:00.000Z' }).items).toEqual([]);
    expect(run(p, 'completed_history', { from: '2026-09-27T10:00:00.001Z' }).items).toEqual([]);
  });

  it('never calls an empty partial or unavailable read clear, and keeps known partial rows', () => {
    expect(run(projection([], [], 'partial')).clear).toBe(false);
    expect(run(projection([], [], 'unavailable')).clear).toBe(false);
    const known = row(1, item('open_thread', 'open'));
    expect(run(projection([known], [], 'partial')).items).toHaveLength(1);
    expect(run(projection()).clear).toBe(true);
  });

  it('does not carry a complete projection claim forward to a later digest as-of', () => {
    const later = '2026-09-28T12:00:00.000Z';
    const known = row(1, item('open_thread', 'open'));
    const d = run(projection([known]), 'everything_owed', { asOf: later });
    expect(d.items).toHaveLength(1);
    expect(d.coverage).toBe('partial');
    expect(d.clear).toBe(false);
    expect(d.gaps).toContainEqual({ code: 'OBSERVATION_AS_OF_MISMATCH' });
    const earlier = run(projection([known]), 'everything_owed', {
      asOf: '2026-09-26T12:00:00.000Z'
    });
    expect(earlier.items).toEqual([]);
    expect(earlier.errors).toContainEqual({ code: 'AS_OF_BEFORE_PROJECTION' });
    expect(earlier.clear).toBe(false);
  });

  it('does not forward opaque source metadata or private candidate claims', () => {
    const p = projection([row(1, item('open_thread', 'open'))]);
    /** @type {any} */ (p).rejected = [
      { code: 'DENIED_SCOPE', private_note: 'secret', subject_id: 'private-subject' }
    ];
    /** @type {any} */ (p.observation.sources[0]).private_token = 'secret';
    const json = JSON.stringify(run(p));
    expect(json).not.toContain('secret');
    expect(json).not.toContain('private-subject');
  });
});
