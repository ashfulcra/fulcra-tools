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
    expect(url).toBe("http://127.0.0.1:8771/attention");
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
});
