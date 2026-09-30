/** The only supported annotation-note protocol version. */
export const PROTOCOL = 'gatekeeper/1';

/** @typedef {{protocol:string,event_id:string,conversation_id:string,sender:string,kind:string,created_at:string,payload:Record<string, any>,causation_id?:string}} GatekeeperEvent */

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const zonedTime =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|[+-](\d{2}):(\d{2}))$/;
const speakers = new Set(['visitor', 'gatekeeper', 'agent', 'human']);
const statuses = new Set(['ready', 'active', 'blocked', 'paused', 'completed', 'cancelled']);
const coverages = new Set(['complete', 'partial', 'unavailable', 'unsupported']);
const presenceClocks = ['contact_at', 'inbox_observed_at', 'progress_at', 'checkpoint_at'];

/** @param {unknown} value @returns {value is Record<string, any>} */
const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
/** @param {unknown} value */
const isUuid = (value) => typeof value === 'string' && uuid.test(value);
/** @param {unknown} value */
const isText = (value) => typeof value === 'string' && value.trim().length > 0;
/** @param {unknown} value */
const isTime = (value) => {
  if (typeof value !== 'string') return false;
  const parts = zonedTime.exec(value);
  if (!parts) return false;
  const year = Number(parts[1]);
  const month = Number(parts[2]);
  const day = Number(parts[3]);
  const hour = Number(parts[4]);
  const minute = Number(parts[5]);
  const second = Number(parts[6]);
  const offsetHour = parts[7] === undefined ? 0 : Number(parts[7]);
  const offsetMinute = parts[8] === undefined ? 0 : Number(parts[8]);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return (
    month >= 1 &&
    month <= 12 &&
    day >= 1 &&
    day <= days[month - 1] &&
    hour <= 23 &&
    minute <= 59 &&
    second <= 59 &&
    offsetHour <= 23 &&
    offsetMinute <= 59 &&
    Number.isFinite(Date.parse(value))
  );
};

/**
 * Validate a decoded gatekeeper event. A result is either `{ ok: true, event }`
 * or `{ ok: false, error }`; inputs are never changed.
 * @param {unknown} value
 * @returns {{ok:true,event:GatekeeperEvent}|{ok:false,error:string}}
 */
export function validateEvent(value) {
  if (!isObject(value)) return { ok: false, error: 'event must be an object' };
  if (value.protocol !== PROTOCOL) return { ok: false, error: 'unsupported protocol' };
  if (
    !isUuid(value.event_id) ||
    !isUuid(value.conversation_id) ||
    !isText(value.sender) ||
    !isTime(value.created_at)
  ) {
    return { ok: false, error: 'invalid event envelope' };
  }
  if (value.causation_id !== undefined && !isUuid(value.causation_id))
    return { ok: false, error: 'invalid causation_id' };
  if (!isObject(value.payload)) return { ok: false, error: 'payload must be an object' };

  const p = value.payload;
  let valid;
  switch (value.kind) {
    case 'message':
      valid = isText(p.text) && speakers.has(p.speaker);
      break;
    case 'work.opened':
      valid = isUuid(p.work_id) && isText(p.title);
      break;
    case 'work.updated':
      valid = isUuid(p.work_id) && statuses.has(p.status) && isUuid(p.expected_event_id);
      break;
    case 'question.opened':
    case 'question.answered':
      valid = isUuid(p.question_id) && isText(p.text);
      break;
    case 'checkpoint.published':
      valid = isUuid(p.work_id) && isHttpsUrl(p.artifact) && typeof p.verified === 'boolean';
      break;
    case 'presence.observed':
      valid =
        isText(p.session_id) &&
        coverages.has(p.coverage) &&
        presenceClocks.every((clock) => p[clock] === undefined || isTime(p[clock]));
      break;
    default:
      return { ok: false, error: 'unsupported kind' };
  }
  return valid
    ? { ok: true, event: /** @type {GatekeeperEvent} */ (value) }
    : { ok: false, error: `invalid ${value.kind} payload` };
}

/** @param {unknown} value */
function isHttpsUrl(value) {
  if (typeof value !== 'string') return false;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && Boolean(url.hostname);
  } catch {
    return false;
  }
}

/** @param {unknown} note Parse a MomentAnnotation.note containing one event. */
export function parseAnnotationNote(note) {
  if (typeof note !== 'string') return { ok: false, error: 'note must be a JSON string' };
  try {
    return validateEvent(JSON.parse(note));
  } catch {
    return { ok: false, error: 'malformed JSON' };
  }
}

/** @param {unknown} event Serialize a validated event for MomentAnnotation.note. */
export function serializeEvent(event) {
  const result = validateEvent(event);
  if (!result.ok) throw new TypeError(result.error);
  return JSON.stringify(event);
}
