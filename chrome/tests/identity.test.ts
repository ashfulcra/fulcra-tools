// chrome/tests/identity.test.ts
import { describe, test, expect, beforeEach, vi } from "vitest";
import { getChromeIdentity } from "../src/identity";
import { saveSettings } from "../src/storage";
import { DEFAULT_SETTINGS } from "../src/types";

beforeEach(async () => {
  await chrome.storage.local.clear();
  vi.mocked(chrome.identity.getProfileUserInfo).mockReset();
});

describe("getChromeIdentity", () => {
  test("returns Google account email when signed in", async () => {
    vi.mocked(chrome.identity.getProfileUserInfo).mockImplementation((_opts, cb) => {
      cb({ email: "ash@fulcradynamics.com", id: "google-id-123" });
    });
    expect(await getChromeIdentity()).toBe("ash@fulcradynamics.com");
  });

  test("returns popup-set label when not signed in to Google", async () => {
    vi.mocked(chrome.identity.getProfileUserInfo).mockImplementation((_opts, cb) => {
      cb({ email: "", id: "" });
    });
    await saveSettings({ ...DEFAULT_SETTINGS, identityLabel: "Side Project" });
    expect(await getChromeIdentity()).toBe("Side Project");
  });

  test("returns null when neither source available", async () => {
    vi.mocked(chrome.identity.getProfileUserInfo).mockImplementation((_opts, cb) => {
      cb({ email: "", id: "" });
    });
    expect(await getChromeIdentity()).toBeNull();
  });

  test("popup label overrides Google email when both set", async () => {
    vi.mocked(chrome.identity.getProfileUserInfo).mockImplementation((_opts, cb) => {
      cb({ email: "ash@fulcradynamics.com", id: "google-id" });
    });
    await saveSettings({ ...DEFAULT_SETTINGS, identityLabel: "Custom Label" });
    expect(await getChromeIdentity()).toBe("Custom Label");
  });
});
