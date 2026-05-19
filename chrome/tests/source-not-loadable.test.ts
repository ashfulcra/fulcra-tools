// chrome/tests/source-not-loadable.test.ts
//
// Regression guard for the "blank popup on a fresh clone" bug.
//
// The source dir's index.html loads raw `/src/popup/main.tsx`, which a
// browser cannot execute. If a `manifest.json` ever lands here again,
// Chrome's "Load unpacked" will silently accept the SOURCE directory and
// the user gets a favicon-sized white box with no error. The real
// loadable extension is emitted to dist/ by the build.
import { describe, test, expect } from "vitest";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const chromeDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");

describe("source directory is not a loadable extension", () => {
  test("chrome/ has no manifest.json (only the build output does)", () => {
    expect(existsSync(resolve(chromeDir, "manifest.json"))).toBe(false);
  });

  test("the @crxjs manifest source exists under a non-loadable name", () => {
    expect(existsSync(resolve(chromeDir, "manifest.config.json"))).toBe(true);
  });
});
