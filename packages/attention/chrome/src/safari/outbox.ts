// chrome/src/safari/outbox.ts
//
// The Safari flush loop, kept separate from the Chrome outbox on purpose.
//
// The Chrome outbox (src/outbox.ts) carries the whole cloud path: a TokenStore,
// device-flow refresh, definition resolution, and a direct POST to Fulcra. On
// Safari the NATIVE app owns all of that, so importing it here would drag the
// entire relayless core — and its permissions — into a bundle that must not
// have them. This module does one thing: buffer visits and hand them to the
// bridge.
//
// RETENTION IS THE WHOLE CONTRACT. Events are removed ONLY after the native
// side reports a validated success. Any other outcome — unreachable, needs
// sign-in, a malformed receipt — leaves them in storage for the next flush.
// Clearing on anything weaker is how browsing history gets deleted instead of
// uploaded, and both ends of this bridge have already made that mistake once
// each (PR 601 r2 in Swift, PR 610 r1 in TypeScript).

import type { AttentionEvent } from "../types";
import { sendBatchViaNative, type NativeOutcome } from "../relayless/nativeTransport";

const KEY = "safariOutbox";

/** Bounded so a long offline stretch cannot grow storage without limit. The
 * oldest are dropped first: they are the least likely to still matter, and an
 * unbounded buffer eventually fails to write at all, losing everything. */
export const SAFARI_OUTBOX_CAP = 5_000;

type Area = {
  get(key: string): Promise<Record<string, unknown>>;
  set(items: Record<string, unknown>): Promise<void>;
};

function defaultArea(): Area {
  return chrome.storage.local as unknown as Area;
}

/**
 * Every outbox MUTATION goes through this lock (pr-617 r1, codex-coder).
 *
 * `add` and the flush cleanup are both read-modify-write over the same
 * storage key, and every `await` between the read and the write is a yield
 * point: two tab events arriving together each read the same array, each
 * push their own visit, and the second `set` erases the first — the
 * deterministic 1-of-2 loss the review reproduced. Retention is the whole
 * contract of this module, so mutations are serialized through a promise
 * chain: each critical section starts only after the previous one's write
 * has landed. Reads stay lock-free — a stale read is harmless (flush
 * re-reads under the lock before it deletes anything), and blocking reads
 * on a long native send would stall the capture path for nothing.
 *
 * One chain per JS context is enough: background is the only writer of this
 * key, and a rejected section must not wedge the chain (the tail swallows,
 * the next caller still runs — its own failure surfaces to its own caller).
 */
let outboxTail: Promise<void> = Promise.resolve();

function withOutboxLock<T>(op: () => Promise<T>): Promise<T> {
  const run = outboxTail.then(op);
  outboxTail = run.then(
    () => undefined,
    () => undefined,
  );
  return run;
}

export async function loadSafariOutbox(area: Area = defaultArea()): Promise<AttentionEvent[]> {
  const r = await area.get(KEY);
  const v = r[KEY];
  return Array.isArray(v) ? (v as AttentionEvent[]) : [];
}

export async function addSafariEvent(
  event: AttentionEvent,
  area: Area = defaultArea(),
): Promise<void> {
  return withOutboxLock(async () => {
    const cur = await loadSafariOutbox(area);
    cur.push(event);
    if (cur.length > SAFARI_OUTBOX_CAP) cur.splice(0, cur.length - SAFARI_OUTBOX_CAP);
    await area.set({ [KEY]: cur });
  });
}

/** Value identity for an event. These four fields are what the wire layer
 * folds into a source_id, so two events agreeing on all of them are the same
 * visit as far as ingest is concerned. */
function eventKey(e: AttentionEvent): string {
  return [e.url ?? "", e.category ?? "", e.start_time, e.end_time, e.client].join("\u0000");
}

/**
 * Flush the buffer through the native bridge.
 *
 * Removes ONLY the events this flush actually handed over, and only on a
 * validated success — by VALUE, not by count. Splicing "the first N" would
 * drop the wrong events whenever a visit arrived mid-flight, which is precisely
 * when a flush is running.
 */
export async function flushSafariOutbox(
  area: Area = defaultArea(),
  send = sendBatchViaNative,
): Promise<NativeOutcome> {
  const batch = await loadSafariOutbox(area);
  if (batch.length === 0) return { kind: "ok", sent: 0, skipped: 0 };

  const outcome = await send(batch);
  if (outcome.kind !== "ok") {
    // Retained deliberately. needs-sign-in and unreachable are both temporary
    // from the buffer's point of view; the user signs in, or the app comes
    // back, and the next flush delivers.
    return outcome;
  }

  // Re-read rather than reusing `batch`: a visit may have completed while the
  // native call was in flight, and it has not been sent.
  //
  // Matched by VALUE, not object identity. Every storage read deserializes new
  // objects, so `now` and `batch` never share a reference even for the same
  // event — an identity Set would filter nothing, the buffer would never drain,
  // and the same visits would be re-sent forever while every flush reported
  // success. Server-side dedup would hide it; the buffer growing without bound
  // would not.
  const handed = new Set(batch.map(eventKey));
  await withOutboxLock(async () => {
    const now = await loadSafariOutbox(area);
    await area.set({ [KEY]: now.filter((e) => !handed.has(eventKey(e))) });
  });
  return outcome;
}
