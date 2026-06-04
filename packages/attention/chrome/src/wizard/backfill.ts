// chrome/src/wizard/backfill.ts
//
// Take the wizard's filtered-and-grouped history and synthesise
// AttentionEvent batches that get POSTed via the existing outbox.
// Uses a distinct CLIENT string so backfilled events can be told
// apart from real-time ones in Fulcra queries.

import { addToOutbox, flushOutbox } from "../outbox";
import { categorize } from "../categorize";
import { isIgnored } from "../ignore";
import { getChromeIdentity } from "../identity";
import type { AttentionEvent } from "../types";
import type { DomainGroup, ScannedUrl } from "./history";

export const BACKFILL_CLIENT = "fulcra-attention-chrome-backfill/0.1.0";

/** Synthetic duration for a backfilled visit (chrome.history exposes no real duration). */
const SYNTHETIC_VISIT_DURATION_MS = 60_000;  // 60 s — short enough to not over-claim attention

/**
 * Walk groups → urls, drop anything that's now ignored, build wire
 * payloads, batch into the outbox, and let outbox.flushOutbox push
 * them through. Returns the count of events queued.
 *
 * Reports progress via `onProgress(done, total)` callback.
 */
export async function backfillHistory(
  groups: DomainGroup[],
  opts: { onProgress?: (done: number, total: number) => void } = {},
): Promise<number> {
  // Pre-flatten so we can report total + progress accurately.
  const rows: ScannedUrl[] = [];
  for (const g of groups) for (const u of g.urls) rows.push(u);
  // Dedup by (scrubbed URL + lastVisitTime-truncated-to-second) — the
  // exact key that drives the wire source_id. chrome.history returns
  // one row per URL but the rows that came in via the same browser
  // session frequently share `lastVisitTime`, and the wizard can be
  // re-run, so without this dedup we get hundreds of identical-on-the-
  // wire events. Fulcra's ingest doesn't dedupe by source_id (it's a
  // dedup hint for QUERY-time, not write-time), so the client has to.
  const seen = new Set<string>();
  const unique: ScannedUrl[] = [];
  for (const row of rows) {
    const sec = Math.floor(row.lastVisitTime / 1000) * 1000;
    const key = `${row.url}|${sec}`;
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(row);
  }
  const identity = await getChromeIdentity();
  let queued = 0;
  for (const row of unique) {
    // Re-check the current ignore list — the wizard updated it before
    // calling us, so any newly-excluded host should be dropped here.
    if (await isIgnored(row.url)) continue;
    const category = await categorize(row.url);
    const startMs = row.lastVisitTime - SYNTHETIC_VISIT_DURATION_MS;
    const endMs = row.lastVisitTime;
    const payload: AttentionEvent = {
      url: category ? null : row.url,
      title: category ? null : row.title,
      og_description: null,   // chrome.history doesn't expose page meta
      favicon_url: null,
      category,
      chrome_identity: identity,
      og_type: null,
      lang: null,
      start_time: toIsoSecondZ(startMs),
      end_time: toIsoSecondZ(endMs),
      client: BACKFILL_CLIENT,
    };
    await addToOutbox(payload);
    queued += 1;
    if (opts.onProgress) opts.onProgress(queued, unique.length);
  }
  // Queueing is done — the progress bar has hit 100%. Kick off a flush
  // but DO NOT await it: flushOutbox POSTs the queued events to the Fulcra
  // API, which for a few-thousand-URL backfill takes a while. Awaiting it
  // here froze the wizard at 100% with the advance button disabled the
  // whole time. The outbox is a write-ahead queue — the background alarm
  // drains whatever this flush doesn't, and the SentSet de-dups repeats —
  // so a fire-and-forget flush is safe and lets the wizard advance the
  // moment queueing finishes.
  void flushOutbox();
  return queued;
}

function toIsoSecondZ(ms: number): string {
  return new Date(Math.floor(ms / 1000) * 1000).toISOString().replace(".000", "");
}
