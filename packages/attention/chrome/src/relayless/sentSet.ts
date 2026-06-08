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

  /** Load the whole sent-set once into an in-memory Set for O(1) membership
   * checks. Used by batch callers (the relayless sender) so a flush of N events
   * does ONE storage read instead of N. Insertion order matches the persisted
   * order, so the same Set can be handed back to {@link addMany} as its `known`
   * arg to write the merged set WITHOUT a second read (preserving the
   * cap/trim-oldest semantics). */
  async snapshot(): Promise<Set<string>> {
    return new Set(await this.load());
  }

  /** Record one or more ids as sent. De-dupes against existing entries,
   * preserves insertion order, and drops the oldest ids once over the cap. */
  async add(ids: string[]): Promise<void> {
    return this.addMany(ids);
  }

  /** Record one or more ids as sent. De-dupes against existing entries,
   * preserves insertion order, and drops the oldest ids once over the cap. A
   * no-op (no read, no write) when `ids` is empty.
   *
   * Reads the current set once and writes once. Batch callers that already hold
   * a {@link snapshot} from the same flush can pass it as `known` to skip the
   * read entirely (so the whole flush is one read + one write); `known` MUST be
   * the snapshot of THIS set (its insertion order is the persisted order used
   * for trim-oldest). */
  async addMany(ids: string[], known?: Set<string>): Promise<void> {
    if (ids.length === 0) return;
    const cur = known ? [...known] : await this.load();
    const seen = known ?? new Set(cur);
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
