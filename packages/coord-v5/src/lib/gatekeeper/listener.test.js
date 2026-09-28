import { describe, expect, it } from 'vitest';
import fixture from '../../../tests/fixtures/conversation.json';
import { advanceListenerPolicy, evaluateWake, buildSuccessorResumeBundle } from './listener.js';
import { replayEvents } from './projection.js';

const conversationId = fixture.conversation_id;
const otherConversation = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee';
const workId = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const questionId = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';
/** @param {number} hours */
const at = (hours) =>
  new Date(Date.parse('2026-09-26T10:00:00Z') + hours * 3_600_000).toISOString();
/** @param {string} id @param {string} kind @param {Record<string, any>} payload @param {Record<string, any>} [extra] */
const event = (id, kind, payload, extra = {}) => ({
  ...fixture,
  event_id: id,
  kind,
  payload,
  created_at: at(0),
  ...extra
});
const clearWake = evaluateWake({
  eventObservation: { coverage: 'complete', events: [] },
  obligationObservation: { coverage: 'complete', obligations: [] }
});
const workWake = evaluateWake({
  eventObservation: { coverage: 'complete', events: [fixture] },
  obligationObservation: { coverage: 'complete', obligations: [] }
});

describe('listener policy', () => {
  const enrolledAt = at(0);
  it.each([
    [0.99, 15],
    [1, 30],
    [3.99, 30],
    [4, 60],
    [23.99, 60],
    [24, 180]
  ])('uses the %s-hour idle tier of %s minutes after a successful empty read', (hours, minutes) => {
    const result = advanceListenerPolicy(
      { enrolledAt, lastWorkAt: enrolledAt, policyIntervalMinutes: 15 },
      { status: 'success', wake: clearWake },
      at(hours)
    );
    expect(result.policyIntervalMinutes).toBe(minutes);
    expect(result.lastSuccessfulObservationAt).toBe(at(hours));
  });

  it('backs off past a previous policy interval but retains a faster operator cap', () => {
    const base = { enrolledAt, lastWorkAt: enrolledAt, policyIntervalMinutes: 15 };
    expect(
      advanceListenerPolicy(base, { status: 'success', wake: clearWake }, at(24))
        .policyIntervalMinutes
    ).toBe(180);
    expect(
      advanceListenerPolicy(
        { ...base, operatorIntervalMinutes: 10 },
        { status: 'success', wake: clearWake },
        at(24)
      ).nextIntervalMinutes
    ).toBe(10);
  });

  it('does not back off or advance successful-observation state on failed reads', () => {
    const previous = {
      enrolledAt,
      lastWorkAt: enrolledAt,
      lastSuccessfulObservationAt: at(1),
      policyIntervalMinutes: 30
    };
    expect(advanceListenerPolicy(previous, { status: 'error' }, at(24))).toEqual({
      ...previous,
      nextIntervalMinutes: 30
    });
  });

  it('resets quiet time from work arrival and caps active work or a deadline at 15 minutes', () => {
    const state = { enrolledAt, lastWorkAt: enrolledAt, policyIntervalMinutes: 180 };
    const arrival = advanceListenerPolicy(
      state,
      { status: 'success', wake: workWake, newlyObservedWorkAt: at(25) },
      at(25)
    );
    expect(arrival.lastWorkAt).toBe(at(25));
    expect(arrival.policyIntervalMinutes).toBe(15);
    expect(
      advanceListenerPolicy(
        state,
        { status: 'success', wake: clearWake, hasActiveWork: true },
        at(25)
      ).nextIntervalMinutes
    ).toBe(15);
    expect(
      advanceListenerPolicy(
        state,
        { status: 'success', wake: clearWake, deadlineApproaching: true },
        at(25)
      ).nextIntervalMinutes
    ).toBe(15);
  });

  it('uses enrollment as baseline and never treats invalid or future clocks as proof of inactivity', () => {
    expect(
      advanceListenerPolicy(
        { enrolledAt, policyIntervalMinutes: 15 },
        { status: 'success', wake: clearWake },
        at(24)
      ).policyIntervalMinutes
    ).toBe(180);
    expect(
      advanceListenerPolicy(
        { enrolledAt: 'bad', policyIntervalMinutes: 15 },
        { status: 'success', wake: clearWake },
        at(24)
      ).policyIntervalMinutes
    ).toBe(15);
    expect(
      advanceListenerPolicy(
        { enrolledAt: '2026-02-31T10:00:00Z', policyIntervalMinutes: 15 },
        { status: 'success', wake: clearWake },
        at(24)
      ).policyIntervalMinutes
    ).toBe(15);
    expect(
      advanceListenerPolicy(
        { enrolledAt, lastWorkAt: at(25), policyIntervalMinutes: 15 },
        { status: 'success', wake: clearWake },
        at(24)
      ).policyIntervalMinutes
    ).toBe(15);
  });

  it('does not back off or advance the success clock on partial, unknown, or unsubstantiated reads', () => {
    const previous = {
      enrolledAt,
      lastWorkAt: enrolledAt,
      lastSuccessfulObservationAt: at(1),
      policyIntervalMinutes: 30
    };
    const partialWake = evaluateWake({
      eventObservation: { coverage: 'partial', events: [] },
      obligationObservation: { coverage: 'complete', obligations: [] }
    });
    const expected = { ...previous, nextIntervalMinutes: 30 };
    expect(
      advanceListenerPolicy(previous, { status: 'success', wake: partialWake }, at(24))
    ).toEqual(expected);
    expect(advanceListenerPolicy(previous, { status: 'success' }, at(24))).toEqual(expected);
    expect(
      advanceListenerPolicy(
        previous,
        /** @type {any} */ ({ status: 'success', actionable: false }),
        at(24)
      )
    ).toEqual(expected);
  });

  it('keeps old outstanding obligations fast without re-dating last work arrival', () => {
    const previous = { enrolledAt, lastWorkAt: enrolledAt, policyIntervalMinutes: 60 };
    const result = advanceListenerPolicy(
      previous,
      { status: 'success', wake: workWake, hasActiveWork: true },
      at(24)
    );
    expect(result.lastWorkAt).toBe(enrolledAt);
    expect(result.policyIntervalMinutes).toBe(60);
    expect(result.nextIntervalMinutes).toBe(15);
    expect(
      advanceListenerPolicy(
        previous,
        { status: 'success', wake: workWake, newlyObservedWorkAt: at(25) },
        at(24)
      ).lastWorkAt
    ).toBe(enrolledAt);
  });

  it('does not move a success clock backward or infer inactivity from a future prior read', () => {
    const previous = {
      enrolledAt,
      lastWorkAt: enrolledAt,
      lastSuccessfulObservationAt: at(25),
      policyIntervalMinutes: 30
    };
    expect(advanceListenerPolicy(previous, { status: 'success', wake: clearWake }, at(24))).toEqual(
      { ...previous, nextIntervalMinutes: 30 }
    );
  });
});

describe('unified wake read', () => {
  it('clears only when both event and obligation coverage are complete and empty', () => {
    expect(
      evaluateWake({
        eventObservation: { coverage: 'complete', events: [] },
        obligationObservation: { coverage: 'complete', obligations: [] }
      }).status
    ).toBe('clear');
    expect(
      evaluateWake({
        eventObservation: { coverage: 'partial', events: [] },
        obligationObservation: { coverage: 'complete', obligations: [] }
      }).status
    ).toBe('unknown');
    expect(
      evaluateWake({
        eventObservation: { coverage: 'complete', events: [] },
        obligationObservation: { coverage: 'unavailable', obligations: [] }
      }).status
    ).toBe('unknown');
  });

  it('keeps outstanding obligations actionable despite an empty event read', () => {
    const result = evaluateWake({
      eventObservation: { coverage: 'complete', events: [] },
      obligationObservation: { coverage: 'complete', obligations: [{ work_id: workId }] }
    });
    expect(result.status).toBe('actionable');
    expect(result.obligations).toEqual([{ work_id: workId }]);
  });
});

describe('successor resume', () => {
  const opened = event('22222222-2222-4222-8222-222222222222', 'work.opened', {
    work_id: workId,
    title: 'Build'
  });
  const checkpoint = event(
    '33333333-3333-4333-8333-333333333333',
    'checkpoint.published',
    { work_id: workId, artifact: 'https://example.com/checkpoint', verified: true },
    { created_at: at(2) }
  );
  const question = event(
    '44444444-4444-4444-8444-444444444444',
    'question.opened',
    { question_id: questionId, text: 'Next?' },
    { created_at: at(3) }
  );
  const late = event(
    '55555555-5555-4555-8555-555555555555',
    'message',
    { text: 'Late arrival', speaker: 'agent' },
    { created_at: at(1) }
  );

  it('preserves logical identity and all accepted scoped events as deduplicated full replay', () => {
    const foreign = {
      ...fixture,
      conversation_id: otherConversation,
      event_id: '66666666-6666-4666-8666-666666666666'
    };
    const result = buildSuccessorResumeBundle({
      conversationId,
      logicalIdentity: 'agent:gatekeeper',
      previousSessionId: 'old-runtime',
      successorSessionId: 'new-runtime',
      events: [opened, checkpoint, question, late, late, foreign],
      accessRequirements: [
        { resource: 'team/public', action: 'read', scope: 'approved', token: { secret: 'NO' } }
      ]
    });
    expect(result.logicalIdentity).toBe('agent:gatekeeper');
    expect(result.runtime).toEqual({
      previousSessionId: 'old-runtime',
      successorSessionId: 'new-runtime'
    });
    expect(result.replayMode).toBe('full_replay');
    expect(result.requiresEventIdDedupe).toBe(true);
    expect(result.events.map((item) => item.event_id)).toEqual([
      opened.event_id,
      late.event_id,
      checkpoint.event_id,
      question.event_id
    ]);
    expect(result.latestVerifiedCheckpoint?.event_id).toBe(checkpoint.event_id);
    expect(result.outstandingWork[0].status).toBe('proposed');
    expect(result.openQuestions[0].text).toBe('Next?');
    expect(result.accessRequirements).toEqual([
      { resource: 'team/public', action: 'read', scope: 'approved' }
    ]);
    expect(JSON.stringify(result)).not.toContain('NO');
  });

  it('does not include conflicted, unresolved, foreign, or unverified checkpoints', () => {
    const bad = { ...late, payload: { text: 'conflict', speaker: 'agent' } };
    const unverified = {
      ...checkpoint,
      event_id: '77777777-7777-4777-8777-777777777777',
      payload: { ...checkpoint.payload, verified: false }
    };
    const result = buildSuccessorResumeBundle({
      conversationId,
      logicalIdentity: 'agent:gatekeeper',
      previousSessionId: 'old',
      successorSessionId: 'new',
      events: [opened, late, bad, unverified]
    });
    expect(result.events.map((item) => item.event_id)).toEqual([
      opened.event_id,
      unverified.event_id
    ]);
    expect(result.latestVerifiedCheckpoint).toBeNull();
  });

  it('excludes foreign diagnostics and extra event fields from the scoped bundle', () => {
    const foreign = event(
      '88888888-8888-4888-8888-888888888888',
      'work.updated',
      { work_id: workId, status: 'active', expected_event_id: opened.event_id },
      { conversation_id: otherConversation }
    );
    const withExtra = {
      ...opened,
      credential: { token: 'HIDDEN' },
      payload: { ...opened.payload, credential: { token: 'HIDDEN' } }
    };
    const result = buildSuccessorResumeBundle({
      conversationId,
      logicalIdentity: 'agent:gatekeeper',
      previousSessionId: 'old',
      successorSessionId: 'new',
      events: [withExtra, foreign]
    });
    expect(result.coverage.unresolved).toEqual([]);
    expect(JSON.stringify(result)).not.toContain('HIDDEN');
  });

  it('does not defer an applied local event because a malformed foreign event reuses its ID', () => {
    const malformedForeign = {
      ...opened,
      conversation_id: otherConversation,
      protocol: 'gatekeeper/2',
      payload: { work_id: workId, title: 'Private' }
    };
    const malformedLocal = {
      ...opened,
      event_id: 'eeeeeeee-1111-4111-8111-111111111111',
      protocol: 'gatekeeper/2'
    };
    const result = buildSuccessorResumeBundle({
      conversationId,
      logicalIdentity: 'agent:gatekeeper',
      previousSessionId: 'old',
      successorSessionId: 'new',
      events: [opened, malformedForeign, malformedLocal]
    });
    expect(result.events.map((item) => item.event_id)).toEqual([opened.event_id]);
    expect(result.deferredReplayEvents).toEqual([]);
    expect(result.coverage.unresolved).toEqual([
      { event_id: malformedLocal.event_id, reason: 'unsupported protocol' }
    ]);
    expect(JSON.stringify(result)).not.toContain('Private');
  });

  it('does not expose foreign conflict IDs in scoped recovery diagnostics', () => {
    const competing = event(
      '99999999-9999-4999-8999-999999999999',
      'work.opened',
      { work_id: workId, title: 'Foreign' },
      { conversation_id: otherConversation }
    );
    const result = buildSuccessorResumeBundle({
      conversationId,
      logicalIdentity: 'agent:gatekeeper',
      previousSessionId: 'old',
      successorSessionId: 'new',
      events: [opened, competing]
    });
    expect(result.coverage.conflicts).toEqual([
      { type: 'competing_openings', event_ids: [opened.event_id] }
    ]);
    expect(JSON.stringify(result)).not.toContain(competing.event_id);
  });

  it('carries unresolved original bodies so a successor can apply them after a missing parent arrives', () => {
    const missingParent = event('aaaaaaaa-1111-4111-8111-111111111111', 'work.opened', {
      work_id: workId,
      title: 'Recovered'
    });
    const child = event('bbbbbbbb-1111-4111-8111-111111111111', 'work.updated', {
      work_id: workId,
      status: 'active',
      expected_event_id: missingParent.event_id
    });
    const bundle = buildSuccessorResumeBundle({
      conversationId,
      logicalIdentity: 'agent:gatekeeper',
      previousSessionId: 'old',
      successorSessionId: 'new',
      events: [child]
    });
    expect(bundle.events).toEqual([]);
    expect(bundle.deferredReplayEvents.map((item) => item.event_id)).toEqual([child.event_id]);
    const serialized = /** @type {typeof bundle} */ (JSON.parse(JSON.stringify(bundle)));
    const resumed = replayEvents([
      ...serialized.events,
      ...serialized.deferredReplayEvents,
      missingParent
    ]);
    expect(resumed.work[workId].status).toBe('active');
  });

  it('defers a local event with foreign causation without leaking the foreign body into the serialized bundle', () => {
    const foreign = event(
      'aaaaaaaa-3333-4333-8333-333333333333',
      'message',
      { text: 'Private foreign context', speaker: 'agent' },
      { conversation_id: otherConversation }
    );
    const local = { ...opened, causation_id: foreign.event_id };
    const bundle = buildSuccessorResumeBundle({
      conversationId,
      logicalIdentity: 'agent:gatekeeper',
      previousSessionId: 'old',
      successorSessionId: 'new',
      events: [foreign, local]
    });
    const serialized = /** @type {typeof bundle} */ (JSON.parse(JSON.stringify(bundle)));
    expect(serialized.events).toEqual([]);
    expect(serialized.deferredReplayEvents.map((item) => item.event_id)).toEqual([local.event_id]);
    expect(serialized.outstandingWork).toEqual([]);
    expect(serialized.coverage.unresolved).toContainEqual({ event_id: local.event_id, reason: 'missing or unsatisfied dependency' });
    expect(JSON.stringify(serialized)).not.toContain('Private foreign context');
    expect(replayEvents(serialized.deferredReplayEvents).work[workId]).toBeUndefined();
  });

  it('quarantines conflicting variants without a silent winner or foreign payload leak', () => {
    const variant = { ...opened, payload: { work_id: workId, title: 'Other title' } };
    const foreign = {
      ...opened,
      conversation_id: otherConversation,
      event_id: 'cccccccc-1111-4111-8111-111111111111',
      payload: { work_id: workId, title: 'Foreign secret' }
    };
    const bundle = buildSuccessorResumeBundle({
      conversationId,
      logicalIdentity: 'agent:gatekeeper',
      previousSessionId: 'old',
      successorSessionId: 'new',
      events: [opened, opened, variant, foreign]
    });
    expect(bundle.events).toEqual([]);
    expect(bundle.quarantinedEvents.map((item) => item.payload.title)).toEqual([
      'Build',
      'Other title'
    ]);
    expect(bundle.outstandingWork).toEqual([]);
    expect(JSON.stringify(bundle)).not.toContain('Foreign secret');
    expect(bundle.replayCollections).toEqual([
      'events',
      'deferredReplayEvents',
      'quarantinedEvents'
    ]);
  });

  it('quarantines a scoped cross-conversation conflict rather than resurrecting it in scoped replay', () => {
    const foreign = {
      ...opened,
      conversation_id: otherConversation,
      event_id: 'dddddddd-1111-4111-8111-111111111111',
      payload: { work_id: workId, title: 'Private foreign' }
    };
    const bundle = buildSuccessorResumeBundle({
      conversationId,
      logicalIdentity: 'agent:gatekeeper',
      previousSessionId: 'old',
      successorSessionId: 'new',
      events: [opened, foreign]
    });
    expect(bundle.events).toEqual([]);
    expect(bundle.deferredReplayEvents).toEqual([]);
    expect(bundle.quarantinedEvents.map((item) => item.event_id)).toEqual([opened.event_id]);
    expect(JSON.stringify(bundle)).not.toContain('Private foreign');
  });

  it('rejects non-scalar identity fields instead of serializing credential objects', () => {
    expect(() =>
      buildSuccessorResumeBundle({
        conversationId,
        logicalIdentity: /** @type {any} */ ({ token: 'HIDDEN' }),
        previousSessionId: 'old',
        successorSessionId: 'new',
        events: []
      })
    ).toThrow(TypeError);
    expect(() =>
      buildSuccessorResumeBundle({
        conversationId,
        logicalIdentity: 'agent:gatekeeper',
        previousSessionId: /** @type {any} */ ({ token: 'HIDDEN' }),
        successorSessionId: 'new',
        events: []
      })
    ).toThrow(TypeError);
  });
});
