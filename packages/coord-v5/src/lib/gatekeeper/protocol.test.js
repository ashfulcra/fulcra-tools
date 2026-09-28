import { describe, expect, it } from 'vitest';
import fixture from '../../../tests/fixtures/conversation.json';
import { parseAnnotationNote, serializeEvent, validateEvent } from './protocol.js';

const id = '22222222-2222-4222-8222-222222222222';
/** @param {string} kind @param {Record<string, any>} payload */
const event = (kind, payload) => ({ ...fixture, event_id: id, kind, payload });

describe('gatekeeper/1 annotation protocol', () => {
  it('round trips a valid event through the annotation note', () => {
    const note = serializeEvent(fixture);
    expect(parseAnnotationNote(note)).toEqual({ ok: true, event: fixture });
  });

  it.each(['{', '{}', 'null'])('rejects malformed or incomplete JSON %s', (note) => {
    expect(parseAnnotationNote(note).ok).toBe(false);
  });

  it('rejects unsupported protocol, kind and envelope shapes', () => {
    expect(validateEvent({ ...fixture, protocol: 'gatekeeper/2' }).ok).toBe(false);
    expect(validateEvent({ ...fixture, kind: 'admin.granted' }).ok).toBe(false);
    expect(validateEvent({ ...fixture, sender: ' ', event_id: 'not-a-uuid' }).ok).toBe(false);
    expect(validateEvent({ ...fixture, created_at: '2026-09-26T10:00:00' }).ok).toBe(false);
    expect(validateEvent({ ...fixture, causation_id: 'wrong' }).ok).toBe(false);
  });

  it('rejects impossible calendar dates instead of accepting Date.parse normalization', () => {
    expect(validateEvent({ ...fixture, created_at: '2026-02-30T10:00:00Z' }).ok).toBe(false);
    expect(validateEvent({ ...fixture, created_at: '2024-02-29T10:00:00+01:00' }).ok).toBe(true);
    expect(validateEvent({ ...fixture, created_at: '2026-09-26T25:00:00Z' }).ok).toBe(false);
  });

  it('validates each relevant payload shape', () => {
    const valid = [
      event('work.opened', { work_id: id, title: 'Investigate' }),
      event('work.updated', { work_id: id, status: 'paused', expected_event_id: id }),
      event('question.opened', { question_id: id, text: 'Which team?' }),
      event('question.answered', { question_id: id, text: 'The build team' }),
      event('checkpoint.published', {
        work_id: id,
        artifact: 'https://example.com/1',
        verified: true
      }),
      event('presence.observed', {
        session_id: 'guest-session-1',
        coverage: 'partial',
        contact_at: '2026-09-26T14:00:00Z'
      })
    ];
    for (const item of valid) expect(validateEvent(item).ok).toBe(true);
    expect(validateEvent(event('message', { text: '', speaker: 'visitor' })).ok).toBe(false);
    expect(validateEvent(event('work.updated', { work_id: id, status: 'completed' })).ok).toBe(
      false
    );
    expect(
      validateEvent(event('work.updated', { work_id: id, status: 'done', expected_event_id: id }))
        .ok
    ).toBe(false);
    expect(
      validateEvent(
        event('checkpoint.published', {
          work_id: id,
          artifact: 'http://example.com',
          verified: true
        })
      ).ok
    ).toBe(false);
    expect(
      validateEvent(
        event('presence.observed', {
          session_id: id,
          coverage: 'complete',
          progress_at: 'yesterday'
        })
      ).ok
    ).toBe(false);
    expect(
      validateEvent(event('presence.observed', { session_id: '', coverage: 'complete' })).ok
    ).toBe(false);
  });
});
