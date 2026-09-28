import { describe, expect, it } from 'vitest';
import fixture from '../../../tests/fixtures/conversation.json';
import { replayEvents } from './projection.js';

const wid = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const qid = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const sid = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd';
/** @param {string} event_id @param {string} kind @param {Record<string, any>} payload @param {Record<string, any>} [extra] */
const e = (event_id, kind, payload, extra = {}) => ({
  ...fixture,
  event_id,
  kind,
  payload,
  ...extra
});
const opened = e('22222222-2222-4222-8222-222222222222', 'work.opened', {
  work_id: wid,
  title: 'Build'
});
const updated = e('33333333-3333-4333-8333-333333333333', 'work.updated', {
  work_id: wid,
  status: 'active',
  expected_event_id: opened.event_id
});

describe('durable projection replay', () => {
  it('deduplicates identical IDs and reports conflicting reuse without accepting either copy', () => {
    const conflict = { ...fixture, payload: { text: 'forged', speaker: 'visitor' } };
    const result = replayEvents([fixture, fixture, conflict]);
    expect(result.conversations).toEqual({});
    expect(result.conflicts).toHaveLength(1);
  });

  it('does not leave a ghost conversation for excluded conflicting IDs', () => {
    const otherConversation = {
      ...fixture,
      conversation_id: 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'
    };
    const result = replayEvents([fixture, otherConversation]);
    expect(result.conversations).toEqual({});
    expect(result.conflicts[0].type).toBe('event_id_reuse');
  });

  it('stably orders invalid-event diagnostics regardless of input order', () => {
    const badVersion = {
      ...fixture,
      event_id: 'aaaaaaaa-1111-4111-8111-111111111111',
      protocol: 'gatekeeper/2'
    };
    const badKind = {
      ...fixture,
      event_id: 'bbbbbbbb-1111-4111-8111-111111111111',
      kind: 'unknown'
    };
    expect(replayEvents([badKind, badVersion]).unresolved).toEqual(
      replayEvents([badVersion, badKind]).unresolved
    );
  });

  it('retries an out-of-order update and leaves orphan dependencies unresolved', () => {
    const orphan = e('44444444-4444-4444-8444-444444444444', 'work.updated', {
      work_id: wid,
      status: 'completed',
      expected_event_id: '99999999-9999-4999-8999-999999999999'
    });
    const result = replayEvents([updated, orphan, opened]);
    expect(result.work[wid].status).toBe('active');
    expect(result.work[wid].head_event_id).toBe(updated.event_id);
    expect(result.unresolved.map((item) => item.event_id)).toContain(orphan.event_id);
  });

  it('does not apply a work transition from a different conversation', () => {
    const foreign = {
      ...updated,
      conversation_id: 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'
    };
    const result = replayEvents([opened, foreign]);
    expect(result.work[wid].status).toBe('proposed');
    expect(result.unresolved.map((item) => item.event_id)).toContain(foreign.event_id);
  });

  it('does not let a foreign transition block a valid same-head transition', () => {
    const foreign = {
      ...updated,
      event_id: '44444444-4444-4444-8444-444444444444',
      conversation_id: 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'
    };
    const result = replayEvents([opened, foreign, updated]);
    expect(result.work[wid].status).toBe('active');
    expect(result.conflicts).toEqual([]);
    expect(result.unresolved.map((item) => item.event_id)).toContain(foreign.event_id);
  });

  it('does not satisfy a local causal dependency with an applied foreign event', () => {
    const foreign = e(
      'aaaaaaaa-3333-4333-8333-333333333333',
      'message',
      { text: 'Foreign context', speaker: 'agent' },
      { conversation_id: 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee' }
    );
    const local = { ...opened, causation_id: foreign.event_id };
    const result = replayEvents([foreign, local]);
    expect(result.work[wid]).toBeUndefined();
    expect(result.unresolved).toContainEqual({ event_id: local.event_id, reason: 'missing or unsatisfied dependency' });
    expect(result.conversations[foreign.conversation_id].messages).toHaveLength(1);
  });

  it('does not answer a question from a different conversation', () => {
    const question = e('88888888-8888-4888-8888-888888888888', 'question.opened', {
      question_id: qid,
      text: 'Ready?'
    });
    const answer = e(
      '99999999-9999-4999-8999-999999999999',
      'question.answered',
      {
        question_id: qid,
        text: 'Yes'
      },
      { conversation_id: 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee' }
    );
    const result = replayEvents([question, answer]);
    expect(result.openQuestions[qid].text).toBe('Ready?');
    expect(result.unresolved.map((item) => item.event_id)).toContain(answer.event_id);
  });

  it('conflicts same-conversation question openings with one question ID', () => {
    const first = e('aaaaaaaa-2222-4222-8222-222222222222', 'question.opened', {
      question_id: qid,
      text: 'First?'
    });
    const second = e('bbbbbbbb-2222-4222-8222-222222222222', 'question.opened', {
      question_id: qid,
      text: 'Second?'
    });
    const result = replayEvents([second, first]);
    expect(result.openQuestions).toEqual({});
    expect(result.conflicts).toContainEqual({
      type: 'competing_question_openings',
      event_ids: [first.event_id, second.event_id]
    });
  });

  it('conflicts cross-conversation question ID reuse and leaves answers unresolved in either replay order', () => {
    const first = e('aaaaaaaa-2222-4222-8222-222222222222', 'question.opened', {
      question_id: qid,
      text: 'First?'
    });
    const second = e(
      'bbbbbbbb-2222-4222-8222-222222222222',
      'question.opened',
      { question_id: qid, text: 'Second?' },
      { conversation_id: 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee' }
    );
    const answer = e('cccccccc-2222-4222-8222-222222222222', 'question.answered', {
      question_id: qid,
      text: 'Yes'
    });
    const forward = replayEvents([first, second, answer]);
    expect(forward).toEqual(replayEvents([answer, second, first]));
    expect(forward.openQuestions).toEqual({});
    expect(forward.conversations).toEqual({});
    expect(forward.conflicts[0].type).toBe('competing_question_openings');
    expect(forward.unresolved.map((item) => item.event_id)).toContain(answer.event_id);
  });

  it('does not publish a checkpoint to work in a different conversation', () => {
    const checkpoint = e(
      '66666666-6666-4666-8666-666666666666',
      'checkpoint.published',
      {
        work_id: wid,
        artifact: 'https://example.com/foreign',
        verified: true
      },
      { conversation_id: 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee' }
    );
    const result = replayEvents([opened, checkpoint]);
    expect(result.verifiedCheckpoints[wid]).toBeUndefined();
    expect(result.unresolved.map((item) => item.event_id)).toContain(checkpoint.event_id);
  });

  it('keeps competing siblings from being silently authoritative', () => {
    const sibling = e('55555555-5555-4555-8555-555555555555', 'work.updated', {
      work_id: wid,
      status: 'completed',
      expected_event_id: opened.event_id
    });
    const result = replayEvents([sibling, opened, updated]);
    expect(result.work[wid].status).toBe('proposed');
    expect(result.work[wid].head_event_id).toBe(opened.event_id);
    expect(result.conflicts).toHaveLength(1);
  });

  it('does not complete work through silence or an unverified checkpoint', () => {
    const verified = e('66666666-6666-4666-8666-666666666666', 'checkpoint.published', {
      work_id: wid,
      artifact: 'https://example.com/verified',
      verified: true
    });
    const unverified = e('77777777-7777-4777-8777-777777777777', 'checkpoint.published', {
      work_id: wid,
      artifact: 'https://example.com/draft',
      verified: false
    });
    const result = replayEvents([opened, verified, unverified]);
    expect(result.work[wid].status).toBe('proposed');
    expect(result.verifiedCheckpoints[wid].artifact).toBe('https://example.com/verified');
  });

  it('orders checkpoint replacements by instant across timezone offsets', () => {
    const early = e(
      '66666666-6666-4666-8666-666666666666',
      'checkpoint.published',
      { work_id: wid, artifact: 'https://example.com/early', verified: true },
      { created_at: '2026-09-26T15:00:00+02:00' }
    );
    const late = e(
      '77777777-7777-4777-8777-777777777777',
      'checkpoint.published',
      { work_id: wid, artifact: 'https://example.com/late', verified: true },
      { created_at: '2026-09-26T12:00:00-02:00' }
    );
    const result = replayEvents([late, opened, early]);
    expect(result.verifiedCheckpoints[wid].artifact).toBe('https://example.com/late');
  });

  it('tracks open questions and separate presence clocks', () => {
    const question = e('88888888-8888-4888-8888-888888888888', 'question.opened', {
      question_id: qid,
      text: 'Ready?'
    });
    const answer = e('99999999-9999-4999-8999-999999999999', 'question.answered', {
      question_id: qid,
      text: 'Yes'
    });
    const presence = e('aaaaaaaa-1111-4111-8111-111111111111', 'presence.observed', {
      session_id: sid,
      coverage: 'partial',
      contact_at: '2026-09-26T10:00:00Z',
      inbox_observed_at: '2026-09-26T11:00:00Z'
    });
    const result = replayEvents([answer, presence, question]);
    expect(result.openQuestions).toEqual({});
    expect(result.presence[sid]).toMatchObject({
      contact_at: '2026-09-26T10:00:00Z',
      inbox_observed_at: '2026-09-26T11:00:00Z'
    });
    expect(result.presence[sid]).not.toHaveProperty('progress_at');
  });

  it('safely represents a logical session ID that is a prototype property name', () => {
    const observation = e('aaaaaaaa-1111-4111-8111-111111111111', 'presence.observed', {
      session_id: '__proto__',
      coverage: 'partial',
      contact_at: '2026-09-26T10:00:00Z'
    });
    const result = replayEvents([observation]);
    expect(Object.hasOwn(result.presence, '__proto__')).toBe(true);
    expect(result.presence['__proto__'].contact_at).toBe('2026-09-26T10:00:00Z');
  });
});
