// chrome/tests/setup.ts
// Minimal chrome.* API stub for Vitest + jsdom. Individual tests
// override pieces via vi.stubGlobal / vi.fn() as needed.

import { vi } from "vitest";

const memStore: Record<string, unknown> = {};

function makeArea() {
  return {
    get: vi.fn(async (keys?: string | string[] | Record<string, unknown> | null) => {
      if (keys == null) return { ...memStore };
      if (typeof keys === "string") return { [keys]: memStore[keys] };
      if (Array.isArray(keys)) {
        const out: Record<string, unknown> = {};
        for (const k of keys) out[k] = memStore[k];
        return out;
      }
      const out: Record<string, unknown> = {};
      for (const k of Object.keys(keys)) out[k] = memStore[k] ?? (keys as Record<string, unknown>)[k];
      return out;
    }),
    set: vi.fn(async (items: Record<string, unknown>) => {
      Object.assign(memStore, items);
    }),
    remove: vi.fn(async (keys: string | string[]) => {
      const arr = Array.isArray(keys) ? keys : [keys];
      for (const k of arr) delete memStore[k];
    }),
    clear: vi.fn(async () => {
      for (const k of Object.keys(memStore)) delete memStore[k];
    }),
  };
}

(globalThis as unknown as { chrome: unknown }).chrome = {
  storage: {
    local: makeArea(),
    sync: makeArea(),
    session: makeArea(),
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
    onRemoved: { addListener: vi.fn() },
  },
  windows: {
    onFocusChanged: { addListener: vi.fn() },
    WINDOW_ID_NONE: -1,
  },
  runtime: {
    onStartup: { addListener: vi.fn() },
    onSuspend: { addListener: vi.fn() },
    onMessage: { addListener: vi.fn() },
    sendMessage: vi.fn(),
  },
  scripting: {
    executeScript: vi.fn(),
  },
  identity: {
    getProfileUserInfo: vi.fn(),
  },
};
