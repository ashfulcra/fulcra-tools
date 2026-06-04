// chrome/tests/outbox.test.ts
//
// Transport-agnostic outbox behavior: addToOutbox queueing/cap, and the
// flushOutbox single-flight guard. The relayless flush wiring itself
// (POST shape, 401/refresh, ingest-error mapping) is covered end-to-end in
// outbox-relayless.test.ts. Here we exercise the guard + queueing with the
// relayless path seeded so flushes actually run and drain.

import { describe, test, expect, beforeEach, vi } from "vitest";
import { addToOutbox, flushOutbox, OUTBOX_CAP } from "../src/outbox";
import { loadOutbox } from "../src/storage";
import type { AttentionEvent } from "../src/types";
import { INGEST_BATCH_URL } from "../src/relayless/config";
import { mockFetch } from "./relayless/memStorage";

function makeEvent(url = "https://x.com/"): AttentionEvent {
  return {
    url, title: "T", og_description: null, favicon_url: null,
    category: null, chrome_identity: null, og_type: null, lang: null,
    start_time: "2026-05-18T14:00:00Z", end_time: "2026-05-18T14:05:00Z",
    client: "fulcra-attention-chrome/0.1.0",
  };
}

/** Seed a non-expired token + the resolved-attention cache so the relayless
 * flush runs without hitting the network for auth/definition. */
async function seedRelayless() {
  await chrome.storage.local.set({
    relaylessTokens: {
      accessToken: "ACCESS",
      refreshToken: "REFRESH",
      expiresAt: Date.now() + 3_600_000,
    },
    relaylessResolvedAttention: {
      definitionId: "def-1",
      tagIds: ["tag-attn", "tag-web"],
    },
  });
}

/** Count ingest POSTs across a mockFetch's recorded calls. */
function ingestCallCount(f: ReturnType<typeof mockFetch>): number {
  return f.mock.calls.filter((c) => String(c[0]) === INGEST_BATCH_URL).length;
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

describe("flushOutbox — single-flight guard", () => {
  test("no-op when outbox empty (no ingest POST)", async () => {
    await seedRelayless();
    const f = mockFetch(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", f);
    await flushOutbox();
    expect(ingestCallCount(f)).toBe(0);
  });

  test("two concurrent flushes do not double-send (single-flight)", async () => {
    await seedRelayless();
    const N = 4;
    for (let i = 0; i < N; i++) {
      await addToOutbox(makeEvent(`https://x${i}.com/`));
    }
    // Slow fetch so the two flushes genuinely overlap in time if both run.
    const f = mockFetch(async () => {
      await new Promise((r) => setTimeout(r, 5));
      return new Response("{}", { status: 200 });
    });
    vi.stubGlobal("fetch", f);
    // Fire two flushes WITHOUT awaiting the first before starting the second.
    const a = flushOutbox();
    const b = flushOutbox();
    await Promise.all([a, b]);
    // The batch is sent exactly once — not twice.
    expect(ingestCallCount(f)).toBe(1);
    expect(await loadOutbox()).toHaveLength(0);
  });

  test("guard resets: a later flush sends newly-queued entries", async () => {
    await seedRelayless();
    const f = mockFetch(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", f);
    await addToOutbox(makeEvent("https://a.com/"));
    await flushOutbox();
    expect(ingestCallCount(f)).toBe(1);
    expect(await loadOutbox()).toHaveLength(0);
    // After the first flush settled, a fresh flush must run again.
    await addToOutbox(makeEvent("https://b.com/"));
    await flushOutbox();
    expect(ingestCallCount(f)).toBe(2);
    expect(await loadOutbox()).toHaveLength(0);
  });

  test("guard resets after a throwing flush", async () => {
    await seedRelayless();
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
    const f = mockFetch(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", f);
    await flushOutbox();
    expect(ingestCallCount(f)).toBe(1);
    expect(await loadOutbox()).toHaveLength(0);
  });
});
