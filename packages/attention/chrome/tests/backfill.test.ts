// chrome/tests/backfill.test.ts
import { describe, test, expect, beforeEach, afterEach, vi } from "vitest";

// Bug A3: backfill runs in the wizard (page) context, so it must NOT flush
// the outbox in-context — it asks the SW to flush via requestFlush().
vi.mock("../src/flushRequest", () => ({ requestFlush: vi.fn() }));

import { backfillHistory, BACKFILL_CLIENT } from "../src/wizard/backfill";
import { loadOutbox, saveIgnoreList, saveCategoryMap } from "../src/storage";
import { requestFlush } from "../src/flushRequest";
import type { DomainGroup } from "../src/wizard/history";

const origFetch = globalThis.fetch;

beforeEach(async () => {
  await chrome.storage.local.clear();
  await chrome.storage.sync.clear();
  vi.mocked(requestFlush).mockClear();
  (chrome.identity.getProfileUserInfo as unknown as ReturnType<typeof vi.fn>).mockImplementation(
    (_o: chrome.identity.ProfileDetails, cb: (info: chrome.identity.UserInfo) => void) => cb({ email: "", id: "" }),
  );
});

afterEach(() => {
  globalThis.fetch = origFetch;
});

function fakeGroup(host: string, urls: { url: string; lastVisitTime: number; title?: string }[]): DomainGroup {
  return {
    host,
    count: urls.length,
    urls: urls.map((u) => ({
      url: u.url, title: u.title ?? null,
      lastVisitTime: u.lastVisitTime, visitCount: 1,
    })),
  };
}

describe("backfillHistory", () => {
  test("queues one event per URL with the backfill client tag", async () => {
    const groups = [fakeGroup("example.com", [
      { url: "https://example.com/a", lastVisitTime: 1_700_000_000_000 },
      { url: "https://example.com/b", lastVisitTime: 1_700_000_300_000 },
    ])];
    const count = await backfillHistory(groups);
    expect(count).toBe(2);
    const ob = await loadOutbox();
    expect(ob).toHaveLength(2);
    expect(ob[0].payload.client).toBe(BACKFILL_CLIENT);
    expect(ob[0].payload.url).toBe("https://example.com/a");
    // 60s synthetic duration ends at the visit time.
    expect(ob[0].payload.end_time).toBe("2023-11-14T22:13:20Z");
    expect(ob[0].payload.start_time).toBe("2023-11-14T22:12:20Z");
  });

  test("skips URLs whose host is now on the ignore list", async () => {
    await saveIgnoreList([{ pattern: "skipme.com", addedAt: "2026-05-18T00:00:00Z" }]);
    const groups = [
      fakeGroup("keep.com", [{ url: "https://keep.com/a", lastVisitTime: 1 }]),
      fakeGroup("skipme.com", [{ url: "https://skipme.com/a", lastVisitTime: 2 }]),
    ];
    const count = await backfillHistory(groups);
    expect(count).toBe(1);
    const ob = await loadOutbox();
    expect(ob).toHaveLength(1);
    expect(ob[0].payload.url).toBe("https://keep.com/a");
  });

  test("categorized URLs emit category + null url + null title", async () => {
    await saveCategoryMap([{ pattern: "chatgpt.com", category: "ai-chat" }]);
    const groups = [fakeGroup("chatgpt.com", [
      { url: "https://chatgpt.com/c/abc", lastVisitTime: 1, title: "Chat" },
    ])];
    await backfillHistory(groups);
    const ob = await loadOutbox();
    expect(ob[0].payload.category).toBe("ai-chat");
    expect(ob[0].payload.url).toBeNull();
    expect(ob[0].payload.title).toBeNull();
  });

  test("dedups by (url + lastVisitTime-to-second) so re-runs don't flood Fulcra", async () => {
    // Same URL at the same second → one event. The wire source_id is
    // derived from url+second, so emitting both would produce identical
    // source_ids and (before server-side dedup) duplicate events. Even with
    // server dedup, we shouldn't waste outbox slots / POST round-trips.
    const groups = [fakeGroup("example.com", [
      { url: "https://example.com/a", lastVisitTime: 1_700_000_000_000 },
      { url: "https://example.com/a", lastVisitTime: 1_700_000_000_412 }, // same second, different ms
      { url: "https://example.com/a", lastVisitTime: 1_700_000_001_000 }, // next second → kept
      { url: "https://example.com/b", lastVisitTime: 1_700_000_000_000 }, // different URL → kept
    ])];
    const count = await backfillHistory(groups);
    expect(count).toBe(3);
    const ob = await loadOutbox();
    expect(ob).toHaveLength(3);
  });

  test("returns after queueing — asks the SW to flush, does not flush in-context", async () => {
    // Bug A3: backfill runs in the wizard (page) context. It must NOT flush
    // the outbox itself (a concurrent cross-context flush re-POSTs the same
    // snapshot). It queues the events, then fire-and-forget asks the SW to
    // flush via requestFlush() and returns immediately so the wizard advances.
    globalThis.fetch = vi.fn(() => new Promise<Response>(() => {})) as typeof fetch;
    const groups = [fakeGroup("example.com", [
      { url: "https://example.com/a", lastVisitTime: 1 },
      { url: "https://example.com/b", lastVisitTime: 2 },
    ])];
    const count = await backfillHistory(groups);
    expect(count).toBe(2);
    // Events are queued and the SW was asked to flush them.
    expect(await loadOutbox()).toHaveLength(2);
    expect(requestFlush).toHaveBeenCalledTimes(1);
  });

  test("reports progress via onProgress callback", async () => {
    const groups = [fakeGroup("example.com", [
      { url: "https://example.com/a", lastVisitTime: 1 },
      { url: "https://example.com/b", lastVisitTime: 2 },
      { url: "https://example.com/c", lastVisitTime: 3 },
    ])];
    const calls: { done: number; total: number }[] = [];
    await backfillHistory(groups, {
      onProgress: (done, total) => calls.push({ done, total }),
    });
    expect(calls).toHaveLength(3);
    expect(calls[2]).toEqual({ done: 3, total: 3 });
  });
});
