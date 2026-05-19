// chrome/tests/storage.test.ts
import { describe, test, expect, beforeEach } from "vitest";
import {
  loadSettings, saveSettings,
  loadOutbox, saveOutbox,
  loadIgnoreList, saveIgnoreList,
  loadCategoryMap,
  loadVisits, saveVisits,
} from "../src/storage";
import { DEFAULT_SETTINGS } from "../src/types";

beforeEach(async () => {
  await chrome.storage.local.clear();
  await chrome.storage.sync.clear();
  await chrome.storage.session.clear();
});

describe("settings", () => {
  test("loadSettings returns defaults when empty", async () => {
    expect(await loadSettings()).toEqual(DEFAULT_SETTINGS);
  });
  test("saveSettings then loadSettings round-trips", async () => {
    await saveSettings({
      bearerToken: "x", relayPort: 8771, enabled: false,
      identityLabel: "Work", onboarded: true,
      pausedUntil: null, heartbeatEnabled: false,
    });
    expect(await loadSettings()).toEqual({
      bearerToken: "x", relayPort: 8771, enabled: false,
      identityLabel: "Work", onboarded: true,
      pausedUntil: null, heartbeatEnabled: false,
    });
  });
});

describe("outbox", () => {
  test("loadOutbox returns [] when empty", async () => {
    expect(await loadOutbox()).toEqual([]);
  });
  test("save then load round-trips", async () => {
    const entry = {
      id: "abc",
      payload: {
        url: "https://x.com/", title: "T", og_description: null, favicon_url: null,
        category: null, chrome_identity: null, og_type: null, lang: null,
        start_time: "2026-05-18T14:00:00Z", end_time: "2026-05-18T14:05:00Z",
        client: "fulcra-attention-chrome/0.1.0",
      },
      queuedAt: 1700000000000,
      attempts: 0,
    };
    await saveOutbox([entry]);
    expect(await loadOutbox()).toEqual([entry]);
  });
});

describe("ignore list (sync)", () => {
  test("loadIgnoreList returns [] when empty", async () => {
    expect(await loadIgnoreList()).toEqual([]);
  });
  test("uses chrome.storage.sync, not local", async () => {
    await saveIgnoreList([{ pattern: "chase.com", addedAt: "2026-05-18T14:00:00Z" }]);
    const sync = await chrome.storage.sync.get(null);
    expect(sync).toHaveProperty("ignoreList");
  });
});

describe("category map (local)", () => {
  test("loadCategoryMap returns [] when empty", async () => {
    expect(await loadCategoryMap()).toEqual([]);
  });
});

describe("active visits (session)", () => {
  test("loadVisits returns {} when empty", async () => {
    expect(await loadVisits()).toEqual({});
  });
  test("uses chrome.storage.session under the `visits` key", async () => {
    await saveVisits({
      7: {
        tabId: 7, windowId: 1, url: "https://x.com/", scrubbedUrl: "https://x.com/",
        category: null, startTime: 1700000000000, state: "focused",
        focusEpoch: 1700000000000, accumulatedFocusMs: 0, blurredAt: null,
        lastHeartbeat: null,
      },
    });
    const session = await chrome.storage.session.get(null);
    expect(session).toHaveProperty("visits");
  });
});
