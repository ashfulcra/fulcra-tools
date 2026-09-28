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
import { validateWorkTransportConfig } from './work-transport-config.js';
import { assertTrustedWorkReadResult } from './work-transport-read.js';

const STREAM_ID = '00000000-0000-4000-8000-000000000901';
const SOURCE_ID = `com.fulcradynamics.annotation.${STREAM_ID}`;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const EVENT_LIMIT = 1000;

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
    stream_id: STREAM_ID,
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
          stream_id: STREAM_ID,
          event_id: field === 'record_id' ? record.event_id : key
        });
      else if (previous === undefined) groups.set(key, value);
    }
  }
  return found;
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

/** A private retention cache and one-shot intent journal, never an authority or completeness proof.
 * @param {{dbPath:string,config:unknown}} input */
export function openWorkTransportStore(input) {
  const config = validateWorkTransportConfig(input?.config);
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
        db.prepare('INSERT INTO meta (key,value) VALUES (?,?)').run(
          'config',
          JSON.stringify(config)
        );
        db.exec('PRAGMA user_version = 1');
        db.exec('COMMIT');
      } catch (error) {
        db.exec('ROLLBACK');
        throw error;
      }
    } else {
      if (
        version !== 1 ||
        db.prepare('SELECT value FROM meta WHERE key = ?').get('config')?.value !==
          JSON.stringify(config)
      )
        throw new Error('STORE_SCOPE_MISMATCH');
      for (const name of ['observations', 'record_variants', 'intents', 'intent_transitions'])
        if (!db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name=?").get(name))
          throw new Error('STORE_CORRUPT');
      if (db.prepare('PRAGMA quick_check').get()?.quick_check !== 'ok')
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
        { stream_id: STREAM_ID, status: success ? 'partial' : 'unavailable', pending_pages: null }
      ],
      gaps: ordered.flatMap((item) => item.gaps),
      errors: ordered.flatMap((item) => item.errors),
      completeness_evidence_id: null
    };
  }
  return {
    /** @param {unknown} readResult */
    appendWindow(readResult) {
      /** @type {any} */
      let result;
      try {
        result = /** @type {any} */ (assertTrustedWorkReadResult(readResult, config));
      } catch {
        return { status: 'blocked', added_records: 0, code: 'UNTRUSTED_READ_RESULT' };
      }
      if (Date.parse(result.observation.as_of) < Date.parse(result.window.end))
        return { status: 'blocked', added_records: 0, code: 'INVALID_OBSERVATION_TIME' };
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
            sources: [{ stream_id: STREAM_ID, status: 'unavailable', pending_pages: null }],
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
            ? { status: 'same', state: displayState({ state: state(checked.event.event_id) }) }
            : { status: 'conflict', state: displayState({ state: state(checked.event.event_id) }) };
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
