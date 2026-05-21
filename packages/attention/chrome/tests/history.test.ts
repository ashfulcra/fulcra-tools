// chrome/tests/history.test.ts
import { describe, test, expect } from "vitest";
import {
  groupByHost, matchesAnyPattern, buildIgnoreList,
  EXCLUSION_PRESETS,
} from "../src/wizard/history";

function fakeItem(url: string, lastVisitTime = 1_700_000_000_000, visitCount = 1, title = "T"): chrome.history.HistoryItem {
  return { id: url, url, title, lastVisitTime, visitCount, typedCount: 0 };
}

describe("groupByHost", () => {
  test("groups multiple URLs from the same host together and sums visits", () => {
    const items = [
      fakeItem("https://example.com/a", 2, 3),
      fakeItem("https://example.com/b", 1, 5),
      fakeItem("https://other.com/p", 1, 1),
    ];
    const groups = groupByHost(items);
    expect(groups).toHaveLength(2);
    // Sorted by total visits desc.
    expect(groups[0].host).toBe("example.com");
    expect(groups[0].count).toBe(8);
    expect(groups[1].host).toBe("other.com");
  });

  test("scrubs URLs (defense in depth) before grouping", () => {
    const items = [fakeItem("https://example.com/p?access_token=DEADBEEF&id=42")];
    const g = groupByHost(items);
    expect(g[0].urls[0].url).toBe("https://example.com/p?id=42");
    expect(g[0].urls[0].url).not.toContain("access_token");
  });

  test("drops non-http URLs", () => {
    const items = [
      fakeItem("https://example.com/p"),
      fakeItem("chrome://extensions/"),
      fakeItem("file:///tmp/x"),
    ];
    const g = groupByHost(items);
    expect(g).toHaveLength(1);
    expect(g[0].host).toBe("example.com");
  });

  test("urls within a group are sorted by lastVisitTime desc", () => {
    const items = [
      fakeItem("https://example.com/old", 100),
      fakeItem("https://example.com/new", 500),
      fakeItem("https://example.com/mid", 300),
    ];
    const g = groupByHost(items);
    expect(g[0].urls.map((u) => u.url)).toEqual([
      "https://example.com/new",
      "https://example.com/mid",
      "https://example.com/old",
    ]);
  });
});

describe("matchesAnyPattern", () => {
  test("exact host match", () => {
    expect(matchesAnyPattern("chase.com", ["chase.com"])).toBe(true);
    expect(matchesAnyPattern("foo.chase.com", ["chase.com"])).toBe(false);
  });
  test("wildcard subdomain match", () => {
    expect(matchesAnyPattern("foo.chase.com", ["*.chase.com"])).toBe(true);
    expect(matchesAnyPattern("chase.com", ["*.chase.com"])).toBe(true);  // base host matches the wildcard
    expect(matchesAnyPattern("other.com", ["*.chase.com"])).toBe(false);
  });
  test("returns false against an empty list", () => {
    expect(matchesAnyPattern("anything.com", [])).toBe(false);
  });
});

describe("buildIgnoreList", () => {
  test("merges presets + manual hosts + existing patterns into a sorted unique set", () => {
    const merged = buildIgnoreList(
      ["banking"],
      ["totalblock.com"],
      ["existing.com"],
    );
    expect(merged).toContain("existing.com");
    expect(merged).toContain("totalblock.com");
    expect(merged).toContain("chase.com");           // from banking preset
    expect(merged).toContain("*.chase.com");
    // No duplicates.
    expect(new Set(merged).size).toBe(merged.length);
    // Sorted.
    expect([...merged].sort()).toEqual(merged);
  });

  test("unknown preset id is silently skipped", () => {
    const merged = buildIgnoreList(["does-not-exist"], [], []);
    expect(merged).toEqual([]);
  });
});

describe("EXCLUSION_PRESETS sanity", () => {
  test("every preset has a non-empty patterns list", () => {
    for (const p of EXCLUSION_PRESETS) {
      expect(p.patterns.length).toBeGreaterThan(0);
    }
  });
  test("preset IDs are unique", () => {
    const ids = EXCLUSION_PRESETS.map((p) => p.id);
    expect(new Set(ids).size).toBe(ids.length);
  });
});
