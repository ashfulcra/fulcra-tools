import { createHash } from 'node:crypto';

/** @typedef {{principal_id:string,logical_agent_id:string,instance_id:string,session_id:string}} WorkActor */
/** @typedef {{id:string,uri:string,sha256:string|null,version:string|null,media_type:string,owner_principal_id:string,audience:'workspace',portable:boolean}} ArtifactRef */
/** @typedef {{id:string,text:string}} AcceptanceCriterion */
/** @typedef {{title:string,intent:string,type:'task'|'open_thread'|'deferred',status:string,owner_id:string|null,next_action:string|null,acceptance_criteria:AcceptanceCriterion[],priority:'normal'|'urgent',dependency_ids:string[],due_at:string|null,revisit:{at:string|null,condition:string|null}|null,blocker:{condition_id:string,kind:'question'|'dependency'|'failure',ref_id:string|null,unlock_condition:string}|null,result:{summary:string,evidence:{criterion_id:string,artifact:ArtifactRef}[]}|null}} WorkItem */
/** @typedef {{protocol:'gatekeeper-work/1',event_id:string,operation_id:string,workspace_id:string,stream_id:string,workstream_id:string,actor:WorkActor,kind:string,subject:{type:'work'|'question'|'checkpoint'|'handoff',id:string},parents:string[],audience:'workspace',occurred_at:string,payload:Record<string,unknown>,correlation_id?:string}} WorkEvent */
/** @typedef {{ok:true,event:WorkEvent}|{ok:false,error:{code:string,path:string}}} WorkValidationResult */

export const WORK_PROTOCOL = 'gatekeeper-work/1';
const MAX_NOTE_BYTES = 64 * 1024;
const MAX_TEXT = 4096;
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const identifier = /^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$/;
const zonedTime =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-](\d{2}):(\d{2}))$/;
/** @param {object} value @param {PropertyKey} key */
const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);

class ContractError extends TypeError {
  /** @param {string} code @param {string} path */
  constructor(code, path) {
    super(`${code} at ${path}`);
    this.code = code;
    this.path = path;
  }
}
/** @param {string} code @param {string} path @returns {never} */
const invalid = (code, path) => {
  throw new ContractError(code, path);
};
/** @param {string} path @param {string} key */
const pathKey = (path, key) => `${path}.${key}`;

/** Reject anything that cannot be safely traversed as JSON before reading schema fields.
 * @param {any} value @param {string} path @param {Set<object>} [seen] */
function jsonValue(value, path, seen = new Set()) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) invalid('NON_JSON_VALUE', path);
    return;
  }
  if (typeof value !== 'object' || seen.has(value)) invalid('NON_JSON_VALUE', path);
  if (Array.isArray(value)) {
    if (Object.getPrototypeOf(value) !== Array.prototype) invalid('NON_PLAIN_OBJECT', path);
    seen.add(value);
    if (value.length > 1000) invalid('LIMIT_EXCEEDED', path);
    for (const key of Reflect.ownKeys(value)) {
      if (key === 'length') continue;
      if (typeof key !== 'string' || !/^(0|[1-9]\d*)$/.test(key) || Number(key) >= value.length)
        invalid('UNKNOWN_FIELD', path);
    }
    for (let i = 0; i < value.length; i++) {
      if (!own(value, i)) invalid('NON_JSON_VALUE', `${path}[${i}]`);
      const descriptor = Object.getOwnPropertyDescriptor(value, i);
      if (!descriptor || !own(descriptor, 'value') || !descriptor.enumerable)
        invalid('NON_JSON_VALUE', `${path}[${i}]`);
      jsonValue(descriptor.value, `${path}[${i}]`, seen);
    }
    seen.delete(value);
    return;
  }
  if (Object.getPrototypeOf(value) !== Object.prototype) invalid('NON_PLAIN_OBJECT', path);
  seen.add(value);
  for (const key of Reflect.ownKeys(value)) {
    if (
      typeof key !== 'string' ||
      key === '__proto__' ||
      key === 'prototype' ||
      key === 'constructor'
    )
      invalid('UNKNOWN_FIELD', path);
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (!descriptor || !own(descriptor, 'value') || !descriptor.enumerable)
      invalid('NON_JSON_VALUE', `${path}.*`);
    jsonValue(descriptor.value, `${path}.*`, seen);
  }
  seen.delete(value);
}

/** Sort keys recursively without changing array order. @param {unknown} value */
export function canonicalWorkJson(value) {
  jsonValue(value, '$');
  /** @param {any} item @returns {string} */
  const encode = (item) => {
    if (Array.isArray(item)) {
      const entries = [];
      for (let i = 0; i < item.length; i++) entries.push(encode(item[i]));
      return `[${entries.join(',')}]`;
    }
    return item && typeof item === 'object'
      ? `{${Object.keys(item)
          .sort()
          .map((key) => `${JSON.stringify(key)}:${encode(item[key])}`)
          .join(',')}}`
      : JSON.stringify(item);
  };
  return encode(value);
}

/** Lowercase SHA-256 of canonical UTF-8 JSON. @param {unknown} value */
export function workContentDigest(value) {
  return createHash('sha256').update(canonicalWorkJson(value), 'utf8').digest('hex');
}

/** @param {any} value @param {string[]} required @param {string[]} optional @param {string} path */
function shape(value, required, optional, path) {
  if (value === null || typeof value !== 'object' || Array.isArray(value))
    invalid('INVALID_OBJECT', path);
  for (const key of required) if (!own(value, key)) invalid('REQUIRED_FIELD', pathKey(path, key));
  for (const key of Object.keys(value))
    if (!required.includes(key) && !optional.includes(key)) invalid('UNKNOWN_FIELD', `${path}.*`);
}
/** @param {any} value @param {any[]} choices @param {string} path */
function oneOf(value, choices, path) {
  if (!choices.includes(value)) invalid('INVALID_VALUE', path);
}
/** @param {any} value @param {string} path @param {boolean} [nullable] */
function text(value, path, nullable = false) {
  if (nullable && value === null) return;
  if (typeof value !== 'string' || !value.trim() || value.length > MAX_TEXT)
    invalid('INVALID_TEXT', path);
}
/** @param {any} value @param {string} path @param {boolean} [nullable] */
function safeId(value, path, nullable = false) {
  if (nullable && value === null) return;
  if (typeof value !== 'string' || value.length > 128 || !identifier.test(value))
    invalid('INVALID_ID', path);
}
/** @param {any} value @param {string} path @param {boolean} [nullable] */
function uuidField(value, path, nullable = false) {
  if (nullable && value === null) return;
  if (typeof value !== 'string' || !uuid.test(value)) invalid('INVALID_UUID', path);
}
/** @param {any} value @param {string} path @param {boolean} [nullable] */
function time(value, path, nullable = false) {
  if (nullable && value === null) return;
  if (typeof value !== 'string') invalid('INVALID_TIME', path);
  const match = zonedTime.exec(value);
  if (!match) invalid('INVALID_TIME', path);
  const [year, month, day, hour, minute, second] = match.slice(1, 7).map(Number);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  const instant = new Date(value);
  const utcYear = instant.getUTCFullYear();
  if (
    !year ||
    month < 1 ||
    month > 12 ||
    day < 1 ||
    day > days[month - 1] ||
    hour > 23 ||
    minute > 59 ||
    second > 59 ||
    (match[8] && (Number(match[8]) > 23 || Number(match[9]) > 59)) ||
    !Number.isFinite(instant.getTime()) ||
    utcYear < 1 ||
    utcYear > 9999
  )
    invalid('INVALID_TIME', path);
}
/** Preserve the supplied instant's precision while converting its zone to UTC. @param {string} value */
function utcTime(value) {
  const fraction = /\.(\d+)(?:Z|[+-]\d{2}:\d{2})$/.exec(value)?.[1] ?? '';
  const digits = fraction ? fraction.padEnd(3, '0') : '000';
  return `${new Date(value).toISOString().slice(0, 19)}.${digits}Z`;
}
/** @param {any} value @param {string} path @param {(entry:any,path:string)=>void} each @param {number} [max] @param {boolean} [unique] */
function array(value, path, each, max = 100, unique = false) {
  if (!Array.isArray(value) || Object.getPrototypeOf(value) !== Array.prototype)
    invalid('INVALID_ARRAY', path);
  if (value.length > max) invalid('LIMIT_EXCEEDED', path);
  const seen = new Set();
  for (let index = 0; index < value.length; index++) {
    const entry = value[index];
    each(entry, `${path}[${index}]`);
    if (unique) {
      const key = typeof entry === 'object' ? entry.id : entry;
      if (seen.has(key)) invalid('DUPLICATE_ENTRY', `${path}[${index}]`);
      seen.add(key);
    }
  }
}
/** @param {any} value @param {string} path */
function actor(value, path) {
  shape(value, ['principal_id', 'logical_agent_id', 'instance_id', 'session_id'], [], path);
  for (const key of ['principal_id', 'logical_agent_id', 'instance_id', 'session_id'])
    safeId(value[key], pathKey(path, key));
}
/** @param {any} value @param {string} path */
function artifact(value, path) {
  shape(
    value,
    ['id', 'uri', 'sha256', 'version', 'media_type', 'owner_principal_id', 'audience', 'portable'],
    [],
    path
  );
  uuidField(value.id, `${path}.id`);
  text(value.uri, `${path}.uri`);
  if (typeof value.portable !== 'boolean') invalid('INVALID_VALUE', `${path}.portable`);
  if (value.uri.startsWith('https:')) {
    let parsed;
    try {
      parsed = new URL(value.uri);
    } catch {
      invalid('INVALID_URI', `${path}.uri`);
    }
    if (
      parsed.protocol !== 'https:' ||
      !parsed.hostname ||
      parsed.username ||
      parsed.password ||
      parsed.search ||
      parsed.hash ||
      /[?#]/.test(value.uri)
    )
      invalid('INVALID_URI', `${path}.uri`);
  } else if (value.portable || !value.uri.startsWith('/') || value.uri.startsWith('//')) {
    invalid('INVALID_URI', `${path}.uri`);
  }
  if (
    value.sha256 !== null &&
    (typeof value.sha256 !== 'string' || !/^[a-f0-9]{64}$/i.test(value.sha256))
  )
    invalid('INVALID_DIGEST', `${path}.sha256`);
  text(value.version, `${path}.version`, true);
  if (value.sha256 === null && value.version === null)
    invalid('IMMUTABLE_REFERENCE_REQUIRED', path);
  text(value.media_type, `${path}.media_type`);
  safeId(value.owner_principal_id, `${path}.owner_principal_id`);
  oneOf(value.audience, ['workspace'], `${path}.audience`);
}
/** @param {any} value @param {string} path @param {boolean} opened */
function item(value, path, opened) {
  shape(
    value,
    [
      'title',
      'intent',
      'type',
      'status',
      'owner_id',
      'next_action',
      'acceptance_criteria',
      'priority',
      'dependency_ids',
      'due_at',
      'revisit',
      'blocker',
      'result'
    ],
    [],
    path
  );
  text(value.title, `${path}.title`);
  text(value.intent, `${path}.intent`);
  oneOf(value.type, ['task', 'open_thread', 'deferred'], `${path}.type`);
  const statuses =
    value.type === 'task'
      ? ['proposed', 'ready', 'active', 'blocked', 'paused', 'completed', 'cancelled']
      : value.type === 'open_thread'
        ? ['open', 'closed']
        : ['deferred', 'closed'];
  oneOf(value.status, statuses, `${path}.status`);
  if (opened)
    oneOf(
      value.status,
      [value.type === 'task' ? 'proposed' : value.type === 'open_thread' ? 'open' : 'deferred'],
      `${path}.status`
    );
  safeId(value.owner_id, `${path}.owner_id`, true);
  text(value.next_action, `${path}.next_action`, true);
  array(
    value.acceptance_criteria,
    `${path}.acceptance_criteria`,
    (criterion, p) => {
      shape(criterion, ['id', 'text'], [], p);
      safeId(criterion.id, `${p}.id`);
      text(criterion.text, `${p}.text`);
    },
    100,
    true
  );
  oneOf(value.priority, ['normal', 'urgent'], `${path}.priority`);
  array(value.dependency_ids, `${path}.dependency_ids`, uuidField, 100, true);
  time(value.due_at, `${path}.due_at`, true);
  if (value.revisit !== null) {
    shape(value.revisit, ['at', 'condition'], [], `${path}.revisit`);
    time(value.revisit.at, `${path}.revisit.at`, true);
    text(value.revisit.condition, `${path}.revisit.condition`, true);
    if (value.revisit.at === null && value.revisit.condition === null)
      invalid('REVISIT_REQUIRED', `${path}.revisit`);
  }
  if (value.blocker !== null) {
    const p = `${path}.blocker`;
    shape(value.blocker, ['condition_id', 'kind', 'ref_id', 'unlock_condition'], [], p);
    safeId(value.blocker.condition_id, `${p}.condition_id`);
    oneOf(value.blocker.kind, ['question', 'dependency', 'failure'], `${p}.kind`);
    uuidField(value.blocker.ref_id, `${p}.ref_id`, true);
    text(value.blocker.unlock_condition, `${p}.unlock_condition`);
    if (value.blocker.kind !== 'failure' && value.blocker.ref_id === null)
      invalid('REFERENCE_REQUIRED', `${p}.ref_id`);
  }
  if (value.result !== null) {
    const p = `${path}.result`;
    shape(value.result, ['summary', 'evidence'], [], p);
    text(value.result.summary, `${p}.summary`);
    array(value.result.evidence, `${p}.evidence`, (entry, ep) => {
      shape(entry, ['criterion_id', 'artifact'], [], ep);
      safeId(entry.criterion_id, `${ep}.criterion_id`);
      artifact(entry.artifact, `${ep}.artifact`);
    });
    const actual = value.result.evidence.map(
      /** @param {any} entry */ (entry) => entry.criterion_id
    );
    const expected = value.acceptance_criteria.map(
      /** @param {any} criterion */ (criterion) => criterion.id
    );
    if (
      actual.length !== expected.length ||
      new Set(actual).size !== actual.length ||
      actual.some(/** @param {string} id */ (id) => !expected.includes(id))
    )
      invalid('EVIDENCE_MISMATCH', `${p}.evidence`);
  }
  if (value.type === 'task') {
    if (
      !['completed', 'cancelled'].includes(value.status) &&
      (value.next_action === null || value.acceptance_criteria.length === 0)
    )
      invalid('TASK_ACTION_REQUIRED', path);
    if (value.status === 'completed' && value.result === null)
      invalid('RESULT_REQUIRED', `${path}.result`);
    if (value.status !== 'completed' && value.result !== null)
      invalid('INVALID_RESULT', `${path}.result`);
    if (value.status === 'paused' && (value.owner_id === null || value.revisit === null))
      invalid('PAUSE_POLICY_REQUIRED', path);
    if (value.status === 'blocked' && value.blocker === null)
      invalid('BLOCKER_REQUIRED', `${path}.blocker`);
  } else {
    if (value.result !== null || value.blocker !== null) invalid('INVALID_VALUE', path);
    if (value.type === 'deferred' && value.status === 'deferred' && value.revisit === null)
      invalid('REVISIT_REQUIRED', `${path}.revisit`);
  }
  if (value.status !== 'blocked' && value.blocker !== null)
    invalid('INVALID_BLOCKER', `${path}.blocker`);
}

/** @type {Record<string,[string,string[]]>} */
const kinds = {
  'handoff.offered': [
    'handoff',
    [
      'work_id',
      'expected_assignment_event_id',
      'expected_version',
      'checkpoint_event_id',
      'package_digest',
      'target'
    ]
  ],
  'handoff.ready': ['handoff', ['offer_event_id', 'package_digest', 'verification_receipt_id']],
  'checkpoint.published': [
    'checkpoint',
    [
      'work_id',
      'checkpoint_id',
      'artifact',
      'body_digest',
      'assignment_version',
      'assignment_event_id'
    ]
  ],
  'work.opened': ['work', ['item']],
  'work.revised': ['work', ['expected_event_id', 'item', 'reason']],
  'assignment.offered': [
    'work',
    ['work_id', 'expected_assignment_event_id', 'expected_version', 'target']
  ],
  'assignment.accepted': [
    'work',
    [
      'work_id',
      'offer_event_id',
      'expected_assignment_event_id',
      'expected_version',
      'ready_event_id'
    ]
  ],
  'assignment.released': [
    'work',
    ['work_id', 'expected_assignment_event_id', 'expected_version', 'reason']
  ],
  'question.opened': [
    'question',
    [
      'work_ids',
      'text',
      'decision_maker_id',
      'options',
      'recommendation',
      'deadline_at',
      'escalation_target_id'
    ]
  ],
  'question.acknowledged': ['question', ['opened_event_id']],
  'question.answered': ['question', ['opened_event_id', 'supersedes_answer_event_id', 'text']],
  'question.applied': ['question', ['answer_event_id', 'work_id', 'condition_id']]
};
/** @param {any} value @param {string} path */
function version(value, path) {
  if (!Number.isSafeInteger(value) || value < 0) invalid('INVALID_VERSION', path);
}
/** @param {any} value @param {string} path */
function digest(value, path) {
  if (typeof value !== 'string' || !/^[a-f0-9]{64}$/.test(value)) invalid('INVALID_DIGEST', path);
}
/** @param {any} value @param {string} kind @param {string} subjectId @param {string[]} parents */
function payload(value, kind, subjectId, parents) {
  const p = '$.payload';
  shape(value, kinds[kind][1], [], p);
  for (const key of Object.keys(value))
    if (key.endsWith('_event_id') && value[key] !== null) {
      uuidField(value[key], `${p}.${key}`);
      if (!parents.includes(value[key])) invalid('PARENT_REQUIRED', `${p}.${key}`);
    }
  switch (kind) {
    case 'handoff.offered':
      uuidField(value.work_id, `${p}.work_id`);
      version(value.expected_version, `${p}.expected_version`);
      if ((value.expected_version === 0) !== (value.expected_assignment_event_id === null))
        invalid('INVALID_ASSIGNMENT_HEAD', p);
      digest(value.package_digest, `${p}.package_digest`);
      actor(value.target, `${p}.target`);
      break;
    case 'handoff.ready':
      digest(value.package_digest, `${p}.package_digest`);
      safeId(value.verification_receipt_id, `${p}.verification_receipt_id`);
      break;
    case 'checkpoint.published':
      uuidField(value.work_id, `${p}.work_id`);
      uuidField(value.checkpoint_id, `${p}.checkpoint_id`);
      artifact(value.artifact, `${p}.artifact`);
      version(value.assignment_version, `${p}.assignment_version`);
      if ((value.assignment_version === 0) !== (value.assignment_event_id === null))
        invalid('INVALID_ASSIGNMENT_HEAD', p);
      if (value.checkpoint_id !== subjectId) invalid('SUBJECT_MISMATCH', p);
      if (!value.artifact.portable) invalid('PORTABLE_REFERENCE_REQUIRED', `${p}.artifact`);
      if (
        typeof value.body_digest !== 'string' ||
        !/^[a-f0-9]{64}$/.test(value.body_digest) ||
        (value.artifact.sha256 !== null && value.artifact.sha256 !== value.body_digest)
      )
        invalid('INVALID_DIGEST', `${p}.body_digest`);
      break;
    case 'work.opened':
      item(value.item, `${p}.item`, true);
      break;
    case 'work.revised':
      item(value.item, `${p}.item`, false);
      text(value.reason, `${p}.reason`, true);
      break;
    case 'assignment.offered':
      uuidField(value.work_id, `${p}.work_id`);
      version(value.expected_version, `${p}.expected_version`);
      actor(value.target, `${p}.target`);
      if ((value.expected_version === 0) !== (value.expected_assignment_event_id === null))
        invalid('INVALID_ASSIGNMENT_HEAD', p);
      break;
    case 'assignment.accepted':
      uuidField(value.work_id, `${p}.work_id`);
      uuidField(value.offer_event_id, `${p}.offer_event_id`);
      version(value.expected_version, `${p}.expected_version`);
      if ((value.expected_version === 0) !== (value.expected_assignment_event_id === null))
        invalid('INVALID_ASSIGNMENT_HEAD', p);
      // Whether this is still unassigned (including a released version) is a
      // causal replay rule, not a version-zero-only schema restriction.
      break;
    case 'assignment.released':
      uuidField(value.work_id, `${p}.work_id`);
      version(value.expected_version, `${p}.expected_version`);
      text(value.reason, `${p}.reason`);
      break;
    case 'question.opened':
      array(value.work_ids, `${p}.work_ids`, uuidField, 100, true);
      text(value.text, `${p}.text`);
      safeId(value.decision_maker_id, `${p}.decision_maker_id`);
      array(value.options, `${p}.options`, text);
      text(value.recommendation, `${p}.recommendation`, true);
      time(value.deadline_at, `${p}.deadline_at`, true);
      safeId(value.escalation_target_id, `${p}.escalation_target_id`, true);
      break;
    case 'question.answered':
      text(value.text, `${p}.text`);
      break;
    case 'question.applied':
      uuidField(value.work_id, `${p}.work_id`);
      safeId(value.condition_id, `${p}.condition_id`);
      break;
  }
  if (kind.startsWith('assignment.') || kind === 'question.applied')
    if (value.work_id !== subjectId && kind.startsWith('assignment.'))
      invalid('SUBJECT_MISMATCH', `${p}.work_id`);
}

/** Validate shape and intrinsic field invariants only; replay authorizes transitions and trust. @param {unknown} value @returns {WorkValidationResult} */
export function validateWorkEvent(value) {
  try {
    jsonValue(value, '$');
    const candidate = /** @type {any} */ (value);
    shape(
      candidate,
      [
        'protocol',
        'event_id',
        'operation_id',
        'workspace_id',
        'stream_id',
        'workstream_id',
        'actor',
        'kind',
        'subject',
        'parents',
        'audience',
        'occurred_at',
        'payload'
      ],
      ['correlation_id'],
      '$'
    );
    oneOf(candidate.protocol, [WORK_PROTOCOL], '$.protocol');
    for (const key of ['event_id', 'operation_id', 'workspace_id', 'stream_id', 'workstream_id'])
      uuidField(candidate[key], `$.${key}`);
    if (own(candidate, 'correlation_id')) uuidField(candidate.correlation_id, '$.correlation_id');
    actor(candidate.actor, '$.actor');
    if (typeof candidate.kind !== 'string' || !own(kinds, candidate.kind))
      invalid('UNSUPPORTED_KIND', '$.kind');
    shape(candidate.subject, ['type', 'id'], [], '$.subject');
    oneOf(candidate.subject.type, [kinds[candidate.kind][0]], '$.subject.type');
    uuidField(candidate.subject.id, '$.subject.id');
    array(candidate.parents, '$.parents', uuidField, 100, true);
    oneOf(candidate.audience, ['workspace'], '$.audience');
    time(candidate.occurred_at, '$.occurred_at');
    payload(candidate.payload, candidate.kind, candidate.subject.id, candidate.parents);
    const normalized = JSON.parse(canonicalWorkJson(candidate));
    normalized.occurred_at = utcTime(candidate.occurred_at);
    const data = normalized.payload;
    if (data.item) {
      if (data.item.due_at !== null) data.item.due_at = utcTime(data.item.due_at);
      if (data.item.revisit?.at) data.item.revisit.at = utcTime(data.item.revisit.at);
    }
    if (data.deadline_at) data.deadline_at = utcTime(data.deadline_at);
    if (Buffer.byteLength(JSON.stringify(normalized), 'utf8') > MAX_NOTE_BYTES)
      invalid('LIMIT_EXCEEDED', '$');
    return { ok: true, event: normalized };
  } catch (error) {
    return {
      ok: false,
      error:
        error instanceof ContractError
          ? { code: error.code, path: error.path }
          : { code: 'INVALID_EVENT', path: '$' }
    };
  }
}

/** Parse one local MomentAnnotation.note. This does not publish it. @param {unknown} note @returns {WorkValidationResult} */
export function parseWorkNote(note) {
  if (typeof note !== 'string') return { ok: false, error: { code: 'INVALID_NOTE', path: '$' } };
  if (Buffer.byteLength(note, 'utf8') > MAX_NOTE_BYTES)
    return { ok: false, error: { code: 'LIMIT_EXCEEDED', path: '$' } };
  try {
    return validateWorkEvent(JSON.parse(note));
  } catch {
    return { ok: false, error: { code: 'INVALID_JSON', path: '$' } };
  }
}

/** Serialize a validated event into a local annotation note string. @param {unknown} event */
export function serializeWorkEvent(event) {
  const result = validateWorkEvent(event);
  if (!result.ok) throw new TypeError(`${result.error.code} at ${result.error.path}`);
  return canonicalWorkJson(result.event);
}
