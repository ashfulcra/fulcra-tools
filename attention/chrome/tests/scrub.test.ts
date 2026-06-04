// chrome/tests/scrub.test.ts
import { describe, test, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { scrubUrl } from "../src/scrub";

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURE_PATH = join(__dirname, "..", "..", "tests", "fixtures", "scrub_cases.json");
const CASES: Array<{ input: string; expected: string }> = JSON.parse(
  readFileSync(FIXTURE_PATH, "utf-8"),
);

describe("scrub_url cross-language contract", () => {
  test.each(CASES)("scrubs $input -> $expected", ({ input, expected }) => {
    expect(scrubUrl(input)).toBe(expected);
  });
});
