// chrome/tests/ignore.test.ts
import { describe, test, expect, beforeEach } from "vitest";
import { isIgnored, matchesPattern } from "../src/ignore";
import { saveIgnoreList } from "../src/storage";

beforeEach(async () => {
  await chrome.storage.sync.clear();
});

describe("matchesPattern", () => {
  test("exact host match", () => {
    expect(matchesPattern("example.com", "example.com")).toBe(true);
    expect(matchesPattern("other.com", "example.com")).toBe(false);
  });
  test("wildcard *.example.com matches subdomain", () => {
    expect(matchesPattern("mail.example.com", "*.example.com")).toBe(true);
    expect(matchesPattern("app.mail.example.com", "*.example.com")).toBe(true);
  });
  test("wildcard *.example.com does NOT match apex", () => {
    expect(matchesPattern("example.com", "*.example.com")).toBe(false);
  });
  test("wildcard does not match unrelated host", () => {
    expect(matchesPattern("other.com", "*.example.com")).toBe(false);
  });
});

describe("isIgnored", () => {
  test("returns false when ignore list is empty", async () => {
    expect(await isIgnored("https://example.com/page")).toBe(false);
  });
  test("returns true when host matches an exact entry", async () => {
    await saveIgnoreList([{ pattern: "chase.com", addedAt: "2026-05-18T14:00:00Z" }]);
    expect(await isIgnored("https://chase.com/login")).toBe(true);
    expect(await isIgnored("https://example.com/")).toBe(false);
  });
  test("returns true when host matches a wildcard", async () => {
    await saveIgnoreList([{ pattern: "*.bank.com", addedAt: "2026-05-18T14:00:00Z" }]);
    expect(await isIgnored("https://my.bank.com/account")).toBe(true);
    expect(await isIgnored("https://bank.com/")).toBe(false);
  });
});
