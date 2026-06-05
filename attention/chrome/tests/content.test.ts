// chrome/tests/content.test.ts
//
// Tests the REAL page-meta extractor that background.ts injects into the
// page via chrome.scripting.executeScript({ func: extractPageMeta }).
//
// extractPageMeta is self-contained: it reads the page's global `document`
// and takes only the page URL as an argument (matching what executeScript
// passes through `args`). To exercise it we install fixture HTML into the
// jsdom global document, then call the exact function background.ts injects.

import { describe, test, expect, afterEach } from "vitest";
import { extractPageMeta } from "../src/content";

// Replace the global document's contents with the fixture. extractPageMeta
// reads `document` (the page global) directly, exactly as it does in-page.
function setDoc(html: string): void {
  document.open();
  document.write(`<!doctype html>${html}`);
  document.close();
}

afterEach(() => {
  // Reset to a clean document so fixtures don't leak between tests.
  setDoc("<html><head></head><body></body></html>");
});

describe("extractPageMeta (the function injected by executeScript)", () => {
  test("extracts title", () => {
    setDoc(`<html><head><title>My Page</title></head><body></body></html>`);
    expect(extractPageMeta("https://example.com/p").title).toBe("My Page");
  });

  test("extracts og:description", () => {
    setDoc(`
      <html><head>
        <title>T</title>
        <meta property="og:description" content="A short summary." />
      </head></html>
    `);
    expect(extractPageMeta("https://example.com/").og_description).toBe("A short summary.");
  });

  test("extracts og:type", () => {
    setDoc(`<html><head><meta property="og:type" content="article" /></head></html>`);
    expect(extractPageMeta("https://example.com/").og_type).toBe("article");
  });

  test("extracts html lang", () => {
    setDoc(`<html lang="ja"><head></head></html>`);
    expect(extractPageMeta("https://example.com/").lang).toBe("ja");
  });

  test("resolves favicon relative to page URL", () => {
    setDoc(`<html><head><link rel="icon" href="/favicon.ico" /></head></html>`);
    expect(extractPageMeta("https://example.com/p/q").favicon_url)
      .toBe("https://example.com/favicon.ico");
  });

  test("resolves a relative favicon href against the page URL", () => {
    setDoc(`<html><head><link rel="icon" href="icon.png" /></head></html>`);
    expect(extractPageMeta("https://example.com/a/b").favicon_url)
      .toBe("https://example.com/a/icon.png");
  });

  test("honors rel=\"shortcut icon\"", () => {
    setDoc(`<html><head><link rel="shortcut icon" href="/s.ico" /></head></html>`);
    expect(extractPageMeta("https://example.com/").favicon_url)
      .toBe("https://example.com/s.ico");
  });

  test("falls back to /favicon.ico when no link tag", () => {
    setDoc(`<html><head></head></html>`);
    expect(extractPageMeta("https://example.com/p/q").favicon_url)
      .toBe("https://example.com/favicon.ico");
  });

  test("returns null for missing optional fields", () => {
    setDoc(`<html><head><title>T</title></head></html>`);
    const m = extractPageMeta("https://example.com/");
    expect(m.og_description).toBeNull();
    expect(m.og_type).toBeNull();
    expect(m.lang).toBeNull();
  });

  test("title is null when document has no title", () => {
    setDoc(`<html><head></head></html>`);
    expect(extractPageMeta("https://example.com/").title).toBeNull();
  });

  test("returns the full PageMeta shape", () => {
    setDoc(`
      <html lang="en"><head>
        <title>Full</title>
        <meta property="og:description" content="desc" />
        <meta property="og:type" content="website" />
        <link rel="icon" href="/f.ico" />
      </head></html>
    `);
    expect(extractPageMeta("https://example.com/")).toEqual({
      title: "Full",
      og_description: "desc",
      og_type: "website",
      favicon_url: "https://example.com/f.ico",
      lang: "en",
    });
  });
});
