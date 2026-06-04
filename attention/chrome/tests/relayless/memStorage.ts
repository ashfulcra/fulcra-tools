// chrome/tests/relayless/memStorage.ts
// In-memory StorageArea + typed fetch mock for relayless tests — no chrome.*
// global needed.

import { vi, type Mock } from "vitest";
import type { StorageArea } from "../../src/relayless/storageArea";

/** A vi.fn typed with fetch's signature so `.mock.calls[i]` is the proper
 * [url, init] tuple under tsc strict. */
export function mockFetch(
  handler: (
    input: RequestInfo | URL,
    init?: RequestInit,
  ) => Promise<Response>,
): Mock<typeof fetch> {
  return vi.fn(handler) as unknown as Mock<typeof fetch>;
}

export function memStorage(): StorageArea {
  const store: Record<string, unknown> = {};
  return {
    async get(keys) {
      if (keys == null) return { ...store };
      if (typeof keys === "string") return { [keys]: store[keys] };
      const out: Record<string, unknown> = {};
      for (const k of keys) out[k] = store[k];
      return out;
    },
    async set(items) {
      Object.assign(store, items);
    },
    async remove(keys) {
      const arr = Array.isArray(keys) ? keys : [keys];
      for (const k of arr) delete store[k];
    },
  };
}
