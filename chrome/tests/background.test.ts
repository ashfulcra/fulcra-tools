// chrome/tests/background.test.ts
import { describe, test, expect, beforeEach, vi } from "vitest";
import {
  handleNavigation, handleTabClose, handleWindowFocusChange,
  buildPayload,
} from "../src/background";
import { saveSettings, loadOutbox, loadActiveVisits, saveIgnoreList, saveCategoryMap } from "../src/storage";
import { DEFAULT_SETTINGS } from "../src/types";

beforeEach(async () => {
  await chrome.storage.local.clear();
  await chrome.storage.sync.clear();
  await chrome.storage.session.clear();
  vi.mocked(chrome.identity.getProfileUserInfo).mockImplementation((_o, cb) => cb({ email: "", id: "" }));
  vi.mocked(chrome.tabs.get).mockResolvedValue({
    id: 1, url: "https://example.com/p", title: "Example", incognito: false,
  } as chrome.tabs.Tab);
  vi.mocked(chrome.scripting.executeScript).mockResolvedValue([
    { result: { title: "Example", og_description: null, og_type: null, favicon_url: null, lang: null }, frameId: 0 },
  ] as chrome.scripting.InjectionResult[]);
});

describe("handleNavigation", () => {
  test("opens an active visit on first nav", async () => {
    await handleNavigation({
      tabId: 1, url: "https://example.com/p", frameId: 0, timeStamp: 1_700_000_000_000,
    });
    const visits = await loadActiveVisits();
    expect(visits[1]).toBeDefined();
    expect(visits[1].scrubbedUrl).toBe("https://example.com/p");
  });

  test("ignores iframe navigations (frameId != 0)", async () => {
    await handleNavigation({
      tabId: 1, url: "https://example.com/p", frameId: 99, timeStamp: 1_700_000_000_000,
    });
    expect(await loadActiveVisits()).toEqual({});
  });

  test("scrubs URL before opening visit", async () => {
    await handleNavigation({
      tabId: 1, url: "https://example.com/p?access_token=secret&id=1", frameId: 0, timeStamp: 1_700_000_000_000,
    });
    const visits = await loadActiveVisits();
    expect(visits[1].scrubbedUrl).toBe("https://example.com/p?id=1");
  });

  test("drops nav entirely when host is on ignore list", async () => {
    await saveIgnoreList([{ pattern: "chase.com", addedAt: "2026-05-18T14:00:00Z" }]);
    await handleNavigation({
      tabId: 1, url: "https://chase.com/login", frameId: 0, timeStamp: 1_700_000_000_000,
    });
    expect(await loadActiveVisits()).toEqual({});
  });

  test("closes prior visit and opens new one on subsequent nav", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, bearerToken: "tok" });
    await handleNavigation({
      tabId: 1, url: "https://a.com/", frameId: 0, timeStamp: 1_700_000_000_000,
    });
    await handleNavigation({
      tabId: 1, url: "https://b.com/", frameId: 0, timeStamp: 1_700_000_300_000,
    });
    const ob = await loadOutbox();
    expect(ob).toHaveLength(1);
    expect(ob[0].payload.url).toBe("https://a.com/");
    const visits = await loadActiveVisits();
    expect(visits[1].scrubbedUrl).toBe("https://b.com/");
  });

  test("skips non-http(s) schemes", async () => {
    await handleNavigation({
      tabId: 1, url: "chrome://settings/", frameId: 0, timeStamp: 1_700_000_000_000,
    });
    expect(await loadActiveVisits()).toEqual({});
  });

  test("when categorized, scrubbedUrl is null and category is the slug", async () => {
    await saveCategoryMap([{ pattern: "chatgpt.com", category: "ai-chat" }]);
    await saveSettings({ ...DEFAULT_SETTINGS, bearerToken: "tok" });
    await handleNavigation({
      tabId: 1, url: "https://chatgpt.com/c/abc", frameId: 0, timeStamp: 1_700_000_000_000,
    });
    await handleNavigation({
      tabId: 1, url: "https://other.com/", frameId: 0, timeStamp: 1_700_000_300_000,
    });
    const ob = await loadOutbox();
    expect(ob[0].payload.category).toBe("ai-chat");
    expect(ob[0].payload.url).toBeNull();
    expect(ob[0].payload.title).toBeNull();
  });
});

describe("handleTabClose", () => {
  test("closes the visit and queues an event", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, bearerToken: "tok" });
    await handleNavigation({
      tabId: 1, url: "https://x.com/", frameId: 0, timeStamp: 1_700_000_000_000,
    });
    await handleTabClose(1);
    const ob = await loadOutbox();
    expect(ob).toHaveLength(1);
    expect(ob[0].payload.url).toBe("https://x.com/");
    expect(await loadActiveVisits()).toEqual({});
  });
});

describe("handleWindowFocusChange", () => {
  test("WINDOW_ID_NONE closes ALL open visits", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, bearerToken: "tok" });
    await handleNavigation({
      tabId: 1, url: "https://a.com/", frameId: 0, timeStamp: 1_700_000_000_000,
    });
    await handleNavigation({
      tabId: 2, url: "https://b.com/", frameId: 0, timeStamp: 1_700_000_100_000,
    });
    await handleWindowFocusChange(chrome.windows.WINDOW_ID_NONE);
    const ob = await loadOutbox();
    expect(ob).toHaveLength(2);
    expect(await loadActiveVisits()).toEqual({});
  });
  test("non-NONE focus is a no-op", async () => {
    await handleNavigation({
      tabId: 1, url: "https://a.com/", frameId: 0, timeStamp: 1_700_000_000_000,
    });
    await handleWindowFocusChange(42);
    expect(await loadOutbox()).toEqual([]);
    expect(Object.keys(await loadActiveVisits())).toHaveLength(1);
  });
});

describe("buildPayload", () => {
  test("includes chrome_identity from popup label override", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, identityLabel: "Acme Corp" });
    const p = await buildPayload({
      visit: { tabId: 1, scrubbedUrl: "https://x.com/", startTime: 1_700_000_000_000 },
      category: null,
      endTime: 1_700_000_300_000,
      meta: { title: "T", og_description: "d", og_type: "article", favicon_url: "https://x.com/f.ico", lang: "en" },
    });
    expect(p.chrome_identity).toBe("Acme Corp");
    expect(p.og_type).toBe("article");
    expect(p.lang).toBe("en");
    expect(p.start_time).toBe("2023-11-14T22:13:20Z");
    expect(p.end_time).toBe("2023-11-14T22:18:20Z");
  });
});
