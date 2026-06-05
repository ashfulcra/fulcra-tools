// chrome/src/relayless/sentSet.ts
//
// A bounded record of attention source_ids the relayless sender has already
// POSTed to /ingest/v1/record/batch. Persisted in the extension's local
// storage area so a service-worker restart doesn't re-send. Fulcra dedupes
// server-side on source_id, so this is a client-side optimization (avoids
// re-POSTing the same events every flush) rather than a correctness
// guarantee — but it also implements the daemon's "claim-then-record"
// posture so a flush never double-sends within itself.
//
// Bounded: insertion order is preserved and the oldest ids are dropped once
// the cap is exceeded, so the set can't grow without limit. The cap is sized
// well above a realistic in-flight backlog; the worst case of dropping an
// old id is one redundant (server-deduped) re-POST.

import {
  type StorageArea,
  defaultLocalStorageArea,
} from "./storageArea";

const SENT_KEY = "relaylessSentIds";
export const SENT_SET_CAP = 10_000;

export interface SentSetOpts {
  storage?: StorageArea;
  cap?: number;
}

export class SentSet {
  private readonly storage: StorageArea;
  private readonly cap: number;

  constructor(opts: SentSetOpts = {}) {
    this.storage = opts.storage ?? defaultLocalStorageArea();
    this.cap = opts.cap ?? SENT_SET_CAP;
  }

  private async load(): Promise<string[]> {
    const r = await this.storage.get(SENT_KEY);
    return (r[SENT_KEY] as string[] | undefined) ?? [];
  }

  private async save(ids: string[]): Promise<void> {
    await this.storage.set({ [SENT_KEY]: ids });
  }

  /** True if `id` has already been recorded as sent. */
  async has(id: string): Promise<boolean> {
    const ids = await this.load();
    return ids.includes(id);
  }

  /** Record one or more ids as sent. De-dupes against existing entries,
   * preserves insertion order, and drops the oldest ids once over the cap. */
  async add(ids: string[]): Promise<void> {
    if (ids.length === 0) return;
    const cur = await this.load();
    const seen = new Set(cur);
    for (const id of ids) {
      if (!seen.has(id)) {
        cur.push(id);
        seen.add(id);
      }
    }
    while (cur.length > this.cap) cur.shift();
    await this.save(cur);
  }

  /** Number of recorded ids (for diagnostics/tests). */
  async size(): Promise<number> {
    return (await this.load()).length;
  }

  /** Drop the entire sent-set. Used on sign-out / account switch so a
   * re-queued source_id from a prior account is not skipped against the new
   * account's definition (Bug A1: source_id omits account/definition). */
  async clear(): Promise<void> {
    await this.storage.remove(SENT_KEY);
  }
}
