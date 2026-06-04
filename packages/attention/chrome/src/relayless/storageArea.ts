// chrome/src/relayless/storageArea.ts
//
// A tiny, mockable wrapper over an extension storage area. The relayless
// modules read/write a handful of keys (token set, sent-set) in
// chrome.storage.local. The existing extension code calls chrome.storage.*
// directly; the relayless core instead takes a StorageArea so that:
//   - tests can inject an in-memory area (no chrome.* global needed), and
//   - Safari Web Extensions, which expose the same get/set/remove surface
//     under either `browser.storage.local` or `chrome.storage.local`, work
//     unchanged.
//
// The shape is the WebExtension promise-based API subset we use.

export interface StorageArea {
  get(keys: string | string[] | null): Promise<Record<string, unknown>>;
  set(items: Record<string, unknown>): Promise<void>;
  remove(keys: string | string[]): Promise<void>;
}

/**
 * Resolve the default local storage area. Prefers `browser.storage.local`
 * (Firefox / Safari with the native namespace) and falls back to
 * `chrome.storage.local` (Chrome, and Safari's chrome alias). Throws if
 * neither exists — callers in a non-extension context must inject an area.
 */
export function defaultLocalStorageArea(): StorageArea {
  const g = globalThis as unknown as {
    browser?: { storage?: { local?: StorageArea } };
    chrome?: { storage?: { local?: StorageArea } };
  };
  const area = g.browser?.storage?.local ?? g.chrome?.storage?.local;
  if (!area) {
    throw new Error(
      "no extension storage.local available; inject a StorageArea",
    );
  }
  return area;
}
