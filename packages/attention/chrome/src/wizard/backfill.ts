// chrome/src/wizard/backfill.ts
//
// Take the wizard's filtered-and-grouped history and synthesise
// AttentionEvent batches that get POSTed via the existing outbox.
// Uses a distinct CLIENT string so backfilled events can be told
// apart from real-time ones in Fulcra queries.

import {v1HeldMode} from "../relayless/v1AuthClient";
import {loadSettings} from "../storage";
import {sha256Hex} from "../relayless/sha256";
import {scrubTitle} from "../title-scrub";
import { addToOutbox } from "../outbox";
import { requestFlush } from "../flushRequest";
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
  if(await v1HeldMode())return backfillHeld(groups,opts);
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
  // Queueing is done — the progress bar has hit 100%. Ask the service worker
  // to flush (Bug A3: the flush must run in the SW context, not here in the
  // wizard page, or it races the SW's alarm-driven flush and double-POSTs).
  // Fire-and-forget by design: the ingest POST for a few-thousand-URL backfill
  // takes a while, and awaiting it froze the wizard at 100% with the advance
  // button disabled. The outbox is a write-ahead queue — the background alarm
  // drains whatever this flush doesn't, and the SentSet de-dups repeats — so
  // requesting the flush and returning immediately lets the wizard advance.
  requestFlush();
  return queued;
}

function toIsoSecondZ(ms: number): string {
  return new Date(Math.floor(ms / 1000) * 1000).toISOString().replace(".000", "");
}

async function backfillHeld(groups:DomainGroup[],opts:{onProgress?:(done:number,total:number)=>void}):Promise<number>{
 const settings=await loadSettings();
 if(!settings.enabled||settings.pausedUntil!==null&&Date.now()<settings.pausedUntil)throw new Error('Attention capture consent closed; selection remains available');
 const selected=groups.flatMap(g=>g.urls);let retained=0;const seen=new Set<string>();
 for(const row of selected){
  const current=await loadSettings();if(!current.enabled||current.pausedUntil!==null&&Date.now()<current.pausedUntil)throw new Error('Attention capture consent closed; partial retention only');
  if(await isIgnored(row.url))continue;
  const category=await categorize(row.url);let host:string|null=null;try{host=new URL(row.url).hostname;}catch{/* invalid selection rejected by worker */}
  // row.url already crossed groupByHost's canonical JS scrub boundary.
  const raw={kind:'backfill',url:category?null:row.url,title:category?null:scrubTitle(host,row.title),category,
   startTime:row.lastVisitTime-SYNTHETIC_VISIT_DURATION_MS,endTime:row.lastVisitTime,syntheticDurationMs:SYNTHETIC_VISIT_DURATION_MS};
  const id='backfill:'+await sha256Hex(JSON.stringify([row.url,row.lastVisitTime,BACKFILL_CLIENT]));if(seen.has(id))continue;seen.add(id);
  const response=await chrome.runtime.sendMessage({type:'attention-capture-v1',version:1,command:'backfill',payload:{id,raw,matchingUrl:row.url}});
  if(!response||response.version!==1||response.ok!==true||response.receipt?.id!==id||response.receipt?.durable!==true)throw new Error('Attention capture retention UNKNOWN; partial selection retained locally; retry same selection');
  retained++;opts.onProgress?.(retained,selected.length);
 }
 return retained;
}
