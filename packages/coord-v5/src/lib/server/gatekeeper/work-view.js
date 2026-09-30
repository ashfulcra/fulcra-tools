import { canonicalWorkJson } from '../../gatekeeper/work-contract.js';
import { replayWorkEvents } from '../../gatekeeper/work-projection.js';
import { id as listenerId } from './listener-validation.js';
import { evaluateWorkPresence } from '../../gatekeeper/work-presence.js';
import { evaluateWorkRoles } from '../../gatekeeper/work-roles.js';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const ACTOR = ['principal_id', 'logical_agent_id', 'instance_id', 'session_id'];
const CAPABILITIES = new Set([
  'presence.publish',
  'role.manage',
  'role.claim',
  'role.checkpoint',
  'work.write',
  'question.ask',
  'question.answer',
  'question.apply',
  'assignment.manage',
  'assignment.accept',
  'checkpoint.publish',
  'handoff.offer',
  'handoff.ready'
]);
/** @param {unknown} value @param {string[]} keys */
function exact(value, keys) {
  return (
    value !== null &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    Object.getPrototypeOf(value) === Object.prototype &&
    Reflect.ownKeys(value).length === keys.length &&
    keys.every((key) => Object.hasOwn(value, key))
  );
}
/** @param {unknown} value */
const uuid = (value) => typeof value === 'string' && UUID.test(value);
/** @param {unknown} value */
function validPolicy(value) {
  if (!exact(value, ['principal_id', 'workspace_id', 'stream_id', 'grants', 'work_jobs']))
    return false;
  const p = /** @type {any} */ (value);
  if (
    !uuid(p.principal_id) ||
    !uuid(p.workspace_id) ||
    !uuid(p.stream_id) ||
    !Array.isArray(p.grants) ||
    p.grants.length > 100 ||
    !Array.isArray(p.work_jobs) ||
    p.work_jobs.length > 1000
  )
    return false;
  const actors = new Set();
  for (const grant of p.grants) {
    if (
      !exact(grant, [...ACTOR, 'capabilities']) ||
      grant.principal_id !== p.principal_id ||
      !ACTOR.every((key) => {
        try {
          listenerId(grant[key]);
          return true;
        } catch {
          return false;
        }
      }) ||
      !Array.isArray(grant.capabilities) ||
      grant.capabilities.length > CAPABILITIES.size ||
      grant.capabilities.some(
        (/** @type {string} */ capability) => !CAPABILITIES.has(capability)
      ) ||
      new Set(grant.capabilities).size !== grant.capabilities.length
    )
      return false;
    const key = canonicalWorkJson(ACTOR.map((field) => grant[field]));
    if (actors.has(key)) return false;
    actors.add(key);
  }
  const workIds = new Set();
  for (const mapping of p.work_jobs) {
    if (!exact(mapping, ['work_id', 'job_id']) || !uuid(mapping.work_id)) return false;
    try {
      listenerId(mapping.job_id);
    } catch {
      return false;
    }
    if (workIds.has(mapping.work_id)) return false;
    workIds.add(mapping.work_id);
  }
  return true;
}
/** @param {{store:any,policy:any,now:()=>number,evaluated_at?:string,max_source_age_ms?:number}} input */
export function buildAuthorizedWorkView({
  store,
  policy,
  now,
  evaluated_at,
  max_source_age_ms = 300000
}) {
  if (
    !validPolicy(policy) ||
    !store ||
    typeof store.inspect !== 'function' ||
    typeof store.accumulated !== 'function' ||
    typeof store.handoffVerifications !== 'function' ||
    typeof now !== 'function'
  )
    return { status: 'blocked', code: 'INVALID_POLICY' };
  let scope;
  let accumulated;
  try {
    scope = store.inspect().scope;
    accumulated = store.accumulated();
  } catch {
    return { status: 'unavailable', code: 'STORE_UNAVAILABLE' };
  }
  if (
    scope.principal_id !== policy.principal_id ||
    scope.workspace_id !== policy.workspace_id ||
    scope.stream_id !== policy.stream_id
  )
    return { status: 'blocked', code: 'POLICY_SCOPE_MISMATCH' };
  if (
    accumulated.status !== 'ready' ||
    !accumulated.observation?.as_of ||
    !accumulated.observation?.last_successful_observation_at ||
    !Array.isArray(accumulated.events) ||
    !Array.isArray(accumulated.event_evidence)
  )
    return {
      status: 'unavailable',
      code: accumulated.status === 'blocked_limit' ? 'SOURCE_LIMIT' : 'SOURCE_UNAVAILABLE'
    };
  let clock;
  try {
    clock = now();
  } catch {
    return { status: 'blocked', code: 'INVALID_CLOCK' };
  }
  const observedAt = accumulated.observation.as_of;
  if (
    !Number.isFinite(clock) ||
    !Number.isFinite(Date.parse(observedAt)) ||
    Date.parse(observedAt) > clock
  )
    return { status: 'blocked', code: 'INVALID_CLOCK' };
  let receipts;
  try {
    receipts = store.handoffVerifications({ now: clock });
  } catch {
    return { status: 'unavailable', code: 'VERIFICATION_UNAVAILABLE' };
  }
  if (receipts.status !== 'ready')
    return { status: 'unavailable', code: 'VERIFICATION_UNAVAILABLE' };
  const inactive = new Set(receipts.inactive_ready_event_ids);
  const replay = (verifications) => replayWorkEvents({
    events: accumulated.events,
    trust: {
      workspace_id: policy.workspace_id,
      allowed_stream_ids: [policy.stream_id],
      event_evidence: accumulated.event_evidence,
      grants: policy.grants,
      handoff_verifications: verifications
    },
    observation: accumulated.observation,
    asOf: observedAt
  });
  let projection = replay(receipts.verifications);
  if (inactive.size) {
    // The displayed ready_event is only the first retained ready for a handoff.
    // Preserve proofs in each replay-accepted transfer's actual causal ancestry.
    const byId = new Map(accumulated.events.map((event) => [event.event_id, event]));
    const historicallyAccepted = new Set();
    const visited = new Set();
    const visit = (eventId) => {
      if (visited.has(eventId)) return;
      visited.add(eventId);
      const event = byId.get(eventId);
      if (!event) return;
      if (event.kind === 'handoff.ready') historicallyAccepted.add(eventId);
      for (const parentId of event.parents) visit(parentId);
    };
    for (const handoff of projection.handoffs) {
      if (!handoff.accepted_event) continue;
      visit(handoff.accepted_event.event_id);
      historicallyAccepted.add(handoff.accepted_event.payload.ready_event_id);
    }
    const current = receipts.verifications.filter(
      (verification) => !inactive.has(verification.ready_event_id) ||
        historicallyAccepted.has(verification.ready_event_id)
    );
    if (current.length !== receipts.verifications.length) projection = replay(current);
  }
  const evaluation = {
    projection,
    evaluated_at: evaluated_at ?? new Date(clock).toISOString(),
    max_source_age_ms
  };
  const presence = evaluateWorkPresence(evaluation),
    roles = evaluateWorkRoles(evaluation);
  if (presence.status !== 'ready' || roles.status !== 'ready')
    return { status: 'blocked', code: 'INVALID_EVALUATION' };
  return {
    status: 'ready',
    projection,
    evaluated_at: presence.evaluated_at,
    source_freshness: presence.source_freshness,
    max_source_age_ms,
    presence: presence.rows,
    roles: roles.rows
  };
}
