import { createHash } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import { closeSync, constants, lstatSync, openSync } from 'node:fs';
import { basename, dirname, isAbsolute, resolve } from 'node:path';
import {
  canonicalWorkJson,
  parseWorkNote,
  serializeWorkEvent,
  validateWorkEvent,
  workContentDigest
} from '../../gatekeeper/work-contract.js';
import { validateHandoffPackage } from '../../gatekeeper/checkpoint.js';
import { validHandoffVerification, compareInstants } from '../../gatekeeper/work-projection.js';
import { validateWorkTransportConfig } from './work-transport-config.js';
import { assertTrustedWorkReadResult } from './work-transport-read.js';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const EVENT_LIMIT = 1000;
const RECEIPT_LIMIT = 2 * 1024 * 1024;
const RECEIPT_KEYS = ['schema', 'principal_id', 'workspace_id', 'workstream_id', 'stream_id', 'package', 'verification'];

/** @param {any} receipt @param {any} config @param {any} accumulated */
function verifyReceipt(receipt, config, accumulated) {
  if (
    !plain(receipt) ||
    Reflect.ownKeys(receipt).length !== RECEIPT_KEYS.length ||
    !RECEIPT_KEYS.every((key) => Object.hasOwn(receipt, key)) ||
    receipt.schema !== 'handoff-verification/1' ||
    receipt.principal_id !== config.principalId ||
    receipt.workspace_id !== config.workspaceId ||
    receipt.workstream_id !== config.workstreamId ||
    receipt.stream_id !== config.channel.slice('MomentAnnotation/'.length) ||
    !validHandoffVerification(receipt.verification) ||
    accumulated.status !== 'ready'
  ) return false;
  const checked = validateHandoffPackage(receipt.package);
  if (!checked.ok) return false;
  const pkg = checked.package;
  const v = receipt.verification;
  if (
    workContentDigest(pkg) !== v.package_digest ||
    pkg.workspace_id !== receipt.workspace_id ||
    pkg.workstream_id !== receipt.workstream_id ||
    pkg.recipient.principal_id !== receipt.principal_id ||
    canonicalWorkJson(pkg.recipient) !== canonicalWorkJson(v.checks.receiver) ||
    v.checks.package_digest !== v.package_digest ||
    pkg.publication_event_id !== v.checks.publication.event_id
  ) return false;
  // Multiple authenticated source records may carry the same canonical event.
  // Distinct content for one ID remains ambiguous and must fail closed.
  const matches = (id) => [...new Map(
    accumulated.events.filter((event) => event.event_id === id)
      .map((event) => [canonicalWorkJson(event), event])
  ).values()];
  const ready = matches(v.ready_event_id);
  const offer = matches(v.offer_event_id);
  const publication = matches(v.checks.publication.event_id);
  if (ready.length !== 1 || offer.length !== 1 || publication.length !== 1) return false;
  const [r] = ready, [o] = offer, [p] = publication;
  if (
    r.kind !== 'handoff.ready' || o.kind !== 'handoff.offered' || p.kind !== 'checkpoint.published' ||
    r.workstream_id !== receipt.workstream_id || o.workstream_id !== receipt.workstream_id ||
    p.workstream_id !== receipt.workstream_id ||
    workContentDigest(r) !== v.ready_event_digest ||
    r.payload.offer_event_id !== o.event_id ||
    r.payload.package_digest !== v.package_digest ||
    o.payload.package_digest !== v.package_digest ||
    o.payload.work_id !== pkg.work_id ||
    r.subject.id !== o.subject.id ||
    o.payload.checkpoint_event_id !== p.event_id ||
    canonicalWorkJson(r.actor) !== canonicalWorkJson(o.payload.target) ||
    canonicalWorkJson(r.actor) !== canonicalWorkJson(v.checks.receiver) ||
    r.payload.verification_receipt_id !== v.checks.publication.receipt_id ||
    p.subject.id !== pkg.checkpoint.checkpoint_id ||
    p.payload.work_id !== pkg.work_id ||
    p.payload.artifact.id !== v.checks.publication.artifact_id ||
    p.payload.body_digest !== v.checks.publication.body_digest ||
    canonicalWorkJson(p) !== canonicalWorkJson(pkg.events.find((event) => event.event_id === p.event_id)) ||
    v.checks.publication.status !== 'verified' || !v.checks.publication.receipt_id ||
    v.checks.resources.some((resource) => resource.status !== 'verified' || !resource.receipt_id) ||
    compareInstants(v.checks.checked_at, r.occurred_at) > 0 ||
    compareInstants(r.occurred_at, v.checks.valid_until) >= 0
  ) return false;
  const artifactHash = p.payload.artifact.sha256;
  if (v.checks.publication.artifact_sha256 === undefined
    ? artifactHash !== null && artifactHash !== p.payload.body_digest
    : v.checks.publication.artifact_sha256 !== artifactHash) return false;
  const required = pkg.access_requirements.map((resource) => `${resource.resource_id}:${resource.scope_id}:${resource.action}`).sort();
  const verified = v.checks.resources.map((resource) => `${resource.resource_id}:${resource.scope_id}:${resource.action}`).sort();
  return canonicalWorkJson(required) === canonicalWorkJson(verified);
}

/** @param {string} dbPath */
function ensurePath(dbPath) {
  if (
    typeof dbPath !== 'string' ||
    !isAbsolute(dbPath) ||
    resolve(dbPath) !== dbPath ||
    basename(dbPath) === '.' ||
    dbPath.includes('\0')
  )
    throw new TypeError('UNSAFE_DB_PATH');
  const parent = lstatSync(dirname(dbPath));
  if (!parent.isDirectory() || parent.isSymbolicLink() || (parent.mode & 0o077) !== 0)
    throw new TypeError('UNSAFE_DB_PATH');
  try {
    const entry = lstatSync(dbPath);
    if (!entry.isFile() || entry.isSymbolicLink() || (entry.mode & 0o077) !== 0)
      throw new TypeError('UNSAFE_DB_PATH');
    return false;
  } catch (error) {
    if (/** @type {NodeJS.ErrnoException} */ (error).code !== 'ENOENT') throw error;
  }
  try {
    const fd = openSync(
      dbPath,
      constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW,
      0o600
    );
    closeSync(fd);
    return true;
  } catch (error) {
    if (/** @type {NodeJS.ErrnoException} */ (error).code !== 'EEXIST') throw error;
    const entry = lstatSync(dbPath);
    if (!entry.isFile() || entry.isSymbolicLink() || (entry.mode & 0o077) !== 0)
      throw new TypeError('UNSAFE_DB_PATH');
    return false;
  }
}
/** @param {any} config */
function scope(config) {
  const STREAM_ID = config.channel.slice('MomentAnnotation/'.length);
  return {
    principal_id: config.principalId,
    channel: config.channel,
    workspace_id: config.workspaceId,
    workstream_id: config.workstreamId,
    stream_id: STREAM_ID
  };
}
/** @param {unknown} value */
function plain(value) {
  return (
    value !== null &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    Object.getPrototypeOf(value) === Object.prototype
  );
}
/** @param {any} record @param {any} config */
function validRecord(record, config) {
  const STREAM_ID = config.channel.slice('MomentAnnotation/'.length);
  const SOURCE_ID = `com.fulcradynamics.annotation.${STREAM_ID}`;
  if (
    !plain(record) ||
    typeof record.record_id !== 'string' ||
    !UUID.test(record.record_id) ||
    typeof record.note !== 'string' ||
    Buffer.byteLength(record.note, 'utf8') > 64 * 1024 ||
    !plain(record.source_binding) ||
    record.source_binding.source_id !== SOURCE_ID
  )
    return false;
  const metadata = record.source_binding.metadata;
  if (
    !plain(metadata) ||
    JSON.stringify(metadata) !==
      JSON.stringify({
        id: STREAM_ID,
        fulcra_userid: config.principalId,
        annotation_type: 'moment',
        fulcra_source_id: SOURCE_ID,
        deleted_at: null
      })
  )
    return false;
  const parsed = parseWorkNote(record.note);
  if (
    !parsed.ok ||
    parsed.event.workspace_id !== config.workspaceId ||
    parsed.event.workstream_id !== config.workstreamId ||
    parsed.event.stream_id !== STREAM_ID ||
    parsed.event.actor.principal_id !== config.principalId ||
    record.event_id !== parsed.event.event_id ||
    record.event_digest !== workContentDigest(parsed.event) ||
    JSON.stringify(record.event) !== JSON.stringify(parsed.event) ||
    typeof record.received_at !== 'string' ||
    !Number.isFinite(Date.parse(record.received_at))
  )
    return false;
  return true;
}
/** @param {any} record */
function evidence(record) {
  return {
    event_id: record.event_id,
    event_digest: record.event_digest,
    record_id: record.record_id,
    source_principal_id: record.source_binding.metadata.fulcra_userid,
    stream_id: record.event.stream_id,
    received_at: record.received_at
  };
}
/** @param {any} record */
function variantKey(record) {
  return createHash('sha256')
    .update(`${record.record_id}\0${record.note}\0${record.event_digest}`)
    .digest('hex');
}
/** @param {any} record */
function immutableRecord(record) {
  const immutable = { ...record };
  delete immutable.received_at;
  return canonicalWorkJson(immutable);
}
/** @param {any[]} records */
function conflicts(records) {
  /** @type {{code:string,stream_id:string,event_id?:string}[]} */ const found = [];
  for (const [field, code] of [
    ['event_id', 'EVENT_ID_CONFLICT'],
    ['operation_id', 'OPERATION_ID_CONFLICT'],
    ['record_id', 'RECORD_ID_CONFLICT']
  ]) {
    const groups = new Map();
    for (const record of records) {
      const key = field === 'operation_id' ? record.event.operation_id : record[field];
      const value =
        field === 'record_id' ? record.note : `${record.event_id}:${record.event_digest}`;
      const previous = groups.get(key);
      if (
        previous !== undefined &&
        previous !== value &&
        !found.some(
          (item) =>
            item.code === code && item.event_id === (field === 'record_id' ? record.event_id : key)
        )
      )
        found.push({
          code,
          stream_id: record.event.stream_id,
          event_id: field === 'record_id' ? record.event_id : key
        });
      else if (previous === undefined) groups.set(key, value);
    }
  }
  return found;
}
/** @param {any} config @param {any} event */
function ownEvent(config, event) {
  const STREAM_ID = config.channel.slice('MomentAnnotation/'.length);
  return (
    event.workspace_id === config.workspaceId &&
    event.workstream_id === config.workstreamId &&
    event.stream_id === STREAM_ID &&
    ['principal_id', 'logical_agent_id', 'instance_id', 'session_id'].every(
      (key) => event.actor[key] === config.actorBinding[key]
    )
  );
}

/** A private retention cache and one-shot intent journal, never an authority or completeness proof.
 * @param {{dbPath:string,config:unknown}} input */
export function openWorkTransportStore(input) {
  const config = validateWorkTransportConfig(input?.config);
  const STREAM_ID = config.channel.slice('MomentAnnotation/'.length);
  const dbPath = input?.dbPath;
  const fresh = ensurePath(dbPath);
  const db = new DatabaseSync(dbPath, { timeout: 5000 });
  try {
    db.exec('PRAGMA busy_timeout = 5000');
    const version = /** @type {{user_version:number}} */ (db.prepare('PRAGMA user_version').get())
      .user_version;
    if (fresh) {
      db.exec('BEGIN IMMEDIATE');
      try {
        db.exec('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)');
        db.exec(
          'CREATE TABLE observations (seq INTEGER PRIMARY KEY AUTOINCREMENT, as_of TEXT NOT NULL, coverage TEXT NOT NULL, observation_json TEXT NOT NULL)'
        );
        db.exec(
          'CREATE TABLE record_variants (variant_key TEXT PRIMARY KEY, record_id TEXT NOT NULL, event_id TEXT NOT NULL, operation_id TEXT NOT NULL, digest TEXT NOT NULL, note TEXT NOT NULL, record_json TEXT NOT NULL)'
        );
        db.exec(
          'CREATE TABLE intents (event_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, digest TEXT NOT NULL, note TEXT NOT NULL, event_json TEXT NOT NULL)'
        );
        db.exec(
          'CREATE TABLE intent_transitions (seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL, state TEXT NOT NULL, code TEXT, upload_id TEXT, record_id TEXT)'
        );
        db.exec(
          'CREATE TABLE handoff_verifications (ready_event_id TEXT PRIMARY KEY, receipt_digest TEXT NOT NULL, receipt_json TEXT NOT NULL)'
        );
        db.prepare('INSERT INTO meta (key,value) VALUES (?,?)').run(
          'config',
          JSON.stringify(config)
        );
        db.exec('PRAGMA user_version = 2');
        db.exec('COMMIT');
      } catch (error) {
        db.exec('ROLLBACK');
        throw error;
      }
    } else {
      if (
        ![1, 2].includes(version) ||
        db.prepare('SELECT value FROM meta WHERE key = ?').get('config')?.value !==
          JSON.stringify(config)
      )
        throw new Error('STORE_SCOPE_MISMATCH');
      for (const name of ['observations', 'record_variants', 'intents', 'intent_transitions'])
        if (!db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name=?").get(name))
          throw new Error('STORE_CORRUPT');
      if (db.prepare('PRAGMA quick_check').get()?.quick_check !== 'ok')
        throw new Error('STORE_CORRUPT');
      if (version === 1) {
        db.exec('BEGIN IMMEDIATE');
        try {
          db.exec('CREATE TABLE handoff_verifications (ready_event_id TEXT PRIMARY KEY, receipt_digest TEXT NOT NULL, receipt_json TEXT NOT NULL)');
          db.exec('PRAGMA user_version = 2');
          db.exec('COMMIT');
        } catch (error) {
          db.exec('ROLLBACK');
          throw error;
        }
      } else if (!db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='handoff_verifications'").get())
        throw new Error('STORE_CORRUPT');
    }
  } catch (error) {
    db.close();
    throw error;
  }
  const insertObservation = db.prepare(
    'INSERT INTO observations (as_of,coverage,observation_json) VALUES (?,?,?)'
  );
  const insertRecord = db.prepare(
    'INSERT OR IGNORE INTO record_variants (variant_key,record_id,event_id,operation_id,digest,note,record_json) VALUES (?,?,?,?,?,?,?)'
  );
  const selectRecord = db.prepare(
    'SELECT variant_key,record_id,event_id,operation_id,digest,note,record_json FROM record_variants WHERE variant_key=?'
  );
  const selectIntent = db.prepare(
    'SELECT event_id,operation_id,digest,note,event_json FROM intents WHERE event_id=?'
  );
  const insertIntent = db.prepare(
    'INSERT INTO intents (event_id,operation_id,digest,note,event_json) VALUES (?,?,?,?,?)'
  );
  const insertTransition = db.prepare(
    'INSERT INTO intent_transitions (event_id,state,code,upload_id,record_id) VALUES (?,?,?,?,?)'
  );
  const currentTransition = db.prepare(
    'SELECT state,code,upload_id,record_id FROM intent_transitions WHERE event_id=? ORDER BY seq DESC LIMIT 1'
  );
  const insertVerification = db.prepare(
    'INSERT INTO handoff_verifications (ready_event_id,receipt_digest,receipt_json) VALUES (?,?,?)'
  );
  const selectVerification = db.prepare(
    'SELECT ready_event_id,receipt_digest,receipt_json FROM handoff_verifications WHERE ready_event_id=?'
  );
  const allVerifications = db.prepare(
    'SELECT ready_event_id,receipt_digest,receipt_json FROM handoff_verifications ORDER BY ready_event_id'
  );
  let closed = false;
  /** @param {()=>any} operation */
  function transaction(operation) {
    if (closed) throw new Error('STORE_CLOSED');
    db.exec('BEGIN IMMEDIATE');
    try {
      const result = operation();
      db.exec('COMMIT');
      return result;
    } catch (error) {
      db.exec('ROLLBACK');
      throw error;
    }
  }
  /** @param {string} eventId */
  function state(eventId) {
    return /** @type {string|null} */ (currentTransition.get(eventId)?.state ?? null);
  }
  /** @param {any} row */
  function displayState(row) {
    return row.state === 'POST_STARTED' ? 'UNKNOWN_IN_FLIGHT' : row.state;
  }
  /** @param {any[]} observations */
  function observationOf(observations) {
    const ordered = observations.map((item) => {
      const value = JSON.parse(item.observation_json);
      const status = value.coverage;
      if (
        !['partial', 'unavailable'].includes(status) ||
        status !== item.coverage ||
        value.as_of !== item.as_of ||
        !Number.isFinite(Date.parse(value.as_of)) ||
        value.last_successful_observation_at !== (status === 'partial' ? value.as_of : null) ||
        value.completeness_evidence_id !== null ||
        JSON.stringify(value.sources) !==
          JSON.stringify([{ stream_id: STREAM_ID, status, pending_pages: null }]) ||
        !Array.isArray(value.gaps) ||
        !Array.isArray(value.errors) ||
        [...value.gaps, ...value.errors].some(
          (entry) =>
            !plain(entry) ||
            typeof entry.code !== 'string' ||
            !/^[A-Z_]{1,64}$/.test(entry.code) ||
            entry.stream_id !== STREAM_ID
        )
      )
        throw new Error('STORE_CORRUPT');
      return value;
    });
    const latest = ordered.toSorted((a, b) => Date.parse(a.as_of) - Date.parse(b.as_of)).at(-1);
    const success = ordered
      .filter((item) => item.coverage === 'partial')
      .toSorted((a, b) => Date.parse(a.as_of) - Date.parse(b.as_of))
      .at(-1);
    return {
      coverage: success ? 'partial' : 'unavailable',
      as_of: latest?.as_of ?? null,
      last_successful_observation_at: success?.as_of ?? null,
      sources: [
        {
          stream_id: STREAM_ID,
          status: success ? 'partial' : 'unavailable',
          pending_pages: null
        }
      ],
      gaps: ordered.flatMap((item) => item.gaps),
      // Preserve every window in the journal, but report current contact health.
      // Equal-time failures remain ambiguous; only a strictly newer success heals them.
      errors: ordered
        .filter((item) => !success || Date.parse(item.as_of) >= Date.parse(success.as_of))
        .flatMap((item) => item.errors),
      completeness_evidence_id: null
    };
  }
  return {
    /** Explicit trusted local attestation, never a remote verification or completeness claim.
     * @param {unknown} receipt @param {{now:number}} timing */
    importHandoffVerification(receipt, { now } = /** @type {any} */ ({})) {
      try {
        if (!Number.isFinite(now)) return { status: 'blocked', code: 'INVALID_CLOCK' };
        const canonical = canonicalWorkJson(receipt);
        if (Buffer.byteLength(canonical, 'utf8') > RECEIPT_LIMIT)
          return { status: 'blocked', code: 'RECEIPT_LIMIT' };
        const value = JSON.parse(canonical);
        return transaction(() => {
          const accumulated = this.accumulated();
          if (!verifyReceipt(value, config, accumulated))
            return { status: 'blocked', code: 'INVALID_VERIFICATION' };
          const stamp = new Date(now).toISOString();
          if (compareInstants(value.verification.checks.checked_at, stamp) > 0 ||
              compareInstants(stamp, value.verification.checks.valid_until) >= 0)
            return { status: 'blocked', code: 'VERIFICATION_EXPIRED' };
          const digest = createHash('sha256').update(canonical).digest('hex');
          const prior = selectVerification.get(value.verification.ready_event_id);
          if (prior) {
            if (prior.receipt_digest !== createHash('sha256').update(prior.receipt_json).digest('hex'))
              return { status: 'unavailable', code: 'STORE_CORRUPT' };
            return prior.receipt_digest === digest && prior.receipt_json === canonical
              ? { status: 'same' } : { status: 'blocked', code: 'VERIFICATION_CONFLICT' };
          }
          insertVerification.run(value.verification.ready_event_id, digest, canonical);
          return { status: 'stored' };
        });
      } catch {
        return { status: 'blocked', code: 'INVALID_VERIFICATION' };
      }
    },
    /** Local advisory update watermark, separate from retained record observations. */
    updateCursor() {
      const row = db.prepare('SELECT value FROM meta WHERE key=?').get('updates_cursor_v1');
      if (!row) return null;
      const value = JSON.parse(row.value);
      if (!plain(value) || Object.keys(value).length !== 4 ||
          !['cursor', 'last_direct_at', 'record_start'].every(key => typeof value[key] === 'string' && Number.isFinite(Date.parse(value[key]))) ||
          typeof value.hints_disabled !== 'boolean') throw new Error('STORE_CORRUPT');
      return value;
    },
    /** Durable safety evidence is independent of advisory cursor advancement. */
    updateHealth() {
      const row = db.prepare('SELECT value FROM meta WHERE key=?').get('updates_health_v1');
      if (!row) return { revision: 0, retry_required: false, hints_disabled: false };
      const value = JSON.parse(row.value);
      if (!plain(value) || Object.keys(value).length !== 3 ||
          !Number.isSafeInteger(value.revision) || value.revision < 0 ||
          typeof value.retry_required !== 'boolean' || typeof value.hints_disabled !== 'boolean')
        throw new Error('STORE_CORRUPT');
      return value;
    },
    /** Failures and detected omissions are monotonic, including stale readers.
     * @param {{retryRequired?:boolean,hintsDisabled?:boolean}} flags */
    noteUpdateHealth({ retryRequired = false, hintsDisabled = false }) {
      return transaction(() => {
        const current = this.updateHealth();
        if (current.revision === Number.MAX_SAFE_INTEGER) throw new Error('STORE_CORRUPT');
        const next = { revision: current.revision + 1,
          retry_required: current.retry_required || retryRequired,
          hints_disabled: current.hints_disabled || hintsDisabled };
        db.prepare('INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value')
          .run('updates_health_v1', canonicalWorkJson(next));
        return next;
      });
    },
    /** Compare-and-swap: concurrent/stale reads cannot advance past unprocessed work.
     * A health revision binds recovery to failures known when the read began.
     * @param {any} expected @param {any} next @param {number|undefined} healthRevision */
    commitUpdateCursor(expected, next, healthRevision = undefined) {
      if (!plain(next) || Object.keys(next).length !== 4 ||
          !['cursor', 'last_direct_at', 'record_start'].every(key => typeof next[key] === 'string' && Number.isFinite(Date.parse(next[key]))) ||
          typeof next.hints_disabled !== 'boolean') return { status: 'blocked', code: 'INVALID_UPDATE_CURSOR' };
      return transaction(() => {
        const current = this.updateCursor();
        if (canonicalWorkJson(current) !== canonicalWorkJson(expected) ||
            (current && Date.parse(next.cursor) < Date.parse(current.cursor)))
          return { status: 'blocked', code: 'UPDATE_CURSOR_CONFLICT' };
        const health = this.updateHealth();
        if (healthRevision !== undefined && health.revision !== healthRevision)
          return { status: 'blocked', code: 'UPDATE_HEALTH_CONFLICT' };
        // Only a revision-bound clean recovery may clear the retry obligation.
        if (healthRevision !== undefined && health.retry_required) {
          if (health.revision === Number.MAX_SAFE_INTEGER) throw new Error('STORE_CORRUPT');
          db.prepare('INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value')
            .run('updates_health_v1', canonicalWorkJson({ ...health, revision: health.revision + 1, retry_required: false }));
        }
        db.prepare('INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value')
          .run('updates_cursor_v1', canonicalWorkJson(next));
        return { status: 'stored' };
      });
    },
    /** Return historically valid proofs plus current inactivity markers. The view decides
     * whether an inactive proof is needed to preserve an already accepted transfer.
     * @param {{now:number}} timing */
    handoffVerifications({ now } = /** @type {any} */ ({})) {
      if (!Number.isFinite(now)) return { status: 'blocked', code: 'INVALID_CLOCK' };
      try {
        if (closed) throw new Error('STORE_CLOSED');
        const accumulated = this.accumulated();
        const rows = allVerifications.all();
        if (accumulated.status !== 'ready' && rows.length) throw new Error('STORE_CORRUPT');
        const stamp = new Date(now).toISOString();
        const verifications = [];
        const inactive_ready_event_ids = [];
        for (const row of rows) {
          if (Buffer.byteLength(row.receipt_json, 'utf8') > RECEIPT_LIMIT ||
              createHash('sha256').update(row.receipt_json).digest('hex') !== row.receipt_digest)
            throw new Error('STORE_CORRUPT');
          const receipt = JSON.parse(row.receipt_json);
          if (canonicalWorkJson(receipt) !== row.receipt_json ||
              row.ready_event_id !== receipt.verification?.ready_event_id ||
              !verifyReceipt(receipt, config, accumulated)) throw new Error('STORE_CORRUPT');
          const checks = receipt.verification.checks;
          if (compareInstants(checks.checked_at, stamp) > 0 ||
              compareInstants(stamp, checks.valid_until) >= 0)
            inactive_ready_event_ids.push(row.ready_event_id);
          verifications.push(receipt.verification);
        }
        return { status: 'ready', verifications, inactive_ready_event_ids };
      } catch {
        return { status: 'unavailable', code: 'STORE_CORRUPT' };
      }
    },
    /** @param {unknown} readResult */
    appendWindow(readResult) {
      /** @type {any} */
      let result;
      try {
        result = /** @type {any} */ (assertTrustedWorkReadResult(readResult, config));
      } catch {
        return {
          status: 'blocked',
          added_records: 0,
          code: 'UNTRUSTED_READ_RESULT'
        };
      }
      if (Date.parse(result.observation.as_of) < Date.parse(result.window.end))
        return {
          status: 'blocked',
          added_records: 0,
          code: 'INVALID_OBSERVATION_TIME'
        };
      if (!result.records.every((/** @type {any} */ record) => validRecord(record, config)))
        return { status: 'blocked', added_records: 0, code: 'INVALID_RECORD' };
      try {
        return transaction(() => {
          insertObservation.run(
            result.observation.as_of,
            result.observation.coverage,
            JSON.stringify(result.observation)
          );
          let added = 0;
          for (const record of result.records) {
            const key = variantKey(record);
            added += Number(
              insertRecord.run(
                key,
                record.record_id,
                record.event_id,
                record.event.operation_id,
                record.event_digest,
                record.note,
                JSON.stringify(record)
              ).changes
            );
          }
          return { status: 'stored', added_records: added };
        });
      } catch {
        return { status: 'blocked', added_records: 0, code: 'STORE_FAILURE' };
      }
    },
    accumulated() {
      let observations = [];
      let eventCount = 0;
      try {
        if (closed) throw new Error('STORE_CLOSED');
        observations = db
          .prepare('SELECT as_of,coverage,observation_json FROM observations ORDER BY seq')
          .all();
        eventCount = Number(
          db.prepare('SELECT COUNT(*) AS count FROM record_variants').get()?.count
        );
        const observation = observationOf(observations);
        if (observations.length === 0) {
          if (eventCount !== 0) throw new Error('STORE_CORRUPT');
          return {
            status: 'unavailable',
            events: null,
            event_evidence: null,
            records: null,
            observation,
            event_count: 0,
            conflicts: []
          };
        }
        const rows = db
          .prepare(
            'SELECT variant_key,record_id,event_id,operation_id,digest,note,record_json FROM record_variants ORDER BY rowid'
          )
          .all();
        const records = rows.map((row) => {
          const record = JSON.parse(/** @type {string} */ (row.record_json));
          const key = variantKey(record);
          if (
            !validRecord(record, config) ||
            row.variant_key !== key ||
            row.record_id !== record.record_id ||
            row.event_id !== record.event_id ||
            row.operation_id !== record.event.operation_id ||
            row.digest !== record.event_digest ||
            row.note !== record.note
          )
            throw new Error('STORE_CORRUPT');
          return record;
        });
        if (observation.last_successful_observation_at === null) {
          if (eventCount !== 0) throw new Error('STORE_CORRUPT');
          return {
            status: 'unavailable',
            events: null,
            event_evidence: null,
            records: null,
            observation,
            event_count: 0,
            conflicts: []
          };
        }
        if (eventCount > EVENT_LIMIT) {
          observation.gaps.push({ code: 'EVENT_LIMIT', stream_id: STREAM_ID });
          return {
            status: 'blocked_limit',
            events: null,
            event_evidence: null,
            records: null,
            observation,
            event_count: eventCount,
            conflicts: []
          };
        }
        const conflictRows = conflicts(records);
        observation.gaps.push(...conflictRows);
        return {
          status: 'ready',
          events: records.map((record) => record.event),
          event_evidence: records.map(evidence),
          records,
          observation,
          conflicts: conflictRows
        };
      } catch {
        return {
          status: 'unavailable',
          events: null,
          event_evidence: null,
          records: null,
          observation: {
            coverage: 'unavailable',
            as_of: null,
            last_successful_observation_at: null,
            sources: [
              {
                stream_id: STREAM_ID,
                status: 'unavailable',
                pending_pages: null
              }
            ],
            gaps: [],
            errors: [{ code: 'STORE_CORRUPT', stream_id: STREAM_ID }],
            completeness_evidence_id: null
          },
          event_count: eventCount,
          conflicts: []
        };
      }
    },
    /** @param {{event:any,note:string,digest:string}} intent */
    reserveIntent(intent) {
      const checked = validateWorkEvent(intent?.event);
      if (
        !checked.ok ||
        !ownEvent(config, checked.event) ||
        typeof intent.note !== 'string' ||
        intent.note !== serializeWorkEvent(checked.event) ||
        workContentDigest(checked.event) !== intent.digest
      )
        throw new TypeError('INVALID_INTENT');
      const parsed = parseWorkNote(intent.note);
      if (
        !parsed.ok ||
        workContentDigest(parsed.event) !== intent.digest ||
        JSON.stringify(parsed.event) !== JSON.stringify(checked.event)
      )
        throw new TypeError('INVALID_INTENT');
      return transaction(() => {
        const prior = selectIntent.get(checked.event.event_id);
        if (prior)
          return prior.operation_id === checked.event.operation_id &&
            prior.digest === intent.digest &&
            prior.note === intent.note
            ? {
                status: 'same',
                state: displayState({ state: state(checked.event.event_id) })
              }
            : {
                status: 'conflict',
                state: displayState({ state: state(checked.event.event_id) })
              };
        insertIntent.run(
          checked.event.event_id,
          checked.event.operation_id,
          intent.digest,
          intent.note,
          JSON.stringify(checked.event)
        );
        insertTransition.run(checked.event.event_id, 'PREPARED', null, null, null);
        return { status: 'new', state: 'PREPARED' };
      });
    },
    /** @param {string} eventId */
    startPost(eventId) {
      return transaction(() => {
        const current = state(eventId);
        if (current === 'PREPARED') {
          insertTransition.run(eventId, 'POST_STARTED', null, null, null);
          return { status: 'send_once' };
        }
        if (current) return { status: 'already_started' };
        return { status: 'blocked' };
      });
    },
    /** @param {string} eventId @param {any} outcome */
    recordPostOutcome(eventId, outcome) {
      return transaction(() => {
        if (state(eventId) !== 'POST_STARTED') return { status: 'blocked' };
        if (
          outcome?.status === 'upload_accepted' &&
          typeof outcome.upload_id === 'string' &&
          outcome.upload_id.length > 0 &&
          outcome.upload_id.length <= 256
        ) {
          insertTransition.run(eventId, 'UPLOAD_ACCEPTED', null, outcome.upload_id, null);
          return { status: 'upload_accepted' };
        }
        const code =
          typeof outcome?.code === 'string' && /^[A-Z_]{1,64}$/.test(outcome.code)
            ? outcome.code
            : 'POST_UNKNOWN';
        insertTransition.run(eventId, 'UNKNOWN', code, null, null);
        return { status: 'unknown', code };
      });
    },
    /** @param {string} eventId @param {any} readRecord */
    recordReadback(eventId, readRecord) {
      return transaction(() => {
        const prior = selectIntent.get(eventId);
        const current = state(eventId);
        if (
          !prior ||
          !current ||
          !['POST_STARTED', 'UPLOAD_ACCEPTED', 'UNKNOWN', 'VERIFIED', 'CONFLICT'].includes(
            current
          ) ||
          !validRecord(readRecord, config)
        )
          return { status: 'blocked' };
        if (readRecord.event_id !== eventId) return { status: 'blocked' };
        const retained = selectRecord.get(variantKey(readRecord));
        if (!retained) return { status: 'blocked' };
        let stored;
        try {
          stored = JSON.parse(/** @type {string} */ (retained.record_json));
          if (
            !validRecord(stored, config) ||
            retained.variant_key !== variantKey(stored) ||
            retained.record_id !== stored.record_id ||
            retained.event_id !== stored.event_id ||
            retained.operation_id !== stored.event.operation_id ||
            retained.digest !== stored.event_digest ||
            retained.note !== stored.note ||
            immutableRecord(readRecord) !== immutableRecord(stored)
          )
            return { status: 'blocked' };
        } catch {
          return { status: 'blocked' };
        }
        if (
          readRecord.note !== prior.note ||
          readRecord.event_digest !== prior.digest ||
          readRecord.event.operation_id !== prior.operation_id ||
          JSON.stringify(readRecord.event) !== prior.event_json
        ) {
          insertTransition.run(
            eventId,
            'CONFLICT',
            'READBACK_CONFLICT',
            null,
            readRecord.record_id
          );
          return { status: 'conflict' };
        }
        if (current === 'CONFLICT') return { status: 'conflict' };
        if (current !== 'VERIFIED')
          insertTransition.run(eventId, 'VERIFIED', null, null, readRecord.record_id);
        return { status: 'verified', record_id: readRecord.record_id };
      });
    },
    inspect() {
      if (closed) throw new Error('STORE_CLOSED');
      const intents = db
        .prepare('SELECT event_id,operation_id,digest FROM intents ORDER BY rowid')
        .all()
        .map((row) => {
          const current = currentTransition.get(row.event_id);
          return /** @type {any} */ ({
            ...row,
            state: displayState(current),
            ...(current?.upload_id ? { upload_id: current.upload_id } : {}),
            ...(current?.record_id ? { record_id: current.record_id } : {})
          });
        });
      return {
        scope: scope(config),
        intents,
        record_count: Number(
          db.prepare('SELECT COUNT(*) AS count FROM record_variants').get()?.count
        ),
        observation_count: Number(
          db.prepare('SELECT COUNT(*) AS count FROM observations').get()?.count
        )
      };
    },
    close() {
      if (!closed) {
        closed = true;
        db.close();
      }
    }
  };
}
