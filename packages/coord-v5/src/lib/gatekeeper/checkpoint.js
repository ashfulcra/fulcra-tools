import { canonicalWorkJson, validateWorkEvent, workContentDigest } from './work-contract.js';
import { replayWorkEvents } from './work-projection.js';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const ID = /^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$/;
const COVERAGE = ['complete', 'partial', 'unavailable', 'unsupported'];
const ACTOR = ['principal_id', 'logical_agent_id', 'instance_id', 'session_id'];
class Invalid extends Error {}
/** @param {boolean} condition @param {string} [code] */
function need(condition, code = 'INVALID_CHECKPOINT') {
  if (!condition) throw new Invalid(code);
}
/** @param {any} v @param {string[]} keys @param {string[]} [optional] */
function shape(v, keys, optional = []) {
  need(
    v !== null &&
      typeof v === 'object' &&
      !Array.isArray(v) &&
      keys.every((k) => Object.hasOwn(v, k)) &&
      Object.keys(v).every((k) => keys.includes(k) || optional.includes(k)),
    'INVALID_SHAPE'
  );
}
/** @param {any} v */
function uuid(v) {
  need(typeof v === 'string' && UUID.test(v), 'INVALID_UUID');
}
/** @param {any} v */
function identifier(v) {
  need(typeof v === 'string' && ID.test(v), 'INVALID_ID');
}
/** @param {any} v */
function text(v) {
  need(typeof v === 'string' && !!v.trim() && v.length <= 4096, 'INVALID_TEXT');
}
/** @param {any} v @param {any[]} choices */
function choice(v, choices) {
  need(choices.includes(v), 'INVALID_VALUE');
}
/** @param {any} v @param {(v:any)=>void} each @param {number} [max] @param {(v:any)=>any} [key] */
function list(v, each, max = 100, key) {
  need(Array.isArray(v) && v.length <= max, 'LIMIT_EXCEEDED');
  v.forEach(each);
  if (key) need(new Set(v.map(key)).size === v.length, 'DUPLICATE_ENTRY');
}
/** @param {any} v */
function actor(v) {
  shape(v, ACTOR);
  ACTOR.forEach((k) => identifier(v[k]));
}
/** @param {any} v */
function instant(v) {
  need(typeof v === 'string', 'INVALID_TIME');
  const m =
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-](\d{2}):(\d{2}))$/.exec(v);
  need(!!m, 'INVALID_TIME');
  if (!m) return '';
  const [y, mo, d, h, mi, s] = m.slice(1, 7).map(Number);
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
  const date = new Date(v);
  need(
    y > 0 &&
      mo > 0 &&
      mo <= 12 &&
      d > 0 &&
      d <= days[mo - 1] &&
      h < 24 &&
      mi < 60 &&
      s < 60 &&
      (!m[8] || (Number(m[8]) < 24 && Number(m[9]) < 60)) &&
      Number.isFinite(date.getTime()) &&
      date.getUTCFullYear() > 0 &&
      date.getUTCFullYear() <= 9999,
    'INVALID_TIME'
  );
  const fraction = /\.(\d+)(?:Z|[+-]\d{2}:\d{2})$/.exec(v)?.[1] ?? '';
  return `${date.toISOString().slice(0, 19)}.${fraction.padEnd(3, '0')}Z`;
}
/** @param {string} a @param {string} b */
function notAfter(a, b) {
  const x = instant(a),
    y = instant(b);
  const n = Math.max(x.length, y.length);
  return x.slice(0, -1).padEnd(n, '0') <= y.slice(0, -1).padEnd(n, '0');
}
/** Best-effort recognized patterns only; authors must redact free text. @param {any} v */
function credentials(v) {
  if (typeof v === 'string') {
    need(
      !/-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----|\bBearer\s+[^\s]+/i.test(v),
      'CREDENTIAL_PATTERN'
    );
    for (const match of v.matchAll(/https?:\/\/[^\s<>"']+/gi)) {
      let u;
      try {
        u = new URL(match[0]);
      } catch {
        continue;
      }
      need(
        !u.username && !u.password && !u.search && !u.hash && !/[?#]/.test(match[0]),
        'CREDENTIAL_PATTERN'
      );
    }
  } else if (v && typeof v === 'object')
    for (const [k, value] of Object.entries(v)) {
      need(
        !/(?:token|password|secret|authorization|cookie|private_key)/i.test(k),
        'CREDENTIAL_PATTERN'
      );
      credentials(value);
    }
}
/** @param {any} v @param {number} bytes */
function copy(v, bytes) {
  const json = canonicalWorkJson(v);
  need(Buffer.byteLength(json, 'utf8') <= bytes, 'LIMIT_EXCEEDED');
  const c = JSON.parse(json);
  credentials(c);
  return c;
}
/** @param {any} v */
function artifact(v) {
  shape(v, [
    'id',
    'uri',
    'sha256',
    'version',
    'media_type',
    'owner_principal_id',
    'audience',
    'portable'
  ]);
  uuid(v.id);
  text(v.uri);
  text(v.media_type);
  identifier(v.owner_principal_id);
  choice(v.audience, ['workspace']);
  need(typeof v.portable === 'boolean');
  need(
    v.sha256 === null || (typeof v.sha256 === 'string' && /^[a-f0-9]{64}$/i.test(v.sha256)),
    'INVALID_DIGEST'
  );
  if (v.version !== null) text(v.version);
  need(v.sha256 !== null || v.version !== null, 'IMMUTABLE_REFERENCE_REQUIRED');
  if (v.uri.startsWith('https:')) {
    let u;
    try {
      u = new URL(v.uri);
    } catch {
      throw new Invalid('INVALID_URI');
    }
    need(
      u.protocol === 'https:' &&
        !!u.hostname &&
        !u.username &&
        !u.password &&
        !u.search &&
        !u.hash &&
        !/[?#]/.test(v.uri),
      'INVALID_URI'
    );
  } else need(!v.portable && v.uri.startsWith('/') && !v.uri.startsWith('//'), 'INVALID_URI');
}
/** @param {any} c */
function checkpoint(c) {
  shape(c, [
    'schema',
    'checkpoint_id',
    'workspace_id',
    'workstream_id',
    'work_id',
    'assignment_version',
    'assignment_event_id',
    'identity',
    'created_at',
    'objective',
    'acceptance_criteria',
    'completed_actions',
    'in_progress_actions',
    'decisions',
    'unresolved_question_ids',
    'next_actions',
    'artifacts',
    'external_operations',
    'source_frontier'
  ]);
  choice(c.schema, ['gatekeeper-checkpoint/1']);
  ['checkpoint_id', 'workspace_id', 'workstream_id', 'work_id'].forEach((k) => uuid(c[k]));
  need(
    Number.isSafeInteger(c.assignment_version) && c.assignment_version >= 0,
    'INVALID_ASSIGNMENT'
  );
  if (c.assignment_event_id !== null) uuid(c.assignment_event_id);
  need((c.assignment_version === 0) === (c.assignment_event_id === null), 'INVALID_ASSIGNMENT');
  actor(c.identity);
  c.created_at = instant(c.created_at);
  text(c.objective);
  list(
    c.acceptance_criteria,
    (v) => {
      shape(v, ['id', 'text']);
      identifier(v.id);
      text(v.text);
    },
    100,
    (v) => v.id
  );
  for (const k of ['completed_actions', 'in_progress_actions', 'next_actions']) list(c[k], text);
  list(c.decisions, (v) => {
    shape(v, ['decision', 'reason']);
    text(v.decision);
    text(v.reason);
  });
  list(c.unresolved_question_ids, uuid, 100, (v) => v);
  list(c.artifacts, artifact, 100, (v) => v.id);
  list(
    c.external_operations,
    (v) => {
      shape(v, [
        'operation_id',
        'description',
        'status',
        'receipt',
        'retry_policy',
        'idempotency_key'
      ]);
      uuid(v.operation_id);
      text(v.description);
      choice(v.status, ['not_started', 'succeeded', 'failed', 'unknown']);
      if (v.receipt !== null) artifact(v.receipt);
      choice(v.retry_policy, ['never', 'verify_first']);
      if (v.idempotency_key !== null) text(v.idempotency_key);
    },
    100,
    (v) => v.operation_id
  );
  const f = c.source_frontier;
  shape(f, ['mode', 'event_ids', 'coverage', 'as_of']);
  choice(f.mode, ['observed_ids_only']);
  list(f.event_ids, uuid, 1000, (v) => v);
  choice(f.coverage, COVERAGE);
  f.as_of = instant(f.as_of);
  need(notAfter(f.as_of, c.created_at), 'FUTURE_FRONTIER');
  return c;
}
/** @param {unknown} value @returns {any} */
export function validateCheckpoint(value) {
  try {
    return { ok: true, checkpoint: checkpoint(copy(value, 128 * 1024)) };
  } catch (e) {
    return failure(e);
  }
}
/** @param {unknown} e */
function failure(e) {
  return {
    ok: false,
    error: { code: e instanceof Invalid ? e.message : 'INVALID_JSON', path: '$' }
  };
}
/** @param {any} o */
function observation(o) {
  shape(o, [
    'coverage',
    'as_of',
    'last_successful_observation_at',
    'sources',
    'gaps',
    'errors',
    'completeness_evidence_id'
  ]);
  choice(o.coverage, COVERAGE);
  o.as_of = instant(o.as_of);
  if (o.last_successful_observation_at !== null) {
    o.last_successful_observation_at = instant(o.last_successful_observation_at);
    need(notAfter(o.last_successful_observation_at, o.as_of), 'INVALID_OBSERVATION');
  }
  need(
    o.completeness_evidence_id === null ||
      (typeof o.completeness_evidence_id === 'string' && o.completeness_evidence_id.length <= 128),
    'INVALID_OBSERVATION'
  );
  list(
    o.sources,
    (s) => {
      shape(s, ['stream_id', 'status', 'pending_pages']);
      uuid(s.stream_id);
      choice(s.status, [...COVERAGE, 'error']);
      need(
        s.pending_pages === null || (Number.isSafeInteger(s.pending_pages) && s.pending_pages >= 0)
      );
    },
    100,
    (s) => s.stream_id
  );
  list(o.gaps, (g) => {
    shape(g, ['code'], ['stream_id', 'event_id']);
    identifier(g.code);
    if (g.stream_id !== undefined) uuid(g.stream_id);
    if (g.event_id !== undefined) uuid(g.event_id);
  });
  list(o.errors, (g) => {
    shape(g, ['code'], ['stream_id']);
    identifier(g.code);
    if (g.stream_id !== undefined) uuid(g.stream_id);
  });
  if (
    o.coverage === 'complete' &&
    (!o.completeness_evidence_id?.trim() ||
      !o.sources.length ||
      o.sources.some((/** @type {any} */ s) => s.status !== 'complete' || s.pending_pages !== 0) ||
      o.gaps.length ||
      o.errors.length)
  )
    o.coverage = 'partial';
}
/** Find semantic/parent closure over authorized candidates, retaining every variant.
 * @param {any[]} events @param {any} c @param {string} publicationId */
function closure(events, c, publicationId) {
  const entities = new Set([
    `work:${c.work_id}`,
    `checkpoint:${c.checkpoint_id}`,
    ...c.unresolved_question_ids.map((/** @type {string} */ q) => `question:${q}`)
  ]);
  const ids = new Set([publicationId]);
  const operations = new Set();
  let changed = true;
  /** @param {Set<string>} set @param {string} value */
  const add = (set, value) => {
    if (!set.has(value)) {
      set.add(value);
      changed = true;
    }
  };
  while (changed) {
    changed = false;
    for (const e of events) {
      const p = e.payload;
      if (
        e.kind === 'question.opened' &&
        p.work_ids.some((/** @type {string} */ w) => entities.has(`work:${w}`))
      )
        add(entities, `question:${e.subject.id}`);
      if (e.kind === 'checkpoint.published' && entities.has(`work:${p.work_id}`))
        add(entities, `checkpoint:${e.subject.id}`);
      if (
        !entities.has(`${e.subject.type}:${e.subject.id}`) &&
        !ids.has(e.event_id) &&
        !operations.has(e.operation_id)
      )
        continue;
      add(entities, `${e.subject.type}:${e.subject.id}`);
      add(ids, e.event_id);
      add(operations, e.operation_id);
      e.parents.forEach((/** @type {string} */ id) => add(ids, id));
      if (p.work_id) add(entities, `work:${p.work_id}`);
      for (const w of p.work_ids ?? []) add(entities, `work:${w}`);
      for (const w of p.item?.dependency_ids ?? []) add(entities, `work:${w}`);
      if (p.item?.blocker?.ref_id && ['question', 'dependency'].includes(p.item.blocker.kind))
        add(
          entities,
          `${p.item.blocker.kind === 'question' ? 'question' : 'work'}:${p.item.blocker.ref_id}`
        );
    }
  }
  const selected = events.filter(
    (e) => ids.has(e.event_id) || entities.has(`${e.subject.type}:${e.subject.id}`)
  );
  need(
    [...ids].every((id) => selected.some((e) => e.event_id === id)),
    'MISSING_HISTORY_CLOSURE'
  );
  need(
    [...entities].every((key) =>
      selected.some(
        (e) =>
          `${e.subject.type}:${e.subject.id}` === key &&
          (e.kind.endsWith('.opened') ||
            e.kind === 'checkpoint.published' ||
            e.kind === 'handoff.offered')
      )
    ),
    'MISSING_SEMANTIC_CLOSURE'
  );
  need(
    selected.every((e) => e.workspace_id === c.workspace_id && e.workstream_id === c.workstream_id),
    'CROSS_SCOPE_CLOSURE'
  );
  need(
    c.source_frontier.event_ids.every((/** @type {string} */ id) =>
      selected.some((e) => e.event_id === id)
    ),
    'FRONTIER_OUTSIDE_CLOSURE'
  );
  return [...new Map(selected.map((e) => [canonicalWorkJson(e), e])).values()].sort(
    (a, b) =>
      a.event_id.localeCompare(b.event_id) ||
      canonicalWorkJson(a).localeCompare(canonicalWorkJson(b))
  );
}
/** Select the same minimal relevant authorized closure used by package assembly.
 * @param {import('./work-projection.js').WorkProjection} projection
 * @param {any} checkpointBody @param {string} publicationId */
export function relevantHandoffEvents(projection, checkpointBody, publicationId) {
  const all = [
    ...projection.work,
    ...projection.questions,
    ...projection.checkpoints,
    ...projection.handoffs
  ].flatMap((r) =>
    r.history.map((/** @type {import('./work-projection.js').WorkHistoryEntry} */ h) => h.event)
  );
  return closure(all, checkpointBody, publicationId);
}
/** Structural/integrity validation only: caller must independently authorize replay.
 * @param {unknown} value @returns {any} */
export function validateHandoffPackage(value) {
  try {
    const p = copy(value, 1024 * 1024);
    shape(p, [
      'schema',
      'workspace_id',
      'workstream_id',
      'work_id',
      'checkpoint',
      'publication_event_id',
      'recipient',
      'replay_mode',
      'events',
      'tail_event_ids',
      'access_requirements',
      'observation',
      'created_at'
    ]);
    choice(p.schema, ['gatekeeper-handoff-package/1']);
    choice(p.replay_mode, ['full']);
    ['workspace_id', 'workstream_id', 'work_id', 'publication_event_id'].forEach((k) => uuid(p[k]));
    actor(p.recipient);
    p.created_at = instant(p.created_at);
    p.checkpoint = checkpoint(copy(p.checkpoint, 128 * 1024));
    const c = p.checkpoint;
    need(
      ['workspace_id', 'workstream_id', 'work_id'].every((k) => p[k] === c[k]),
      'CHECKPOINT_SCOPE_MISMATCH'
    );
    need(notAfter(c.created_at, p.created_at), 'FUTURE_CHECKPOINT');
    list(
      p.events,
      (e) => {
        const v = validateWorkEvent(e);
        need(v.ok, 'INVALID_EVENT');
      },
      1000
    );
    p.events = p.events.map((/** @type {any} */ e) => {
      const v = validateWorkEvent(e);
      if (!v.ok) throw new Invalid('INVALID_EVENT');
      return v.event;
    });
    const pubs = p.events.filter((/** @type {any} */ e) => e.event_id === p.publication_event_id);
    need(pubs.length === 1, 'PUBLICATION_REQUIRED');
    const e = pubs[0];
    const b = e.payload;
    need(
      e.kind === 'checkpoint.published' &&
        e.subject.id === c.checkpoint_id &&
        b.checkpoint_id === c.checkpoint_id &&
        b.work_id === c.work_id &&
        b.assignment_version === c.assignment_version &&
        b.assignment_event_id === c.assignment_event_id &&
        canonicalWorkJson(e.actor) === canonicalWorkJson(c.identity),
      'PUBLICATION_BINDING_MISMATCH'
    );
    need(b.body_digest === workContentDigest(c), 'BODY_DIGEST_MISMATCH');
    const selected = closure(p.events, c, p.publication_event_id);
    need(selected.length === p.events.length, 'UNRELATED_OR_DUPLICATE_HISTORY');
    p.events = selected;
    list(p.tail_event_ids, uuid, 1000, (v) => v);
    const tail = [
      ...new Set(
        selected
          .filter((e) => !c.source_frontier.event_ids.includes(e.event_id))
          .map((e) => e.event_id)
      )
    ].sort();
    need(
      canonicalWorkJson([...p.tail_event_ids].sort()) === canonicalWorkJson(tail),
      'TAIL_MISMATCH'
    );
    p.tail_event_ids = tail;
    list(
      p.access_requirements,
      (r) => {
        shape(r, ['resource_id', 'action', 'scope_id']);
        uuid(r.resource_id);
        uuid(r.scope_id);
        choice(r.action, ['read']);
        const refs = [...c.artifacts, b.artifact].filter((a) => a.id === r.resource_id);
        need(refs.length > 0 && refs.every((a) => a.portable), 'NONPORTABLE_REQUIRED_RESOURCE');
      },
      100,
      (r) => canonicalWorkJson(r)
    );
    p.access_requirements.sort((/** @type {any} */ a, /** @type {any} */ b) =>
      canonicalWorkJson(a).localeCompare(canonicalWorkJson(b))
    );
    observation(p.observation);
    need(notAfter(p.observation.as_of, p.created_at), 'FUTURE_OBSERVATION');
    if (
      p.observation.coverage === 'complete' &&
      (p.observation.as_of !== p.created_at ||
        p.events.some(
          (/** @type {any} */ e) =>
            !p.observation.sources.some((/** @type {any} */ s) => s.stream_id === e.stream_id)
        ))
    )
      p.observation.coverage = 'partial';
    return { ok: true, package: p };
  } catch (e) {
    return failure(e);
  }
}
/** Assemble relevant full authorized history. Never serializes injected trust or claims remote verification.
 * @param {any} input @returns {any} */
export function buildHandoffPackage(input) {
  try {
    const c = validateCheckpoint(input.checkpoint);
    need(c.ok, 'INVALID_CHECKPOINT');
    need(Array.isArray(input.events) && input.events.length <= 1000, 'LIMIT_EXCEEDED');
    const publication = validateWorkEvent(input.publicationEvent);
    need(publication.ok, 'INVALID_PUBLICATION');
    if (!publication.ok) return publication;
    const projection = replayWorkEvents({
      events: input.events,
      trust: input.trust,
      observation: input.observation,
      asOf: input.asOf
    });
    need(
      projection.workspace_id === c.checkpoint.workspace_id && projection.as_of !== null,
      'INVALID_TRUST'
    );
    const row = projection.checkpoints.find((r) => r.checkpoint_id === c.checkpoint.checkpoint_id);
    need(
      !!row?.publication_event &&
        canonicalWorkJson(row.publication_event) === canonicalWorkJson(publication.event),
      'PUBLICATION_NOT_AUTHORIZED'
    );
    const events = relevantHandoffEvents(projection, c.checkpoint, publication.event.event_id);
    // Validate caller observation directly before using replay's normalized/downgraded result.
    observation(copy(input.observation, 1024 * 1024));
    return validateHandoffPackage({
      schema: 'gatekeeper-handoff-package/1',
      workspace_id: c.checkpoint.workspace_id,
      workstream_id: c.checkpoint.workstream_id,
      work_id: c.checkpoint.work_id,
      checkpoint: c.checkpoint,
      publication_event_id: publication.event.event_id,
      recipient: input.recipient,
      replay_mode: 'full',
      events,
      tail_event_ids: [
        ...new Set(
          events
            .filter((e) => !c.checkpoint.source_frontier.event_ids.includes(e.event_id))
            .map((e) => e.event_id)
        )
      ].sort(),
      access_requirements: input.accessRequirements,
      observation: projection.observation,
      created_at: input.asOf
    });
  } catch (e) {
    return failure(e);
  }
}
