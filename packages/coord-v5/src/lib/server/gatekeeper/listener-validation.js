const idPattern = /^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$/;
const unsafe = new Set(['__proto__', 'prototype', 'constructor']);

/** @param {unknown} value @param {string[]} required @param {string[]} [optional] @returns {Record<string, any>} */
export function exact(value, required, optional = []) {
  if (
    !value ||
    typeof value !== 'object' ||
    Array.isArray(value) ||
    Object.getPrototypeOf(value) !== Object.prototype
  )
    throw new TypeError('Invalid listener input');
  const keys = Object.keys(value);
  if (
    required.some((key) => !Object.hasOwn(value, key)) ||
    keys.some((key) => unsafe.has(key) || (!required.includes(key) && !optional.includes(key)))
  )
    throw new TypeError('Invalid listener input');
  return /** @type {Record<string, any>} */ (value);
}

/** @param {unknown} value @returns {string} */
export function id(value) {
  if (
    typeof value !== 'string' ||
    value.length < 1 ||
    value.length > 256 ||
    !idPattern.test(value) ||
    unsafe.has(value)
  )
    throw new TypeError('Invalid listener identifier');
  return value;
}

/** @param {unknown} input @returns {Record<string, any>} */
export function routes(input) {
  if (!Array.isArray(input) || input.length > 1000) throw new TypeError('Invalid listener routes');
  /** @type {Record<string, any>} */
  const result = {};
  for (const raw of input) {
    const route = exact(
      raw,
      ['jobId', 'logicalIdentity', 'lifecycle'],
      ['threadId', 'hostId', 'coordinatorJobId']
    );
    const jobId = id(route.jobId);
    id(route.logicalIdentity);
    if (!['active', 'dormant', 'retired'].includes(route.lifecycle) || Object.hasOwn(result, jobId))
      throw new TypeError('Invalid listener route');
    for (const key of ['threadId', 'hostId', 'coordinatorJobId'])
      if (Object.hasOwn(route, key)) id(route[key]);
    result[jobId] = { ...route };
  }
  for (const start of Object.keys(result)) {
    const seen = new Set();
    let current = start;
    while (result[current]?.coordinatorJobId) {
      if (seen.has(current)) throw new TypeError('Listener coordinator cycle');
      seen.add(current);
      current = result[current].coordinatorJobId;
    }
  }
  return result;
}

/** @param {unknown} value */
function collection(value) {
  const source = exact(value, ['coverage', 'items']);
  if (
    !['complete', 'partial', 'unavailable', 'unsupported'].includes(source.coverage) ||
    !Array.isArray(source.items) ||
    source.items.length > 1000
  )
    throw new TypeError('Invalid listener observation');
  const seen = new Map();
  const items = source.items.map((raw) => {
    const item = exact(raw, ['itemId', 'jobId', 'revision']);
    const clean = { itemId: id(item.itemId), jobId: id(item.jobId), revision: id(item.revision) };
    const key = JSON.stringify([clean.jobId, clean.itemId]);
    if (seen.has(key) && seen.get(key) !== clean.revision)
      throw new TypeError('Conflicting listener revisions');
    seen.set(key, clean.revision);
    return clean;
  });
  return { coverage: source.coverage, items };
}

/** @param {unknown} value @param {number} nowMs @param {number} priorMs */
export function observation(value, nowMs, priorMs) {
  const source = exact(value, [
    'version',
    'eventObservation',
    'obligationObservation',
    'observedAt'
  ]);
  if (
    source.version !== 1 ||
    typeof source.observedAt !== 'string' ||
    !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)$/.test(source.observedAt)
  )
    throw new TypeError('Invalid listener observation');
  const match =
    /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?(?:Z|[+-](\d\d):(\d\d))$/.exec(
      source.observedAt
    );
  if (!match) throw new TypeError('Invalid listener observation');
  const [
    ,
    yearText,
    monthText,
    dayText,
    hourText,
    minuteText,
    secondText,
    offsetHourText,
    offsetMinuteText
  ] = match;
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (
    month < 1 ||
    month > 12 ||
    day < 1 ||
    day > days[month - 1] ||
    Number(hourText) > 23 ||
    Number(minuteText) > 59 ||
    Number(secondText) > 59 ||
    Number(offsetHourText ?? 0) > 23 ||
    Number(offsetMinuteText ?? 0) > 59
  )
    throw new RangeError('Invalid listener observation time');
  const observedMs = Date.parse(source.observedAt);
  if (!Number.isFinite(observedMs) || observedMs > nowMs || observedMs < priorMs)
    throw new RangeError('Invalid listener observation time');
  const eventObservation = collection(source.eventObservation);
  const obligationObservation = collection(source.obligationObservation);
  const revisions = new Map(
    eventObservation.items.map((item) => [JSON.stringify([item.jobId, item.itemId]), item.revision])
  );
  for (const item of obligationObservation.items) {
    const key = JSON.stringify([item.jobId, item.itemId]);
    if (revisions.has(key) && revisions.get(key) !== item.revision)
      throw new TypeError('Conflicting listener revisions');
  }
  return {
    observedAt: source.observedAt,
    observedMs,
    eventObservation,
    obligationObservation
  };
}

/** @param {unknown} value @param {boolean} [receipt] */
export function correlation(value, receipt = false) {
  const source = exact(
    value,
    receipt ? ['wakeId', 'attemptId', 'target', 'status'] : ['wakeId', 'attemptId', 'target']
  );
  id(source.wakeId);
  id(source.attemptId);
  const target = exact(source.target, ['threadId'], ['hostId']);
  id(target.threadId);
  if (Object.hasOwn(target, 'hostId')) id(target.hostId);
  if (receipt && !['accepted', 'error', 'unknown'].includes(source.status))
    throw new TypeError('Invalid listener receipt');
  return source;
}
