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
  for (const entry of entries) {
    let ok = false;
    let permanentFail = false;
    try {
      const resp = await fetch(RELAY_URL, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${settings.bearerToken}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(entry.payload),
      });
      if (resp.status === 200) ok = true;
      else if (resp.status >= 400 && resp.status < 500) permanentFail = true;
    } catch {
      // Network error — keep for retry.
    }
    if (ok) continue;
    if (permanentFail) continue;
    remaining.push({ ...entry, attempts: entry.attempts + 1 });
  }
  await saveOutbox(remaining);
}
