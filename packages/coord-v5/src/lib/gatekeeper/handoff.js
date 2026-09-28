import { relevantHandoffEvents, validateHandoffPackage } from './checkpoint.js';
import { canonicalWorkJson, validateWorkEvent, workContentDigest } from './work-contract.js';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const ID = /^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$/;
const ACTOR = ['principal_id', 'logical_agent_id', 'instance_id', 'session_id'];
/** @param {any} a @param {any} b */
const sameActor = (a, b) => a && b && ACTOR.every((k) => a[k] === b[k]);
/** @param {any} v @param {string[]} keys @param {string[]} [optional] */
const shape = (v, keys, optional = []) =>
  v &&
  typeof v === 'object' &&
  !Array.isArray(v) &&
  keys.every((k) => Object.hasOwn(v, k)) &&
  Object.keys(v).every((k) => keys.includes(k) || optional.includes(k));
/** @param {any} v */
const digest = (v) => typeof v === 'string' && /^[a-f0-9]{64}$/.test(v);
/** @param {any} v */
function time(v) {
  if (typeof v !== 'string') return false;
  const m =
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-](\d{2}):(\d{2}))$/.exec(v);
  if (!m) return false;
  const [year, month, day, hour, minute, second] = m.slice(1, 7).map(Number);
  const days = [
    31,
    year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0) ? 29 : 28,
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
  const parsed = new Date(v);
  return (
    year > 0 &&
    month >= 1 &&
    month <= 12 &&
    day >= 1 &&
    day <= days[month - 1] &&
    hour < 24 &&
    minute < 60 &&
    second < 60 &&
    (!m[8] || (Number(m[8]) < 24 && Number(m[9]) < 60)) &&
    Number.isFinite(parsed.getTime()) &&
    parsed.getUTCFullYear() >= 1 &&
    parsed.getUTCFullYear() <= 9999
  );
}
/** Compare validated zoned instants while retaining sub-millisecond precision.
 * @param {string} a @param {string} b */
function compareTime(a, b) {
  const normalized = (/** @type {string} */ value) => {
    const fraction = /\.(\d+)(?:Z|[+-]\d{2}:\d{2})$/.exec(value)?.[1] ?? '';
    return [new Date(value).toISOString().slice(0, 19), fraction.padEnd(3, '0')];
  };
  const [as, af] = normalized(a),
    [bs, bf] = normalized(b);
  const first = as.localeCompare(bs);
  return (
    first ||
    af
      .padEnd(Math.max(af.length, bf.length), '0')
      .localeCompare(bf.padEnd(Math.max(af.length, bf.length), '0'))
  );
}
/** @param {any} v */
function checksShape(v) {
  const receipt = (/** @type {any} */ x) => x === null || (typeof x === 'string' && ID.test(x));
  const status = ['verified', 'missing', 'denied', 'unknown'];
  return (
    shape(v, [
      'checked_at',
      'valid_until',
      'receiver',
      'package_digest',
      'publication',
      'resources'
    ]) &&
    time(v.checked_at) &&
    time(v.valid_until) &&
    shape(v.receiver, ACTOR) &&
    ACTOR.every((k) => typeof v.receiver[k] === 'string' && ID.test(v.receiver[k])) &&
    digest(v.package_digest) &&
    shape(
      v.publication,
      ['event_id', 'artifact_id', 'body_digest', 'status', 'receipt_id'],
      ['artifact_sha256']
    ) &&
    UUID.test(v.publication.event_id) &&
    UUID.test(v.publication.artifact_id) &&
    digest(v.publication.body_digest) &&
    (v.publication.artifact_sha256 === undefined || digest(v.publication.artifact_sha256)) &&
    status.includes(v.publication.status) &&
    receipt(v.publication.receipt_id) &&
    Array.isArray(v.resources) &&
    v.resources.length <= 100 &&
    v.resources.every(
      (/** @type {any} */ r) =>
        shape(r, ['resource_id', 'scope_id', 'action', 'status', 'receipt_id']) &&
        UUID.test(r.resource_id) &&
        UUID.test(r.scope_id) &&
        r.action === 'read' &&
        status.includes(r.status) &&
        receipt(r.receipt_id)
    )
  );
}
/** A local decision over independently supplied checks. It performs no probe or transfer.
 * @param {{package:any,projection:any,receiver:any,checks:any,asOf:any}} input */
export function assessHandoffReadiness(input) {
  /** @type {string[]} */ const errors = [];
  let pkg;
  try {
    const v = validateHandoffPackage(input?.package);
    if (!v.ok) throw new Error('INVALID_PACKAGE');
    pkg = v.package;
  } catch {
    return {
      status: 'blocked',
      package_digest: null,
      requirements: [],
      blocking_operations: [],
      errors: ['INVALID_PACKAGE']
    };
  }
  const package_digest = workContentDigest(pkg);
  const blocking_operations = pkg.checkpoint.external_operations
    .filter((/** @type {any} */ o) => o.status === 'unknown')
    .map((/** @type {any} */ o) => o.operation_id)
    .sort();
  /** @type {any[]} */ const requirements = [];
  if (!sameActor(input.receiver, pkg.recipient)) errors.push('RECEIVER_MISMATCH');
  const projection = input.projection;
  const row = projection?.work?.find((/** @type {any} */ w) => w.work_id === pkg.work_id);
  const publication = pkg.events.find(
    (/** @type {any} */ e) => e.event_id === pkg.publication_event_id
  );
  const pubRow = projection?.checkpoints?.find(
    (/** @type {any} */ c) => c.checkpoint_id === pkg.checkpoint.checkpoint_id
  );
  if (
    projection?.workspace_id !== pkg.workspace_id ||
    !row ||
    row.workstream_id !== pkg.workstream_id ||
    !pubRow?.publication_event ||
    canonicalWorkJson(pubRow.publication_event) !== canonicalWorkJson(publication)
  )
    errors.push('PROJECTION_MISMATCH');
  try {
    const relevant = relevantHandoffEvents(projection, pkg.checkpoint, pkg.publication_event_id);
    if (canonicalWorkJson(relevant) !== canonicalWorkJson(pkg.events))
      errors.push('RELEVANT_HISTORY_MISMATCH');
  } catch {
    errors.push('RELEVANT_HISTORY_UNAVAILABLE');
  }
  if (
    !row?.item ||
    row?.assignment?.state !== 'accepted' ||
    row?.state !== 'current' ||
    row?.execution_authority !== 'accepted' ||
    row.assignment.version !== pkg.checkpoint.assignment_version ||
    row.assignment.head_event_id !== pkg.checkpoint.assignment_event_id ||
    ['completed', 'cancelled'].includes(row.item.status)
  )
    errors.push('WORK_NOT_READY');
  if (
    row?.head_event_id &&
    !pkg.events.some(
      (/** @type {any} */ e) =>
        e.event_id === row.head_event_id && ['work.opened', 'work.revised'].includes(e.kind)
    )
  )
    errors.push('WORK_CHANGED_SINCE_PACKAGE');
  if (
    projection?.observation?.coverage !== 'complete' ||
    projection?.conflicts?.some(
      (/** @type {any} */ d) =>
        d.subject_id === pkg.work_id || d.subject_id === pkg.checkpoint.checkpoint_id
    )
  )
    errors.push('PROJECTION_INCOMPLETE');
  if (!time(input.asOf) || projection?.as_of !== input.asOf) errors.push('AS_OF_MISMATCH');
  if (input.checks === null || input.checks === undefined)
    return {
      status: errors.length ? 'blocked' : 'unknown',
      package_digest,
      requirements,
      blocking_operations,
      errors: [...errors, 'CHECKS_MISSING']
    };
  let checks;
  try {
    checks = JSON.parse(canonicalWorkJson(input.checks));
  } catch {
    errors.push('INVALID_CHECKS');
  }
  if (!checksShape(checks)) errors.push('INVALID_CHECKS');
  if (errors.includes('INVALID_CHECKS'))
    return {
      status: 'blocked',
      package_digest,
      requirements,
      blocking_operations,
      errors: [...new Set(errors)]
    };
  if (!sameActor(checks.receiver, pkg.recipient) || checks.package_digest !== package_digest)
    errors.push('CHECK_BINDING_MISMATCH');
  if (
    compareTime(checks.checked_at, input.asOf) > 0 ||
    compareTime(input.asOf, checks.valid_until) >= 0
  )
    errors.push('CHECKS_EXPIRED');
  const pub = checks.publication;
  const artifactHash = publication.payload.artifact.sha256;
  const artifactVerified =
    pub.artifact_sha256 === undefined
      ? artifactHash === null || artifactHash === publication.payload.body_digest
      : pub.artifact_sha256 === artifactHash;
  requirements.push({
    kind: 'publication',
    resource_id: pub.artifact_id,
    status: pub.status,
    receipt_id: pub.receipt_id
  });
  if (
    pub.event_id !== publication.event_id ||
    pub.artifact_id !== publication.payload.artifact.id ||
    pub.body_digest !== publication.payload.body_digest ||
    !artifactVerified ||
    pub.status !== 'verified' ||
    !pub.receipt_id
  )
    errors.push('PUBLICATION_UNVERIFIED');
  const expected = new Set(
    pkg.access_requirements.map(
      (/** @type {any} */ r) => `${r.resource_id}:${r.scope_id}:${r.action}`
    )
  );
  const seen = new Set();
  for (const r of checks.resources) {
    const key = `${r.resource_id}:${r.scope_id}:${r.action}`;
    requirements.push({ kind: 'resource', ...r });
    if (seen.has(key) || !expected.has(key)) errors.push('RESOURCE_SET_MISMATCH');
    seen.add(key);
    if (r.status !== 'verified' || !r.receipt_id) errors.push('RESOURCE_UNVERIFIED');
  }
  if (seen.size !== expected.size) errors.push('RESOURCE_SET_MISMATCH');
  return {
    status: errors.length ? 'blocked' : 'ready',
    package_digest,
    requirements,
    blocking_operations,
    errors: [...new Set(errors)]
  };
}
/** Returns a validated proposal only. No authorization or publication happens here.
 * @param {{assessment:any,offerEvent:any,eventEnvelope:any}} input */
export function buildReadinessEvent(input) {
  const { assessment, offerEvent, eventEnvelope } = input;
  const offer = validateWorkEvent(offerEvent);
  if (
    assessment?.status !== 'ready' ||
    !offer.ok ||
    offer.event.kind !== 'handoff.offered' ||
    assessment.package_digest !== offer.event.payload.package_digest ||
    !sameActor(eventEnvelope?.actor, offer.event.payload.target)
  )
    throw new TypeError('HANDOFF_NOT_READY');
  const receipt = assessment.requirements?.find(
    (/** @type {any} */ r) => r.kind === 'publication'
  )?.receipt_id;
  if (typeof receipt !== 'string' || !ID.test(receipt)) throw new TypeError('HANDOFF_NOT_READY');
  const candidate = {
    ...eventEnvelope,
    kind: 'handoff.ready',
    subject: offer.event.subject,
    parents: [...new Set([...(eventEnvelope.parents ?? []), offer.event.event_id])],
    payload: {
      offer_event_id: offer.event.event_id,
      package_digest: assessment.package_digest,
      verification_receipt_id: receipt
    }
  };
  const valid = validateWorkEvent(candidate);
  if (!valid.ok) throw new TypeError(valid.error.code);
  return valid.event;
}
