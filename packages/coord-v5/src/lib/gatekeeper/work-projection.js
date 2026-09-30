import { canonicalWorkJson, validateWorkEvent, workContentDigest } from './work-contract.js';
import { roleTransitionError, foldWorkRoles } from './work-roles.js';
import { presenceTransitionError, foldWorkPresence } from './work-presence.js';

/** @typedef {import('./work-contract.js').WorkEvent} WorkEvent */
/** @typedef {{code:string,event_ids?:string[],subject_id?:string,stream_id?:string}} WorkDiagnostic */
/** @typedef {{version:number,head_event_id:string|null,owner_id:string|null,accepted_actor:import('./work-contract.js').WorkActor|null,state:'unassigned'|'accepted'|'conflicted'}} WorkAssignment */
/** @typedef {{event_id:string,kind:string,event:WorkEvent,disposition:string}} WorkHistoryEntry */
/** @typedef {{work_id:string,workstream_id:string,item:import('./work-contract.js').WorkItem|null,head_event_id:string|null,event_ids:string[],history:WorkHistoryEntry[],assignment:WorkAssignment,state:string,execution_authority:string,provisional:boolean,validation:WorkDiagnostic[],branch_event_ids:string[]}} WorkRow */
/** @typedef {{question_id:string,workstream_id:string,opened_event:WorkEvent|null,event_ids:string[],history:WorkHistoryEntry[],acknowledgments:WorkEvent[],answers:WorkEvent[],current_answer:WorkEvent|null,applications:WorkEvent[],state:string,provisional:boolean,branch_event_ids:string[]}} QuestionRow */
/** @typedef {{schema:'gatekeeper-work-view/1',workspace_id:string|null,as_of:string|null,observation:WorkObservation,work:WorkRow[],questions:QuestionRow[],checkpoints:any[],handoffs:any[],presence:any[],roles:any[],conflicts:WorkDiagnostic[],pending:WorkDiagnostic[],rejected:WorkDiagnostic[]}} WorkProjection */
/** @typedef {{event:WorkEvent,ancestors:Set<string>}} Fact */
/** @typedef {{workspace_id:string,allowed_stream_ids:string[],event_evidence:{event_id:string,event_digest:string,record_id:string,source_principal_id:string,stream_id:string,received_at:string}[],grants:(import('./work-contract.js').WorkActor & {capabilities:string[]})[],handoff_verifications?:any[]}} WorkTrust */
/** @typedef {{coverage:string,as_of:string|null,last_successful_observation_at:string|null,sources:{stream_id:string,status:string,pending_pages:number|null}[],gaps:{code:string,stream_id?:string,event_id?:string}[],errors:{code:string,stream_id?:string}[],completeness_evidence_id:string|null}} WorkObservation */

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const ID = /^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$/;
const ACTOR_KEYS = ['principal_id', 'logical_agent_id', 'instance_id', 'session_id'];
const CAPABILITIES = [
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
];
const COVERAGE = ['complete', 'partial', 'unavailable', 'unsupported'];
const CAPABILITY = new Map([
  ['presence.observed', 'presence.publish'],
  ['role.defined', 'role.manage'],
  ['role.claimed', 'role.claim'],
  ['role.checkpoint', 'role.checkpoint'],
  ['role.resolved', 'role.manage'],
  ['handoff.offered', 'handoff.offer'],
  ['handoff.ready', 'handoff.ready'],
  ['checkpoint.published', 'checkpoint.publish'],
  ['work.opened', 'work.write'],
  ['work.revised', 'work.write'],
  ['assignment.offered', 'assignment.manage'],
  ['assignment.accepted', 'assignment.accept'],
  ['question.opened', 'question.ask'],
  ['question.acknowledged', 'question.answer'],
  ['question.answered', 'question.answer'],
  ['question.applied', 'question.apply']
]);
/** @param {any} value @param {string[]} keys @param {string[]} [optional] */
function shape(value, keys, optional = []) {
  return (
    value !== null &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    keys.every((key) => Object.hasOwn(value, key)) &&
    Object.keys(value).every((key) => keys.includes(key) || optional.includes(key))
  );
}
/** @param {any} value */
const uuid = (value) => typeof value === 'string' && UUID.test(value);
/** @param {any} value */
const identifier = (value) => typeof value === 'string' && ID.test(value);
/** @param {any} value @param {(v:any)=>boolean} predicate @param {number} [max] */
const array = (value, predicate, max = 100) =>
  Array.isArray(value) && value.length <= max && value.every(predicate);
/** @param {any[]} value */
const unique = (value) => new Set(value).size === value.length;
/** @param {any} a @param {any} b */
const sameActor = (a, b) => a && b && ACTOR_KEYS.every((key) => a[key] === b[key]);
/** @param {any} value */
function instant(value) {
  if (typeof value !== 'string') return false;
  const m =
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-](\d{2}):(\d{2}))$/.exec(
      value
    );
  if (!m) return false;
  const [y, month, day, h, min, s] = m.slice(1, 7).map(Number);
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
  const date = new Date(value);
  return (
    y > 0 &&
    month >= 1 &&
    month <= 12 &&
    day >= 1 &&
    day <= days[month - 1] &&
    h < 24 &&
    min < 60 &&
    s < 60 &&
    (!m[8] || (Number(m[8]) < 24 && Number(m[9]) < 60)) &&
    Number.isFinite(date.getTime()) &&
    date.getUTCFullYear() >= 1 &&
    date.getUTCFullYear() <= 9999
  );
}
/** @param {string} value */
function utc(value) {
  const fraction = /\.(\d+)(?:Z|[+-]\d{2}:\d{2})$/.exec(value)?.[1] ?? '';
  return `${new Date(value).toISOString().slice(0, 19)}.${fraction.padEnd(3, '0')}Z`;
}
/** Compare already-validated instants without dropping sub-millisecond precision.
 * Only receipt/observation bounds use clocks; event state never does.
 * @param {string} left @param {string} right */
export function compareInstants(left, right) {
  const a = utc(left);
  const b = utc(right);
  const seconds = a.slice(0, 19).localeCompare(b.slice(0, 19));
  if (seconds) return seconds;
  const af = a.slice(20, -1);
  const bf = b.slice(20, -1);
  const length = Math.max(af.length, bf.length);
  return af.padEnd(length, '0').localeCompare(bf.padEnd(length, '0'));
}
/** All context is copied through the strict JSON scanner before any property traversal.
 * @param {any} value */
function jsonCopy(value) {
  try {
    return JSON.parse(canonicalWorkJson(value));
  } catch {
    return null;
  }
}
/** @param {any} value */
function validTrust(value) {
  return (
    shape(
      value,
      ['workspace_id', 'allowed_stream_ids', 'event_evidence', 'grants'],
      ['handoff_verifications']
    ) &&
    uuid(value.workspace_id) &&
    array(value.allowed_stream_ids, uuid) &&
    unique(value.allowed_stream_ids) &&
    array(
      value.event_evidence,
      (e) =>
        shape(e, [
          'event_id',
          'event_digest',
          'record_id',
          'source_principal_id',
          'stream_id',
          'received_at'
        ]) &&
        uuid(e.event_id) &&
        typeof e.event_digest === 'string' &&
        /^[a-f0-9]{64}$/.test(e.event_digest) &&
        identifier(e.record_id) &&
        identifier(e.source_principal_id) &&
        uuid(e.stream_id) &&
        instant(e.received_at),
      1000
    ) &&
    array(
      value.grants,
      (g) =>
        shape(g, [...ACTOR_KEYS, 'capabilities']) &&
        ACTOR_KEYS.every((k) => identifier(g[k])) &&
        array(g.capabilities, (c) => CAPABILITIES.includes(c)) &&
        unique(g.capabilities)
    ) &&
    (value.handoff_verifications === undefined ||
      array(value.handoff_verifications, validHandoffVerification, 1000))
  );
}
/** @param {any} c */
function validHandoffChecks(c) {
  const status = ['verified', 'missing', 'denied', 'unknown'];
  const receipt = (/** @type {any} */ r) => r === null || identifier(r);
  return (
    shape(c, [
      'checked_at',
      'valid_until',
      'receiver',
      'package_digest',
      'publication',
      'resources'
    ]) &&
    instant(c.checked_at) &&
    instant(c.valid_until) &&
    compareInstants(c.checked_at, c.valid_until) < 0 &&
    shape(c.receiver, ACTOR_KEYS) &&
    ACTOR_KEYS.every((k) => identifier(c.receiver[k])) &&
    /^[a-f0-9]{64}$/.test(c.package_digest) &&
    shape(
      c.publication,
      ['event_id', 'artifact_id', 'body_digest', 'status', 'receipt_id'],
      ['artifact_sha256']
    ) &&
    uuid(c.publication.event_id) &&
    uuid(c.publication.artifact_id) &&
    /^[a-f0-9]{64}$/.test(c.publication.body_digest) &&
    (c.publication.artifact_sha256 === undefined ||
      /^[a-f0-9]{64}$/.test(c.publication.artifact_sha256)) &&
    status.includes(c.publication.status) &&
    receipt(c.publication.receipt_id) &&
    array(
      c.resources,
      (/** @type {any} */ r) =>
        shape(r, ['resource_id', 'scope_id', 'action', 'status', 'receipt_id']) &&
        uuid(r.resource_id) &&
        uuid(r.scope_id) &&
        r.action === 'read' &&
        status.includes(r.status) &&
        receipt(r.receipt_id)
    ) &&
    unique(c.resources.map((/** @type {any} */ r) => `${r.resource_id}:${r.scope_id}:${r.action}`))
  );
}
/** @param {any} v */
export function validHandoffVerification(v) {
  return (
    shape(v, [
      'ready_event_id',
      'ready_event_digest',
      'offer_event_id',
      'package_digest',
      'checks'
    ]) &&
    uuid(v.ready_event_id) &&
    /^[a-f0-9]{64}$/.test(v.ready_event_digest) &&
    uuid(v.offer_event_id) &&
    /^[a-f0-9]{64}$/.test(v.package_digest) &&
    validHandoffChecks(v.checks)
  );
}
/** @param {any} value */
function validObservation(value) {
  return (
    shape(value, [
      'coverage',
      'as_of',
      'last_successful_observation_at',
      'sources',
      'gaps',
      'errors',
      'completeness_evidence_id'
    ]) &&
    COVERAGE.includes(value.coverage) &&
    instant(value.as_of) &&
    (value.last_successful_observation_at === null ||
      (instant(value.last_successful_observation_at) &&
        compareInstants(value.last_successful_observation_at, value.as_of) <= 0)) &&
    (value.completeness_evidence_id === null ||
      (typeof value.completeness_evidence_id === 'string' &&
        value.completeness_evidence_id.length <= 128)) &&
    array(
      value.sources,
      (s) =>
        shape(s, ['stream_id', 'status', 'pending_pages']) &&
        uuid(s.stream_id) &&
        [...COVERAGE, 'error'].includes(s.status) &&
        (s.pending_pages === null ||
          (Number.isSafeInteger(s.pending_pages) && s.pending_pages >= 0))
    ) &&
    unique(value.sources.map((/** @type {any} */ s) => s.stream_id)) &&
    array(
      value.gaps,
      (g) =>
        shape(g, ['code'], ['stream_id', 'event_id']) &&
        identifier(g.code) &&
        (g.stream_id === undefined || uuid(g.stream_id)) &&
        (g.event_id === undefined || uuid(g.event_id))
    ) &&
    array(
      value.errors,
      (g) =>
        shape(g, ['code'], ['stream_id']) &&
        identifier(g.code) &&
        (g.stream_id === undefined || uuid(g.stream_id))
    )
  );
}
/** @param {any} a @param {any} b */
const compare = (a, b) => {
  const left = canonicalWorkJson(a);
  const right = canonicalWorkJson(b);
  return left < right ? -1 : left > right ? 1 : 0;
};
/** @param {WorkEvent} e @param {string} code @returns {WorkDiagnostic} */
const diagnostic = (e, code) => ({
  code,
  event_ids: [e.event_id],
  subject_id: e.subject.id,
  stream_id: e.stream_id
});
/** @param {Fact[]} facts */
const tips = (facts) =>
  facts.filter((f) => !facts.some((other) => other.ancestors.has(f.event.event_id)));
/** @param {Fact[]} facts @param {string} workId */
function workAt(facts, workId) {
  const own = facts.filter((f) => f.event.subject.type === 'work' && f.event.subject.id === workId);
  const heads = tips(
    own.filter((f) => f.event.kind === 'work.opened' || f.event.kind === 'work.revised')
  );
  const assignmentHeads = tips(
    own.filter(
      (f) => f.event.kind === 'assignment.accepted' || f.event.kind === 'assignment.released'
    )
  );
  const head = heads.length === 1 ? heads[0] : null;
  const assignmentHead = assignmentHeads.length === 1 ? assignmentHeads[0] : null;
  /** @type {WorkAssignment} */
  const assignment = {
    version: 0,
    head_event_id: null,
    owner_id: null,
    accepted_actor: null,
    state: 'unassigned'
  };
  if (assignmentHead) {
    const e = assignmentHead.event;
    const p = /** @type {any} */ (e.payload);
    assignment.version = p.expected_version + 1;
    assignment.head_event_id = e.event_id;
    if (e.kind === 'assignment.accepted') {
      assignment.state = 'accepted';
      assignment.accepted_actor = e.actor;
      assignment.owner_id = e.actor.logical_agent_id;
    }
  }
  const item = head
    ? structuredClone(
        /** @type {import('./work-contract.js').WorkItem} */ (head.event.payload.item)
      )
    : null;
  if (
    item &&
    assignmentHead &&
    (assignment.state === 'accepted' || !head?.ancestors.has(assignmentHead.event.event_id))
  )
    item.owner_id = assignment.owner_id;
  return { head, item, assignment, ambiguous: heads.length > 1 || assignmentHeads.length > 1 };
}
/** @param {Fact[]} facts @param {string} questionId */
function questionAt(facts, questionId) {
  const own = facts.filter(
    (f) => f.event.subject.type === 'question' && f.event.subject.id === questionId
  );
  const openings = own.filter((f) => f.event.kind === 'question.opened');
  const answers = tips(own.filter((f) => f.event.kind === 'question.answered'));
  return {
    opened: openings.length === 1 ? openings[0].event : null,
    answer: answers.length === 1 ? answers[0].event : null,
    ambiguous: openings.length > 1 || answers.length > 1
  };
}
/** Validate against causal ancestors only. Sorting incomparable events never grants authority.
 * @param {WorkEvent} e @param {Fact[]} facts @param {WorkTrust} trust @returns {string|null} */
function transitionError(e, facts, trust) {
  if (e.kind.startsWith('role.')) return roleTransitionError(e, facts, trust);
  if (e.kind === 'presence.observed') return presenceTransitionError(e, facts);
  const p = /** @type {any} */ (e.payload);
  const byId = new Map(facts.map((f) => [f.event.event_id, f.event]));
  if (e.kind === 'handoff.offered') {
    const w = workAt(facts, p.work_id);
    const publication = byId.get(p.checkpoint_event_id);
    if (
      !w.head ||
      w.ambiguous ||
      w.head.event.workstream_id !== e.workstream_id ||
      w.item?.type !== 'task' ||
      ['completed', 'cancelled'].includes(w.item.status)
    )
      return 'HANDOFF_WORK_REQUIRED';
    if (w.assignment.state !== 'accepted') return 'ACCEPTED_ASSIGNMENT_REQUIRED';
    if (
      p.expected_version !== w.assignment.version ||
      p.expected_assignment_event_id !== w.assignment.head_event_id
    )
      return 'STALE_ASSIGNMENT_HEAD';
    if (
      !publication ||
      publication.kind !== 'checkpoint.published' ||
      publication.payload.work_id !== p.work_id ||
      publication.workstream_id !== e.workstream_id ||
      publication.payload.assignment_version !== p.expected_version ||
      publication.payload.assignment_event_id !== p.expected_assignment_event_id
    )
      return 'CHECKPOINT_PUBLICATION_REQUIRED';
    if (
      !sameActor(e.actor, w.assignment.accepted_actor) &&
      !trust.grants.some(
        (g) => sameActor(g, e.actor) && g.capabilities.includes('assignment.manage')
      )
    )
      return 'HANDOFF_SENDER_REQUIRED';
    return null;
  }
  if (e.kind === 'handoff.ready') {
    const offer = byId.get(p.offer_event_id);
    if (
      !offer ||
      offer.kind !== 'handoff.offered' ||
      offer.subject.id !== e.subject.id ||
      offer.workstream_id !== e.workstream_id
    )
      return 'HANDOFF_OFFER_REQUIRED';
    if (
      !sameActor(e.actor, offer.payload.target) ||
      p.package_digest !== offer.payload.package_digest
    )
      return 'HANDOFF_BINDING_MISMATCH';
    const evidence = (trust.handoff_verifications ?? []).filter(
      (v) =>
        v.ready_event_id === e.event_id &&
        v.ready_event_digest === workContentDigest(e) &&
        v.offer_event_id === offer.event_id &&
        v.package_digest === p.package_digest
    );
    if (!evidence.length) return 'HANDOFF_VERIFICATION_REQUIRED';
    if (evidence.length !== 1) return 'HANDOFF_VERIFICATION_CONFLICT';
    const c = evidence[0].checks;
    const publication = byId.get(/** @type {string} */ (offer.payload.checkpoint_event_id));
    const artifactHash = /** @type {any} */ (publication?.payload.artifact)?.sha256;
    const artifactVerified =
      c.publication.artifact_sha256 === undefined
        ? artifactHash === null || artifactHash === publication?.payload.body_digest
        : c.publication.artifact_sha256 === artifactHash;
    if (
      !sameActor(c.receiver, e.actor) ||
      c.package_digest !== p.package_digest ||
      c.publication.event_id !== publication?.event_id ||
      c.publication.artifact_id !== /** @type {any} */ (publication?.payload.artifact)?.id ||
      c.publication.body_digest !== publication?.payload.body_digest ||
      !artifactVerified ||
      c.publication.status !== 'verified' ||
      !c.publication.receipt_id ||
      p.verification_receipt_id !== c.publication.receipt_id ||
      c.resources.some((/** @type {any} */ r) => r.status !== 'verified' || !r.receipt_id) ||
      compareInstants(c.checked_at, e.occurred_at) > 0 ||
      compareInstants(e.occurred_at, c.valid_until) >= 0
    )
      return 'HANDOFF_VERIFICATION_INVALID';
    return null;
  }
  if (e.kind === 'checkpoint.published') {
    const target = workAt(facts, p.work_id);
    if (!target.head || target.head.event.workstream_id !== e.workstream_id || target.ambiguous)
      return 'CHECKPOINT_WORK_ANCESTRY_REQUIRED';
    if (
      target.assignment.state === 'accepted' &&
      !sameActor(e.actor, target.assignment.accepted_actor)
    )
      return 'CURRENT_WORKER_REQUIRED';
    if (
      p.assignment_version !== target.assignment.version ||
      p.assignment_event_id !== target.assignment.head_event_id
    )
      return 'STALE_ASSIGNMENT_HEAD';
    return null;
  }
  const w = workAt(facts, e.subject.id);
  if (e.kind === 'work.opened' || e.kind === 'question.opened') return null;
  if (e.subject.type === 'work') {
    if (!w.head || !w.item) return 'WORK_ANCESTRY_REQUIRED';
    if (w.ambiguous) return 'CONFLICTED_ANCESTRY';
    if (w.head.event.workstream_id !== e.workstream_id) return 'IDENTITY_MISMATCH';
  }
  if (e.kind === 'work.revised') {
    if (w.head?.event.event_id !== p.expected_event_id) return 'STALE_WORK_HEAD';
    const before = /** @type {import('./work-contract.js').WorkItem} */ (w.item);
    const after = p.item;
    if (before.type !== after.type) return 'ITEM_TYPE_IMMUTABLE';
    if (['completed', 'cancelled', 'closed'].includes(before.status)) return 'TERMINAL_WORK';
    const allowed = new Map([
      ['proposed', ['ready', 'cancelled']],
      ['ready', ['active', 'blocked', 'paused', 'cancelled']],
      ['active', ['blocked', 'paused', 'completed', 'cancelled']],
      ['blocked', ['ready', 'active', 'cancelled']],
      ['paused', ['ready', 'active', 'cancelled']],
      ['open', ['closed']],
      ['deferred', ['closed']]
    ]);
    if (before.status !== after.status && !allowed.get(before.status)?.includes(after.status))
      return 'INVALID_TRANSITION';
    if (
      before.status !== after.status &&
      (['blocked', 'paused', 'cancelled', 'closed'].includes(after.status) ||
        ['blocked', 'paused'].includes(before.status)) &&
      !p.reason
    )
      return 'REASON_REQUIRED';
    if (w.assignment.state === 'accepted' && after.owner_id !== w.assignment.owner_id)
      return 'ACCEPTED_OWNER_IMMUTABLE';
    if (after.status === 'active' && w.assignment.state !== 'accepted')
      return 'ACCEPTED_ASSIGNMENT_REQUIRED';
    if (after.blocker?.kind === 'question') {
      const q = questionAt(facts, after.blocker.ref_id);
      const linkedWork = /** @type {string[]|undefined} */ (q.opened?.payload.work_ids);
      if (
        !q.opened ||
        q.ambiguous ||
        q.opened.workstream_id !== e.workstream_id ||
        !linkedWork?.includes(e.subject.id)
      )
        return 'LINKED_QUESTION_REQUIRED';
    }
    if (
      before.blocker?.kind === 'question' &&
      ['blocked', 'paused'].includes(before.status) &&
      ['ready', 'active'].includes(after.status)
    ) {
      const q = questionAt(facts, before.blocker.ref_id ?? '');
      if (
        !q.answer ||
        q.ambiguous ||
        !facts.some(
          (f) =>
            f.event.kind === 'question.applied' &&
            f.event.subject.id === before.blocker?.ref_id &&
            f.event.payload.answer_event_id === q.answer?.event_id &&
            f.event.payload.work_id === e.subject.id &&
            f.event.payload.condition_id === before.blocker?.condition_id
        )
      )
        return 'ANSWER_APPLICATION_REQUIRED';
    }
    return null;
  }
  if (e.kind.startsWith('assignment.')) {
    if (w.item?.type !== 'task' || ['completed', 'cancelled'].includes(w.item.status))
      return 'ASSIGNABLE_TASK_REQUIRED';
    if (
      p.expected_version !== w.assignment.version ||
      p.expected_assignment_event_id !== w.assignment.head_event_id
    )
      return 'STALE_ASSIGNMENT_HEAD';
    if (e.kind === 'assignment.offered') return null;
    if (e.kind === 'assignment.accepted') {
      const offer = byId.get(p.offer_event_id);
      if (
        !offer ||
        !['assignment.offered', 'handoff.offered'].includes(offer.kind) ||
        (offer.kind === 'assignment.offered'
          ? offer.subject.id !== e.subject.id
          : offer.payload.work_id !== e.subject.id) ||
        offer.workstream_id !== e.workstream_id
      )
        return 'OFFER_REQUIRED';
      const offered = /** @type {any} */ (offer.payload);
      if (!sameActor(e.actor, offered.target)) return 'OFFER_TARGET_MISMATCH';
      if (
        offered.expected_version !== p.expected_version ||
        offered.expected_assignment_event_id !== p.expected_assignment_event_id
      )
        return 'STALE_OFFER';
      if (offer.kind === 'handoff.offered') {
        if (w.assignment.state !== 'accepted' || p.ready_event_id === null)
          return 'HANDOFF_READY_REQUIRED';
        const ready = byId.get(p.ready_event_id);
        if (
          !ready ||
          ready.kind !== 'handoff.ready' ||
          ready.payload.offer_event_id !== offer.event_id ||
          ready.payload.package_digest !== offered.package_digest ||
          ready.workstream_id !== e.workstream_id
        )
          return 'HANDOFF_READY_REQUIRED';
        const proof = (trust.handoff_verifications ?? []).find(
          (v) =>
            v.ready_event_id === ready.event_id &&
            v.ready_event_digest === workContentDigest(ready) &&
            v.offer_event_id === offer.event_id &&
            v.package_digest === offered.package_digest
        );
        if (
          !proof ||
          compareInstants(proof.checks.checked_at, e.occurred_at) > 0 ||
          compareInstants(e.occurred_at, proof.checks.valid_until) >= 0
        )
          return 'HANDOFF_VERIFICATION_REQUIRED';
        return null;
      }
      if (w.assignment.state === 'accepted' || p.ready_event_id !== null) return 'HANDOFF_REQUIRED';
      if (w.item?.owner_id !== null && w.item?.owner_id !== e.actor.logical_agent_id)
        return 'DECLARED_OWNER_MISMATCH';
      return null;
    }
    if (w.assignment.state !== 'accepted') return 'ACCEPTED_ASSIGNMENT_REQUIRED';
    if (
      !sameActor(e.actor, w.assignment.accepted_actor) &&
      !trust.grants.some(
        (g) => sameActor(g, e.actor) && g.capabilities.includes('assignment.manage')
      )
    )
      return 'RELEASE_NOT_AUTHORIZED';
    return null;
  }
  const q = questionAt(facts, e.subject.id);
  if (!q.opened || q.ambiguous) return 'QUESTION_ANCESTRY_REQUIRED';
  if (q.opened.workstream_id !== e.workstream_id) return 'IDENTITY_MISMATCH';
  if (e.kind === 'question.acknowledged' || e.kind === 'question.answered') {
    if (p.opened_event_id !== q.opened.event_id) return 'QUESTION_OPENING_MISMATCH';
    if (e.actor.logical_agent_id !== q.opened.payload.decision_maker_id)
      return 'DECISION_MAKER_REQUIRED';
    if (
      e.kind === 'question.answered' &&
      p.supersedes_answer_event_id !== (q.answer?.event_id ?? null)
    )
      return 'LATEST_ANSWER_REQUIRED';
    return null;
  }
  if (e.kind === 'question.applied') {
    if (q.answer?.event_id !== p.answer_event_id) return 'LATEST_ANSWER_REQUIRED';
    const linked = /** @type {string[]} */ (q.opened.payload.work_ids);
    const target = workAt(facts, p.work_id);
    if (
      !linked.includes(p.work_id) ||
      !target.head ||
      target.head.event.workstream_id !== e.workstream_id ||
      target.ambiguous
    )
      return 'LINKED_WORK_REQUIRED';
    if (
      target.assignment.state !== 'accepted' ||
      !sameActor(e.actor, target.assignment.accepted_actor)
    )
      return 'CURRENT_WORKER_REQUIRED';
    if (
      target.item?.blocker?.kind !== 'question' ||
      target.item.blocker.ref_id !== e.subject.id ||
      target.item.blocker.condition_id !== p.condition_id
    )
      return 'EXACT_CONDITION_REQUIRED';
    return null;
  }
  return 'UNSUPPORTED_KIND';
}

/** Pure rebuild of caller-supplied accumulated history. No trust discovery, persistence, or network.
 * @param {{events?:unknown,trust?:unknown,observation?:unknown,asOf?:unknown}} input
 * @returns {WorkProjection} */
export function replayWorkEvents(input = {}) {
  const trustCandidate = jsonCopy(input.trust);
  const trust = validTrust(trustCandidate) ? /** @type {WorkTrust} */ (trustCandidate) : null;
  const asOf = instant(input.asOf) ? utc(/** @type {string} */ (input.asOf)) : null;
  const suppliedObservation = jsonCopy(input.observation);
  /** @type {WorkObservation} */
  const observation = validObservation(suppliedObservation)
    ? suppliedObservation
    : {
        coverage: 'unavailable',
        as_of: asOf,
        last_successful_observation_at: null,
        sources: [],
        gaps: [],
        errors: [],
        completeness_evidence_id: null
      };
  if (observation.as_of) observation.as_of = utc(observation.as_of);
  if (observation.last_successful_observation_at)
    observation.last_successful_observation_at = utc(observation.last_successful_observation_at);
  /** @type {WorkProjection} */
  const output = {
    schema: 'gatekeeper-work-view/1',
    workspace_id: trust?.workspace_id ?? null,
    as_of: asOf,
    observation,
    work: [],
    questions: [],
    checkpoints: [],
    handoffs: [],
    presence: [],
    roles: [],
    conflicts: [],
    pending: [],
    rejected: []
  };
  /** @type {WorkEvent[]} */ const candidates = [];
  const raw = input.events;
  // Validate the array container without traversing candidates: one malformed record
  // must not prevent independent well-formed records from being replayed.
  const safeBatch =
    Array.isArray(raw) &&
    Object.getPrototypeOf(raw) === Array.prototype &&
    raw.length <= 1000 &&
    Reflect.ownKeys(raw).every(
      (key) =>
        key === 'length' ||
        (typeof key === 'string' && /^(0|[1-9]\d*)$/.test(key) && Number(key) < raw.length)
    ) &&
    Array.from({ length: raw.length }, (_, i) => Object.getOwnPropertyDescriptor(raw, i)).every(
      (d) => d && Object.hasOwn(d, 'value') && d.enumerable
    );
  if (!safeBatch || !Array.isArray(raw)) output.rejected.push({ code: 'INVALID_EVENT_BATCH' });
  else if (!trust || !asOf)
    output.rejected.push({ code: !trust ? 'INVALID_TRUST' : 'INVALID_AS_OF' });
  else
    for (const value of raw) {
      const result = validateWorkEvent(value);
      if (!result.ok) {
        output.rejected.push({ code: 'MALFORMED_EVENT' });
        continue;
      }
      const e = result.event;
      // Scope and exact external receipt/actor binding precede all identity grouping.
      if (
        e.workspace_id !== trust.workspace_id ||
        !trust.allowed_stream_ids.includes(e.stream_id)
      ) {
        output.rejected.push({ code: 'DENIED_SCOPE' });
        continue;
      }
      if (
        (e.kind.startsWith('role.') || e.kind === 'presence.observed') &&
        compareInstants(e.occurred_at, asOf) > 0
      ) {
        output.rejected.push(diagnostic(e, 'FUTURE_COORDINATION_EVENT'));
        continue;
      }
      const receipt = trust.event_evidence.some(
        (r) =>
          r.event_id === e.event_id &&
          r.event_digest === workContentDigest(e) &&
          r.source_principal_id === e.actor.principal_id &&
          r.stream_id === e.stream_id &&
          compareInstants(r.received_at, asOf) <= 0
      );
      const grant = trust.grants.some(
        (g) =>
          sameActor(g, e.actor) &&
          (e.kind === 'role.released'
            ? g.capabilities.includes('role.claim') || g.capabilities.includes('role.manage')
            : e.kind === 'assignment.released'
              ? g.capabilities.includes('assignment.accept') ||
                g.capabilities.includes('assignment.manage')
              : e.kind === 'handoff.offered'
                ? g.capabilities.includes('handoff.offer') ||
                  g.capabilities.includes('assignment.manage')
                : g.capabilities.includes(CAPABILITY.get(e.kind) ?? ''))
      );
      if (!receipt || !grant) {
        output.rejected.push({ code: 'UNTRUSTED_EVENT', stream_id: e.stream_id });
        continue;
      }
      candidates.push(e);
    }
  const all = [...new Map(candidates.map((e) => [canonicalWorkJson(e), e])).values()].sort(
    (a, b) => a.event_id.localeCompare(b.event_id) || compare(a, b)
  );
  const byId = new Map(all.map((e) => [e.event_id, e]));
  /** @type {Set<string>} */ const quarantined = new Set();
  /** @type {Map<string,string>} */ const disposition = new Map();
  /** @param {string} code @param {WorkEvent[]} group */
  function conflict(code, group) {
    const ids = [...new Set(group.map((e) => e.event_id))].sort();
    const subjects = [...new Set(group.map((e) => e.subject.id))].sort();
    for (const subject_id of subjects) output.conflicts.push({ code, subject_id, event_ids: ids });
    for (const id of ids) quarantined.add(id);
  }
  /** @param {WorkEvent[]} events @param {(e:WorkEvent)=>string|null} key @param {string} code */
  function groupedConflicts(events, key, code) {
    /** @type {Map<string,WorkEvent[]>} */ const groups = new Map();
    for (const e of events) {
      const k = key(e);
      if (k !== null) groups.set(k, [...(groups.get(k) ?? []), e]);
    }
    for (const group of groups.values()) if (group.length > 1) conflict(code, group);
  }
  groupedConflicts(all, (e) => e.event_id, 'EVENT_ID_CONFLICT');
  groupedConflicts(all, (e) => e.operation_id, 'OPERATION_ID_CONFLICT');
  /** @param {Set<string>} ids */
  function expandDescendants(ids) {
    let changed = true;
    while (changed) {
      changed = false;
      for (const e of all)
        if (!ids.has(e.event_id) && e.parents.some((id) => ids.has(id))) {
          ids.add(e.event_id);
          changed = true;
        }
    }
  }
  expandDescendants(quarantined);
  /** @type {Map<string,Fact>} */ const facts = new Map();
  const remaining = new Map([...byId].filter(([id]) => !quarantined.has(id)));
  // Kahn traversal only schedules evaluation: every semantic read is ancestor-scoped.
  let progressed = true;
  while (progressed) {
    progressed = false;
    for (const [id, e] of remaining) {
      if (e.parents.some((p) => remaining.has(p))) continue;
      remaining.delete(id);
      progressed = true;
      if (e.parents.some((p) => !byId.has(p))) {
        disposition.set(id, 'pending');
        output.pending.push(diagnostic(e, 'MISSING_PARENT'));
        continue;
      }
      if (e.parents.some((p) => !facts.has(p))) {
        disposition.set(id, 'pending');
        output.pending.push(diagnostic(e, 'UNAVAILABLE_PARENT'));
        continue;
      }
      const ancestors = new Set(e.parents);
      for (const p of e.parents)
        for (const ancestor of facts.get(p)?.ancestors ?? []) ancestors.add(ancestor);
      const causalFacts = [...ancestors].map((p) => /** @type {Fact} */ (facts.get(p)));
      const error = transitionError(e, causalFacts, /** @type {WorkTrust} */ (trust));
      if (error) {
        const pending = error === 'HANDOFF_VERIFICATION_REQUIRED';
        disposition.set(id, pending ? 'pending' : 'rejected');
        output[pending ? 'pending' : 'rejected'].push(diagnostic(e, error));
      } else {
        disposition.set(id, 'applied');
        facts.set(id, { event: e, ancestors });
      }
    }
  }
  for (const e of remaining.values()) {
    const visited = new Set();
    const stack = [...e.parents];
    let cycle = false;
    while (stack.length) {
      const id = /** @type {string} */ (stack.pop());
      if (id === e.event_id) {
        cycle = true;
        break;
      }
      if (!visited.has(id)) {
        visited.add(id);
        stack.push(...(remaining.get(id)?.parents ?? []));
      }
    }
    disposition.set(e.event_id, 'pending');
    output.pending.push(diagnostic(e, cycle ? 'CAUSAL_CYCLE' : 'UNAVAILABLE_PARENT'));
  }
  const applied = [...facts.values()].map((f) => f.event);
  groupedConflicts(
    applied,
    (e) => (e.kind === 'checkpoint.published' ? e.subject.id : null),
    'CHECKPOINT_PUBLICATION_CONFLICT'
  );
  groupedConflicts(
    applied,
    (e) => (e.kind.endsWith('.opened') ? `${e.subject.type}:${e.subject.id}` : null),
    'MULTIPLE_OPENINGS'
  );
  groupedConflicts(
    applied,
    (e) => (e.kind === 'work.revised' ? `${e.subject.id}:${e.payload.expected_event_id}` : null),
    'WORK_REVISION_CONFLICT'
  );
  groupedConflicts(
    applied,
    (e) =>
      e.kind === 'assignment.accepted' || e.kind === 'assignment.released'
        ? `${e.subject.id}:${e.payload.expected_assignment_event_id}`
        : null,
    'ASSIGNMENT_CONFLICT'
  );
  groupedConflicts(
    applied,
    (e) =>
      e.kind === 'question.answered'
        ? `${e.subject.id}:${e.payload.supersedes_answer_event_id}`
        : null,
    'ANSWER_CONFLICT'
  );
  // Concurrent owner edits and acceptance cannot manufacture a new responsibility order.
  for (const a of facts.values())
    if (a.event.kind === 'assignment.accepted')
      for (const b of facts.values()) {
        if (
          b.event.kind !== 'work.revised' ||
          b.event.subject.id !== a.event.subject.id ||
          a.ancestors.has(b.event.event_id) ||
          b.ancestors.has(a.event.event_id)
        )
          continue;
        const predecessor = workAt(
          [...b.ancestors].map((id) => /** @type {Fact} */ (facts.get(id))),
          b.event.subject.id
        );
        const revisedOwner = /** @type {import('./work-contract.js').WorkItem} */ (
          b.event.payload.item
        ).owner_id;
        if (
          revisedOwner !== predecessor.item?.owner_id &&
          revisedOwner !== a.event.actor.logical_agent_id
        )
          conflict('ASSIGNMENT_OWNER_CONFLICT', [a.event, b.event]);
      }
  expandDescendants(quarantined);
  for (const id of quarantined) disposition.set(id, 'conflicted');
  const retained = [...facts.values()].filter((f) => !quarantined.has(f.event.event_id));
  output.roles = foldWorkRoles(retained);
  output.presence = foldWorkPresence(retained);
  /** @param {WorkEvent[]} events @returns {WorkHistoryEntry[]} */
  const history = (events) =>
    events.map((e) => ({
      event_id: e.event_id,
      kind: e.kind,
      event: e,
      disposition: disposition.get(e.event_id) ?? 'pending'
    }));
  const workIds = [
    ...new Set(all.filter((e) => e.subject.type === 'work').map((e) => e.subject.id))
  ].sort();
  for (const work_id of workIds) {
    const events = all.filter((e) => e.subject.type === 'work' && e.subject.id === work_id);
    const w = workAt(retained, work_id);
    const branch_event_ids = [
      ...new Set(events.filter((e) => quarantined.has(e.event_id)).map((e) => e.event_id))
    ].sort();
    const assignmentConflict = events.some(
      (e) =>
        quarantined.has(e.event_id) &&
        e.kind.startsWith('assignment.') &&
        e.kind !== 'assignment.offered'
    );
    if (assignmentConflict) w.assignment.state = 'conflicted';
    output.work.push({
      work_id,
      workstream_id: w.head?.event.workstream_id ?? events[0].workstream_id,
      item: w.item,
      head_event_id: w.head?.event.event_id ?? null,
      event_ids: [...new Set(events.map((e) => e.event_id))].sort(),
      history: history(events),
      assignment: w.assignment,
      state: branch_event_ids.length ? 'conflicted' : w.item ? 'current' : 'pending',
      execution_authority: branch_event_ids.length
        ? 'blocked_conflict'
        : w.assignment.state === 'accepted'
          ? 'accepted'
          : 'unassigned',
      provisional: false,
      validation: [],
      branch_event_ids
    });
  }
  for (const question_id of [
    ...new Set(all.filter((e) => e.subject.type === 'question').map((e) => e.subject.id))
  ].sort()) {
    const events = all.filter((e) => e.subject.type === 'question' && e.subject.id === question_id);
    const valid = retained
      .filter((f) => f.event.subject.type === 'question' && f.event.subject.id === question_id)
      .map((f) => f.event);
    const q = questionAt(retained, question_id);
    const branch_event_ids = [
      ...new Set(events.filter((e) => quarantined.has(e.event_id)).map((e) => e.event_id))
    ].sort();
    output.questions.push({
      question_id,
      workstream_id: q.opened?.workstream_id ?? events[0].workstream_id,
      opened_event: q.opened,
      event_ids: [...new Set(events.map((e) => e.event_id))].sort(),
      history: history(events),
      acknowledgments: valid.filter((e) => e.kind === 'question.acknowledged'),
      answers: valid.filter((e) => e.kind === 'question.answered'),
      current_answer: q.answer,
      applications: valid.filter((e) => e.kind === 'question.applied'),
      state: branch_event_ids.length ? 'conflicted' : q.opened ? 'current' : 'pending',
      provisional: false,
      branch_event_ids
    });
  }
  const workMap = new Map(output.work.map((w) => [w.work_id, w]));
  for (const checkpoint_id of [
    ...new Set(all.filter((e) => e.subject.type === 'checkpoint').map((e) => e.subject.id))
  ].sort()) {
    const events = all.filter(
      (e) => e.subject.type === 'checkpoint' && e.subject.id === checkpoint_id
    );
    const valid = retained.filter(
      (f) => f.event.subject.type === 'checkpoint' && f.event.subject.id === checkpoint_id
    );
    const branch_event_ids = [
      ...new Set(events.filter((e) => quarantined.has(e.event_id)).map((e) => e.event_id))
    ].sort();
    output.checkpoints.push({
      checkpoint_id,
      work_id: events[0].payload.work_id,
      workstream_id: events[0].workstream_id,
      publication_event: valid.length === 1 ? valid[0].event : null,
      event_ids: [...new Set(events.map((e) => e.event_id))].sort(),
      history: history(events),
      branch_event_ids,
      state: branch_event_ids.length ? 'conflicted' : valid.length === 1 ? 'current' : 'pending',
      provisional: false
    });
  }
  for (const handoff_id of [
    ...new Set(all.filter((e) => e.subject.type === 'handoff').map((e) => e.subject.id))
  ].sort()) {
    const events = all.filter((e) => e.subject.type === 'handoff' && e.subject.id === handoff_id);
    const valid = retained
      .map((f) => f.event)
      .filter((e) => e.subject.type === 'handoff' && e.subject.id === handoff_id);
    const offer = valid.find((e) => e.kind === 'handoff.offered') ?? null;
    const ready = valid.find((e) => e.kind === 'handoff.ready') ?? null;
    const accepted = offer
      ? (retained
          .map((f) => f.event)
          .find(
            (e) => e.kind === 'assignment.accepted' && e.payload.offer_event_id === offer.event_id
          ) ?? null)
      : null;
    const competingAccepts = offer
      ? all.filter(
          (e) =>
            e.kind === 'assignment.accepted' &&
            e.payload.offer_event_id === offer.event_id &&
            quarantined.has(e.event_id)
        )
      : [];
    const branch_event_ids = [
      ...new Set(
        [...events, ...competingAccepts]
          .filter((e) => quarantined.has(e.event_id))
          .map((e) => e.event_id)
      )
    ].sort();
    const proof =
      ready &&
      (trust?.handoff_verifications ?? []).find(
        (v) =>
          v.ready_event_id === ready.event_id && v.ready_event_digest === workContentDigest(ready)
      );
    const expired = !!(proof && asOf && compareInstants(asOf, proof.checks.valid_until) >= 0);
    const state = branch_event_ids.length
      ? 'conflicted'
      : accepted
        ? 'accepted'
        : expired
          ? 'blocked'
          : ready
            ? 'ready'
            : offer
              ? 'offered'
              : 'blocked';
    output.handoffs.push({
      handoff_id,
      work_id: offer?.payload.work_id ?? null,
      workstream_id: events[0].workstream_id,
      offer_event: offer,
      ready_event: ready,
      accepted_event: accepted,
      event_ids: [...new Set(events.map((e) => e.event_id))].sort(),
      history: history(events),
      branch_event_ids,
      state,
      provisional: false
    });
  }
  for (const w of output.work) {
    for (const dependency of w.item?.dependency_ids ?? [])
      if (!workMap.get(dependency)?.item)
        w.validation.push({ code: 'MISSING_DEPENDENCY', subject_id: w.work_id });
    /** @param {string} current @param {Set<string>} visited @returns {boolean} */
    const reachesSelf = (current, visited) => {
      if (visited.has(current)) return current === w.work_id;
      visited.add(current);
      return (workMap.get(current)?.item?.dependency_ids ?? []).some((next) =>
        reachesSelf(next, visited)
      );
    };
    if (reachesSelf(w.work_id, new Set()))
      w.validation.push({ code: 'DEPENDENCY_CYCLE', subject_id: w.work_id });
    if (w.item?.result?.evidence.some((e) => !e.artifact.portable))
      w.validation.push({ code: 'NONPORTABLE_RESULT', subject_id: w.work_id });
    if (
      w.validation.some((d) => d.code !== 'NONPORTABLE_RESULT') &&
      w.execution_authority !== 'blocked_conflict'
    )
      w.execution_authority = 'blocked_validation';
  }
  // Remove stale pre-conflict diagnostics for quarantined descendants; retain safe conflict facts.
  output.pending = output.pending.filter((d) => !d.event_ids?.some((id) => quarantined.has(id)));
  output.rejected = output.rejected.filter((d) => !d.event_ids?.some((id) => quarantined.has(id)));
  for (const d of [...output.pending, ...output.rejected])
    observation.gaps.push({ code: d.code, ...(d.stream_id ? { stream_id: d.stream_id } : {}) });
  const complete =
    observation.completeness_evidence_id?.trim() &&
    observation.sources.length > 0 &&
    observation.sources.every((s) => s.status === 'complete' && s.pending_pages === 0) &&
    observation.gaps.length === 0 &&
    observation.errors.length === 0 &&
    asOf &&
    observation.as_of &&
    compareInstants(asOf, observation.as_of) === 0 &&
    trust &&
    trust.allowed_stream_ids.every((id) => observation.sources.some((s) => s.stream_id === id));
  if (observation.coverage === 'complete' && !complete) observation.coverage = 'partial';
  for (const row of [
    ...output.work,
    ...output.questions,
    ...output.checkpoints,
    ...output.handoffs,
    ...output.roles,
    ...output.presence
  ])
    row.provisional = observation.coverage !== 'complete';
  /** @template T @param {T[]} values @returns {T[]} */
  const stableUnique = (values) =>
    [...new Map(values.map((d) => [canonicalWorkJson(d), d])).values()].sort(compare);
  output.conflicts = stableUnique(output.conflicts);
  output.pending = stableUnique(output.pending);
  output.rejected = stableUnique(output.rejected);
  observation.sources = stableUnique(observation.sources);
  observation.gaps = stableUnique(observation.gaps);
  observation.errors = stableUnique(observation.errors);
  return output;
}
