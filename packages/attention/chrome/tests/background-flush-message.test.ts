// chrome/tests/background-flush-message.test.ts
//
// Bug A3: the service worker is the ONE context allowed to flush the outbox.
// Page contexts send { type: "flushOutbox" } and the SW's onMessage handler
// runs flushOutbox() in-context (where the module-scope single-flight guard
// actually serializes). This test asserts that handler exists and fires.
import { describe, test, expect, vi, beforeEach } from "vitest";

// Mock the outbox so flushOutbox is an observable spy. Keep the rest of the
// module intact (background imports addToOutbox too).
vi.mock("../src/outbox", async () => {
  const actual = await vi.importActual<typeof import("../src/outbox")>("../src/outbox");
  return { ...actual, flushOutbox: vi.fn(async () => undefined) };
});

import { flushOutbox } from "../src/outbox";
// Importing background registers its chrome.runtime.onMessage listeners.
import "../src/background";

type MessageListener = (
  msg: unknown,
  sender: chrome.runtime.MessageSender,
  sendResponse: (response?: unknown) => void,
) => boolean | undefined;

function registeredMessageListeners(): MessageListener[] {
  return vi.mocked(chrome.runtime.onMessage.addListener).mock.calls.map(
    (c) => c[0] as unknown as MessageListener,
  );
}

describe("background onMessage flushOutbox handler", () => {
  beforeEach(() => {
    vi.mocked(flushOutbox).mockClear();
  });

  test("a { type: 'flushOutbox' } message runs flushOutbox() in the SW context", async () => {
    const listeners = registeredMessageListeners();
    expect(listeners.length).toBeGreaterThan(0);

    const sendResponse = vi.fn();
    // Chrome dispatches to every registered listener; mirror that.
    const returns = listeners.map((l) =>
      l({ type: "flushOutbox" }, {} as chrome.runtime.MessageSender, sendResponse),
    );

    expect(flushOutbox).toHaveBeenCalledTimes(1);
    // The handler does async work, so it must keep the channel open (return true).
    expect(returns.some((r) => r === true)).toBe(true);
  });

  test("an unrelated message does not trigger flushOutbox", () => {
    const listeners = registeredMessageListeners();
    const sendResponse = vi.fn();
    for (const l of listeners) {
      l({ kind: "heartbeat" }, {} as chrome.runtime.MessageSender, sendResponse);
    }
    expect(flushOutbox).not.toHaveBeenCalled();
  });
});
