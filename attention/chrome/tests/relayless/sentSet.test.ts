// chrome/tests/relayless/sentSet.test.ts
import { describe, test, expect } from "vitest";
import { SentSet } from "../../src/relayless/sentSet";
import { memStorage, spyStorage } from "./memStorage";

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

  test("snapshot() returns a Set of all recorded ids via one storage read", async () => {
    const storage = spyStorage();
    const s = new SentSet({ storage });
    await s.add(["a", "b", "c"]);
    storage.get.mockClear();
    const snap = await s.snapshot();
    expect(snap).toBeInstanceOf(Set);
    expect([...snap].sort()).toEqual(["a", "b", "c"]);
    expect(snap.has("a")).toBe(true);
    expect(snap.has("z")).toBe(false);
    // Exactly one storage read.
    expect(storage.get).toHaveBeenCalledTimes(1);
  });

  test("snapshot() is empty before any add", async () => {
    const s = new SentSet({ storage: memStorage() });
    expect((await s.snapshot()).size).toBe(0);
  });

  test("addMany() merges + caps via one read + one write, same semantics as add", async () => {
    const storage = spyStorage();
    const s = new SentSet({ storage, cap: 3 });
    await s.addMany(["a", "b", "c"]);
    storage.get.mockClear();
    storage.set.mockClear();
    await s.addMany(["c", "d"]); // 'c' dup, 'd' new -> oldest 'a' dropped
    expect(storage.get).toHaveBeenCalledTimes(1);
    expect(storage.set).toHaveBeenCalledTimes(1);
    expect(await s.size()).toBe(3);
    expect(await s.has("a")).toBe(false);
    expect(await s.has("b")).toBe(true);
    expect(await s.has("c")).toBe(true);
    expect(await s.has("d")).toBe(true);
  });

  test("addMany([]) is a no-op (no write)", async () => {
    const storage = spyStorage();
    const s = new SentSet({ storage });
    await s.addMany([]);
    expect(storage.set).not.toHaveBeenCalled();
    expect(await s.size()).toBe(0);
  });

  test("clear() empties the set so a previously-sent id can send again", async () => {
    // Repro of Bug A1: after an account switch the dedup set must be cleared so
    // a re-queued same source_id is not skipped against the OLD account's ids.
    const storage = memStorage();
    const s = new SentSet({ storage });
    await s.add(["a", "b"]);
    expect(await s.size()).toBe(2);
    await s.clear();
    expect(await s.size()).toBe(0);
    expect(await s.has("a")).toBe(false);
    // A fresh instance over the same storage also sees it cleared.
    expect(await new SentSet({ storage }).has("b")).toBe(false);
  });
});
