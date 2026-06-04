// chrome/src/relayless/relaylessSender.ts
//
// Dedup + send for the relayless path. Transforms a batch of AttentionEvents
// into wire records, skips any whose source_id is already in the sent-set,
// POSTs the survivors to /ingest/v1/record/batch with a Bearer token, and on
// a 2xx records their source_ids as sent.
//
// Claim-then-record posture (mirrors the daemon): within a single flush an
// event is sent at most once — we de-dup the batch by source_id before
// POSTing, and only mark ids sent AFTER a successful POST. A 401 triggers a
// single token refresh + retry (getToken({force:true})); any other non-2xx
// leaves the sent-set untouched so the next flush retries.

import { INGEST_BATCH_URL } from "./config";
import { buildWireRecord, encodeBatch, type WireContext, type WireRecord } from "./wire";
import { SentSet } from "./sentSet";
import type { FetchFn } from "./oidc";
import type { AttentionEvent } from "../types";

export interface SendBatchOpts {
  /** Return a valid Bearer access token. Called with {force:true} after a
   * 401 so the token store refreshes rather than returning the cached
   * (rejected) token. Returns null when not signed in. */
  getToken: (opts?: { force?: boolean }) => Promise<string | null>;
  fetch?: FetchFn;
  /** Wire context (bound definition id + resolved tag ids). Supplied by the
   * wiring layer. */
  context: WireContext;
  /** Injectable for tests. Defaults to a SentSet over the default storage. */
  sentSet?: SentSet;
}

export interface SendBatchResult {
  /** source_ids successfully POSTed this flush. */
  sent: string[];
  /** source_ids skipped because they were already in the sent-set. */
  skipped: string[];
  /** True if the POST ultimately succeeded (or there was nothing to send). */
  ok: boolean;
  /** Set when the POST failed; carries the HTTP status (0 = network/no auth). */
  failureStatus?: number;
}

/**
 * Build records for `events`, skip already-sent ones, POST the rest, and on
 * success record their source_ids. Returns a summary. Never throws on an
 * HTTP failure — it reports via the result so the caller (a flush loop) can
 * decide retry timing; transport errors are caught and surfaced as ok=false.
 */
export async function sendBatch(
  events: AttentionEvent[],
  opts: SendBatchOpts,
): Promise<SendBatchResult> {
  const fetchFn = opts.fetch ?? ((...a: Parameters<FetchFn>) => fetch(...a));
  const sentSet = opts.sentSet ?? new SentSet();

  const sent: string[] = [];
  const skipped: string[] = [];
  const toSend: { record: WireRecord; sourceId: string }[] = [];
  // De-dup within this flush so a batch carrying the same source_id twice is
  // claimed once.
  const claimed = new Set<string>();

  for (const ev of events) {
    const { record, sourceId } = await buildWireRecord(ev, opts.context);
    if (claimed.has(sourceId)) continue;
    if (await sentSet.has(sourceId)) {
      skipped.push(sourceId);
      continue;
    }
    claimed.add(sourceId);
    toSend.push({ record, sourceId });
  }

  if (toSend.length === 0) {
    return { sent, skipped, ok: true };
  }

  const body = encodeBatch(toSend.map((t) => t.record));

  // First attempt with a cached token; on 401, refresh once and retry.
  let status = await postBatch(fetchFn, body, opts.getToken, false);
  if (status === 401) {
    status = await postBatch(fetchFn, body, opts.getToken, true);
  }

  if (status >= 200 && status < 300) {
    const ids = toSend.map((t) => t.sourceId);
    await sentSet.add(ids);
    sent.push(...ids);
    return { sent, skipped, ok: true };
  }
  return { sent, skipped, ok: false, failureStatus: status };
}

/** POST the JSONL body. Returns the HTTP status, or 0 for a transport error
 * or a missing token. A forced (post-401) token refresh that THROWS means the
 * refresh grant was rejected — the refresh token is invalid/revoked, so the
 * user must re-authenticate; we report that as 401 (→ unauthorized) rather
 * than 0 (→ unreachable). */
async function postBatch(
  fetchFn: FetchFn,
  body: string,
  getToken: (opts?: { force?: boolean }) => Promise<string | null>,
  force: boolean,
): Promise<number> {
  let token: string | null;
  try {
    token = await getToken(force ? { force: true } : undefined);
  } catch {
    return force ? 401 : 0;
  }
  if (!token) return 0;
  try {
    const resp = await fetchFn(INGEST_BATCH_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/x-jsonl",
      },
      body,
    });
    return resp.status;
  } catch {
    return 0;
  }
}
