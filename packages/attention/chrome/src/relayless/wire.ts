// chrome/src/relayless/wire.ts
//
// The event -> ingest-record transform. Ports the daemon's Python transform
// so the relayless extension emits the SAME Fulcra record the daemon would,
// byte-for-byte. Three Python sites are replicated here:
//
//   fulcra_attention/ingest.py
//     - SOURCE_PREFIX + source_id(key, start_time): the sha256-derived,
//       second-truncated idempotency id.
//     - build_attention_event(): scrub url, derive host/note/sid_key, the
//       five top-level attention data keys, external_ids.
//   fulcra_common/ingest.py  (IngestPipeline.build_record)
//     - the data_inner assembly incl. the #30 duration_seconds field.
//   fulcra_common/wire.py    (build_record)
//     - recorded_at union, the source array (source_id + extra +
//       com.fulcradynamics.annotation.<definition_id>), metadata envelope,
//       and json.dumps(data, sort_keys=True) for the inner `data` string.
//
// CRITICAL: source_id and the inner `data` string MUST match the Python
// output exactly. See pyjson.ts for the json.dumps parity encoder and
// relayless/wire.test.ts for the golden vectors (computed by running the
// Python).

import { scrubUrl } from "../scrub";
import { pyJsonStringify } from "./pyjson";
import { sha256Hex } from "./sha256";
import type { AttentionEvent } from "../types";

// Bumped 2026-06-04 v2 -> v3: the source_id now also folds in the per-browser
// identity slug, so the same url+second from two different browsers produces
// DISTINCT source_ids (the multi-browser distinctness guarantee). The daemon
// is gone, so this is no longer constrained by Python parity.
export const SOURCE_PREFIX = "com.fulcra.attention.v3.";
export const DATA_TYPE = "DurationAnnotation";

/** Parse an ISO-8601 timestamp accepting both trailing 'Z' and explicit
 * offset, returning epoch milliseconds. */
function parseIsoMs(s: string): number {
  // Date.parse handles both 'Z' and '+00:00'. The attention payload always
  // carries an explicit zone, so this is unambiguous.
  const ms = Date.parse(s);
  if (Number.isNaN(ms)) throw new Error(`unparseable timestamp: ${s}`);
  return ms;
}

/** Truncate to whole seconds and render as ISO-8601 with a trailing 'Z'.
 * Mirrors Python's `dt.replace(microsecond=0).isoformat()` +
 * `.replace("+00:00","Z")`. */
function toSecondIsoZ(ms: number): string {
  const truncated = Math.floor(ms / 1000) * 1000;
  // toISOString always renders UTC with a trailing 'Z' and milliseconds; we
  // truncated to the second so the .000 is stripped to match Python.
  return new Date(truncated).toISOString().replace(".000Z", "Z");
}

/**
 * Deterministic source-id from `key` (scrubbed URL or category), the
 * second-truncated start time, and the per-browser identity slug:
 *   sec = start_time.replace(microsecond=0).isoformat()   # +00:00 form
 *   sha256(f"{key}|{sec}|{identitySlug}").hexdigest()[:16]
 * The +00:00 second-form is kept exactly as before; only the identity slug is
 * appended (empty string when no identity), so two browsers with different
 * identity slugs produce DISTINCT source_ids for the same url+second — the
 * multi-browser distinctness guarantee.
 */
export async function sourceId(
  key: string,
  startTimeIso: string,
  identitySlug: string,
): Promise<string> {
  const ms = parseIsoMs(startTimeIso);
  const truncated = Math.floor(ms / 1000) * 1000;
  // Tz-aware UTC second-form → "YYYY-MM-DDTHH:MM:SS+00:00".
  const sec = new Date(truncated).toISOString().replace(".000Z", "+00:00");
  const hash = await sha256Hex(`${key}|${sec}|${identitySlug}`);
  return `${SOURCE_PREFIX}${hash.slice(0, 16)}`;
}

/** Lowercased hostname of a URL, matching Python urlsplit(url).hostname.
 * Returns null for a URL without a host. */
function hostnameOf(url: string): string | null {
  try {
    const h = new URL(url).hostname;
    return h === "" ? null : h.toLowerCase();
  } catch {
    return null;
  }
}

/** Inputs the caller resolves (definition + tags) for an attention record.
 * The wiring layer supplies these from extension state; the pure transform
 * here does not know how definitions/tags are minted. */
export interface WireContext {
  /** The bound Attention annotation-definition id. */
  definitionId: string;
  /** Resolved tag ids, in order:
   * [attention, web, (machine:<slug>?)] — the caller is responsible for that
   * ordering and membership. */
  tagIds: string[];
  /** The per-browser identity slug (slugifyIdentity of the label), folded into
   * the source_id so records from distinct browsers are distinct. Empty string
   * when no identity label is set. */
  identitySlug: string;
  /** The raw per-browser identity label, surfaced in external_ids.device_label
   * for display. Null/absent when no label is set. */
  identityLabel?: string | null;
}

/** The result of transforming one event. */
export interface WireResult {
  /** The record dict to place in the /ingest/v1/record/batch body. */
  record: WireRecord;
  /** The event's attention source_id — used for dedup by the sender. */
  sourceId: string;
}

export interface WireRecord {
  specversion: 1;
  /** json.dumps(data, sort_keys=True) — byte-identical to the daemon. */
  data: string;
  metadata: {
    data_type: string;
    recorded_at: { start_time: string; end_time: string };
    tags: string[];
    source: string[];
    content_type: "application/json";
  };
}

/**
 * Transform a single AttentionEvent into its wire record + source_id.
 *
 * Replicates build_attention_event + IngestPipeline.build_record +
 * wire.build_record. Assumes the payload is already validated (exactly one
 * of url/category non-null), matching the daemon's posture where the route
 * validates before transforming.
 */
export async function buildWireRecord(
  event: AttentionEvent,
  ctx: WireContext,
): Promise<WireResult> {
  const rawUrl = event.url;
  const url = rawUrl != null ? scrubUrl(rawUrl) : null;
  const category = event.category;
  const title = event.title;

  let host: string | null;
  let note: string;
  let sidKey: string;
  if (url != null) {
    host = hostnameOf(url);
    note = title ? `${title} — ${url}` : url;
    sidKey = url;
  } else {
    host = null;
    note = `Attention: ${category}`;
    sidKey = category ?? "";
  }

  const startMs = parseIsoMs(event.start_time);
  const endMs = parseIsoMs(event.end_time);
  const startSecIso = toSecondIsoZ(startMs);
  const endSecIso = toSecondIsoZ(endMs);
  // duration_seconds is computed on the second-truncated bounds, clamped at
  // zero (the #30 defensive field).
  const durationSeconds = Math.max(
    0,
    Math.floor((Math.floor(endMs / 1000) - Math.floor(startMs / 1000))),
  );

  const sid = await sourceId(sidKey, event.start_time, ctx.identitySlug);

  // external_ids — always emitted (client is always set). Key order is
  // irrelevant: pyJsonStringify sorts. `device` is the per-browser identity
  // slug (null when none) and `device_label` the raw label, so consumers can
  // tell which browser a record came from.
  const externalIds: Record<string, unknown> = {
    client: event.client,
    host,
    chrome_identity: event.chrome_identity,
    og_type: event.og_type,
    lang: event.lang,
    device: ctx.identitySlug || null,
    device_label: ctx.identityLabel ?? null,
  };

  // data_inner — attention's _emit_attention_fields shape. note + title are
  // emitted unconditionally; the five attention keys are emitted with null
  // when not applicable; duration_seconds is the #30 field; service="web".
  const dataInner: Record<string, unknown> = {
    note,
    title,
    service: "web",
    category,
    url,
    og_description: event.og_description,
    favicon_url: event.favicon_url,
    parent_source_id: null,
    duration_seconds: durationSeconds,
    external_ids: externalIds,
  };

  const source: string[] = [
    sid,
    `com.fulcradynamics.annotation.${ctx.definitionId}`,
  ];

  const record: WireRecord = {
    specversion: 1,
    data: pyJsonStringify(dataInner),
    metadata: {
      data_type: DATA_TYPE,
      recorded_at: { start_time: startSecIso, end_time: endSecIso },
      tags: [...ctx.tagIds],
      source,
      content_type: "application/json",
    },
  };

  return { record, sourceId: sid };
}

/** Encode records as the JSONL body for POST /ingest/v1/record/batch — one
 * sorted-key JSON object per line, newline-joined. Mirrors
 * wire.encode_batch. */
export function encodeBatch(records: WireRecord[]): string {
  return records.map((r) => pyJsonStringify(r)).join("\n");
}
