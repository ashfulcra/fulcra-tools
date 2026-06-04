// chrome/tests/content.test.ts
import { describe, test, expect } from "vitest";
import { extractPageMeta } from "../src/content";

function makeDoc(html: string): Document {
  const parser = new DOMParser();
  return parser.parseFromString(`<!doctype html>${html}`, "text/html");
}

describe("extractPageMeta", () => {
  test("extracts title", () => {
    const d = makeDoc(`<html><head><title>My Page</title></head><body></body></html>`);
    expect(extractPageMeta(d, "https://example.com/p").title).toBe("My Page");
  });

  test("extracts og:description", () => {
    const d = makeDoc(`
      <html><head>
        <title>T</title>
        <meta property="og:description" content="A short summary." />
      </head></html>
    `);
    expect(extractPageMeta(d, "https://example.com/").og_description).toBe("A short summary.");
  });

  test("extracts og:type", () => {
    const d = makeDoc(`<html><head><meta property="og:type" content="article" /></head></html>`);
    expect(extractPageMeta(d, "https://example.com/").og_type).toBe("article");
  });

  test("extracts html lang", () => {
    const d = makeDoc(`<html lang="ja"><head></head></html>`);
    expect(extractPageMeta(d, "https://example.com/").lang).toBe("ja");
  });

  test("resolves favicon relative to page URL", () => {
    const d = makeDoc(`<html><head><link rel="icon" href="/favicon.ico" /></head></html>`);
    expect(extractPageMeta(d, "https://example.com/p/q").favicon_url)
      .toBe("https://example.com/favicon.ico");
  });

  test("falls back to /favicon.ico when no link tag", () => {
    const d = makeDoc(`<html><head></head></html>`);
    expect(extractPageMeta(d, "https://example.com/p/q").favicon_url)
      .toBe("https://example.com/favicon.ico");
  });

  test("returns null for missing optional fields", () => {
    const d = makeDoc(`<html><head><title>T</title></head></html>`);
    const m = extractPageMeta(d, "https://example.com/");
    expect(m.og_description).toBeNull();
    expect(m.og_type).toBeNull();
  });

  test("title is null when document.title is empty", () => {
    const d = makeDoc(`<html><head></head></html>`);
    expect(extractPageMeta(d, "https://example.com/").title).toBeNull();
  });
});
