import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, test } from "vitest";

const source = resolve(dirname(fileURLToPath(import.meta.url)), "../src");

describe("extension page style resources", () => {
  for (const page of ["popup", "options", "wizard"]) {
    test(`${page} styles do not request remote stylesheets or assets`, () => {
      const css = readFileSync(resolve(source, page, `${page}.css`), "utf8")
        .replace(/\/\*[\s\S]*?\*\//g, "");
      const resources = [
        ...[...css.matchAll(/url\(\s*["']?([^\s"')]+)["']?\s*\)/gi)]
          .map((match) => match[1]),
        ...[...css.matchAll(/@import\s+["']([^"']+)["']/gi)]
          .map((match) => match[1]),
      ];
      expect(resources.filter((url) => /^(?:[a-z][a-z\d+.-]*:)?\/\//i.test(url)))
        .toEqual([]);
    });
  }
});
