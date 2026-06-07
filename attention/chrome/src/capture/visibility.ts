// chrome/src/capture/visibility.ts
//
// Safari/iOS attention capture: a pure, framework-free, page-visibility-driven
// visit tracker, usable as a content script.
//
// WHY THIS EXISTS. On Chrome the service worker owns capture via
// chrome.tabs / chrome.idle / chrome.windows. On Safari/iOS those APIs don't
// exist (safari-web-extension-converter flags `idle` and `history` as
// unsupported), and the extension's background lifetime is not dependable. So
// each page tracks its OWN visible-foreground time using the Page Visibility +
// pageshow/pagehide events, and emits an AttentionEvent when a visit ends. See
// docs/proposals/2026-06-04-relayless-and-mobile-safari-attention.md
// (Sub-project 2 -> Capture model).
//
// DECOUPLED BY DESIGN. This module ONLY detects visits and calls an injected
// `emit(event)` sink. It does NOT touch chrome.* / browser.* / network. On
// Safari the captured events are forwarded to the native app (which owns auth +
// ingest); the Safari<->native bridge is a separate, later piece. Keeping the
// sink injected means tests need no real DOM, no real clock, and no transport.
//
// DEBUG INSTRUMENTATION. All transitions go through a single `log` helper
// (off by default; pass `debug: true` to surface them). Every emit/skip is
// logged with the visit's url + accumulated duration so a misbehaving state
// machine is traceable from the page console.

import { scrubUrl } from "../scrub";
import type { AttentionEvent } from "../types";

/** Identifies events captured by this content-script path (vs the Chrome bg
 * `CLIENT`). Kept distinct so server-side / debugging can tell them apart. */
export const SAFARI_CLIENT = "fulcra-attention-safari/0.1.0";

/** How long after a blur a visit can be resumed instead of starting a new one.
 * Mirrors the Chrome background's BLUR_GRACE_MS so the two capture paths share
 * the same visit-coalescing window. */
export const BLUR_GRACE_MS = 30_000;

/** Visits with less than this much accumulated visible time are dropped (not
 * emitted) — avoids noise from instant tab flips / accidental focus. */
export const MIN_VISIT_MS = 1_000;

/**
 * The minimal event-source surface this module needs. `document` and `window`
 * both satisfy it; tests pass a fake. We read visibilityState/title/location
 * lazily (as getters on the live object) rather than snapshotting, so the
 * values reflect the moment an event fires.
 */
export interface VisibilitySource {
  addEventListener(type: string, listener: (ev?: unknown) => void): void;
  removeEventListener(type: string, listener: (ev?: unknown) => void): void;
  readonly visibilityState: string; // "visible" | "hidden" | ...
  readonly title: string;
  readonly location: { href: string };
}

export interface VisibilityCaptureOpts {
  /** Monotonic-ish clock in ms epoch. Defaults to Date.now. Injected so tests
   * drive durations deterministically. */
  now?: () => number;
  /**
   * Event source + page-state reader. Defaults to a composite over the real
   * `document` (visibilityState/title + visibility/pageshow/pagehide events)
   * and `window.location`. Pass a fake in tests.
   */
  source?: VisibilitySource;
  /** The sink. Called once per completed, above-threshold visit. */
  emit: (event: AttentionEvent) => void;
  /** When true, log every transition to console.debug. Default false. */
  debug?: boolean;
}

/** In-flight visit. There is at most one per page at a time. */
interface Visit {
  url: string; // raw location.href captured at visit start (pre-scrub)
  title: string | null; // document.title captured at visit start
  startTime: number; // ms epoch — first became visible
  /** ms epoch the CURRENT visible period began, or null while hidden. */
  visibleSince: number | null;
  /** total visible time from prior (already-ended) visible periods. */
  accumulatedMs: number;
  /** ms epoch of the most recent blur (hidden), or null while visible. */
  blurredAt: number | null;
}

/** Build the real default source: document for visibility/title + events,
 * window.location for href. Throws nothing here; callers in non-DOM contexts
 * must pass their own `source`. */
function defaultSource(): VisibilitySource {
  // Reference globals lazily so importing this module in a non-DOM context
  // (e.g. a unit test that always injects `source`) doesn't explode at import.
  const doc = document;
  return {
    addEventListener: (type, listener) => doc.addEventListener(type, listener as EventListener),
    removeEventListener: (type, listener) =>
      doc.removeEventListener(type, listener as EventListener),
    get visibilityState() {
      return doc.visibilityState;
    },
    get title() {
      return doc.title;
    },
    get location() {
      return window.location;
    },
  };
}

/** Truncate ms to whole seconds and render ISO-8601 with a trailing 'Z',
 * matching the wire transform's `toSecondIsoZ` so capture and transform agree
 * on bounds. */
function toIsoZ(ms: number): string {
  // We DON'T second-truncate here: the wire layer truncates, and keeping
  // millisecond precision on the emitted event preserves sub-second durations
  // for any non-wire consumer. The trailing-Z format is what buildWireRecord
  // parses (Date.parse handles 'Z').
  return new Date(ms).toISOString();
}

/**
 * Wire page-visibility listeners and begin tracking visits. Returns a teardown
 * function that removes all listeners (and flushes nothing — a teardown is a
 * deliberate stop, not a page unload; pagehide is the unload flush).
 */
export function startVisibilityCapture(opts: VisibilityCaptureOpts): () => void {
  const now = opts.now ?? Date.now;
  const source = opts.source ?? defaultSource();
  const emit = opts.emit;
  const debug = opts.debug ?? false;

  const log = (msg: string, extra?: Record<string, unknown>): void => {
    if (debug) console.debug(`[fulcra-attention-safari] ${msg}`, extra ?? {});
  };

  let visit: Visit | null = null;

  /** Total accumulated visible time of `visit` AS OF now, including the
   * current open visible period if any. */
  const accumulatedOf = (v: Visit, at: number): number =>
    v.accumulatedMs + (v.visibleSince != null ? at - v.visibleSince : 0);

  /** Close the current visible period (if open) into accumulatedMs. Idempotent
   * if already hidden. Records blurredAt. */
  const closeVisiblePeriod = (at: number): void => {
    if (!visit) return;
    if (visit.visibleSince != null) {
      visit.accumulatedMs += at - visit.visibleSince;
      visit.visibleSince = null;
    }
    visit.blurredAt = at;
  };

  /** Emit the current visit if it clears the threshold, then clear it. The
   * end_time spans the visit start through the accumulated visible time, so the
   * emitted duration equals the foreground time (NOT wall-clock incl. blurs). */
  const flush = (at: number): void => {
    if (!visit) return;
    const total = accumulatedOf(visit, at);
    if (total < MIN_VISIT_MS) {
      log("skip sub-threshold visit", { url: visit.url, ms: total });
      visit = null;
      return;
    }
    const event: AttentionEvent = {
      url: safeScrub(visit.url),
      title: visit.title,
      og_description: null,
      favicon_url: null,
      category: null, // no category resolution in the content script
      chrome_identity: null,
      og_type: null,
      lang: null,
      start_time: toIsoZ(visit.startTime),
      end_time: toIsoZ(visit.startTime + total),
      client: SAFARI_CLIENT,
    };
    log("emit visit", { url: event.url, ms: total });
    emit(event);
    visit = null;
  };

  /** Scrub defensively — a malformed href must not throw out of an event
   * handler (which on a real page could wedge capture for the page's life). */
  const safeScrub = (raw: string): string | null => {
    try {
      return scrubUrl(raw);
    } catch (err) {
      log("scrubUrl failed; emitting null url", { raw, err: String(err) });
      return null;
    }
  };

  /** Begin a fresh visit at `at`, snapshotting url + title. */
  const begin = (at: number): void => {
    visit = {
      url: source.location.href,
      title: source.title || null,
      startTime: at,
      visibleSince: at,
      accumulatedMs: 0,
      blurredAt: null,
    };
    log("begin visit", { url: visit.url, at });
  };

  /** Page became visible (visibilitychange->visible or pageshow). */
  const onVisible = (): void => {
    const at = now();
    if (!visit) {
      begin(at);
      return;
    }
    if (visit.visibleSince != null) {
      // Already visible (duplicate/idempotent event) — nothing to do.
      return;
    }
    // Resuming from a blur.
    const sinceBlur = visit.blurredAt != null ? at - visit.blurredAt : Infinity;
    if (sinceBlur <= BLUR_GRACE_MS) {
      // Within grace: resume the same visit.
      visit.visibleSince = at;
      visit.blurredAt = null;
      log("resume visit (within grace)", { url: visit.url, sinceBlur });
    } else {
      // Past grace: the prior visit is done; emit it and start a new one.
      log("past-grace resume -> flush + new visit", { sinceBlur });
      flush(at);
      begin(at);
    }
  };

  /** Page became hidden (visibilitychange->hidden). Ends the current visible
   * period but keeps the visit alive for a possible within-grace resume. */
  const onHidden = (): void => {
    if (!visit) return;
    const at = now();
    closeVisiblePeriod(at);
    log("blur visit", { url: visit.url, accumulatedMs: visit.accumulatedMs });
  };

  /** Dispatch a visibilitychange to onVisible/onHidden based on live state. */
  const onVisibilityChange = (): void => {
    if (source.visibilityState === "visible") onVisible();
    else onHidden();
  };

  /** bfcache restore / first paint: treat a visible pageshow as visit start. */
  const onPageShow = (): void => {
    if (source.visibilityState === "visible") onVisible();
  };

  /** Page is unloading: end the visible period and EMIT immediately. iOS
   * suspends the background aggressively, so this opportunistic tail flush is
   * the only reliable chance to capture the last visit. */
  const onPageHide = (): void => {
    if (!visit) return;
    const at = now();
    closeVisiblePeriod(at);
    flush(at);
  };

  source.addEventListener("visibilitychange", onVisibilityChange);
  source.addEventListener("pageshow", onPageShow);
  source.addEventListener("pagehide", onPageHide);

  // If the page is already visible at install time (typical for a content
  // script injected into a foreground tab), seed the first visit now rather
  // than waiting for the next visibilitychange (which may never come).
  if (source.visibilityState === "visible") onVisible();

  log("capture started", { client: SAFARI_CLIENT });

  return () => {
    source.removeEventListener("visibilitychange", onVisibilityChange);
    source.removeEventListener("pageshow", onPageShow);
    source.removeEventListener("pagehide", onPageHide);
    log("capture torn down");
  };
}
