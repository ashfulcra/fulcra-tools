import { parseAnnotationNote } from '../../gatekeeper/protocol.js';
import { parseWorkNote, workContentDigest } from '../../gatekeeper/work-contract.js';
import { validateWorkTransportConfig } from './work-transport-config.js';

const MAX_RESPONSE = 2 * 1024 * 1024;
const MAX_NOTE = 64 * 1024;
const MAX_ROWS = 1000;
const MAX_TOKEN = 16 * 1024;
const MAX_WINDOW = 7 * 24 * 60 * 60 * 1000;
const STREAM_ID = '00000000-0000-4000-8000-000000000901';
const SOURCE_ID = `com.fulcradynamics.annotation.${STREAM_ID}`;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const ZONED = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-](\d{2}):(\d{2}))$/;
const branded = new WeakSet();

/** @param {unknown} value */
const plain = (value) =>
  value !== null &&
  typeof value === 'object' &&
  !Array.isArray(value) &&
  Object.getPrototypeOf(value) === Object.prototype;
/** @param {unknown} value */
function validTime(value) {
  if (typeof value !== 'string') return false;
  const m = ZONED.exec(value);
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
  const parsed = Date.parse(value);
  return (
    year > 0 &&
    month >= 1 &&
    month <= 12 &&
    day >= 1 &&
    day <= days[month - 1] &&
    hour <= 23 &&
    minute <= 59 &&
    second <= 59 &&
    (!m[8] || (Number(m[8]) <= 23 && Number(m[9]) <= 59)) &&
    Number.isFinite(parsed) &&
    new Date(parsed).getUTCFullYear() >= 1 &&
    new Date(parsed).getUTCFullYear() <= 9999
  );
}
/** @param {unknown} value */
function freeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}
/** @param {unknown} value */
function safeCode(value) {
  return value instanceof Error &&
    ['TIMEOUT', 'HTTP_ERROR', 'BYTE_LIMIT', 'MALFORMED_RESPONSE'].includes(value.message)
    ? value.message
    : value instanceof Error && value.name === 'AbortError'
      ? 'TIMEOUT'
      : 'READ_FAILURE';
}
/** @param {unknown} token */
function tokenValid(token) {
  return (
    typeof token === 'string' &&
    token.length > 0 &&
    Buffer.byteLength(token, 'utf8') <= MAX_TOKEN &&
    !/[\r\n]/.test(token)
  );
}
/** One abort deadline covers fetch and streamed body consumption.
 * @param {typeof fetch} fetchImpl @param {string} token @param {string} url */
async function readJson(fetchImpl, token, url) {
  const controller = new AbortController();
  /** @type {ReturnType<typeof setTimeout>|undefined} */ let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new Error('TIMEOUT'));
    }, 15000);
  });
  /** @param {Promise<any>} promise */
  const bounded = (promise) => Promise.race([promise, timeout]);
  try {
    const response = await bounded(
      fetchImpl(url, {
        method: 'GET',
        headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
        redirect: 'error',
        signal: controller.signal
      })
    );
    if (!response?.ok || response.redirected || (response.status >= 300 && response.status < 400))
      throw new Error('HTTP_ERROR');
    const declared = response.headers?.get('content-length');
    if (declared && /^\d+$/.test(declared) && Number(declared) > MAX_RESPONSE)
      throw new Error('BYTE_LIMIT');
    if (!response.body || typeof response.body.getReader !== 'function')
      throw new Error('MALFORMED_RESPONSE');
    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8', { fatal: true });
    let bytes = 0;
    let raw = '';
    try {
      while (true) {
        const chunk = await bounded(reader.read());
        if (chunk.done) break;
        bytes += chunk.value.byteLength;
        if (bytes > MAX_RESPONSE) {
          try {
            void reader.cancel().catch(() => {});
          } catch {
            /* already closed */
          }
          throw new Error('BYTE_LIMIT');
        }
        raw += decoder.decode(chunk.value, { stream: true });
      }
      raw += decoder.decode();
    } finally {
      try {
        reader.releaseLock();
      } catch {
        /* pending read aborted */
      }
    }
    try {
      return JSON.parse(raw);
    } catch {
      throw new Error('MALFORMED_RESPONSE');
    }
  } finally {
    if (timer) clearTimeout(timer);
  }
}
/** @param {any} config */
function scope(config) {
  return {
    principal_id: config.principalId,
    channel: config.channel,
    workspace_id: config.workspaceId,
    workstream_id: config.workstreamId,
    stream_id: STREAM_ID
  };
}
/** @param {any} config */
function paths(config) {
  return {
    info: new URL('/user/v1alpha1/info', config.baseUrl).href,
    catalog: new URL(
      `/data/v1/catalog?data_type=${encodeURIComponent(config.channel)}`,
      config.baseUrl
    ).href,
    annotation: new URL(
      `/user/v1alpha1/annotation?fulcra_userid=${encodeURIComponent(config.principalId)}&annotation_type=moment`,
      config.baseUrl
    ).href
  };
}
/** @param {any} input */
async function preflight(input) {
  let config;
  try {
    config = validateWorkTransportConfig(input?.config);
  } catch {
    return { status: 'unavailable', code: 'INVALID_CONFIG' };
  }
  if (typeof input?.fetch !== 'function' || !tokenValid(input?.token))
    return { status: 'unavailable', code: 'INVALID_CONFIG' };
  const urls = paths(config);
  try {
    const info = await readJson(input.fetch, input.token, urls.info);
    if (!plain(info) || info.userid !== config.principalId)
      return { status: 'unavailable', code: 'PRINCIPAL_MISMATCH' };
    const catalog = await readJson(input.fetch, input.token, urls.catalog);
    const matches = Array.isArray(catalog)
      ? catalog.filter((entry) => entry?.id === config.channel)
      : [];
    if (
      !Array.isArray(catalog) ||
      catalog.length > MAX_ROWS ||
      matches.length !== 1 ||
      matches[0].api_version !== 'v1alpha1' ||
      matches[0].recordable !== true ||
      matches[0].queryable !== true ||
      matches[0].record_spec?.type !== 'event' ||
      (Object.hasOwn(matches[0], 'fulcra_userid') &&
        matches[0].fulcra_userid !== config.principalId)
    )
      return { status: 'unavailable', code: 'CATALOG_MISMATCH' };
    const annotations = await readJson(input.fetch, input.token, urls.annotation);
    const rows = Array.isArray(annotations)
      ? annotations.filter((entry) => entry?.id === STREAM_ID)
      : [];
    if (
      !Array.isArray(annotations) ||
      annotations.length > MAX_ROWS ||
      rows.length !== 1 ||
      rows[0].fulcra_userid !== config.principalId ||
      rows[0].annotation_type !== 'moment' ||
      rows[0].fulcra_source_id !== SOURCE_ID ||
      !Object.hasOwn(rows[0], 'deleted_at') ||
      rows[0].deleted_at !== null
    )
      return { status: 'unavailable', code: 'ANNOTATION_MISMATCH' };
    return { status: 'verified' };
  } catch (error) {
    return { status: 'unavailable', code: safeCode(error) };
  }
}

/** Verify only the observed bearer principal and owned synthetic channel, not actor grants.
 * @param {{fetch:typeof fetch,token:string,config:any}} input */
export async function verifyOwnedWorkStream(input) {
  return preflight(input);
}

/** @param {any} input */
export async function readWorkWindow(input) {
  let config;
  try {
    config = validateWorkTransportConfig(input?.config);
  } catch {
    throw new TypeError('INVALID_CONFIG');
  }
  const { start, end, now } = input ?? {};
  const stamped = () => {
    try {
      const value = new Date(now()).toISOString();
      return validTime(value) ? value : new Date(0).toISOString();
    } catch {
      return new Date(0).toISOString();
    }
  };
  /** @param {string} coverage @param {any[]} records @param {any[]} gaps @param {any[]} errors */
  const finish = (coverage, records = [], gaps = [], errors = []) => {
    const at = stamped();
    const result = {
      scope: scope(config),
      window: { start, end },
      records,
      candidates: records.map((record) => record.event),
      eventEvidence: records.map((record) => ({
        event_id: record.event_id,
        event_digest: record.event_digest,
        record_id: record.record_id,
        source_principal_id: record.source_binding.metadata.fulcra_userid,
        stream_id: STREAM_ID,
        received_at: at
      })),
      observation: {
        coverage,
        as_of: at,
        last_successful_observation_at: coverage === 'partial' ? at : null,
        sources: [{ stream_id: STREAM_ID, status: coverage, pending_pages: null }],
        gaps,
        errors,
        completeness_evidence_id: null
      }
    };
    for (const record of records) record.received_at = at;
    freeze(result);
    branded.add(result);
    return result;
  };
  if (
    !validTime(start) ||
    !validTime(end) ||
    Date.parse(start) >= Date.parse(end) ||
    Date.parse(end) - Date.parse(start) > MAX_WINDOW ||
    typeof now !== 'function'
  )
    return finish('unavailable', [], [], [{ code: 'INVALID_WINDOW', stream_id: STREAM_ID }]);
  const owner = await preflight(input);
  if (owner.status !== 'verified')
    return finish('unavailable', [], [], [{ code: owner.code, stream_id: STREAM_ID }]);
  let rows;
  try {
    const query = new URLSearchParams({
      start_time: new Date(start).toISOString(),
      end_time: new Date(end).toISOString()
    });
    rows = await readJson(
      input.fetch,
      input.token,
      new URL(`/data/v1alpha1/event/${config.channel}?${query}`, config.baseUrl).href
    );
    if (!Array.isArray(rows))
      return finish('unavailable', [], [], [{ code: 'MALFORMED_RESPONSE', stream_id: STREAM_ID }]);
    if (rows.length > MAX_ROWS)
      return finish('unavailable', [], [], [{ code: 'ROW_LIMIT', stream_id: STREAM_ID }]);
  } catch (error) {
    return finish('unavailable', [], [], [{ code: safeCode(error), stream_id: STREAM_ID }]);
  }
  const accepted = [];
  const gaps = [];
  for (const row of rows) {
    if (!plain(row) || typeof row.id !== 'string' || !UUID.test(row.id)) {
      gaps.push({ code: 'RECORD_ID', stream_id: STREAM_ID });
      continue;
    }
    if (
      row.source_id !== SOURCE_ID ||
      !plain(row.metadata) ||
      row.metadata.id !== STREAM_ID ||
      row.metadata.fulcra_userid !== config.principalId ||
      row.metadata.annotation_type !== 'moment' ||
      row.metadata.fulcra_source_id !== SOURCE_ID ||
      !Object.hasOwn(row.metadata, 'deleted_at') ||
      row.metadata.deleted_at !== null
    ) {
      gaps.push({ code: 'RECORD_SOURCE_MISMATCH', stream_id: STREAM_ID });
      continue;
    }
    if (typeof row.note !== 'string' || Buffer.byteLength(row.note, 'utf8') > MAX_NOTE) {
      gaps.push({ code: 'NOTE_LIMIT', stream_id: STREAM_ID });
      continue;
    }
    if (parseAnnotationNote(row.note).ok) continue;
    const parsed = parseWorkNote(row.note);
    if (!parsed.ok) {
      gaps.push({ code: 'NOTE_FORMAT', stream_id: STREAM_ID });
      continue;
    }
    const event = parsed.event;
    if (
      event.workspace_id !== config.workspaceId ||
      event.workstream_id !== config.workstreamId ||
      event.stream_id !== STREAM_ID ||
      event.actor.principal_id !== config.principalId
    ) {
      gaps.push({ code: 'EVENT_SCOPE_MISMATCH', stream_id: STREAM_ID });
      continue;
    }
    accepted.push({
      record_id: row.id,
      event_id: event.event_id,
      event_digest: workContentDigest(event),
      note: row.note,
      event,
      source_binding: {
        source_id: row.source_id,
        metadata: {
          id: row.metadata.id,
          fulcra_userid: row.metadata.fulcra_userid,
          annotation_type: row.metadata.annotation_type,
          fulcra_source_id: row.metadata.fulcra_source_id,
          deleted_at: null
        }
      },
      received_at: null
    });
  }
  return finish('partial', accepted, gaps);
}

/** Accept only this process's frozen read result after rechecking source and digest bindings.
 * @param {unknown} result @param {unknown} configValue */
export function assertTrustedWorkReadResult(result, configValue) {
  let config;
  try {
    config = validateWorkTransportConfig(configValue);
  } catch {
    throw new TypeError('INVALID_CONFIG');
  }
  if (!plain(result) || !branded.has(/** @type {object} */ (result)))
    throw new TypeError('UNTRUSTED_READ_RESULT');
  const value = /** @type {any} */ (result);
  const expectedScope = scope(config);
  if (
    JSON.stringify(value.scope) !== JSON.stringify(expectedScope) ||
    !validTime(value.window?.start) ||
    !validTime(value.window?.end) ||
    Date.parse(value.window.start) >= Date.parse(value.window.end) ||
    Date.parse(value.window.end) - Date.parse(value.window.start) > MAX_WINDOW ||
    !Array.isArray(value.records) ||
    !Array.isArray(value.candidates) ||
    !Array.isArray(value.eventEvidence) ||
    value.records.length > MAX_ROWS ||
    value.records.length !== value.candidates.length ||
    value.records.length !== value.eventEvidence.length ||
    !['partial', 'unavailable'].includes(value.observation?.coverage) ||
    !validTime(value.observation?.as_of) ||
    value.observation.completeness_evidence_id !== null ||
    value.observation.last_successful_observation_at !==
      (value.observation.coverage === 'partial' ? value.observation.as_of : null) ||
    !Array.isArray(value.observation.sources) ||
    JSON.stringify(value.observation.sources) !==
      JSON.stringify([
        { stream_id: STREAM_ID, status: value.observation.coverage, pending_pages: null }
      ])
  )
    throw new TypeError('UNTRUSTED_READ_RESULT');
  for (let index = 0; index < value.records.length; index++) {
    const record = value.records[index];
    const parsed = parseWorkNote(record.note);
    if (
      !parsed.ok ||
      record.record_id !== value.eventEvidence[index].record_id ||
      !UUID.test(record.record_id) ||
      record.event_id !== parsed.event.event_id ||
      record.event_digest !== workContentDigest(parsed.event) ||
      JSON.stringify(record.event) !== JSON.stringify(parsed.event) ||
      JSON.stringify(value.candidates[index]) !== JSON.stringify(parsed.event) ||
      record.event_digest !== value.eventEvidence[index].event_digest ||
      record.event_id !== value.eventEvidence[index].event_id ||
      record.received_at !== value.observation.as_of ||
      value.eventEvidence[index].received_at !== value.observation.as_of ||
      value.eventEvidence[index].source_principal_id !== config.principalId ||
      value.eventEvidence[index].stream_id !== STREAM_ID ||
      record.source_binding?.source_id !== SOURCE_ID ||
      JSON.stringify(record.source_binding.metadata) !==
        JSON.stringify({
          id: STREAM_ID,
          fulcra_userid: config.principalId,
          annotation_type: 'moment',
          fulcra_source_id: SOURCE_ID,
          deleted_at: null
        }) ||
      parsed.event.workspace_id !== config.workspaceId ||
      parsed.event.workstream_id !== config.workstreamId ||
      parsed.event.stream_id !== STREAM_ID ||
      parsed.event.actor.principal_id !== config.principalId
    )
      throw new TypeError('UNTRUSTED_READ_RESULT');
  }
  return result;
}
