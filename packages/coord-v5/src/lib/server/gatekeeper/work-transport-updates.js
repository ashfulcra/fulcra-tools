import { readOwnedDataUpdates, readWorkWindow } from './work-transport-read.js';
import { reconcileWorkReadback } from './work-transport-publish.js';

const OVERLAP = 120000;
const AUDIT_INTERVAL = 3600000;
const MAX_WINDOW = 7 * 86400000;
const MAX_SUMMARY_WINDOWS = 8;
const ZONED = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|[+-](\d{2}):(\d{2}))$/;
/** Validate before segment normalization; Date.parse silently repairs invalid civil dates.
 * @param {unknown} value */
function instant(value) {
  if (typeof value !== 'string') return false;
  const match = ZONED.exec(value);
  if (!match) return false;
  const [year, month, day, hour, minute, second] = match.slice(1, 7).map(Number);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  const parsed = Date.parse(value);
  return year > 0 && month >= 1 && month <= 12 && day >= 1 && day <= days[month - 1] &&
    hour <= 23 && minute <= 59 && second <= 59 &&
    (!match[7] || (Number(match[7]) <= 23 && Number(match[8]) <= 59)) &&
    Number.isFinite(parsed) && new Date(parsed).getUTCFullYear() >= 1 && new Date(parsed).getUTCFullYear() <= 9999;
}

/** Catch up without dropping ingestion intervals or advancing on partial summaries.
 * @param {any} input @param {string} start @param {string} end */
async function readSummaryInterval(input, start, end) {
  const boundary = Date.parse(end);
  let cursor = Date.parse(start);
  if (Math.ceil((boundary - cursor) / MAX_WINDOW) > MAX_SUMMARY_WINDOWS)
    return { status: 'unavailable', code: 'SUMMARY_CATCHUP_LIMIT' };
  let changed = false;
  while (cursor < boundary) {
    const next = Math.min(cursor + MAX_WINDOW, boundary);
    const hints = await readOwnedDataUpdates({ ...input,
      start: new Date(cursor).toISOString(), end: new Date(next).toISOString() });
    if (hints.status !== 'available') return hints;
    changed ||= hints.changed;
    cursor = next;
  }
  return { status: 'available', changed };
}

/** Parallel, opt-in route. Quiet hints do not replace durable obligations or prove completeness.
 * Caller supplies the RECORD replay window; it is never derived from ingestion time.
 * @param {any} input */
export async function readWorkUpdates(input) {
  const { store, start, end, updatesStart, mode = 'shadow', now = Date.now } = input;
  const prior = store.updateCursor();
  let healthRevision = store.updateHealth().revision;
  const base = () => ({ update_cursor: store.updateCursor()?.cursor ?? null, mode });
  const scope = store.inspect().scope;
  if (scope.principal_id !== input.config?.principalId || scope.channel !== input.config?.channel ||
      scope.workspace_id !== input.config?.workspaceId || scope.workstream_id !== input.config?.workstreamId)
    return { ...base(), status: 'blocked', code: 'STORE_SCOPE_MISMATCH' };
  const clock = now();
  if (!['shadow', 'gated'].includes(mode) || !instant(start) || !instant(end) || !instant(updatesStart) ||
      Date.parse(start) >= Date.parse(end) || Date.parse(updatesStart) >= Date.parse(end) ||
      Date.parse(end) - Date.parse(start) > MAX_WINDOW || !Number.isFinite(clock) || Date.parse(end) > clock ||
      (prior && Date.parse(end) <= Date.parse(prior.cursor)))
    return { ...base(), status: 'blocked', code: 'INVALID_UPDATES_WINDOW' };
  const updateStart = prior ? new Date(Date.parse(prior.cursor) - OVERLAP).toISOString() : updatesStart;
  const hints = await readSummaryInterval(input, updateStart, end);
  const health = store.updateHealth();
  const pending = store.inspect().intents.some(intent => intent.state !== 'VERIFIED');
  const audit = !prior || clock < Date.parse(prior.last_direct_at) || clock - Date.parse(prior.last_direct_at) >= AUDIT_INTERVAL;
  const wouldSkip = hints.status === 'available' && !hints.changed && !audit && !pending &&
    !prior.hints_disabled && !health.hints_disabled && !health.retry_required && prior.record_start === start;
  const direct = mode === 'shadow' || !wouldSkip;
  let result = { status: 'unchanged_hint', added_records: 0, candidate_count: 0, reconciled: [] };
  let clean = true;
  let hintMiss = false;
  if (direct) {
    const readResult = await readWorkWindow({ ...input, start, end, now });
    const stored = store.appendWindow(readResult);
    clean = stored.status === 'stored' && readResult.observation.coverage === 'partial' &&
      readResult.observation.gaps.length === 0 && readResult.observation.errors.length === 0;
    const reconciled = [];
    if (stored.status === 'stored') {
      for (const intent of store.inspect().intents) {
        if (readResult.records.some(record => record.event_id === intent.event_id))
          reconciled.push(reconcileWorkReadback({ store, readResult, eventId: intent.event_id }));
      }
    }
    hintMiss = !!prior && hints.status === 'available' && !hints.changed && stored.added_records > 0;
    if (!clean || hintMiss) {
      store.noteUpdateHealth({ retryRequired: !clean, hintsDisabled: hintMiss });
      healthRevision += 1;
    }
    result = { status: stored.status, code: stored.code, added_records: stored.added_records ?? 0,
      candidate_count: readResult.candidates.length, coverage: readResult.observation.coverage,
      gaps: readResult.observation.gaps, errors: readResult.observation.errors,
      reconciled: reconciled.map(entry => ({ status: entry.status, event_id: entry.event_id })) };
  }
  if (clean && hints.status === 'available') {
    const committed = store.commitUpdateCursor(prior, {
      cursor: end, last_direct_at: direct ? new Date(clock).toISOString() : prior.last_direct_at,
      record_start: start, hints_disabled: (prior?.hints_disabled ?? false) || health.hints_disabled || hintMiss
    }, healthRevision);
    if (committed.status !== 'stored') result = { ...result, status: 'blocked', code: committed.code };
  }
  return { ...result, ...base(), update_status: hints.status, update_error: hints.code,
    direct_read: direct, would_skip: wouldSkip,
    hint_miss: hintMiss, pending_work_requires_processing: true };
}
