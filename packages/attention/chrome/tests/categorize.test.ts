// chrome/tests/categorize.test.ts
import { describe, test, expect, beforeEach } from "vitest";
import { categorize } from "../src/categorize";
import { saveCategoryMap } from "../src/storage";

beforeEach(async () => {
  await chrome.storage.local.clear();
});

describe("categorize", () => {
  test("returns null when no mappings", async () => {
    expect(await categorize("https://example.com/")).toBeNull();
  });
  test("returns category slug on exact host match", async () => {
    await saveCategoryMap([{ pattern: "chatgpt.com", category: "ai-chat" }]);
    expect(await categorize("https://chatgpt.com/c/abc")).toBe("ai-chat");
  });
  test("returns category on wildcard match", async () => {
    await saveCategoryMap([{ pattern: "*.google.com", category: "search" }]);
    expect(await categorize("https://www.google.com/search?q=x")).toBe("search");
  });
  test("first matching rule wins (user-controlled order)", async () => {
    await saveCategoryMap([
      { pattern: "*.example.com", category: "first" },
      { pattern: "*.example.com", category: "second" },
    ]);
    expect(await categorize("https://x.example.com/")).toBe("first");
  });
});
