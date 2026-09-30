import { replayEvents } from './projection.js';
import { validateEvent } from './protocol.js';

/** @param {unknown} value */
function instant(value) {
  if (typeof value !== 'string') return null;
  const parts =
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-](\d{2}):(\d{2}))$/.exec(
      value
    );
  if (!parts) return null;
  const year = Number(parts[1]);
  const month = Number(parts[2]);
  const day = Number(parts[3]);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (
    month < 1 ||
    month > 12 ||
    day < 1 ||
    day > days[month - 1] ||
    Number(parts[4]) > 23 ||
    Number(parts[5]) > 59 ||
    Number(parts[6]) > 59 ||
    Number(parts[8] ?? 0) > 23 ||
    Number(parts[9] ?? 0) > 59
  )
    return null;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : null;
}

/** @param {unknown} value */
function positiveMinutes(value) {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null;
}

/**
 * Advance an idle policy only after a successful, completely covered wake read. The
 * operator interval is an independent cap, not the previous policy tier.
 * This function does not schedule a poll or retry a failed read.
 * @param {{enrolledAt?:string,lastWorkAt?:string,lastSuccessfulObservationAt?:string,policyIntervalMinutes?:number,operatorIntervalMinutes?:number}} state
 * @param {{status:string,wake?:ReturnType<typeof evaluateWake>,newlyObservedWorkAt?:string,hasActiveWork?:boolean,deadlineApproaching?:boolean,urgentDecision?:boolean}} observation
 * @param {string} now Explicit observation clock.
 */
export function advanceListenerPolicy(state, observation, now) {
  const previous = positiveMinutes(state.policyIntervalMinutes) ?? 15;
  const cap = positiveMinutes(state.operatorIntervalMinutes);
  const urgent =
    observation.hasActiveWork || observation.deadlineApproaching || observation.urgentDecision;
  const wake = observation.wake;
  const complete =
    wake?.eventCoverage === 'complete' &&
    wake.obligationCoverage === 'complete' &&
    Array.isArray(wake.events) &&
    Array.isArray(wake.obligations);
  const hasItems = Boolean(wake?.events?.length || wake?.obligations?.length);
  const clear = complete && !hasItems && wake.status === 'clear';
  const actionable = complete && hasItems && wake.status === 'actionable';
  const nowMs = instant(now);
  const priorSuccessMs = instant(state.lastSuccessfulObservationAt);
  const nextCap = urgent || hasItems ? 15 : Infinity;
  if (
    observation.status !== 'success' ||
    (!clear && !actionable) ||
    nowMs === null ||
    (priorSuccessMs !== null && priorSuccessMs > nowMs)
  ) {
    return {
      ...state,
      nextIntervalMinutes: Math.min(previous, cap ?? Infinity, nextCap)
    };
  }

  const arrivalMs = instant(observation.newlyObservedWorkAt);
  const priorWorkMs = instant(state.lastWorkAt ?? state.enrolledAt);
  const hasNewArrival =
    actionable &&
    arrivalMs !== null &&
    arrivalMs <= nowMs &&
    (priorWorkMs === null || arrivalMs > priorWorkMs);
  const workAt = hasNewArrival ? observation.newlyObservedWorkAt : state.lastWorkAt;
  const baselineMs = instant(workAt ?? state.enrolledAt);
  // A malformed, future, or missing baseline is not evidence of quiet time.
  const quietHours =
    nowMs !== null && baselineMs !== null && baselineMs <= nowMs
      ? (nowMs - baselineMs) / 3_600_000
      : 0;
  const tier = quietHours < 1 ? 15 : quietHours < 4 ? 30 : quietHours < 24 ? 60 : 180;
  const policyIntervalMinutes = hasNewArrival ? 15 : clear ? tier : previous;
  return {
    ...state,
    ...(hasNewArrival ? { lastWorkAt: observation.newlyObservedWorkAt } : {}),
    lastSuccessfulObservationAt: now,
    policyIntervalMinutes,
    nextIntervalMinutes: Math.min(policyIntervalMinutes, cap ?? Infinity, nextCap)
  };
}

/**
 * A complete empty event read is insufficient without a complete empty
 * obligation read. Known items remain actionable even amid a coverage gap.
 * @param {{eventObservation?:{coverage?:string,events?:unknown[]},obligationObservation?:{coverage?:string,obligations?:unknown[]}}} read
 */
export function evaluateWake(read) {
  const events = Array.isArray(read.eventObservation?.events) ? read.eventObservation.events : [];
  const obligations = Array.isArray(read.obligationObservation?.obligations)
    ? read.obligationObservation.obligations
    : [];
  const complete =
    read.eventObservation?.coverage === 'complete' &&
    read.obligationObservation?.coverage === 'complete' &&
    Array.isArray(read.eventObservation.events) &&
    Array.isArray(read.obligationObservation.obligations);
  return {
    status: events.length || obligations.length ? 'actionable' : complete ? 'clear' : 'unknown',
    events,
    obligations,
    eventCoverage: read.eventObservation?.coverage ?? 'unavailable',
    obligationCoverage: read.obligationObservation?.coverage ?? 'unavailable'
  };
}

/** @type {Record<string, string[]>} */
const payloadFields = {
  message: ['text', 'speaker'],
  'work.opened': ['work_id', 'title'],
  'work.updated': ['work_id', 'status', 'expected_event_id'],
  'question.opened': ['question_id', 'text'],
  'question.answered': ['question_id', 'text'],
  'checkpoint.published': ['work_id', 'artifact', 'verified'],
  'presence.observed': [
    'session_id',
    'coverage',
    'contact_at',
    'inbox_observed_at',
    'progress_at',
    'checkpoint_at'
  ]
};
const accessFields = ['resource', 'action', 'scope', 'principal_id'];

/** @param {import('./protocol.js').GatekeeperEvent} source */
function copyKnownEvent(source) {
  /** @type {Record<string, any>} */
  const payload = {};
  for (const key of payloadFields[source.kind] ?? []) {
    if (source.payload[key] !== undefined) payload[key] = source.payload[key];
  }
  return {
    protocol: source.protocol,
    event_id: source.event_id,
    conversation_id: source.conversation_id,
    sender: source.sender,
    kind: source.kind,
    created_at: source.created_at,
    ...(source.causation_id ? { causation_id: source.causation_id } : {}),
    payload
  };
}

/** @param {any} value Stable equality independent of key order. @returns {string} */
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object')
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`)
      .join(',')}}`;
  return JSON.stringify(value) ?? 'undefined';
}

/**
 * Build a conservative, scoped handoff from accepted annotation events.
 * The current checkpoint protocol has no verified inclusion frontier, so
 * every accepted scoped event is replayed. Valid but unresolved bodies travel
 * separately; conflicting variants are quarantined until explicitly resolved.
 * This is context, not an authorization or assignment-transfer decision.
 * @param {{conversationId:string,logicalIdentity:string,previousSessionId:string,successorSessionId:string,events:unknown[],accessRequirements?:unknown[]}} input
 */
export function buildSuccessorResumeBundle(input) {
  if (
    !Array.isArray(input.events) ||
    typeof input.conversationId !== 'string' ||
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
      input.conversationId
    ) ||
    [input.logicalIdentity, input.previousSessionId, input.successorSessionId].some(
      (value) => typeof value !== 'string' || !value.trim()
    )
  )
    throw new TypeError('valid conversation, identity, runtime sessions, and events required');
  const projection = replayEvents(input.events);
  const acceptedIds = new Set(projection.conversations[input.conversationId]?.event_ids ?? []);
  const conflictIds = new Set(projection.conflicts.flatMap((conflict) => conflict.event_ids));
  // Projection diagnostics have IDs but not conversation IDs. Validation
  // failures must be attributed from their original raw scoped record;
  // missing-dependency diagnostics apply only to scoped valid unapplied IDs.
  const unresolvedIds = new Set(
    projection.unresolved
      .filter((item) => item.reason === 'missing or unsatisfied dependency')
      .map((item) => item.event_id)
  );
  const seen = new Set();
  const scopedEvents = input.events
    .map((value) => validateEvent(value))
    .flatMap((result) =>
      result.ok && result.event.conversation_id === input.conversationId ? [result.event] : []
    )
    .filter((event) => {
      const signature = canonical(event);
      if (seen.has(signature)) return false;
      seen.add(signature);
      return true;
    })
    .sort(
      (a, b) =>
        Date.parse(a.created_at) - Date.parse(b.created_at) ||
        a.event_id.localeCompare(b.event_id) ||
        canonical(a).localeCompare(canonical(b))
    );
  const events = scopedEvents
    .filter((event) => acceptedIds.has(event.event_id) && !conflictIds.has(event.event_id))
    .map(copyKnownEvent);
  const deferredReplayEvents = scopedEvents
    .filter(
      (event) =>
        unresolvedIds.has(event.event_id) &&
        !acceptedIds.has(event.event_id) &&
        !conflictIds.has(event.event_id)
    )
    .map(copyKnownEvent);
  const quarantinedEvents = scopedEvents
    .filter(
      (event) =>
        conflictIds.has(event.event_id) ||
        (!acceptedIds.has(event.event_id) && !unresolvedIds.has(event.event_id))
    )
    .map(copyKnownEvent);
  const outstandingWork = Object.entries(projection.work)
    .filter(
      ([, work]) =>
        work.conversation_id === input.conversationId &&
        !['completed', 'cancelled'].includes(work.status)
    )
    .map(([work_id, work]) => ({
      work_id,
      title: work.title,
      status: work.status,
      head_event_id: work.head_event_id
    }));
  const openQuestions = Object.entries(projection.openQuestions)
    .filter(([, question]) => question.conversation_id === input.conversationId)
    .map(([question_id, question]) => ({
      question_id,
      text: question.text,
      opened_event_id: question.opened_event_id
    }));
  const scopedWorkIds = new Set(
    Object.entries(projection.work)
      .filter(([, work]) => work.conversation_id === input.conversationId)
      .map(([work_id]) => work_id)
  );
  const checkpoints = Object.entries(projection.verifiedCheckpoints)
    .filter(([work_id]) => scopedWorkIds.has(work_id))
    .map(([work_id, checkpoint]) => ({ work_id, ...checkpoint }))
    .sort(
      (a, b) =>
        Date.parse(b.created_at) - Date.parse(a.created_at) || b.event_id.localeCompare(a.event_id)
    );
  const accessRequirements = Array.isArray(input.accessRequirements)
    ? input.accessRequirements
        .filter((value) => value && typeof value === 'object' && !Array.isArray(value))
        .map((value) => {
          const source = /** @type {Record<string, unknown>} */ (value);
          /** @type {Record<string, string>} */
          const descriptor = {};
          for (const key of accessFields) {
            if (typeof source[key] === 'string' && source[key].trim())
              descriptor[key] = source[key];
          }
          return descriptor;
        })
        .filter((descriptor) => Object.keys(descriptor).length > 0)
    : [];
  const scopedRawIds = new Set(
    input.events.flatMap((value) =>
      value &&
      typeof value === 'object' &&
      'conversation_id' in value &&
      value.conversation_id === input.conversationId &&
      'event_id' in value &&
      typeof value.event_id === 'string'
        ? [value.event_id]
        : []
    )
  );
  const scopedValidIds = new Set(scopedEvents.map((event) => event.event_id));
  const scopedMalformed = input.events.flatMap((value) => {
    if (
      !value ||
      typeof value !== 'object' ||
      !('conversation_id' in value) ||
      value.conversation_id !== input.conversationId
    )
      return [];
    const checked = validateEvent(value);
    if (checked.ok) return [];
    return [
      {
        event_id: 'event_id' in value && typeof value.event_id === 'string' ? value.event_id : null,
        reason: checked.error
      }
    ];
  });
  const scopedUnresolved = [
    ...scopedMalformed,
    ...projection.unresolved.filter(
      (item) =>
        item.reason === 'missing or unsatisfied dependency' &&
        item.event_id !== null &&
        scopedValidIds.has(item.event_id) &&
        !acceptedIds.has(item.event_id) &&
        !conflictIds.has(item.event_id)
    )
  ].sort(
    (a, b) => (a.event_id ?? '').localeCompare(b.event_id ?? '') || a.reason.localeCompare(b.reason)
  );
  return {
    conversationId: input.conversationId,
    logicalIdentity: input.logicalIdentity,
    runtime: {
      previousSessionId: input.previousSessionId,
      successorSessionId: input.successorSessionId
    },
    replayMode: 'full_replay',
    requiresEventIdDedupe: true,
    replayCollections: ['events', 'deferredReplayEvents', 'quarantinedEvents'],
    conflictingVariantsRequireResolution: true,
    latestVerifiedCheckpoint: checkpoints[0] ?? null,
    events,
    deferredReplayEvents,
    quarantinedEvents,
    outstandingWork,
    openQuestions,
    accessRequirements,
    coverage: {
      conflicts: projection.conflicts
        .map((conflict) => ({
          type: conflict.type,
          event_ids: conflict.event_ids.filter((id) => scopedRawIds.has(id))
        }))
        .filter((conflict) => conflict.event_ids.length > 0),
      unresolved: scopedUnresolved
    }
  };
}
