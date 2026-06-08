// chrome/tests/flushRequest.test.ts
//
// Bug A3: flushing must happen ONLY in the service-worker context. Page
// contexts (popup, wizard) must NOT call flushOutbox() directly — they ask
// the SW to flush via a runtime message. requestFlush() is that ask.
import { describe, test, expect, beforeEach, vi } from "vitest";
import { requestFlush } from "../src/flushRequest";

describe("requestFlush", () => {
  beforeEach(() => {
    vi.mocked(chrome.runtime.sendMessage).mockReset();
  });

  test("sends a { type: 'flushOutbox' } runtime message", () => {
    vi.mocked(chrome.runtime.sendMessage).mockResolvedValue(undefined as never);
    requestFlush();
    expect(chrome.runtime.sendMessage).toHaveBeenCalledTimes(1);
    expect(chrome.runtime.sendMessage).toHaveBeenCalledWith({ type: "flushOutbox" });
  });

  test("swallows a rejected send (SW mid-restart / no receiving end)", async () => {
    // The SW may be mid-restart so there is no receiving end. The periodic
    // alarm will flush anyway, so a failed send must not throw or reject.
    vi.mocked(chrome.runtime.sendMessage).mockRejectedValue(
      new Error("Could not establish connection. Receiving end does not exist."),
    );
    // Must not throw synchronously.
    expect(() => requestFlush()).not.toThrow();
    // Let any microtask rejection settle — an unhandled rejection here would
    // fail the suite.
    await Promise.resolve();
    await Promise.resolve();
  });
});
