import { canonicalWorkJson } from '../../gatekeeper/work-contract.js';
import { replayWorkEvents } from '../../gatekeeper/work-projection.js';
import { id as listenerId } from './listener-validation.js';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const ACTOR = ['principal_id', 'logical_agent_id', 'instance_id', 'session_id'];
const CAPABILITIES = new Set([
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
/** @param {{store:any,policy:any,now:()=>number}} input */
export function buildAuthorizedWorkView({ store, policy, now }) {
  if (
    !validPolicy(policy) ||
    !store ||
    typeof store.inspect !== 'function' ||
    typeof store.accumulated !== 'function' ||
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
  const projection = replayWorkEvents({
    events: accumulated.events,
    trust: {
      workspace_id: policy.workspace_id,
      allowed_stream_ids: [policy.stream_id],
      event_evidence: accumulated.event_evidence,
      grants: policy.grants
    },
    observation: accumulated.observation,
    asOf: observedAt
  });
  return { status: 'ready', projection };
}
