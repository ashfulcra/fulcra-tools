import {
  serializeWorkEvent,
  validateWorkEvent,
  workContentDigest
} from '../../gatekeeper/work-contract.js';
import { validateWorkTransportConfig } from './work-transport-config.js';
import { verifyOwnedWorkStream } from './work-transport-read.js';

const SOURCE_ID = 'com.fulcradynamics.annotation.00000000-0000-4000-8000-000000000901';
const STREAM_ID = '00000000-0000-4000-8000-000000000901';
const MAX_TOKEN = 16 * 1024;
const MAX_RESPONSE = 2 * 1024 * 1024;
const MAX_UPLOAD_ID = 256;

/** @param {unknown} value */
function validToken(value) {
  return (
    typeof value === 'string' &&
    value.length > 0 &&
    Buffer.byteLength(value, 'utf8') <= MAX_TOKEN &&
    !/[\r\n]/.test(value)
  );
}
/** @param {any} config @param {any} event */
function ownEvent(config, event) {
  return (
    event.workspace_id === config.workspaceId &&
    event.workstream_id === config.workstreamId &&
    event.stream_id === STREAM_ID &&
    ['principal_id', 'logical_agent_id', 'instance_id', 'session_id'].every(
      (key) => event.actor[key] === config.actorBinding[key]
    )
  );
}
/** @param {unknown} error */
function safeCode(error) {
  return error instanceof Error &&
    ['TIMEOUT', 'HTTP_ERROR', 'BYTE_LIMIT', 'MALFORMED_RESPONSE'].includes(error.message)
    ? error.message
    : error instanceof Error && error.name === 'AbortError'
      ? 'TIMEOUT'
      : 'POST_UNKNOWN';
}
/** Bound both fetch and streamed JSON receipt under one 15-second deadline.
 * @param {typeof fetch} fetchImpl @param {string} token @param {string} url @param {string} body */
async function postJson(fetchImpl, token, url, body) {
  const controller = new AbortController();
  /** @type {ReturnType<typeof setTimeout>|undefined} */ let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new Error('TIMEOUT'));
    }, 15000);
  });
  /** @param {Promise<any>} promise */
  const bounded = (promise) => Promise.race([promise, deadline]);
  try {
    const response = await bounded(
      fetchImpl(url, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/x-jsonl',
          'Content-Length': String(Buffer.byteLength(body, 'utf8')),
          Accept: 'application/json'
        },
        body,
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

/** One attempt only. An upload receipt is pending until exact source-bound read-back.
 * @param {{fetch:typeof fetch,token:string,config:unknown,store:any,event:unknown,now:()=>number}} input */
export async function publishWorkOnce(input) {
  const eventId =
    typeof input?.event === 'object' &&
    input.event !== null &&
    'event_id' in input.event &&
    typeof input.event.event_id === 'string'
      ? input.event.event_id
      : null;
  /** @param {string} code */
  const blocked = (code) => ({ status: 'blocked', event_id: eventId, code });
  let config;
  try {
    config = validateWorkTransportConfig(input?.config);
  } catch {
    return blocked('INVALID_CONFIG');
  }
  const checked = validateWorkEvent(input?.event);
  if (!checked.ok || !ownEvent(config, checked.event)) return blocked('INVALID_EVENT_SCOPE');
  if (
    !validToken(input?.token) ||
    typeof input?.fetch !== 'function' ||
    typeof input?.now !== 'function' ||
    !input?.store ||
    typeof input.store.reserveIntent !== 'function' ||
    typeof input.store.startPost !== 'function' ||
    typeof input.store.recordPostOutcome !== 'function'
  )
    return blocked('INVALID_INPUT');
  const id = checked.event.event_id;
  const note = serializeWorkEvent(checked.event);
  const digest = workContentDigest(checked.event);
  const preflight = await verifyOwnedWorkStream({ fetch: input.fetch, token: input.token, config });
  if (preflight.status !== 'verified')
    return { status: 'blocked', event_id: id, code: preflight.code };
  let reservation;
  try {
    reservation = input.store.reserveIntent({ event: checked.event, note, digest });
  } catch {
    return { status: 'blocked', event_id: id, code: 'INTENT_FAILURE' };
  }
  if (reservation.status === 'conflict')
    return { status: 'blocked', event_id: id, code: 'INTENT_CONFLICT' };
  let start;
  try {
    start = input.store.startPost(id);
  } catch {
    return { status: 'unknown', event_id: id, code: 'JOURNAL_FAILURE' };
  }
  if (start.status !== 'send_once')
    return {
      status: 'unknown',
      event_id: id,
      code: start.status === 'already_started' ? 'ALREADY_STARTED' : 'JOURNAL_FAILURE'
    };
  const body = `${JSON.stringify({ note, sources: ['com.gatekeeper.frontdoor', SOURCE_ID] })}\n`;
  try {
    const receipt = await postJson(
      input.fetch,
      input.token,
      new URL('/ingest/v1/record/MomentAnnotation?api_version=v1alpha1', config.baseUrl).href,
      body
    );
    if (
      !receipt ||
      typeof receipt !== 'object' ||
      Array.isArray(receipt) ||
      typeof receipt.upload_id !== 'string' ||
      receipt.upload_id.length === 0 ||
      receipt.upload_id.length > MAX_UPLOAD_ID
    )
      throw new Error('MALFORMED_RESPONSE');
    const recorded = input.store.recordPostOutcome(id, {
      status: 'upload_accepted',
      upload_id: receipt.upload_id
    });
    if (recorded.status !== 'upload_accepted')
      return { status: 'unknown', event_id: id, code: 'JOURNAL_FAILURE' };
    return { status: 'pending_readback', event_id: id, upload_id: receipt.upload_id };
  } catch (error) {
    const code = safeCode(error);
    try {
      input.store.recordPostOutcome(id, { status: 'unknown', code });
    } catch {
      /* POST_STARTED remains non-resendable */
    }
    return { status: 'unknown', event_id: id, code };
  }
}

/** Read-back is evidence only when it came from Task 1 and was accepted by this store.
 * @param {{store:any,readResult:unknown,eventId:string}} input */
export function reconcileWorkReadback(input) {
  const eventId = input?.eventId;
  if (
    typeof eventId !== 'string' ||
    !input?.store ||
    typeof input.store.appendWindow !== 'function' ||
    typeof input.store.inspect !== 'function' ||
    typeof input.store.recordReadback !== 'function'
  )
    return { status: 'unknown', event_id: eventId };
  const accepted = input.store.appendWindow(input.readResult);
  if (accepted.status !== 'stored') return { status: 'unknown', event_id: eventId };
  const intent = input.store
    .inspect()
    .intents.find((/** @type {any} */ entry) => entry.event_id === eventId);
  if (!intent) return { status: 'unknown', event_id: eventId };
  const records = /** @type {any} */ (input.readResult).records.filter(
    (/** @type {any} */ record) => record.event_id === eventId
  );
  if (records.length === 0) {
    if (intent.state === 'CONFLICT') return { status: 'conflict', event_id: eventId };
    if (intent.state === 'VERIFIED')
      return { status: 'verified', event_id: eventId, record_id: intent.record_id };
    return {
      status: intent.state === 'UPLOAD_ACCEPTED' ? 'pending_readback' : 'unknown',
      event_id: eventId
    };
  }
  let verified = null;
  for (const record of records) {
    const outcome = input.store.recordReadback(eventId, record);
    if (outcome.status === 'conflict') return { status: 'conflict', event_id: eventId };
    if (outcome.status === 'verified') verified = outcome.record_id;
  }
  return verified
    ? { status: 'verified', event_id: eventId, record_id: verified }
    : { status: 'unknown', event_id: eventId };
}
