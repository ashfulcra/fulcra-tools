/** Pure presentation of an already authorized work projection. This module does not replay
 * event claims, discover trust, or infer observation completeness. */

/** @typedef {import('./work-projection.js').WorkProjection} WorkProjection */
/** @typedef {'everything_owed'|'needs_me'|'completed_history'|'lost_track'} WorkQuery */
/** @typedef {'member'|'owner'|'coordinator'} ViewerRole */
/** @typedef {{actor:import('./work-contract.js').WorkActor,contact_at:string|null,inbox_observed_at:string|null,progress_at:string|null,checkpoint_at:string|null,contact_due_at:string|null,progress_due_at:string|null,checkpoint_due_at:string|null,coverage:string}} PresenceRow */

const ROLES = ['member', 'owner', 'coordinator'];
const QUERIES = ['everything_owed', 'needs_me', 'completed_history', 'lost_track'];
const COVERAGE = ['complete', 'partial', 'unavailable', 'unsupported'];
const PRESENCE_COVERAGE = COVERAGE;
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$/;
const ACTOR_KEYS = ['principal_id', 'logical_agent_id', 'instance_id', 'session_id'];
const PRESENCE_KEYS = [
  'actor',
  'contact_at',
  'inbox_observed_at',
  'progress_at',
  'checkpoint_at',
  'contact_due_at',
  'progress_due_at',
  'checkpoint_due_at',
  'coverage'
];

/** @param {unknown} value */
function object(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** @param {unknown} value @param {string[]} keys */
function exactKeys(value, keys) {
  return (
    object(value) &&
    Object.keys(/** @type {object} */ (value)).length === keys.length &&
    keys.every((key) => Object.hasOwn(/** @type {object} */ (value), key))
  );
}

/** Canonical comparison form retains fractional precision beyond milliseconds.
 * @param {unknown} value */
function instant(value) {
  if (typeof value !== 'string') return null;
  const match =
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-](\d{2}):(\d{2}))$/.exec(
      value
    );
  if (!match) return null;
  const [, year, month, day, hour, minute, second, fraction = '', , zoneHour, zoneMinute] = match;
  const y = Number(year);
  const m = Number(month);
  const d = Number(day);
  const days = [
    31,
    y % 4 === 0 && (y % 100 !== 0 || y % 400 === 0) ? 29 : 28,
    31,
    30,
    31,
    30,
    31,
    31,
    30,
    31,
    30,
    31
  ];
  if (
    y < 1 ||
    m < 1 ||
    m > 12 ||
    d < 1 ||
    d > days[m - 1] ||
    Number(hour) > 23 ||
    Number(minute) > 59 ||
    Number(second) > 59 ||
    (zoneHour && (Number(zoneHour) > 23 || Number(zoneMinute) > 59))
  )
    return null;
  const date = new Date(value);
  if (!Number.isFinite(date.getTime()) || date.getUTCFullYear() < 1 || date.getUTCFullYear() > 9999)
    return null;
  const normalized = date.toISOString().slice(0, 19);
  return `${normalized}.${fraction.padEnd(3, '0')}Z`;
}

/** @param {string} left @param {string} right */
function compareTime(left, right) {
  const a = /** @type {string} */ (instant(left));
  const b = /** @type {string} */ (instant(right));
  const seconds = a.slice(0, 19).localeCompare(b.slice(0, 19));
  if (seconds) return seconds;
  const af = a.slice(20, -1);
  const bf = b.slice(20, -1);
  return af
    .padEnd(Math.max(af.length, bf.length), '0')
    .localeCompare(bf.padEnd(Math.max(af.length, bf.length), '0'));
}

/** @param {unknown} value */
function validActor(value) {
  return (
    exactKeys(value, ACTOR_KEYS) &&
    ACTOR_KEYS.every(
      (key) =>
        typeof (/** @type {any} */ (value)[key]) === 'string' &&
        IDENTIFIER.test(/** @type {any} */ (value)[key])
    )
  );
}

/** @param {unknown} value @param {string} asOf */
function validPresence(value, asOf) {
  if (!exactKeys(value, PRESENCE_KEYS)) return false;
  const row = /** @type {any} */ (value);
  if (!validActor(row.actor) || !PRESENCE_COVERAGE.includes(row.coverage)) return false;
  for (const key of PRESENCE_KEYS.slice(1, 8)) {
    if (row[key] !== null && !instant(row[key])) return false;
    if (!key.endsWith('due_at') && row[key] !== null && compareTime(row[key], asOf) > 0)
      return false;
  }
  return true;
}

/** @param {any} actor */
const actorKey = (actor) => ACTOR_KEYS.map((key) => actor[key]).join('\u0000');

/** @param {string|null} at @param {string|null} due @param {string} asOf @param {string} coverage */
function clock(at, due, asOf, coverage) {
  let state = 'unknown';
  if (coverage === 'complete' && at && due) {
    state = compareTime(asOf, due) > 0 && compareTime(at, due) < 0 ? 'overdue' : 'current';
  }
  return { state, at, due_at: due };
}

/** @param {PresenceRow|null} row @param {string} asOf */
function clocks(row, asOf) {
  return {
    contact: clock(
      row?.contact_at ?? null,
      row?.contact_due_at ?? null,
      asOf,
      row?.coverage ?? 'unavailable'
    ),
    inbox: {
      observed_at: row?.inbox_observed_at ?? null,
      coverage: row?.coverage ?? 'unavailable'
    },
    progress: clock(
      row?.progress_at ?? null,
      row?.progress_due_at ?? null,
      asOf,
      row?.coverage ?? 'unavailable'
    ),
    checkpoint: clock(
      row?.checkpoint_at ?? null,
      row?.checkpoint_due_at ?? null,
      asOf,
      row?.coverage ?? 'unavailable'
    )
  };
}

/** @param {any} row @param {string} asOf @param {Map<string,PresenceRow>} presence */
function workItem(row, asOf, presence) {
  const value = row.item;
  if (!value) {
    return {
      id: `work:${row.work_id}`,
      type: 'unknown_work',
      work_id: row.work_id,
      workstream_id: row.workstream_id,
      title: null,
      status: 'unknown',
      owner_id: null,
      priority: 'normal',
      next_action: null,
      due_at: null,
      revisit: null,
      blocker: null,
      assignment_state: row.assignment?.state ?? 'unassigned',
      state: row.state,
      reasons: [row.state === 'conflicted' ? 'conflicted' : 'pending'],
      clocks: null
    };
  }
  const accepted =
    row.assignment?.state === 'accepted' && validActor(row.assignment.accepted_actor);
  const observed = accepted
    ? (presence.get(actorKey(row.assignment.accepted_actor)) ?? null)
    : null;
  const observedClocks = accepted ? clocks(observed, asOf) : null;
  const reasons = [];
  if (row.state === 'conflicted' || row.execution_authority === 'blocked_conflict')
    reasons.push('conflicted');
  if (row.execution_authority === 'blocked_validation' || row.validation?.length)
    reasons.push('validation_blocked');
  if (value.status === 'blocked') reasons.push('blocked');
  if (value.type === 'task' && row.assignment?.state !== 'accepted') reasons.push('unassigned');
  if (value.revisit?.at && compareTime(asOf, value.revisit.at) >= 0) reasons.push('revisit_due');
  if (value.due_at && compareTime(asOf, value.due_at) > 0) reasons.push('due');
  if (observedClocks) {
    for (const name of /** @type {const} */ (['contact', 'progress', 'checkpoint'])) {
      const state = observedClocks[name].state;
      if (state !== 'current') reasons.push(`${name}_${state}`);
    }
  }
  if (row.provisional) reasons.push('provisional');
  return {
    id: `work:${row.work_id}`,
    type: value.type,
    work_id: row.work_id,
    workstream_id: row.workstream_id,
    title: value.title,
    status: value.status,
    owner_id: value.owner_id,
    priority: value.priority,
    next_action: value.next_action,
    due_at: value.due_at,
    revisit: value.revisit,
    blocker: value.blocker,
    assignment_state: row.assignment?.state ?? 'unassigned',
    state: row.state,
    reasons,
    clocks: observedClocks
  };
}

/** @param {any} q @param {any[]} work @param {string} asOf @returns {any[]} */
function questionItems(q, work, asOf) {
  const opened = q.opened_event;
  const unsettled = (q.history ?? []).filter(
    (/** @type {any} */ entry) =>
      entry.disposition === 'conflicted' || entry.disposition === 'pending'
  );
  const acknowledgmentOnly =
    unsettled.length > 0 &&
    unsettled.every((/** @type {any} */ entry) => entry.kind === 'question.acknowledged') &&
    (q.state !== 'conflicted' ||
      (q.branch_event_ids?.length > 0 &&
        q.branch_event_ids.every((/** @type {string} */ id) =>
          unsettled.some(
            (/** @type {any} */ entry) =>
              entry.event_id === id && entry.disposition === 'conflicted'
          )
        )));
  if (
    !opened ||
    (q.state === 'conflicted' && !acknowledgmentOnly) ||
    (!q.current_answer &&
      unsettled.some((/** @type {any} */ entry) => entry.kind !== 'question.acknowledged'))
  ) {
    const conflicted = q.state === 'conflicted';
    return [
      {
        id: `question:${q.question_id}`,
        type: opened ? 'conflicted_question' : 'unknown_question',
        question_id: q.question_id,
        workstream_id: q.workstream_id,
        title: opened?.payload?.text ?? null,
        status: conflicted ? 'conflicted' : 'pending',
        state: q.state,
        owner_id: null,
        priority: 'normal',
        next_action: null,
        deadline_at: null,
        reasons: [conflicted ? 'conflicted' : 'pending']
      }
    ];
  }
  const payload = opened.payload;
  if (!q.current_answer) {
    return [
      {
        id: `question:${q.question_id}`,
        type: 'question',
        question_id: q.question_id,
        workstream_id: q.workstream_id,
        title: payload.text,
        status: 'awaiting_decision',
        owner_id: payload.decision_maker_id,
        priority: 'normal',
        next_action: 'Answer question',
        deadline_at: payload.deadline_at,
        reasons: [
          ...(q.state === 'conflicted' ? ['conflicted'] : []),
          'awaiting_decision',
          ...(q.state === 'conflicted' ? ['conflict_resolution_required'] : []),
          ...(unsettled.some((/** @type {any} */ entry) => entry.disposition === 'pending')
            ? ['acknowledgment_pending']
            : []),
          ...(payload.deadline_at && compareTime(asOf, payload.deadline_at) > 0
            ? ['decision_overdue']
            : [])
        ]
      }
    ];
  }
  const applicationItems = work
    .filter(
      (w) =>
        payload.work_ids.includes(w.work_id) &&
        w.item?.status === 'blocked' &&
        w.item.blocker?.kind === 'question' &&
        w.item.blocker.ref_id === q.question_id
    )
    .filter(
      (w) =>
        !q.applications?.some(
          (/** @type {any} */ a) =>
            a.payload?.answer_event_id === q.current_answer.event_id &&
            a.payload?.work_id === w.work_id &&
            a.payload?.condition_id === w.item.blocker.condition_id
        )
    )
    .map((w) => ({
      id: `application:${q.question_id}:${w.work_id}`,
      type: 'awaiting_application',
      question_id: q.question_id,
      work_id: w.work_id,
      workstream_id: q.workstream_id,
      title: payload.text,
      status: 'awaiting_application',
      owner_id: w.item.owner_id,
      priority: w.item.priority,
      next_action: w.item.next_action,
      condition_id: w.item.blocker.condition_id,
      answer_event_id: q.current_answer.event_id,
      reasons: ['awaiting_application']
    }));
  if (q.state !== 'conflicted' || !acknowledgmentOnly) return applicationItems;
  return [
    {
      id: `question:${q.question_id}`,
      type: 'conflicted_question',
      question_id: q.question_id,
      workstream_id: q.workstream_id,
      title: payload.text,
      status: 'conflicted',
      state: q.state,
      owner_id: null,
      priority: 'normal',
      next_action: null,
      deadline_at: null,
      reasons: ['conflicted']
    },
    ...applicationItems
  ];
}

/** @param {any} row @param {string|null} from @param {string|null} to */
function completedItem(row, from, to) {
  if (row.item?.type !== 'task' || row.item.status !== 'completed') return null;
  const completion = row.history?.find(
    (/** @type {any} */ h) =>
      h.disposition === 'applied' &&
      h.event_id === row.head_event_id &&
      h.kind === 'work.revised' &&
      h.event?.payload?.item?.status === 'completed'
  );
  const at = completion?.event?.occurred_at;
  if (!instant(at) || (from && compareTime(at, from) < 0) || (to && compareTime(at, to) >= 0))
    return null;
  return {
    id: `completed:${row.work_id}`,
    type: 'completed_task',
    work_id: row.work_id,
    workstream_id: row.workstream_id,
    title: row.item.title,
    status: 'completed',
    owner_id: row.item.owner_id,
    priority: row.item.priority,
    next_action: null,
    completed_at: at,
    result: structuredClone(row.item.result),
    reasons: []
  };
}

/** @param {any} item */
const urgent = (item) => (item.priority === 'urgent' ? 0 : 1);
/** @param {any} a @param {any} b */
const itemOrder = (a, b) => urgent(a) - urgent(b) || a.id.localeCompare(b.id);

/**
 * @param {{projection:WorkProjection,viewerId:string,viewerRole:ViewerRole,query:WorkQuery,from?:string|null,to?:string|null,asOf:string,presence?:PresenceRow[]}} input
 */
export function buildWorkDigest(input) {
  const projection = input?.projection;
  const asOf = instant(input?.asOf) ? input.asOf : null;
  const validProjection =
    projection?.schema === 'gatekeeper-work-view/1' &&
    Array.isArray(projection.work) &&
    Array.isArray(projection.questions) &&
    object(projection.observation);
  const validQuery = QUERIES.includes(input?.query);
  const validViewer =
    typeof input?.viewerId === 'string' &&
    IDENTIFIER.test(input.viewerId) &&
    ROLES.includes(input?.viewerRole);
  const from = input?.from ?? null;
  const to = input?.to ?? null;
  const validRange =
    (from === null || instant(from)) &&
    (to === null || instant(to)) &&
    (!from || !to || compareTime(from, to) <= 0);
  const errors = [];
  if (!validProjection) errors.push({ code: 'INVALID_PROJECTION' });
  if (!asOf) errors.push({ code: 'INVALID_AS_OF' });
  if (!validQuery) errors.push({ code: 'INVALID_QUERY' });
  if (!validViewer) errors.push({ code: 'INVALID_VIEWER' });
  if (!validRange || ((from || to) && input.query !== 'completed_history'))
    errors.push({ code: 'INVALID_RANGE' });
  const observation = validProjection ? projection.observation : null;
  const sources = Array.isArray(observation?.sources)
    ? observation.sources.map((s) => ({
        stream_id: s.stream_id,
        status: s.status,
        pending_pages: s.pending_pages
      }))
    : [];
  const gaps = Array.isArray(observation?.gaps)
    ? observation.gaps.map((g) => ({
        code: g.code,
        ...(g.stream_id ? { stream_id: g.stream_id } : {}),
        ...(g.event_id ? { event_id: g.event_id } : {})
      }))
    : [];
  if (validProjection && asOf) {
    const projectedAt = projection.as_of ?? '';
    const observedAt = observation?.as_of ?? '';
    if (
      !instant(projectedAt) ||
      !instant(observedAt) ||
      compareTime(projectedAt, observedAt) !== 0 ||
      compareTime(asOf, projectedAt) !== 0
    ) {
      gaps.push({ code: 'OBSERVATION_AS_OF_MISMATCH' });
      if (instant(projectedAt) && compareTime(asOf, projectedAt) < 0)
        errors.push({ code: 'AS_OF_BEFORE_PROJECTION' });
    }
  }
  if (Array.isArray(observation?.errors))
    errors.push(
      ...observation.errors.map((e) => ({
        code: e.code,
        ...(e.stream_id ? { stream_id: e.stream_id } : {})
      }))
    );
  const presence = new Map();
  const invalidActors = new Set();
  if (input?.presence !== undefined && !Array.isArray(input.presence))
    gaps.push({ code: 'INVALID_PRESENCE' });
  for (const row of Array.isArray(input?.presence) ? input.presence : []) {
    const key = validActor(row?.actor) ? actorKey(row.actor) : null;
    if (
      !asOf ||
      !validPresence(row, asOf) ||
      (key && (presence.has(key) || invalidActors.has(key)))
    ) {
      gaps.push({ code: 'INVALID_PRESENCE' });
      if (key) {
        presence.delete(key);
        invalidActors.add(key);
      }
    } else presence.set(/** @type {string} */ (key), row);
  }
  const statedCoverage = observation?.coverage ?? 'unavailable';
  const coverage =
    errors.length || gaps.length || !COVERAGE.includes(statedCoverage)
      ? statedCoverage === 'unavailable' || statedCoverage === 'unsupported'
        ? statedCoverage
        : 'partial'
      : statedCoverage;
  /** @type {any[]} */
  let items = [];
  if (
    validProjection &&
    asOf &&
    validQuery &&
    validViewer &&
    validRange &&
    !errors.some((e) => e.code.startsWith('INVALID') || e.code === 'AS_OF_BEFORE_PROJECTION')
  ) {
    const openWork = projection.work
      .filter((row) => !row.item || !['completed', 'cancelled', 'closed'].includes(row.item.status))
      .map((row) => workItem(row, asOf, presence));
    const obligations = projection.questions.flatMap((q) =>
      questionItems(q, projection.work, asOf)
    );
    if (input.query === 'everything_owed') items = [...openWork, ...obligations];
    if (input.query === 'completed_history')
      items = projection.work.map((row) => completedItem(row, from, to)).filter(Boolean);
    if (input.query === 'lost_track')
      items = [...openWork, ...obligations].filter((item) =>
        item.reasons.some(
          (/** @type {string} */ reason) =>
            !['awaiting_decision', 'awaiting_application'].includes(reason)
        )
      );
    if (input.query === 'needs_me') {
      const admin = input.viewerRole === 'owner' || input.viewerRole === 'coordinator';
      items = [...openWork, ...obligations].filter((item) => {
        if (item.type === 'question') return item.owner_id === input.viewerId;
        if (item.type === 'awaiting_application') {
          const work = projection.work.find((row) => row.work_id === item.work_id);
          return (
            work?.assignment?.accepted_actor?.logical_agent_id === input.viewerId ||
            (admin && (!item.owner_id || work?.state === 'conflicted'))
          );
        }
        return (
          item.owner_id === input.viewerId ||
          (admin && (!item.owner_id || item.state === 'conflicted'))
        );
      });
    }
  }
  items.sort(itemOrder);
  /** @type {Record<string,number>} */
  const byType = {};
  for (const item of items) byType[item.type] = (byType[item.type] ?? 0) + 1;
  return {
    schema: 'gatekeeper-work-digest/1',
    workspace_id: validProjection ? projection.workspace_id : null,
    as_of: asOf,
    coverage,
    last_successful_observation_at: observation?.last_successful_observation_at ?? null,
    sources,
    gaps,
    errors,
    items,
    counts: { count_scope: 'observed_authorized', total: items.length, by_type: byType },
    clear: coverage === 'complete' && (input.query === 'completed_history' || items.length === 0)
  };
}
