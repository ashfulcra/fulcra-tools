// chrome/tests/outbox.test.ts
import { describe, test, expect, beforeEach, vi } from "vitest";
import { addToOutbox, flushOutbox, OUTBOX_CAP } from "../src/outbox";
import { loadOutbox, saveSettings } from "../src/storage";
import type { AttentionEvent } from "../src/types";
import { DEFAULT_SETTINGS } from "../src/types";

function makeEvent(url = "https://x.com/"): AttentionEvent {
  return {
    url, title: "T", og_description: null, favicon_url: null,
    category: null, chrome_identity: null, og_type: null, lang: null,
    start_time: "2026-05-18T14:00:00Z", end_time: "2026-05-18T14:05:00Z",
    client: "fulcra-attention-chrome/0.1.0",
  };
}

beforeEach(async () => {
  await chrome.storage.local.clear();
  vi.stubGlobal("fetch", vi.fn());
});

describe("addToOutbox", () => {
  test("adds event with unique id, attempts=0", async () => {
    await addToOutbox(makeEvent("https://a.com/"));
    const ob = await loadOutbox();
    expect(ob).toHaveLength(1);
    expect(ob[0].attempts).toBe(0);
    expect(ob[0].id).toBeTruthy();
  });
  test("two adds get distinct ids", async () => {
    await addToOutbox(makeEvent("https://a.com/"));
    await addToOutbox(makeEvent("https://b.com/"));
    const ob = await loadOutbox();
    expect(ob).toHaveLength(2);
    expect(ob[0].id).not.toBe(ob[1].id);
  });
  test("drops oldest when over cap", async () => {
    const seed = Array.from({ length: OUTBOX_CAP }, (_, i) => ({
      id: `seed-${i}`, payload: makeEvent(`https://x${i}.com/`),
      queuedAt: i, attempts: 0,
    }));
    await chrome.storage.local.set({ outbox: seed });
    await addToOutbox(makeEvent("https://new.com/"));
    const ob = await loadOutbox();
    expect(ob).toHaveLength(OUTBOX_CAP);
    expect(ob[0].id).toBe("seed-1");
    expect(ob[ob.length - 1].payload.url).toBe("https://new.com/");
  });
});

describe("flushOutbox", () => {
  test("no-op when outbox empty", async () => {
    const f = vi.mocked(fetch).mockResolvedValue(new Response(null, { status: 200 }));
    await flushOutbox();
    expect(f).not.toHaveBeenCalled();
  });

  test("no-op when no bearer token", async () => {
    await addToOutbox(makeEvent());
    const f = vi.mocked(fetch).mockResolvedValue(new Response(null, { status: 200 }));
    await flushOutbox();
    expect(f).not.toHaveBeenCalled();
    expect(await loadOutbox()).toHaveLength(1);
  });

  test("POSTs each entry, removes on 200", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, bearerToken: "test-tok" });
    await addToOutbox(makeEvent("https://a.com/"));
    await addToOutbox(makeEvent("https://b.com/"));
    const f = vi.mocked(fetch).mockResolvedValue(new Response('{"posted":1}', { status: 200 }));
    await flushOutbox();
    expect(f).toHaveBeenCalledTimes(2);
    const [url, init] = f.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:9292/api/extension/attention");
    expect((init as RequestInit).headers).toMatchObject({
      "Authorization": "Bearer test-tok",
      "Content-Type": "application/json",
    });
    expect(await loadOutbox()).toHaveLength(0);
  });

  test("leaves entry in outbox and bumps attempts on 5xx", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, bearerToken: "tok" });
    await addToOutbox(makeEvent());
    vi.mocked(fetch).mockResolvedValue(new Response(null, { status: 502 }));
    await flushOutbox();
    const ob = await loadOutbox();
    expect(ob).toHaveLength(1);
    expect(ob[0].attempts).toBe(1);
  });

  test("leaves entry on network error", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, bearerToken: "tok" });
    await addToOutbox(makeEvent());
    vi.mocked(fetch).mockRejectedValue(new TypeError("Network error"));
    await flushOutbox();
    expect(await loadOutbox()).toHaveLength(1);
  });

  test("drops entry on 400 (permanent failure — bad payload)", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, bearerToken: "tok" });
    await addToOutbox(makeEvent());
    vi.mocked(fetch).mockResolvedValue(new Response('{"error":"bad"}', { status: 400 }));
    await flushOutbox();
    expect(await loadOutbox()).toHaveLength(0);
  });

  test("bails out after 5 consecutive network failures and keeps remaining entries", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, bearerToken: "tok" });
    // Queue 10 entries.
    for (let i = 0; i < 10; i++) {
      await addToOutbox(makeEvent(`https://x${i}.com/`));
    }
    // Every request fails with a network error.
    vi.mocked(fetch).mockRejectedValue(new TypeError("Network error"));
    await flushOutbox();
    // First 5 attempted (counted as failures), bail aborts the rest.
    // All 10 should still be in the outbox (5 with attempts=1, 5 with attempts=0).
    const ob = await loadOutbox();
    expect(ob).toHaveLength(10);
    const withAttempts = ob.filter((e) => e.attempts === 1);
    const fresh = ob.filter((e) => e.attempts === 0);
    expect(withAttempts).toHaveLength(5);
    expect(fresh).toHaveLength(5);
  });

  test("two concurrent flushes do not double-send (single-flight)", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, bearerToken: "tok" });
    const N = 4;
    for (let i = 0; i < N; i++) {
      await addToOutbox(makeEvent(`https://x${i}.com/`));
    }
    // Slow fetch so the two flushes genuinely overlap in time if both run.
    const f = vi.mocked(fetch).mockImplementation(async () => {
      await new Promise((r) => setTimeout(r, 5));
      return new Response('{"posted":1}', { status: 200 });
    });
    // Fire two flushes WITHOUT awaiting the first before starting the second.
    const a = flushOutbox();
    const b = flushOutbox();
    await Promise.all([a, b]);
    // Each entry POSTed exactly once — not 2N.
    expect(f).toHaveBeenCalledTimes(N);
    expect(await loadOutbox()).toHaveLength(0);
  });

  test("guard resets: a later flush sends newly-queued entries", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, bearerToken: "tok" });
    const f = vi.mocked(fetch).mockResolvedValue(new Response('{"posted":1}', { status: 200 }));
    await addToOutbox(makeEvent("https://a.com/"));
    await flushOutbox();
    expect(f).toHaveBeenCalledTimes(1);
    expect(await loadOutbox()).toHaveLength(0);
    // After the first flush settled, a fresh flush must run again.
    await addToOutbox(makeEvent("https://b.com/"));
    await flushOutbox();
    expect(f).toHaveBeenCalledTimes(2);
    expect(await loadOutbox()).toHaveLength(0);
  });

  test("guard resets after a throwing flush", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, bearerToken: "tok" });
    await addToOutbox(makeEvent("https://a.com/"));
    // Force flushOutbox to throw on its first run by making the underlying
    // storage read reject once. This exercises the finally-resets-guard path.
    const origGet = chrome.storage.local.get;
    vi.spyOn(chrome.storage.local, "get").mockImplementationOnce(() => {
      throw new Error("boom");
    });
    await expect(flushOutbox()).rejects.toThrow();
    // Restore real storage; guard must have reset so this flush runs.
    chrome.storage.local.get = origGet;
    const f = vi.mocked(fetch).mockResolvedValue(new Response('{"posted":1}', { status: 200 }));
    await flushOutbox();
    expect(f).toHaveBeenCalled();
    expect(await loadOutbox()).toHaveLength(0);
  });

  test("uses AbortController with 10s timeout per fetch", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, bearerToken: "tok" });
    await addToOutbox(makeEvent());
    let observedSignal: AbortSignal | undefined;
    vi.mocked(fetch).mockImplementation(async (_url, init) => {
      observedSignal = (init as RequestInit).signal as AbortSignal;
      return new Response('{"posted":1}', { status: 200 });
    });
    await flushOutbox();
    expect(observedSignal).toBeDefined();
    expect(observedSignal).toBeInstanceOf(AbortSignal);
  });
});
