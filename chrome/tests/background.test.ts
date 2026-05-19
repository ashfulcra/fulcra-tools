// chrome/tests/background.test.ts
//
// Foreground-only attention model. The contract under test:
//
//   * Background tabs (never focused) → never produce events.
//   * Foreground tab gets a visit when activated; visit accumulates only
//     while focused + not idle.
//   * Blur within BLUR_GRACE_MS → resumes same visit. Past grace → fresh
//     visit on next focus.
//   * Tab close, navigation, idle freeze, window blur all behave as
//     specified in background.ts.
//
import { describe, test, expect, beforeEach, vi } from "vitest";
import {
  handleNavigation, handleTabActivated, handleTabClose,
  handleWindowFocusChange, handleIdleStateChanged,
  setForegroundTab, sweepStaleBlurred,
  buildPayload, withSwLock,
} from "../src/background";
import {
  saveSettings, loadSettings, loadOutbox, loadVisits, saveVisits,
  saveIgnoreList, saveCategoryMap,
} from "../src/storage";
import { DEFAULT_SETTINGS, BLUR_GRACE_MS } from "../src/types";

// ---------- shared fixtures ----------

const T0 = 1_700_000_000_000;  // arbitrary fixed epoch ms ("now")
const SEC = 1_000;
const MIN = 60 * SEC;

/** Make chrome.tabs.get return a tab with the given properties. */
function stubTab(tabId: number, url: string, opts: { active?: boolean; windowId?: number; title?: string } = {}) {
  vi.mocked(chrome.tabs.get).mockImplementation(async (id: number) => {
    if (id !== tabId) throw new Error(`stubTab: unexpected id ${id}`);
    return {
      id: tabId, url, title: opts.title ?? "T",
      active: opts.active ?? true,
      windowId: opts.windowId ?? 1,
      incognito: false,
    } as chrome.tabs.Tab;
  });
}

beforeEach(async () => {
  await chrome.storage.local.clear();
  await chrome.storage.sync.clear();
  await chrome.storage.session.clear();
  (chrome.identity.getProfileUserInfo as unknown as ReturnType<typeof vi.fn>).mockImplementation(
    (_o: chrome.identity.ProfileDetails, cb: (info: chrome.identity.UserInfo) => void) => cb({ email: "", id: "" }),
  );
  vi.mocked(chrome.scripting.executeScript).mockResolvedValue(
    [{ result: { title: "T", og_description: null, og_type: null, favicon_url: null, lang: null }, frameId: 0 }] as unknown as never,
  );
  await saveSettings({ ...DEFAULT_SETTINGS, bearerToken: "tok" });
});

// ---------- setForegroundTab / handleTabActivated ----------

describe("foreground-only model", () => {
  test("activating a tab on an HTTP URL opens a focused visit", async () => {
    stubTab(1, "https://example.com/p");
    await handleTabActivated(1, T0);
    const visits = await loadVisits();
    expect(visits[1]).toMatchObject({
      tabId: 1, scrubbedUrl: "https://example.com/p",
      state: "focused", startTime: T0, focusEpoch: T0,
      accumulatedFocusMs: 0,
    });
  });

  test("background tab navigations DO NOT create visits", async () => {
    // The tab is NOT active.
    stubTab(2, "https://background.example.com/p", { active: false });
    await handleNavigation({
      tabId: 2, url: "https://background.example.com/p",
      frameId: 0, timeStamp: T0,
    });
    expect(await loadVisits()).toEqual({});
    expect(await loadOutbox()).toEqual([]);
  });

  test("foreground tab nav emits the prior visit and starts a fresh one", async () => {
    stubTab(1, "https://a.com/");
    await handleTabActivated(1, T0);
    // 30 seconds of focus elapses.
    stubTab(1, "https://b.com/");
    await handleNavigation({
      tabId: 1, url: "https://b.com/", frameId: 0, timeStamp: T0 + 30 * SEC,
    });
    const ob = await loadOutbox();
    expect(ob).toHaveLength(1);
    expect(ob[0].payload.url).toBe("https://a.com/");
    // 30s of focus = end_time is 30s after start_time
    expect(ob[0].payload.start_time).toBe("2023-11-14T22:13:20Z");
    expect(ob[0].payload.end_time).toBe("2023-11-14T22:13:50Z");
    // A new focused visit on b.com now exists.
    const visits = await loadVisits();
    expect(visits[1].scrubbedUrl).toBe("https://b.com/");
    expect(visits[1].state).toBe("focused");
  });

  test("switching from tab A to tab B freezes A and opens B", async () => {
    stubTab(1, "https://a.com/");
    await handleTabActivated(1, T0);
    stubTab(2, "https://b.com/");
    await handleTabActivated(2, T0 + 10 * SEC);
    const visits = await loadVisits();
    expect(visits[1].state).toBe("blurred");
    expect(visits[1].accumulatedFocusMs).toBe(10 * SEC);
    expect(visits[1].blurredAt).toBe(T0 + 10 * SEC);
    expect(visits[2].state).toBe("focused");
    // No emit yet — tab A is within grace.
    expect(await loadOutbox()).toEqual([]);
  });

  test("returning to tab A within grace resumes the SAME visit", async () => {
    stubTab(1, "https://a.com/");
    await handleTabActivated(1, T0);
    stubTab(2, "https://b.com/");
    await handleTabActivated(2, T0 + 10 * SEC);
    // Within grace (default 30s), come back.
    stubTab(1, "https://a.com/");
    await handleTabActivated(1, T0 + 25 * SEC);
    const visits = await loadVisits();
    expect(visits[1].state).toBe("focused");
    expect(visits[1].startTime).toBe(T0);  // unchanged — same visit
    expect(visits[1].accumulatedFocusMs).toBe(10 * SEC);  // first 10s baked in
    // Tab B was the foreground when we left it → it should be blurred now.
    expect(visits[2].state).toBe("blurred");
    expect(await loadOutbox()).toEqual([]);
  });

  test("returning to tab A AFTER grace expires emits old + starts fresh", async () => {
    stubTab(1, "https://a.com/");
    await handleTabActivated(1, T0);
    stubTab(2, "https://b.com/");
    await handleTabActivated(2, T0 + 10 * SEC);
    // Past grace — sweep should emit tab A's visit.
    await sweepStaleBlurred(T0 + 10 * SEC + BLUR_GRACE_MS + 5 * SEC);
    const visitsAfterSweep = await loadVisits();
    expect(visitsAfterSweep[1]).toBeUndefined();
    const ob1 = await loadOutbox();
    expect(ob1).toHaveLength(1);
    expect(ob1[0].payload.url).toBe("https://a.com/");
    // Now come back to tab 1.
    stubTab(1, "https://a.com/");
    await handleTabActivated(1, T0 + MIN);
    const visits = await loadVisits();
    expect(visits[1].startTime).toBe(T0 + MIN);
    expect(visits[1].accumulatedFocusMs).toBe(0);
  });

  test("window blur (WINDOW_ID_NONE) freezes focused visit; does NOT emit immediately", async () => {
    stubTab(1, "https://a.com/");
    await handleTabActivated(1, T0);
    await handleWindowFocusChange(chrome.windows.WINDOW_ID_NONE, T0 + 5 * SEC);
    const visits = await loadVisits();
    expect(visits[1].state).toBe("blurred");
    expect(visits[1].accumulatedFocusMs).toBe(5 * SEC);
    // Not emitted yet — sweep handles that after grace.
    expect(await loadOutbox()).toEqual([]);
  });

  test("sweep emits blurred visits past the grace window", async () => {
    stubTab(1, "https://a.com/");
    await handleTabActivated(1, T0);
    await handleWindowFocusChange(chrome.windows.WINDOW_ID_NONE, T0 + 5 * SEC);
    await sweepStaleBlurred(T0 + 5 * SEC + BLUR_GRACE_MS + 1 * SEC);
    const ob = await loadOutbox();
    expect(ob).toHaveLength(1);
    expect(ob[0].payload.url).toBe("https://a.com/");
    // Duration = 5s of focus
    expect(ob[0].payload.start_time).toBe("2023-11-14T22:13:20Z");
    expect(ob[0].payload.end_time).toBe("2023-11-14T22:13:25Z");
    expect(await loadVisits()).toEqual({});
  });

  test("tab close emits the visit (focused or blurred)", async () => {
    stubTab(1, "https://x.com/");
    await handleTabActivated(1, T0);
    await handleTabClose(1, T0 + 15 * SEC);
    const ob = await loadOutbox();
    expect(ob).toHaveLength(1);
    expect(ob[0].payload.url).toBe("https://x.com/");
    expect(ob[0].payload.end_time).toBe("2023-11-14T22:13:35Z");  // T0 + 15s
  });

  test("ignored URL: visit not opened; ignored counter bumps", async () => {
    await saveIgnoreList([{ pattern: "chase.com", addedAt: "2026-05-18T14:00:00Z" }]);
    stubTab(1, "https://chase.com/login");
    await handleNavigation({
      tabId: 1, url: "https://chase.com/login", frameId: 0, timeStamp: T0,
    });
    expect(await loadVisits()).toEqual({});
    const c = await chrome.storage.local.get("counts");
    expect((c.counts as { ignored: number }).ignored).toBe(1);
  });

  test("categorized URL stores category and emits with url=null", async () => {
    await saveCategoryMap([{ pattern: "chatgpt.com", category: "ai-chat" }]);
    stubTab(1, "https://chatgpt.com/c/abc");
    await handleTabActivated(1, T0);
    await handleTabClose(1, T0 + 10 * SEC);
    const ob = await loadOutbox();
    expect(ob[0].payload.category).toBe("ai-chat");
    expect(ob[0].payload.url).toBeNull();
    expect(ob[0].payload.title).toBeNull();
  });

  test("URL is scrubbed before being stored as the visit's URL", async () => {
    stubTab(1, "https://example.com/p?access_token=secret&id=1");
    await handleTabActivated(1, T0);
    const visits = await loadVisits();
    expect(visits[1].scrubbedUrl).toBe("https://example.com/p?id=1");
  });

  test("iframe (frameId != 0) navigations are ignored", async () => {
    // Even if it would otherwise touch a focused tab.
    stubTab(1, "https://example.com/p");
    await handleTabActivated(1, T0);
    await handleNavigation({
      tabId: 1, url: "https://example.com/p", frameId: 99, timeStamp: T0 + SEC,
    });
    // Still just the original focused visit.
    const visits = await loadVisits();
    expect(visits[1].scrubbedUrl).toBe("https://example.com/p");
    expect(visits[1].state).toBe("focused");
  });

  test("non-http schemes are ignored", async () => {
    stubTab(1, "chrome://settings/");
    await handleTabActivated(1, T0);
    expect(await loadVisits()).toEqual({});
  });

  test("settings.enabled=false short-circuits everything", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, enabled: false });
    stubTab(1, "https://example.com/p");
    await handleTabActivated(1, T0);
    expect(await loadVisits()).toEqual({});
  });

  test("zero-duration visits are dropped (less than 1s of focus)", async () => {
    stubTab(1, "https://flash.example.com/");
    await handleTabActivated(1, T0);
    // Close immediately.
    await handleTabClose(1, T0 + 500);  // 0.5s
    expect(await loadOutbox()).toEqual([]);
  });

  test("heartbeat AFK: focused visit with stale lastHeartbeat freezes on sweep", async () => {
    // The opt-in heartbeat path: SW watches lastHeartbeat per visit.
    // When the content script falls silent (no input for >30s) the
    // sweep freezes the visit at lastHeartbeat + HEARTBEAT_STALE_MS,
    // not at `now`, so the duration reflects when the user actually
    // walked away — not when the sweep noticed.
    await saveSettings({ ...DEFAULT_SETTINGS, bearerToken: "tok", heartbeatEnabled: true });
    stubTab(1, "https://reader.example/");
    await handleTabActivated(1, T0);
    // Pretend the content script's last heartbeat was 45s ago.
    const visits = await loadVisits();
    visits[1] = { ...visits[1], lastHeartbeat: T0 + 5 * SEC };
    await saveVisits(visits);
    // Sweep at T0 + 50s — heartbeat is 45s stale (> 30s threshold).
    await sweepStaleBlurred(T0 + 50 * SEC);
    const after = await loadVisits();
    expect(after[1].state).toBe("blurred");
    // Frozen at lastHeartbeat (5s) + HEARTBEAT_STALE_MS (30s) = 35s of focus
    expect(after[1].accumulatedFocusMs).toBe(35 * SEC);
  });

  test("heartbeat AFK: disabled by default — stale lastHeartbeat does nothing", async () => {
    // settings.heartbeatEnabled is false by default. Even with a
    // stale lastHeartbeat, sweep must not freeze the visit.
    stubTab(1, "https://reader.example/");
    await handleTabActivated(1, T0);
    const visits = await loadVisits();
    visits[1] = { ...visits[1], lastHeartbeat: T0 - 60 * SEC };
    await saveVisits(visits);
    await sweepStaleBlurred(T0 + 50 * SEC);
    expect((await loadVisits())[1].state).toBe("focused");
  });

  test("pause: setForegroundTab + handleNavigation short-circuit when paused", async () => {
    await saveSettings({
      ...DEFAULT_SETTINGS, bearerToken: "tok",
      pausedUntil: T0 + 15 * MIN,
    });
    stubTab(1, "https://reddit.com/r/anything");
    await handleTabActivated(1, T0);
    // No visit opened.
    expect(await loadVisits()).toEqual({});
    // Nav into a different URL also a no-op.
    await handleNavigation({
      tabId: 1, url: "https://reddit.com/r/anything", frameId: 0,
      timeStamp: T0 + 1 * SEC,
    });
    expect(await loadVisits()).toEqual({});
  });

  test("pause: sweep clears pausedUntil once the deadline passes", async () => {
    await saveSettings({
      ...DEFAULT_SETTINGS, bearerToken: "tok",
      pausedUntil: T0 + 15 * MIN,
    });
    // Sweep after the deadline.
    await sweepStaleBlurred(T0 + 16 * MIN);
    const s = await loadSettings();
    expect(s.pausedUntil).toBeNull();
  });

  test("sleep-orphan: focused visit older than 30 min caps at the limit", async () => {
    // Simulate the case where the system went to sleep mid-visit:
    // SW evicted, chrome.idle never fired, focused visit sits in storage
    // for 12 hours. When the user wakes the laptop and closes the tab,
    // emission MUST NOT report 12 hours of focused time.
    stubTab(1, "https://accounts.google.com/");
    await handleTabActivated(1, T0);
    // 12 hours later, the tab gets closed.
    const TWELVE_HOURS = 12 * 60 * MIN;
    await handleTabClose(1, T0 + TWELVE_HOURS);
    const ob = await loadOutbox();
    expect(ob).toHaveLength(1);
    const startMs = new Date(ob[0].payload.start_time).getTime();
    const endMs = new Date(ob[0].payload.end_time).getTime();
    // Duration is exactly MAX_FOCUS_PERIOD_MS (30 min), not 12 hours.
    expect(endMs - startMs).toBe(30 * MIN);
  });
});

// ---------- idle (chrome.idle.onStateChanged) ----------

describe("idle handling", () => {
  test("idle freezes the focused visit", async () => {
    stubTab(1, "https://reader.example/");
    await handleTabActivated(1, T0);
    await handleIdleStateChanged("idle", T0 + 60 * SEC);
    const visits = await loadVisits();
    expect(visits[1].state).toBe("blurred");
    expect(visits[1].accumulatedFocusMs).toBe(60 * SEC);
  });

  test("locked is treated the same as idle", async () => {
    stubTab(1, "https://reader.example/");
    await handleTabActivated(1, T0);
    await handleIdleStateChanged("locked", T0 + 60 * SEC);
    const visits = await loadVisits();
    expect(visits[1].state).toBe("blurred");
  });

  test("returning to active within grace resumes the visit", async () => {
    stubTab(1, "https://reader.example/");
    await handleTabActivated(1, T0);
    await handleIdleStateChanged("idle", T0 + 60 * SEC);
    // Active again 10s later — within grace.
    await handleIdleStateChanged("active", T0 + 70 * SEC);
    const visits = await loadVisits();
    expect(visits[1].state).toBe("focused");
    expect(visits[1].startTime).toBe(T0);  // same visit
    expect(visits[1].accumulatedFocusMs).toBe(60 * SEC);  // first 60s baked in
  });

  test("idle past grace results in emit when swept", async () => {
    stubTab(1, "https://reader.example/");
    await handleTabActivated(1, T0);
    await handleIdleStateChanged("idle", T0 + 60 * SEC);
    await sweepStaleBlurred(T0 + 60 * SEC + BLUR_GRACE_MS + SEC);
    const ob = await loadOutbox();
    expect(ob).toHaveLength(1);
    expect(ob[0].payload.url).toBe("https://reader.example/");
    expect(ob[0].payload.end_time).toBe("2023-11-14T22:14:20Z");  // start + 60s
    expect(await loadVisits()).toEqual({});
  });
});

// ---------- buildPayload ----------

describe("buildPayload", () => {
  test("includes chrome_identity from popup label override", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, identityLabel: "Acme Corp" });
    const p = await buildPayload({
      visit: {
        tabId: 1, windowId: 1, url: "https://x.com/", scrubbedUrl: "https://x.com/",
        category: null, startTime: T0, state: "focused",
        focusEpoch: T0, accumulatedFocusMs: 0, blurredAt: null,
        lastHeartbeat: null,
      },
      endTime: T0 + 5 * MIN,
      meta: { title: "T", og_description: "d", og_type: "article", favicon_url: "https://x.com/f.ico", lang: "en" },
    });
    expect(p.chrome_identity).toBe("Acme Corp");
    expect(p.og_type).toBe("article");
    expect(p.lang).toBe("en");
    expect(p.start_time).toBe("2023-11-14T22:13:20Z");
    expect(p.end_time).toBe("2023-11-14T22:18:20Z");
  });
});

// Reference: keep setForegroundTab import alive so the test file is also a
// usage-doc for the public surface area.
void setForegroundTab;


// ---------- mutex regression test ----------

describe("withSwLock serialises concurrent storage transitions", () => {
  function stubTabsByIdMap(tabs: Record<number, string>) {
    vi.mocked(chrome.tabs.get).mockImplementation(async (id: number) => {
      const url = tabs[id];
      if (!url) throw new Error(`stubTabsByIdMap: no tab ${id}`);
      return {
        id, url, title: "T",
        active: true, windowId: 1, incognito: false,
      } as chrome.tabs.Tab;
    });
  }

  test("two handlers fired together execute one-at-a-time", async () => {
    // Without the mutex, the second handler would load the same `visits`
    // map as the first, miss the first's freeze, and overwrite it on
    // save. With the mutex they run in order and the second call sees
    // the first call's freeze.
    stubTabsByIdMap({ 1: "https://a.com/", 2: "https://b.com/", 3: "https://c.com/" });
    await withSwLock(() => handleTabActivated(1, T0));

    // Fire two activations concurrently. Note: we don't await between
    // them — we want both to be queued at the same time.
    const first = withSwLock(() => handleTabActivated(2, T0 + 1 * SEC));
    const second = withSwLock(() => handleTabActivated(3, T0 + 2 * SEC));
    await Promise.all([first, second]);

    const visits = await loadVisits();
    expect(visits[1].state).toBe("blurred");
    expect(visits[2].state).toBe("blurred");
    expect(visits[3].state).toBe("focused");
  });

  test("a failing handler doesn't poison the chain", async () => {
    // The catch on the mutex chain must swallow rejections so the
    // NEXT handler still gets to run. Use a deliberate throw, then
    // queue a real handler behind it and assert the real one ran.
    const bad = withSwLock(async () => { throw new Error("boom"); });
    await expect(bad).rejects.toThrow("boom");
    stubTabsByIdMap({ 1: "https://recover.example/" });
    await withSwLock(() => handleTabActivated(1, T0));
    const visits = await loadVisits();
    expect(visits[1].state).toBe("focused");
  });
});
