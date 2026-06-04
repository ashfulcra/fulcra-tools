// chrome/tests/default-categories.test.ts
import { describe, test, expect } from "vitest";
import {
  DEFAULT_CATEGORY_MAP, mergeDefaults,
} from "../src/default-categories";

describe("DEFAULT_CATEGORY_MAP", () => {
  test("every default uses a slug that exists in the CATEGORY_VOCAB", () => {
    // The slugs MUST be ones the Python relay pre-creates tags for at
    // bootstrap, otherwise the resulting events would be tagless.
    const VOCAB = new Set([
      "search", "webmail", "ai-chat", "dm", "doc-editor", "reddit-thread",
      "calendar", "banking", "brokerage", "crypto", "tax", "healthcare",
      "password-manager", "mental-health", "dating", "adult", "job-hunting",
    ]);
    for (const m of DEFAULT_CATEGORY_MAP) {
      expect(VOCAB).toContain(m.category);
    }
  });

  test("Gmail collapses to webmail (the user's stated ask)", () => {
    const m = DEFAULT_CATEGORY_MAP.find((x) => x.pattern === "mail.google.com");
    expect(m?.category).toBe("webmail");
  });
});

describe("mergeDefaults", () => {
  test("seeds an empty user map with the defaults", () => {
    const merged = mergeDefaults([]);
    expect(merged.length).toBe(DEFAULT_CATEGORY_MAP.length);
    expect(merged.find((m) => m.pattern === "mail.google.com")?.category)
      .toBe("webmail");
  });

  test("user entry for a default pattern wins over the default", () => {
    // User repurposed mail.google.com → reddit-thread (silly but their
    // call). mergeDefaults must NOT overwrite that.
    const userMap = [
      { pattern: "mail.google.com", category: "reddit-thread" },
    ];
    const merged = mergeDefaults(userMap);
    expect(merged.find((m) => m.pattern === "mail.google.com")?.category)
      .toBe("reddit-thread");
  });

  test("user entries for non-default patterns are preserved", () => {
    const userMap = [
      { pattern: "my-private-app.local", category: "doc-editor" },
    ];
    const merged = mergeDefaults(userMap);
    expect(merged.find((m) => m.pattern === "my-private-app.local"))
      .toBeDefined();
    expect(merged.find((m) => m.pattern === "mail.google.com"))
      .toBeDefined();
  });

  test("result is sorted by pattern", () => {
    const merged = mergeDefaults([]);
    const patterns = merged.map((m) => m.pattern);
    const sorted = [...patterns].sort();
    expect(patterns).toEqual(sorted);
  });
});
