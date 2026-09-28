import { createHash } from 'node:crypto';
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
const TERMINAL = new Set(['completed', 'cancelled', 'closed']);
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
/** @param {unknown} value */
function revision(value) {
  return `sha256:${createHash('sha256').update(canonicalWorkJson(value)).digest('hex')}`;
}
/** @param {{store:any,policy:any,now:()=>number}} input */
export function buildWorkListenerObservation({ store, policy, now }) {
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
  /** @type {Map<string,string>} */
  const mapped = new Map(
    policy.work_jobs.map((/** @type {any} */ entry) => [entry.work_id, entry.job_id])
  );
  /** @type {{itemId:string,jobId:string,revision:string}[]} */
  const events = [];
  /** @type {{itemId:string,jobId:string,revision:string}[]} */
  const obligations = [];
  /** @type {{code:string,work_id?:string,subject_id?:string}[]} */
  const diagnostics = [];
  /** @type {Set<string>} */
  const unmapped = new Set();
  /** @param {string} workId @param {string} itemId @param {unknown} content @param {boolean} pending */
  function include(workId, itemId, content, pending) {
    const jobId = mapped.get(workId);
    if (!jobId) {
      if (!unmapped.has(workId)) diagnostics.push({ code: 'UNMAPPED_WORK', work_id: workId });
      unmapped.add(workId);
      return;
    }
    const item = { itemId, jobId, revision: revision(content) };
    events.push(item);
    if (pending) obligations.push(item);
  }
  for (const work of projection.work) {
    if (!work.item) continue;
    include(
      work.work_id,
      `work:${work.work_id}`,
      {
        item: work.item,
        head_event_id: work.head_event_id,
        assignment: work.assignment,
        applied_event_ids: work.history
          .filter((entry) => entry.disposition === 'applied')
          .map((entry) => entry.event_id)
          .sort(),
        state: work.state,
        execution_authority: work.execution_authority,
        validation: work.validation.map((entry) => entry.code).sort(),
        branch_event_ids: [...work.branch_event_ids].sort()
      },
      !TERMINAL.has(work.item.status)
    );
  }
  for (const question of projection.questions) {
    if (!question.opened_event) continue;
    const ids = [
      ...new Set(/** @type {string[]} */ (question.opened_event.payload.work_ids))
    ].sort();
    for (const workId of ids)
      include(
        workId,
        `question:${question.question_id}`,
        {
          opened_event: question.opened_event,
          current_answer: question.current_answer,
          acknowledgments: question.acknowledgments.map((entry) => entry.event_id).sort(),
          applications: question.applications.map((entry) => entry.event_id).sort(),
          state: question.state,
          branch_event_ids: [...question.branch_event_ids].sort()
        },
        !question.current_answer || question.applications.length === 0
      );
  }
  /** @type {Map<string,{code:string,event_ids:string[]}[]>} */
  const conflicts = new Map();
  for (const conflict of projection.conflicts) {
    const subject = conflict.subject_id;
    if (!subject) continue;
    if (!conflicts.has(subject)) conflicts.set(subject, []);
    /** @type {{code:string,event_ids:string[]}[]} */ (conflicts.get(subject)).push({
      code: conflict.code,
      event_ids: [...(conflict.event_ids ?? [])].sort()
    });
  }
  for (const [subject, rows] of conflicts) {
    const question = projection.questions.find((q) => q.question_id === subject);
    const linked = /** @type {string[]} */ (
      question?.opened_event?.payload.work_ids ??
        question?.history
          .filter((entry) => entry.event.kind === 'question.opened')
          .flatMap((entry) => entry.event.payload.work_ids) ??
        []
    );
    for (const workId of linked.length ? [...new Set(linked)].sort() : [subject]) {
      if (!mapped.has(workId)) continue;
      include(
        workId,
        `conflict:${subject}`,
        rows.sort((a, b) => canonicalWorkJson(a).localeCompare(canonicalWorkJson(b))),
        true
      );
    }
  }
  events.sort((a, b) => a.jobId.localeCompare(b.jobId) || a.itemId.localeCompare(b.itemId));
  obligations.sort((a, b) => a.jobId.localeCompare(b.jobId) || a.itemId.localeCompare(b.itemId));
  if (events.length > 1000 || obligations.length > 1000)
    return { status: 'blocked', code: 'OBSERVATION_LIMIT' };
  for (const entry of projection.rejected) diagnostics.push({ code: entry.code });
  for (const entry of projection.pending) diagnostics.push({ code: entry.code });
  for (const entry of projection.conflicts)
    diagnostics.push({ code: entry.code, subject_id: entry.subject_id });
  const coverage = projection.observation.coverage === 'complete' ? 'complete' : 'partial';
  return {
    observation: {
      version: 1,
      eventObservation: { coverage, items: events },
      obligationObservation: { coverage, items: obligations },
      observedAt
    },
    diagnostics
  };
}
