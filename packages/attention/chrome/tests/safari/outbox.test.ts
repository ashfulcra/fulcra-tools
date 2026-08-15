import { describe, expect, it, vi } from "vitest";

import {
  addSafariEvent,
  flushSafariOutbox,
  loadSafariOutbox,
  SAFARI_OUTBOX_CAP,
} from "../../src/safari/outbox";
import type { AttentionEvent } from "../../src/types";

/** A storage double that SERIALIZES, like the real chrome.storage does. That
 * detail is the point of several tests below: every read hands back fresh
 * objects, so anything comparing events by reference silently matches nothing. */
function memArea() {
  let blob = "{}";
  return {
    get: async (key: string) => {
      const all = JSON.parse(blob) as Record<string, unknown>;
      return key in all ? { [key]: all[key] } : {};
    },
    set: async (items: Record<string, unknown>) => {
      blob = JSON.stringify({ ...(JSON.parse(blob) as object), ...items });
    },
  };
}

const event = (start: string, url = "https://a.example/1"): AttentionEvent => ({
  url,
  title: "t",
  og_description: null,
  favicon_url: null,
  category: null,
  chrome_identity: null,
  og_type: null,
  lang: null,
  start_time: start,
  end_time: start,
  client: "fulcra-attention-safari/0.1.0",
});

describe("safari outbox retention", () => {
  it("removes events after a validated success", async () => {
    const area = memArea();
    await addSafariEvent(event("2026-06-01T00:00:00.000Z"), area);
    const send = vi.fn().mockResolvedValue({ kind: "ok", sent: 1, skipped: 0 });

    await flushSafariOutbox(area, send);
    expect(await loadSafariOutbox(area)).toEqual([]);
  });

  it("drains across a serializing store — not by object identity", async () => {
    // The bug this pins: chrome.storage returns NEW objects on every read, so
    // matching the handed-off batch by reference filters nothing. The buffer
    // would never drain and the same visits would re-send forever, while every
    // flush reported success. Server-side dedup would hide it.
    const area = memArea();
    await addSafariEvent(event("2026-06-01T00:00:00.000Z"), area);
    const send = vi.fn().mockResolvedValue({ kind: "ok", sent: 1, skipped: 0 });

    await flushSafariOutbox(area, send);
    await flushSafariOutbox(area, send);

    expect(await loadSafariOutbox(area)).toEqual([]);
    expect(send).toHaveBeenCalledTimes(1); // second flush had nothing to send
  });

  it("RETAINS everything when the native side is unreachable", async () => {
    const area = memArea();
    await addSafariEvent(event("2026-06-01T00:00:00.000Z"), area);
    const send = vi.fn().mockResolvedValue({ kind: "unreachable" });

    const outcome = await flushSafariOutbox(area, send);
    expect(outcome.kind).toBe("unreachable");
    expect(await loadSafariOutbox(area)).toHaveLength(1);
  });

  it("RETAINS everything when the user needs to sign in", async () => {
    // Temporary from the buffer's point of view: the user signs in and the next
    // flush delivers. Dropping here would delete the visits recorded while
    // signed out — the exact window where a user has done nothing wrong.
    const area = memArea();
    await addSafariEvent(event("2026-06-01T00:00:00.000Z"), area);
    const send = vi.fn().mockResolvedValue({ kind: "unauthorized" });

    await flushSafariOutbox(area, send);
    expect(await loadSafariOutbox(area)).toHaveLength(1);
  });

  it("does not drop a visit that arrived DURING the flush", async () => {
    // The race: a page unloads while a native call is in flight. Splicing "the
    // first N" or clearing the key outright would delete an event that was
    // never handed over.
    const area = memArea();
    await addSafariEvent(event("2026-06-01T00:00:00.000Z"), area);

    const send = vi.fn().mockImplementation(async () => {
      await addSafariEvent(event("2026-06-01T00:05:00.000Z"), area);
      return { kind: "ok", sent: 1, skipped: 0 };
    });

    await flushSafariOutbox(area, send);
    const left = await loadSafariOutbox(area);
    expect(left).toHaveLength(1);
    expect(left[0].start_time).toBe("2026-06-01T00:05:00.000Z");
  });

  it("treats an empty buffer as success without calling the bridge", async () => {
    const send = vi.fn();
    const outcome = await flushSafariOutbox(memArea(), send);
    expect(outcome).toEqual({ kind: "ok", sent: 0, skipped: 0 });
    expect(send).not.toHaveBeenCalled();
  });

  it("caps the buffer by dropping the OLDEST", async () => {
    const area = memArea();
    const many = Array.from({ length: SAFARI_OUTBOX_CAP + 2 }, (_, i) =>
      event(new Date(Date.UTC(2026, 5, 1, 0, 0, i)).toISOString()),
    );
    await area.set({ safariOutbox: many });
    await addSafariEvent(event("2026-07-01T00:00:00.000Z"), area);

    const left = await loadSafariOutbox(area);
    expect(left).toHaveLength(SAFARI_OUTBOX_CAP);
    expect(left[left.length - 1].start_time).toBe("2026-07-01T00:00:00.000Z");
    expect(left[0].start_time).not.toBe(many[0].start_time);
  });

  it("retains BOTH events when two adds race (pr-617 r1 regression)", async () => {
    // Pre-lock, each add read the same array at its first await, pushed its
    // own visit, and the second set erased the first — deterministic 1-of-2
    // loss with nothing but microtask interleaving.
    const area = memArea();
    await Promise.all([
      addSafariEvent(event("2026-06-01T00:00:00.000Z", "https://a.example/1"), area),
      addSafariEvent(event("2026-06-01T00:01:00.000Z", "https://a.example/2"), area),
    ]);
    const left = await loadSafariOutbox(area);
    expect(left).toHaveLength(2);
  });

  it("retains a visit that lands while a flush is mid-send", async () => {
    const area = memArea();
    await addSafariEvent(event("2026-06-01T00:00:00.000Z"), area);

    let release!: (o: { kind: "ok"; sent: number; skipped: number }) => void;
    const gate = new Promise<{ kind: "ok"; sent: number; skipped: number }>((r) => {
      release = r;
    });
    const send = vi.fn(async () => gate);

    const flushing = flushSafariOutbox(area, send);
    await addSafariEvent(event("2026-06-01T00:05:00.000Z", "https://a.example/mid"), area);
    release({ kind: "ok", sent: 1, skipped: 0 });
    await flushing;

    const left = await loadSafariOutbox(area);
    expect(left).toHaveLength(1);
    expect(left[0].url).toBe("https://a.example/mid");
  });

  it("a rejected mutation does not wedge later adds", async () => {
    const area = memArea();
    const broken = {
      get: async () => {
        throw new Error("storage down");
      },
      set: async () => {},
    };
    await expect(addSafariEvent(event("2026-06-01T00:00:00.000Z"), broken)).rejects.toThrow();
    await addSafariEvent(event("2026-06-01T00:01:00.000Z"), area);
    expect(await loadSafariOutbox(area)).toHaveLength(1);
  });
});
