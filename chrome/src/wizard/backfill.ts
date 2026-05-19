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
  const identity = await getChromeIdentity();
  let queued = 0;
  for (const row of rows) {
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
    if (opts.onProgress) opts.onProgress(queued, rows.length);
  }
  // addToOutbox only writes to storage; the actual POST happens here.
  // For a backfill of a few thousand URLs this is one big flush at
  // the end — the per-request timeout + 5-failure circuit breaker
  // in outbox.ts caps the worst case.
  await flushOutbox();
  return queued;
}

function toIsoSecondZ(ms: number): string {
  return new Date(Math.floor(ms / 1000) * 1000).toISOString().replace(".000", "");
}
