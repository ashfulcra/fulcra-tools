import { validateEvent } from './protocol.js';

/** @typedef {import('./protocol.js').GatekeeperEvent} GatekeeperEvent */

/** @param {any} value @returns {string} Stable equality independent of key order. */
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`)
      .join(',')}}`;
  }
  return JSON.stringify(value);
}

/**
 * Rebuild read models from already-accepted annotation events. This reducer
 * cannot establish that `sender` or `verified` was trustworthy at ingestion.
 * @param {unknown[]} events
 */
export function replayEvents(events) {
  /** @type {Record<string, {messages: object[], event_ids: string[]}>} */
  const conversations = {};
  /** @type {Record<string, {conversation_id: string, title: string, status: string, head_event_id: string}>} */
  const work = {};
  /** @type {Record<string, {conversation_id: string, text: string, opened_event_id: string}>} */
  const openQuestions = {};
  /** @type {Record<string, {event_id: string, artifact: string, created_at: string}>} */
  const verifiedCheckpoints = {};
  /** @type {Record<string, Record<string, string>>} */
  const presence = Object.create(null);
  /** @type {{type:string,event_ids:string[],detail?:string}[]} */
  const conflicts = [];
  /** @type {{event_id:string|null,reason:string}[]} */
  const unresolved = [];
  /** @type {Map<string, GatekeeperEvent>} */
  const byId = new Map();
  const conflictingIds = new Set();

  if (!Array.isArray(events))
    return {
      conversations,
      work,
      openQuestions,
      verifiedCheckpoints,
      presence,
      conflicts,
      unresolved: [{ event_id: null, reason: 'events must be an array' }]
    };
  for (const value of events) {
    const checked = validateEvent(value);
    if (!checked.ok) {
      unresolved.push({
        event_id:
          value &&
          typeof value === 'object' &&
          'event_id' in value &&
          typeof value.event_id === 'string'
            ? value.event_id
            : null,
        reason: checked.error
      });
      continue;
    }
    const event = checked.event;
    const prior = byId.get(event.event_id);
    if (prior && canonical(prior) !== canonical(event)) conflictingIds.add(event.event_id);
    else if (!prior) byId.set(event.event_id, event);
  }
  for (const event_id of [...conflictingIds].sort()) {
    byId.delete(event_id);
    conflicts.push({ type: 'event_id_reuse', event_ids: [event_id] });
  }

  const ordered = [...byId.values()].sort(
    (a, b) =>
      Date.parse(a.created_at) - Date.parse(b.created_at) || a.event_id.localeCompare(b.event_id)
  );
  /** @type {Map<string, any[]>} */
  const opens = new Map();
  /** @type {Map<string, any[]>} */
  const questionOpens = new Map();
  /** @type {Map<string, any[]>} */
  const siblings = new Map();
  for (const event of ordered) {
    if (event.kind === 'work.opened') {
      const group = opens.get(event.payload.work_id) ?? [];
      group.push(event);
      opens.set(event.payload.work_id, group);
    }
    if (event.kind === 'question.opened') {
      const group = questionOpens.get(event.payload.question_id) ?? [];
      group.push(event);
      questionOpens.set(event.payload.question_id, group);
    }
    if (event.kind === 'work.updated') {
      const key = `${event.conversation_id}:${event.payload.work_id}:${event.payload.expected_event_id}`;
      const group = siblings.get(key) ?? [];
      group.push(event);
      siblings.set(key, group);
    }
  }
  const excluded = new Set();
  for (const group of [...opens.values(), ...questionOpens.values(), ...siblings.values()]) {
    if (group.length < 2) continue;
    group.forEach((event) => excluded.add(event.event_id));
    const type =
      group[0].kind === 'work.opened'
        ? 'competing_openings'
        : group[0].kind === 'question.opened'
          ? 'competing_question_openings'
          : 'competing_transitions';
    conflicts.push({
      type,
      event_ids: group.map((event) => event.event_id)
    });
  }

  /** @type {Map<string,string>} Applied event ID to its conversation scope. */
  const applied = new Map();
  let pending = ordered.filter((event) => !excluded.has(event.event_id));
  while (pending.length) {
    const next = [];
    let progress = false;
    for (const event of pending) {
      if (event.causation_id && applied.get(event.causation_id) !== event.conversation_id) {
        next.push(event);
        continue;
      }
      const p = event.payload;
      if (
        event.kind === 'work.updated' &&
        (!work[p.work_id] ||
          work[p.work_id].conversation_id !== event.conversation_id ||
          work[p.work_id].head_event_id !== p.expected_event_id)
      ) {
        next.push(event);
        continue;
      }
      if (
        event.kind === 'question.answered' &&
        (!openQuestions[p.question_id] ||
          openQuestions[p.question_id].conversation_id !== event.conversation_id)
      ) {
        next.push(event);
        continue;
      }
      if (
        event.kind === 'checkpoint.published' &&
        (!work[p.work_id] || work[p.work_id].conversation_id !== event.conversation_id)
      ) {
        next.push(event);
        continue;
      }

      conversations[event.conversation_id] ??= { messages: [], event_ids: [] };
      switch (event.kind) {
        case 'message':
          conversations[event.conversation_id].messages.push({ event_id: event.event_id, ...p });
          break;
        case 'work.opened':
          work[p.work_id] = {
            conversation_id: event.conversation_id,
            title: p.title,
            status: 'proposed',
            head_event_id: event.event_id
          };
          break;
        case 'work.updated':
          work[p.work_id] = { ...work[p.work_id], status: p.status, head_event_id: event.event_id };
          break;
        case 'question.opened':
          openQuestions[p.question_id] = {
            conversation_id: event.conversation_id,
            text: p.text,
            opened_event_id: event.event_id
          };
          break;
        case 'question.answered':
          delete openQuestions[p.question_id];
          break;
        case 'checkpoint.published':
          if (p.verified) {
            const current = verifiedCheckpoints[p.work_id];
            if (
              !current ||
              Date.parse(event.created_at) > Date.parse(current.created_at) ||
              (Date.parse(event.created_at) === Date.parse(current.created_at) &&
                event.event_id > current.event_id)
            ) {
              verifiedCheckpoints[p.work_id] = {
                event_id: event.event_id,
                artifact: p.artifact,
                created_at: event.created_at
              };
            }
          }
          break;
        case 'presence.observed':
          presence[p.session_id] = { ...(presence[p.session_id] ?? {}), coverage: p.coverage };
          for (const clock of ['contact_at', 'inbox_observed_at', 'progress_at', 'checkpoint_at']) {
            if (p[clock] !== undefined) presence[p.session_id][clock] = p[clock];
          }
          break;
      }
      conversations[event.conversation_id].event_ids.push(event.event_id);
      applied.set(event.event_id, event.conversation_id);
      progress = true;
    }
    if (!progress) {
      unresolved.push(
        ...next.map((event) => ({
          event_id: event.event_id,
          reason: 'missing or unsatisfied dependency'
        }))
      );
      break;
    }
    pending = next;
  }
  unresolved.sort(
    (a, b) => (a.event_id ?? '').localeCompare(b.event_id ?? '') || a.reason.localeCompare(b.reason)
  );
  return {
    conversations,
    work,
    openQuestions,
    verifiedCheckpoints,
    presence,
    conflicts,
    unresolved
  };
}
