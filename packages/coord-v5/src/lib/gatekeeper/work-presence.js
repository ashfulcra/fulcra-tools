/** Self-authored presence does not establish assignment progress or external access. */
import { parseWorkInstant, compareWorkInstants } from './work-contract.js';
const key = (a) =>
  ['principal_id', 'logical_agent_id', 'instance_id', 'session_id'].map((k) => a[k]).join('\0');
export function presenceTransitionError(event, facts) {
  if (!event.payload.checkpoint_event_id) return null;
  const publication = facts.find(
    (f) => f.event.event_id === event.payload.checkpoint_event_id
  )?.event;
  if (
    !publication ||
    publication.kind !== 'checkpoint.published' ||
    key(publication.actor) !== key(event.actor) ||
    publication.workstream_id !== event.workstream_id
  )
    return 'PRESENCE_CHECKPOINT_REQUIRED';
  return null;
}
export function foldWorkPresence(facts) {
  const rows = facts.filter((f) => f.event.kind === 'presence.observed');
  return [...new Set(rows.map((f) => key(f.event.actor)))].sort().map((actorKey) => {
    const own = rows.filter((f) => key(f.event.actor) === actorKey);
    const heads = own.filter((f) => !own.some((g) => g.ancestors.has(f.event.event_id)));
    const e = heads.length === 1 ? heads[0].event : null;
    const publication = e?.payload.checkpoint_event_id
      ? facts.find((f) => f.event.event_id === e.payload.checkpoint_event_id)?.event
      : null;
    return {
      actor: own[0].event.actor,
      event_ids: heads.map((f) => f.event.event_id).sort(),
      ...(e
        ? e.payload
        : {
            contact_at: null,
            inbox_observed_at: null,
            inbox_coverage: 'unavailable',
            progress_at: null,
            checkpoint_event_id: null,
            contact_due_at: null,
            progress_due_at: null,
            checkpoint_due_at: null,
            engagement: null,
            work_ids: []
          }),
      checkpoint_at: publication?.occurred_at ?? null,
      checkpoint_work_id: publication?.payload.work_id ?? null,
      conflicted: heads.length !== 1,
      provisional: false
    };
  });
}

/** Evaluation time is distinct from source observation time; failure never becomes freshness. */
export function evaluateSourceFreshness({ projection, evaluated_at, max_source_age_ms }) {
  const validTime = (v) => parseWorkInstant(v) !== null;
  const observation = projection?.observation;
  if (
    projection?.schema !== 'gatekeeper-work-view/1' ||
    !validTime(evaluated_at) ||
    !Number.isSafeInteger(max_source_age_ms) ||
    max_source_age_ms <= 0 ||
    !validTime(observation?.as_of) ||
    compareWorkInstants(evaluated_at, observation.as_of) < 0
  )
    return {
      status: 'blocked',
      code: 'INVALID_EVALUATION',
      evaluated_at: null,
      source_freshness: 'unknown'
    };
  const successful = observation.last_successful_observation_at;
  let source_freshness = 'unknown';
  if (
    validTime(successful) &&
    compareWorkInstants(successful, observation.as_of) <= 0 &&
    ['complete', 'partial'].includes(observation.coverage) &&
    Array.isArray(observation.errors) &&
    observation.errors.length === 0
  )
    source_freshness =
      Date.parse(evaluated_at) - Date.parse(successful) <= max_source_age_ms ? 'fresh' : 'stale';
  return {
    status: 'ready',
    evaluated_at,
    source_as_of: observation.as_of,
    last_successful_observation_at: successful ?? null,
    max_source_age_ms,
    source_freshness
  };
}
/** Pure evaluation of an already authorized projection; caller evidence is not independently verified. */
export function evaluateWorkPresence(input) {
  const source = evaluateSourceFreshness(input);
  if (source.status !== 'ready') return { ...source, rows: [] };
  if (!Array.isArray(input.projection.presence))
    return { ...source, status: 'blocked', code: 'INVALID_PROJECTION', rows: [] };
  const clock = (at, due, known) => ({
    at,
    due_at: due,
    state:
      !known || !at || !due
        ? 'unknown'
        : compareWorkInstants(input.evaluated_at, due) > 0 && compareWorkInstants(at, due) < 0
          ? 'overdue'
          : 'current'
  });
  return {
    ...source,
    rows: input.projection.presence.map((row) => {
      const known = source.source_freshness === 'fresh' && !row.conflicted;
      return {
        ...row,
        coverage: known ? input.projection.observation.coverage : 'unavailable',
        engagement_state:
          !known || !row.engagement
            ? 'unknown'
            : row.engagement.mode === 'session' &&
                compareWorkInstants(input.evaluated_at, row.engagement.until) >= 0
              ? 'lapsed'
              : 'active',
        clocks: {
          contact: clock(row.contact_at, row.contact_due_at, known),
          inbox: {
            observed_at: row.inbox_observed_at,
            coverage: known ? row.inbox_coverage : 'unavailable'
          },
          progress: clock(row.progress_at, row.progress_due_at, known),
          checkpoint: clock(row.checkpoint_at, row.checkpoint_due_at, known)
        }
      };
    })
  };
}
/** Exact existing digest input shape; unavailable source remains unavailable. */
export function presenceRowsForDigest(evaluation) {
  return (evaluation?.rows ?? []).map((row) => ({
    actor: row.actor,
    contact_at: row.contact_at,
    inbox_observed_at: row.inbox_observed_at,
    progress_at: row.progress_at,
    checkpoint_at: row.checkpoint_at,
    contact_due_at: row.contact_due_at,
    progress_due_at: row.progress_due_at,
    checkpoint_due_at: row.checkpoint_due_at,
    coverage: row.coverage,
    work_ids: row.work_ids,
    checkpoint_work_id: row.checkpoint_work_id
  }));
}
