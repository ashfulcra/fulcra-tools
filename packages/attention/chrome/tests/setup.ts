// chrome/tests/setup.ts
// Minimal chrome.* API stub for Vitest + jsdom. Individual tests
// override pieces via vi.stubGlobal / vi.fn() as needed.

import { vi } from "vitest";

function makeArea() {
  // Each area gets its own independent backing store.
  const store: Record<string, unknown> = {};
  return {
    get: vi.fn(async (keys?: string | string[] | Record<string, unknown> | null) => {
      if (keys == null) return { ...store };
      if (typeof keys === "string") return { [keys]: store[keys] };
      if (Array.isArray(keys)) {
        const out: Record<string, unknown> = {};
        for (const k of keys) out[k] = store[k];
        return out;
      }
      const out: Record<string, unknown> = {};
      for (const k of Object.keys(keys)) out[k] = store[k] ?? (keys as Record<string, unknown>)[k];
      return out;
    }),
    set: vi.fn(async (items: Record<string, unknown>) => {
      Object.assign(store, items);
    }),
    remove: vi.fn(async (keys: string | string[]) => {
      const arr = Array.isArray(keys) ? keys : [keys];
      for (const k of arr) delete store[k];
    }),
    clear: vi.fn(async () => {
      for (const k of Object.keys(store)) delete store[k];
    }),
  };
}

(globalThis as unknown as { chrome: unknown }).chrome = {
  storage: {
    local: makeArea(),
    sync: makeArea(),
    session: makeArea(),
    onChanged: { addListener: vi.fn() },
  },
  action: {
    setIcon: vi.fn(async () => undefined),
    setTitle: vi.fn(async () => undefined),
  },
  contextMenus: {
    create: vi.fn(),
    removeAll: vi.fn((cb?: () => void) => { if (cb) cb(); }),
    onClicked: { addListener: vi.fn() },
  },
  permissions: {
    contains: vi.fn(async () => false),
    request: vi.fn(async () => true),
    remove: vi.fn(async () => true),
  },
  alarms: {
    create: vi.fn(),
    clear: vi.fn(),
    onAlarm: { addListener: vi.fn() },
  },
  webNavigation: {
    onCommitted: { addListener: vi.fn() },
    onHistoryStateUpdated: { addListener: vi.fn() },
  },
  tabs: {
    get: vi.fn(),
    query: vi.fn(async () => []),
    onActivated: { addListener: vi.fn() },
    onRemoved: { addListener: vi.fn() },
  },
  windows: {
    get: vi.fn(),
    onFocusChanged: { addListener: vi.fn() },
    WINDOW_ID_NONE: -1,
  },
  idle: {
    setDetectionInterval: vi.fn(),
    onStateChanged: { addListener: vi.fn() },
  },
  runtime: {
    onStartup: { addListener: vi.fn() },
    onInstalled: { addListener: vi.fn() },
    onSuspend: { addListener: vi.fn() },
    onMessage: { addListener: vi.fn() },
    sendMessage: vi.fn(),
    getURL: vi.fn((path: string) => `chrome-extension://stub/${path}`),
  },
  history: {
    search: vi.fn(async () => []),
  },
  scripting: {
    executeScript: vi.fn(),
    registerContentScripts: vi.fn(async () => undefined),
    unregisterContentScripts: vi.fn(async () => undefined),
    getRegisteredContentScripts: vi.fn(async () => []),
  },
  identity: {
    getProfileUserInfo: vi.fn(),
  },
};
