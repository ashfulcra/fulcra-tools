// chrome/tests/capture/visibility.test.ts
//
// Drives the Safari/iOS page-visibility visit state machine with a FAKE event
// source + FAKE clock — no jsdom, no real document.visibilityState flakiness.
// We construct a controllable source that the module subscribes to, then fire
// visibilitychange/pageshow/pagehide events by hand and advance a manual clock.
//
// The state machine under test (mirrors the proposal's iOS capture model):
//   - visit starts on first visible/pageshow
//   - foreground time accumulates across visible periods
//   - blur within BLUR_GRACE_MS resumes the same visit (no emit)
//   - blur past the grace window ends the visit; next visible starts a new one
//   - pagehide flushes the in-progress visit immediately (tail flush)
//   - sub-threshold (<MIN) visits are not emitted

import { describe, test, expect, beforeEach } from "vitest";
import {
  startVisibilityCapture,
  SAFARI_CLIENT,
  BLUR_GRACE_MS,
  MIN_VISIT_MS,
} from "../../src/capture/visibility";
import { buildWireRecord } from "../../src/relayless/wire";
import type { AttentionEvent } from "../../src/types";

type Handler = (ev?: unknown) => void;

/**
 * A fake event source standing in for document/window. Exposes the same
 * addEventListener/removeEventListener surface plus controllable
 * visibilityState, title, and location.href getters. `fire` dispatches to
 * every registered listener for an event type.
 */
class FakeSource {
  visibilityState: "visible" | "hidden" = "hidden";
  title = "Untitled";
  location = { href: "https://example.com/" };
  private listeners = new Map<string, Set<Handler>>();

  addEventListener(type: string, fn: Handler): void {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type)!.add(fn);
  }

  removeEventListener(type: string, fn: Handler): void {
    this.listeners.get(type)?.delete(fn);
  }

  fire(type: string, ev?: unknown): void {
    for (const fn of this.listeners.get(type) ?? []) fn(ev);
  }

  listenerCount(type: string): number {
    return this.listeners.get(type)?.size ?? 0;
  }
}

/** A manual clock so visit durations are exact and deterministic. */
class FakeClock {
  t = 0;
  now = (): number => this.t;
  advance(ms: number): void {
    this.t += ms;
  }
}

function setup(opts?: {
  href?: string;
  title?: string;
  visibilityState?: "visible" | "hidden";
}) {
  const src = new FakeSource();
  if (opts?.href) src.location.href = opts.href;
  if (opts?.title) src.title = opts.title;
  if (opts?.visibilityState) src.visibilityState = opts.visibilityState;
  const clock = new FakeClock();
  const emitted: AttentionEvent[] = [];
  const teardown = startVisibilityCapture({
    now: clock.now,
    source: src as unknown as never,
    emit: (e) => emitted.push(e),
  });
  return { src, clock, emitted, teardown };
}

/** Drive: become visible at the current clock time. */
function goVisible(src: FakeSource): void {
  src.visibilityState = "visible";
  src.fire("visibilitychange");
}
/** Drive: become hidden at the current clock time. */
function goHidden(src: FakeSource): void {
  src.visibilityState = "hidden";
  src.fire("visibilitychange");
}

describe("startVisibilityCapture", () => {
  test("visible -> hidden after N seconds emits one AttentionEvent with the right url/title and ~N-second duration", () => {
    const { src, clock, emitted } = setup({
      href: "https://news.example.com/story?id=7&utm_source=rss",
      title: "Big Story",
    });

    goVisible(src);
    clock.advance(5_000);
    goHidden(src);
    // A plain visible->hidden does NOT emit yet — the visit stays alive for a
    // possible within-grace resume. The flush comes on pagehide (or past-grace
    // resume). This mirrors iOS where pagehide is the reliable tail flush.
    expect(emitted).toHaveLength(0);
    src.fire("pagehide");

    expect(emitted).toHaveLength(1);
    const e = emitted[0];
    // url is scrubbed: utm_source dropped, id kept.
    expect(e.url).toBe("https://news.example.com/story?id=7");
    expect(e.title).toBe("Big Story");
    expect(e.category).toBeNull();
    expect(e.client).toBe(SAFARI_CLIENT);
    // ISO-8601 with trailing Z.
    expect(e.start_time).toMatch(/Z$/);
    expect(e.end_time).toMatch(/Z$/);
    const durMs = Date.parse(e.end_time) - Date.parse(e.start_time);
    expect(durMs).toBe(5_000);
  });

  test("visible -> hidden -> visible within grace = same visit (no emit on blur), accumulated time spans both visible periods", () => {
    const { src, clock, emitted } = setup({ title: "T", href: "https://a.test/" });

    goVisible(src);
    clock.advance(3_000); // 3s visible
    goHidden(src);
    expect(emitted).toHaveLength(0); // no emit on blur

    clock.advance(BLUR_GRACE_MS - 1); // resume within grace
    goVisible(src);
    clock.advance(4_000); // 4s more visible
    goHidden(src);

    // grace blur (the second) elapsed exactly... still no second visit started.
    // End the visit by exceeding grace then nothing, or flush via pagehide.
    clock.advance(BLUR_GRACE_MS + 1);
    goVisible(src); // past grace -> emits the resumed visit, starts a new one

    expect(emitted).toHaveLength(1);
    const e = emitted[0];
    // accumulated visible time = 3s + 4s = 7s.
    const durMs = Date.parse(e.end_time) - Date.parse(e.start_time);
    expect(durMs).toBe(7_000);
  });

  test("visible -> hidden -> (past grace) visible = first visit emitted, new visit started", () => {
    const { src, clock, emitted } = setup({ href: "https://first.test/" });

    goVisible(src);
    clock.advance(2_000);
    goHidden(src);
    expect(emitted).toHaveLength(0);

    clock.advance(BLUR_GRACE_MS + 5_000); // past grace
    src.location.href = "https://second.test/"; // navigated / new page state
    goVisible(src);
    // First visit emitted on the past-grace resume.
    expect(emitted).toHaveLength(1);
    expect(emitted[0].url).toBe("https://first.test/");

    // The new visit is live; end it via pagehide to confirm it's separate.
    clock.advance(3_000);
    src.fire("pagehide");
    expect(emitted).toHaveLength(2);
    expect(emitted[1].url).toBe("https://second.test/");
    const dur2 = Date.parse(emitted[1].end_time) - Date.parse(emitted[1].start_time);
    expect(dur2).toBe(3_000);
  });

  test("pagehide emits the in-progress visit immediately (tail flush)", () => {
    const { src, clock, emitted } = setup({ href: "https://tail.test/" });

    goVisible(src);
    clock.advance(6_000);
    src.fire("pagehide");

    expect(emitted).toHaveLength(1);
    expect(emitted[0].url).toBe("https://tail.test/");
    const dur = Date.parse(emitted[0].end_time) - Date.parse(emitted[0].start_time);
    expect(dur).toBe(6_000);
  });

  test("pageshow starts a visit (resume from bfcache / first paint)", () => {
    const { src, clock, emitted } = setup({ href: "https://show.test/" });

    src.visibilityState = "visible";
    src.fire("pageshow");
    clock.advance(2_500);
    src.fire("pagehide");

    expect(emitted).toHaveLength(1);
    const dur = Date.parse(emitted[0].end_time) - Date.parse(emitted[0].start_time);
    expect(dur).toBe(2_500);
  });

  test("a sub-threshold (<1s) visit does NOT emit", () => {
    const { src, clock, emitted } = setup({ href: "https://flip.test/" });

    goVisible(src);
    clock.advance(MIN_VISIT_MS - 1); // just under threshold
    goHidden(src);

    expect(emitted).toHaveLength(0);
  });

  test("a sub-threshold tail visit on pagehide does NOT emit either", () => {
    const { src, clock, emitted } = setup({ href: "https://flip2.test/" });

    goVisible(src);
    clock.advance(MIN_VISIT_MS - 1);
    src.fire("pagehide");

    expect(emitted).toHaveLength(0);
  });

  test("teardown removes listeners and stops emitting", () => {
    const { src, clock, emitted, teardown } = setup({ href: "https://td.test/" });

    goVisible(src);
    clock.advance(4_000);
    teardown();
    // After teardown the source has no listeners for our events.
    expect(src.listenerCount("visibilitychange")).toBe(0);
    expect(src.listenerCount("pagehide")).toBe(0);
    expect(src.listenerCount("pageshow")).toBe(0);

    // Firing after teardown does nothing.
    goHidden(src);
    expect(emitted).toHaveLength(0);
  });

  test("the emitted AttentionEvent matches the shape buildWireRecord consumes", async () => {
    const { src, clock, emitted } = setup({
      href: "https://wire.test/page?token=secret&keep=1",
      title: "Wire Page",
    });

    goVisible(src);
    clock.advance(10_000);
    src.fire("pagehide");

    expect(emitted).toHaveLength(1);
    const ev = emitted[0];
    // Sanity: feed it through the real wire transform with a dummy ctx.
    const { record, sourceId } = await buildWireRecord(ev, {
      definitionId: "def-x",
      tagIds: ["tag-attn", "tag-web"],
      identitySlug: "",
    });
    expect(sourceId).toMatch(/^com\.fulcra\.attention\./);
    expect(record.specversion).toBe(1);
    // token (auth-bearing) was scrubbed, keep survived.
    expect(ev.url).toBe("https://wire.test/page?keep=1");
    // category null, client set, ISO-Z bounds.
    expect(ev.category).toBeNull();
    expect(ev.client).toBe(SAFARI_CLIENT);
    expect(ev.start_time.endsWith("Z")).toBe(true);
    expect(ev.end_time.endsWith("Z")).toBe(true);
  });
});

describe("constants", () => {
  test("BLUR_GRACE_MS default is 30s and MIN_VISIT_MS default is 1s", () => {
    expect(BLUR_GRACE_MS).toBe(30_000);
    expect(MIN_VISIT_MS).toBe(1_000);
  });
});

beforeEach(() => {
  // no global state to reset — each setup() builds its own source/clock.
});
