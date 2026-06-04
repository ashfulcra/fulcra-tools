// chrome/tests/relayless/sentSet.test.ts
import { describe, test, expect } from "vitest";
import { SentSet } from "../../src/relayless/sentSet";
import { memStorage } from "./memStorage";

describe("SentSet", () => {
  test("has() is false before add, true after", async () => {
    const s = new SentSet({ storage: memStorage() });
    expect(await s.has("a")).toBe(false);
    await s.add(["a"]);
    expect(await s.has("a")).toBe(true);
    expect(await s.has("b")).toBe(false);
  });

  test("add de-dupes repeated ids", async () => {
    const s = new SentSet({ storage: memStorage() });
    await s.add(["a", "a", "b"]);
    await s.add(["b", "c"]);
    expect(await s.size()).toBe(3);
  });

  test("add([]) is a no-op", async () => {
    const s = new SentSet({ storage: memStorage() });
    await s.add([]);
    expect(await s.size()).toBe(0);
  });

  test("caps the set, dropping the oldest ids", async () => {
    const s = new SentSet({ storage: memStorage(), cap: 3 });
    await s.add(["a", "b", "c"]);
    await s.add(["d"]);
    // 'a' (oldest) is dropped.
    expect(await s.size()).toBe(3);
    expect(await s.has("a")).toBe(false);
    expect(await s.has("b")).toBe(true);
    expect(await s.has("d")).toBe(true);
  });

  test("cap drop handles a single add larger than the cap", async () => {
    const s = new SentSet({ storage: memStorage(), cap: 2 });
    await s.add(["a", "b", "c", "d"]);
    expect(await s.size()).toBe(2);
    expect(await s.has("c")).toBe(true);
    expect(await s.has("d")).toBe(true);
    expect(await s.has("a")).toBe(false);
  });

  test("persists across instances over the same storage", async () => {
    const storage = memStorage();
    await new SentSet({ storage }).add(["x"]);
    expect(await new SentSet({ storage }).has("x")).toBe(true);
  });
});
