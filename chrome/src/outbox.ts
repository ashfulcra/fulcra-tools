// chrome/src/outbox.ts
//
// Write-ahead queue: every captured event lands here before POST. On 200 we
// delete the entry. On network failure / 5xx we leave it for retry. On 4xx
// we drop it (permanent failure — usually a bug or stale state). Cap at
// OUTBOX_CAP entries, dropping the oldest at overflow.

import { loadOutbox, saveOutbox, loadSettings } from "./storage";
import type { AttentionEvent, OutboxEntry } from "./types";

export const OUTBOX_CAP = 5000;
export const RELAY_URL = "http://127.0.0.1:8771/attention";

function genId(): string {
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

export async function addToOutbox(payload: AttentionEvent): Promise<void> {
  const cur = await loadOutbox();
  const entry: OutboxEntry = {
    id: genId(),
    payload,
    queuedAt: Date.now(),
    attempts: 0,
  };
  cur.push(entry);
  while (cur.length > OUTBOX_CAP) cur.shift();
  await saveOutbox(cur);
}

export async function flushOutbox(): Promise<void> {
  const settings = await loadSettings();
  if (!settings.bearerToken) return;
  const entries = await loadOutbox();
  if (entries.length === 0) return;

  const remaining: OutboxEntry[] = [];
  let consecutiveFailures = 0;
  const MAX_CONSECUTIVE_FAILURES = 5;
  const REQUEST_TIMEOUT_MS = 10_000;
  let aborted = false;

  for (const entry of entries) {
    // Bail out early if the relay is clearly unreachable. Keep remaining
    // entries in the outbox — the next alarm tick retries.
    if (aborted) {
      remaining.push(entry);
      continue;
    }

    let ok = false;
    let permanentFail = false;
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), REQUEST_TIMEOUT_MS);
    try {
      const resp = await fetch(RELAY_URL, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${settings.bearerToken}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(entry.payload),
        signal: ac.signal,
      });
      if (resp.status === 200) ok = true;
      else if (resp.status >= 400 && resp.status < 500) permanentFail = true;
    } catch {
      // Network error / abort — keep for retry.
    } finally {
      clearTimeout(timer);
    }

    if (ok) {
      consecutiveFailures = 0;
      continue;
    }
    if (permanentFail) {
      consecutiveFailures = 0;
      continue;  // drop entry
    }
    // Transient failure — keep and bump attempts.
    remaining.push({ ...entry, attempts: entry.attempts + 1 });
    consecutiveFailures += 1;
    if (consecutiveFailures >= MAX_CONSECUTIVE_FAILURES) {
      aborted = true;
    }
  }
  await saveOutbox(remaining);
}
