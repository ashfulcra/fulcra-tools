// @vitest-environment node
import { afterAll, afterEach, beforeEach, describe, expect, test, vi } from "vitest";

describe.sequential("shared fetch denial survives fixture global restoration", () => {
  let originalFetch: typeof fetch;
  let restored = false;
  const fallback = () => { throw new Error("Synthetic fallback reached after reset"); };

  afterEach(() => {
    vi.unstubAllGlobals();
    if (!restored) {
      originalFetch = globalThis.fetch;
      restored = true;
    }
    // No await/call between restoration and replacement. Even RED can reach
    // only this synthetic fallback, never the saved native function.
    globalThis.fetch = fallback;
  });
  afterAll(() => {
    vi.unstubAllGlobals();
    if (restored) globalThis.fetch = originalFetch; // restore, never invoke
  });

  test("a first synthetic transport can run before teardown resets globals", async () => {
    vi.stubGlobal("fetch", async () => new Response("first synthetic response"));
    expect(await (await fetch("https://synthetic.invalid/")).text()).toBe("first synthetic response");
  });

  test("the next case reinstalls denial after unstubAllGlobals", () => {
    expect(restored).toBe(true);
    expect(globalThis.fetch).not.toBe(originalFetch); // fail before any native call
    expect(() => fetch("https://synthetic.invalid/")).toThrow("External network forbidden in browser unit tests");
  });

  describe("fixture transport installed after shared setup", () => {
    beforeEach(() => {
      vi.stubGlobal("fetch", async () => new Response("fixture-owned response"));
    });
    test("the shared hook does not overwrite a later fixture transport", async () => {
      expect(await (await fetch("https://synthetic.invalid/")).text()).toBe("fixture-owned response");
    });
  });

  test("denial returns after the fixture override is restored too", () => {
    expect(restored).toBe(true);
    expect(globalThis.fetch).not.toBe(originalFetch);
    expect(() => fetch("https://synthetic.invalid/")).toThrow("External network forbidden in browser unit tests");
  });
});
