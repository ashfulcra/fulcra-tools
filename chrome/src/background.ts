// chrome/src/background.ts
//
// MV3 service worker. Foreground-only attention model:
//
//   * A visit starts when a tab BECOMES the foreground tab (active in the
//     focused window) and the URL is HTTP(S) and not Tier-3 ignored.
//   * A visit's focus time accumulates while the user is not idle and the
//     tab remains foreground. Background tabs that were never focused
//     produce no event.
//   * Blur (window blur, tab activation switch, idle ≥60s) pauses the
//     visit. Within BLUR_GRACE_MS of pause, returning to the same tab+URL
//     resumes the visit. Past the grace window, the visit is emitted
//     and a fresh visit starts on the next focus.
//   * Navigations on the FOREGROUND tab: emit + start fresh. Navigations
//     on background tabs: ignored (no visit exists there).
//   * Tab close: emit if any visit existed on that tab.
//
// State lives in chrome.storage.session under `visits` (see types.ts).

import { scrubUrl } from "./scrub";
import { isIgnored } from "./ignore";
import { categorize } from "./categorize";
import { getChromeIdentity } from "./identity";
import type { PageMeta } from "./content";
import { addToOutbox, flushOutbox } from "./outbox";
import { loadVisits, saveVisits, loadSettings, saveSettings } from "./storage";
import type { AttentionEvent, Visit, Counts } from "./types";
import { BLUR_GRACE_MS, CLIENT, HEARTBEAT_STALE_MS } from "./types";
import { reconcileHeartbeatOnBoot } from "./heartbeat-control";
import { scrubTitle } from "./title-scrub";

const FLUSH_ALARM = "fulcra-attention-flush";
const SWEEP_ALARM = "fulcra-attention-sweep";
const FLUSH_INTERVAL_MIN = 1;
// Sweep periodically to emit visits whose blur grace window expired.
const SWEEP_INTERVAL_MIN = 1;

// Maximum single continuous "focused" period (ms). Belt-and-braces for
// the case where the SW gets evicted while a visit is open (system
// sleep, browser hibernation, etc.): chrome.idle can't fire while the
// SW is suspended, so a tab that was focused at sleep stays "focused"
// in storage. When the SW wakes up and something eventually freezes
// the visit, the raw delta would be huge. The cap means a 12-hour
// orphan emits as 30 min max, which is still wrong but bounded.
//
// 30 min covers genuine long-form reading without artificially closing
// real sessions; sleep-orphans land at the cap and then get re-opened
// fresh on the next focus signal.
const MAX_FOCUS_PERIOD_MS = 30 * 60 * 1000;

// ---------- single-writer mutex for chrome.storage.session.visits ----------
//
// Every handler that does a load-mutate-save against `visits` (or counts /
// recentEmitted) must run inside this mutex. Chrome service workers are
// single-threaded but the listeners are async — without serialization,
// onCommitted + onActivated + the sweep alarm can interleave their
// load/mutate/save and lose updates. We pay one extra microtask per
// handler and get linearizability across all visit-storage transitions.
//
// Exported handlers themselves stay mutex-free so unit tests can call them
// in any order without artificial sequencing; the chrome.* listener glue
// at the bottom of this file is what acquires the mutex.

let swStorageMutex: Promise<unknown> = Promise.resolve();
export function withSwLock<T>(fn: () => Promise<T>): Promise<T> {
  const next = swStorageMutex.then(fn, fn);
  // Swallow rejections so a failing handler doesn't poison the chain;
  // each task already has its own try/await semantics.
  swStorageMutex = next.catch(() => undefined);
  return next;
}

// ---------- helpers ----------

function isHttpScheme(url: string): boolean {
  return url.startsWith("http://") || url.startsWith("https://");
}

/**
 * Check whether capture is currently paused. `pausedUntil` semantics:
 *   null   → not paused
 *   Number → paused until that ms epoch; values in the past mean the
 *            pause already expired and the SW just hasn't cleared the
 *            field yet (it gets cleared lazily on next sweep). Sentinel
 *            value Number.POSITIVE_INFINITY = pause indefinitely.
 */
function isPaused(
  settings: { pausedUntil: number | null },
  now: number,
): boolean {
  return settings.pausedUntil !== null && now < settings.pausedUntil;
}

function toIsoSecondZ(ms: number): string {
  return new Date(Math.floor(ms / 1000) * 1000).toISOString().replace(".000", "");
}

function focusedDurationMs(v: Visit, now: number): number {
  // Total focused ms = accumulated (prior periods) + current period if still
  // focused. The current period is capped at MAX_FOCUS_PERIOD_MS to handle
  // sleep-orphans (see comment at the constant). While blurred,
  // accumulatedFocusMs already includes the last period (we update it on
  // every focused → blurred transition, also capped there).
  const currentPeriod = v.state === "focused"
    ? Math.min(MAX_FOCUS_PERIOD_MS, Math.max(0, now - v.focusEpoch))
    : 0;
  return v.accumulatedFocusMs + currentPeriod;
}

// ---------- payload + emit ----------

export async function buildPayload(inp: {
  visit: Visit;
  endTime: number;
  meta: PageMeta;
}): Promise<AttentionEvent> {
  const identity = await getChromeIdentity();
  const isCategorized = inp.visit.category !== null;
  // Apply per-host title scrubbing. Gmail subjects, Calendar event
  // names, Slack channels etc. don't belong in the Fulcra attention
  // log — the URL+host is enough.
  let host: string | null = null;
  try { host = new URL(inp.visit.scrubbedUrl).hostname; } catch { /* keep null */ }
  const cleanTitle = scrubTitle(host, inp.meta.title);
  return {
    url: isCategorized ? null : inp.visit.scrubbedUrl,
    title: isCategorized ? null : cleanTitle,
    og_description: isCategorized ? null : inp.meta.og_description,
    favicon_url: isCategorized ? null : inp.meta.favicon_url,
    category: inp.visit.category,
    chrome_identity: identity,
    og_type: isCategorized ? null : inp.meta.og_type,
    lang: isCategorized ? null : inp.meta.lang,
    start_time: toIsoSecondZ(inp.visit.startTime),
    end_time: toIsoSecondZ(inp.endTime),
    client: CLIENT,
  };
}

async function fetchPageMeta(tabId: number): Promise<PageMeta> {
  try {
    const tab = await chrome.tabs.get(tabId);
    const url = tab.url ?? "";
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: (pageUrl: string) => {
        const m = (prop: string) =>
          (document.querySelector(`meta[property="${prop}"]`) as HTMLMetaElement | null)?.content?.trim() || null;
        const linkIcon = document.querySelector('link[rel="icon"], link[rel="shortcut icon"]') as HTMLLinkElement | null;
        const href = linkIcon?.getAttribute("href");
        let favicon: string;
        try {
          favicon = href ? new URL(href, pageUrl).toString() : new URL("/favicon.ico", pageUrl).toString();
        } catch {
          favicon = new URL("/favicon.ico", pageUrl).toString();
        }
        const title = document.title?.trim() || null;
        const lang = document.documentElement.getAttribute("lang");
        return {
          title: title === "" ? null : title,
          og_description: m("og:description"),
          og_type: m("og:type"),
          favicon_url: favicon,
          lang: lang && lang !== "" ? lang : null,
        };
      },
      args: [url],
    });
    return (results[0]?.result as PageMeta) ?? {
      title: tab.title ?? null,
      og_description: null, og_type: null, favicon_url: null, lang: null,
    };
  } catch {
    return { title: null, og_description: null, og_type: null, favicon_url: null, lang: null };
  }
}

async function bumpCounts(category: string | null): Promise<void> {
  const today = new Date().toISOString().slice(0, 10);
  const cRaw = await chrome.storage.local.get("counts");
  const c = (cRaw.counts as Counts | undefined) ?? { date: today, logged: 0, categorized: 0, ignored: 0 };
  const counts = c.date !== today
    ? { date: today, logged: 0, categorized: 0, ignored: 0 }
    : { ...c };
  if (category !== null) counts.categorized += 1;
  else counts.logged += 1;
  await chrome.storage.local.set({ counts });
}

async function bumpIgnoredCount(): Promise<void> {
  const today = new Date().toISOString().slice(0, 10);
  const cRaw = await chrome.storage.local.get("counts");
  const c = (cRaw.counts as Counts | undefined) ?? { date: today, logged: 0, categorized: 0, ignored: 0 };
  const counts = c.date !== today
    ? { date: today, logged: 0, categorized: 0, ignored: 1 }
    : { ...c, ignored: c.ignored + 1 };
  await chrome.storage.local.set({ counts });
}

/**
 * Emit a Visit as a wire event. Caller is responsible for removing it
 * from the visits map.
 */
async function emit(visit: Visit, endTime: number): Promise<void> {
  // Skip zero-duration visits — happens when a tab gets focus then blur in <1s.
  const focusedMs = focusedDurationMs(visit, endTime);
  if (focusedMs < 1000) return;
  // The wire event's start/end represent the focused span. We pin
  // end_time at start + focusedMs so the duration in Fulcra matches
  // ACTUAL focus time, not wall-clock-between-events time.
  const wireEndTime = visit.startTime + focusedMs;
  const meta = visit.category
    ? { title: null, og_description: null, og_type: null, favicon_url: null, lang: null }
    : await fetchPageMeta(visit.tabId);
  const payload = await buildPayload({ visit, endTime: wireEndTime, meta });
  await addToOutbox(payload);
  // Maintain a recent-emitted ring for the popup live stream.
  const r = await chrome.storage.local.get("recentEmitted");
  const recent: AttentionEvent[] = (r.recentEmitted as AttentionEvent[] | undefined) ?? [];
  recent.unshift(payload);
  while (recent.length > 10) recent.pop();
  await chrome.storage.local.set({ recentEmitted: recent });
  await bumpCounts(visit.category);
  await flushOutbox();
}

// ---------- core state transitions ----------

/**
 * Move a visit from focused → blurred. accumulatedFocusMs is bumped by
 * the just-ended focus period. blurredAt records when the blur happened
 * so the sweep can decide when to emit.
 */
function freeze(v: Visit, now: number): Visit {
  if (v.state === "blurred") return v;
  // Apply the same MAX_FOCUS_PERIOD_MS cap as focusedDurationMs so a
  // sleep-orphan visit doesn't bake 12 hours into accumulatedFocusMs
  // when the user finally activates a different tab.
  const period = Math.min(MAX_FOCUS_PERIOD_MS, Math.max(0, now - v.focusEpoch));
  return {
    ...v,
    state: "blurred",
    accumulatedFocusMs: v.accumulatedFocusMs + period,
    blurredAt: now,
  };
}

/**
 * Move a blurred visit back to focused. Resets focusEpoch to `now`.
 */
function thaw(v: Visit, now: number): Visit {
  if (v.state === "focused") return v;
  return { ...v, state: "focused", focusEpoch: now, blurredAt: null };
}

/**
 * Make `tabId` the foreground tab. Freezes any currently-focused visit
 * (different tab), then either resumes the target tab's blurred visit
 * (if within grace) or creates a new visit on it.
 *
 * Returns the updated visit if one was opened, null otherwise (URL
 * filtered out, ignored, settings disabled).
 */
export async function setForegroundTab(tabId: number | null, now: number): Promise<void> {
  const settings = await loadSettings();
  if (!settings.enabled) return;
  if (isPaused(settings, now)) return;
  const visits = await loadVisits();

  // Freeze any currently-focused visit, regardless of tabId. Necessary
  // because chrome can switch focus without us being told via the same
  // event the new tab activated under.
  for (const [k, v] of Object.entries(visits)) {
    if (v.state === "focused" && Number(k) !== tabId) {
      visits[Number(k)] = freeze(v, now);
    }
  }
  if (tabId === null) {
    await saveVisits(visits);
    return;
  }

  let tab: chrome.tabs.Tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch {
    await saveVisits(visits);
    return;
  }
  const url = tab.url ?? "";
  if (!isHttpScheme(url)) {
    await saveVisits(visits);
    return;
  }
  if (await isIgnored(url)) {
    await saveVisits(visits);
    return;
  }
  const scrubbed = scrubUrl(url);
  const category = await categorize(scrubbed);

  const existing = visits[tabId];
  if (existing && existing.scrubbedUrl === scrubbed) {
    if (existing.state === "blurred") {
      // Within grace? Resume. Beyond grace? Sweep should already have
      // emitted it; treat as fresh visit.
      if (existing.blurredAt !== null && now - existing.blurredAt <= BLUR_GRACE_MS) {
        visits[tabId] = thaw(existing, now);
        await saveVisits(visits);
        return;
      }
      // Stale — emit and replace.
      await emit(existing, existing.blurredAt ?? now);
      delete visits[tabId];
    } else {
      // Already focused, same URL — no-op.
      await saveVisits(visits);
      return;
    }
  } else if (existing) {
    // Same tab, different URL. Emit the old visit; start a new one.
    const endTime = existing.state === "blurred" ? (existing.blurredAt ?? now) : now;
    await emit(existing, endTime);
    delete visits[tabId];
  }

  visits[tabId] = {
    tabId,
    windowId: tab.windowId ?? -1,
    url,
    scrubbedUrl: scrubbed,
    category,
    startTime: now,
    state: "focused",
    focusEpoch: now,
    accumulatedFocusMs: 0,
    blurredAt: null,
    // Seeded to `now` so the first sweep cycle doesn't immediately mark
    // a brand-new visit AFK before the content script (if enabled) has
    // had a chance to send its first heartbeat. After this seed, the
    // heartbeat path either keeps it fresh (script enabled) or leaves
    // it at this seed (script disabled — sweep ignores it because
    // settings.heartbeatEnabled is false).
    lastHeartbeat: now,
  };
  await saveVisits(visits);
}

/**
 * Freeze any currently-focused visit. Used by window blur and idle.
 */
export async function blurAll(now: number): Promise<void> {
  const visits = await loadVisits();
  let changed = false;
  for (const [k, v] of Object.entries(visits)) {
    if (v.state === "focused") {
      visits[Number(k)] = freeze(v, now);
      changed = true;
    }
  }
  if (changed) await saveVisits(visits);
}

/**
 * Sweep: emit any blurred visit whose blurredAt is past the grace
 * window. Called from chrome.alarms ticks and on every meaningful
 * state change so stale visits don't sit forever.
 */
export async function sweepStaleBlurred(now: number): Promise<void> {
  const settings = await loadSettings();
  // Clear an expired pause so the next handler invocation actually
  // captures. Pause expiry happens lazily on the next sweep tick.
  if (settings.pausedUntil !== null && now >= settings.pausedUntil) {
    await saveSettings({ ...settings, pausedUntil: null });
  }

  const visits = await loadVisits();
  let changed = false;
  for (const [k, v] of Object.entries(visits)) {
    if (v.state === "blurred" && v.blurredAt !== null && now - v.blurredAt > BLUR_GRACE_MS) {
      await emit(v, v.blurredAt);
      delete visits[Number(k)];
      changed = true;
      continue;
    }
    // Heartbeat-driven AFK detection (only when the user opted in). A
    // focused tab without a recent heartbeat means the page is up but
    // nothing's happening on it. Freeze the visit; the existing blur
    // grace window then handles emit.
    if (settings.heartbeatEnabled
        && v.state === "focused"
        && v.lastHeartbeat !== null
        && now - v.lastHeartbeat > HEARTBEAT_STALE_MS) {
      visits[Number(k)] = freeze(v, v.lastHeartbeat + HEARTBEAT_STALE_MS);
      changed = true;
    }
  }
  if (changed) await saveVisits(visits);
}

// ---------- handlers ----------

export interface NavInput {
  tabId: number;
  url: string;
  frameId: number;
  timeStamp: number;
}

/**
 * Navigation handler. Only affects the FOREGROUND tab — background
 * tab navigations are dropped on the floor (they don't have visits).
 */
export async function handleNavigation(n: NavInput): Promise<void> {
  if (n.frameId !== 0) return;
  if (!isHttpScheme(n.url)) return;

  const settings = await loadSettings();
  if (!settings.enabled) return;
  if (isPaused(settings, n.timeStamp)) return;

  // If the nav target is ignored, count it (so the popup shows the
  // user that ignore-list rules are firing) — but don't open a visit.
  if (await isIgnored(n.url)) {
    await bumpIgnoredCount();
    return;
  }

  // Is this the foreground tab? If yes, emit the old visit (if any)
  // and start a new one. If no, do nothing — the user might never
  // focus this tab.
  const visits = await loadVisits();
  const existing = visits[n.tabId];
  if (existing && existing.state === "focused") {
    // Foreground nav: emit-and-start.
    await emit(existing, n.timeStamp);
    delete visits[n.tabId];
    await saveVisits(visits);
    // Build the new visit by querying the tab (so we capture windowId).
    await setForegroundTab(n.tabId, n.timeStamp);
    return;
  }
  if (existing && existing.state === "blurred") {
    // Same tab, blurred — the tab is in the background and the URL
    // changed without focus. Emit the prior visit; do NOT start a new
    // one (still background).
    await emit(existing, existing.blurredAt ?? n.timeStamp);
    delete visits[n.tabId];
    await saveVisits(visits);
    return;
  }
  // No prior visit on this tab. If it happens to be the foreground
  // tab right now, start a visit. (Chrome doesn't always fire
  // onActivated for tabs that load while already active.)
  try {
    const tab = await chrome.tabs.get(n.tabId);
    if (tab.active) {
      const win = await chrome.windows.get(tab.windowId).catch(() => null);
      if (win?.focused) {
        await setForegroundTab(n.tabId, n.timeStamp);
      }
    }
  } catch {
    // Tab gone; nothing to do.
  }
}

export async function handleTabActivated(tabId: number, now: number): Promise<void> {
  await sweepStaleBlurred(now);
  await setForegroundTab(tabId, now);
}

export async function handleWindowFocusChange(windowId: number, now: number): Promise<void> {
  if (windowId === chrome.windows.WINDOW_ID_NONE) {
    await blurAll(now);
    return;
  }
  // A window got focus — find its active tab and bring it to foreground.
  try {
    const tabs = await chrome.tabs.query({ active: true, windowId });
    const activeId = tabs[0]?.id;
    if (activeId !== undefined) {
      await setForegroundTab(activeId, now);
    } else {
      await blurAll(now);
    }
  } catch {
    await blurAll(now);
  }
}

export async function handleTabClose(tabId: number, now: number): Promise<void> {
  const visits = await loadVisits();
  const v = visits[tabId];
  if (!v) return;
  const endTime = v.state === "focused" ? now : (v.blurredAt ?? now);
  await emit(v, endTime);
  delete visits[tabId];
  await saveVisits(visits);
}

export async function handleIdleStateChanged(
  state: "active" | "idle" | "locked",
  now: number,
): Promise<void> {
  if (state === "active") {
    // User came back. Thaw any blurred visit that's still within
    // grace. We can't tell idle-driven freezes from blur-driven
    // freezes in storage without an extra flag, and the grace
    // window already handles both consistently — anything older
    // gets emitted by the sweep, anything fresher resumes here.
    const visits = await loadVisits();
    let changed = false;
    for (const [k, v] of Object.entries(visits)) {
      if (v.state === "blurred"
          && v.blurredAt !== null
          && now - v.blurredAt <= BLUR_GRACE_MS) {
        visits[Number(k)] = thaw(v, now);
        changed = true;
      }
    }
    if (changed) await saveVisits(visits);
    return;
  }
  // idle | locked → freeze all focused visits as of NOW. The blur
  // grace timer starts now; if the user returns within grace,
  // we resume via the 'active' transition above.
  await blurAll(now);
}

// ---------- wire to chrome APIs at SW boot ----------

chrome.webNavigation.onCommitted.addListener((details) => {
  void withSwLock(() => handleNavigation({
    tabId: details.tabId, url: details.url,
    frameId: details.frameId, timeStamp: details.timeStamp,
  }));
});

chrome.webNavigation.onHistoryStateUpdated.addListener((details) => {
  void withSwLock(() => handleNavigation({
    tabId: details.tabId, url: details.url,
    frameId: details.frameId, timeStamp: details.timeStamp,
  }));
});

chrome.tabs.onActivated.addListener((info) => {
  void withSwLock(() => handleTabActivated(info.tabId, Date.now()));
});

chrome.tabs.onRemoved.addListener((tabId) => {
  void withSwLock(() => handleTabClose(tabId, Date.now()));
});

chrome.windows.onFocusChanged.addListener((windowId) => {
  void withSwLock(() => handleWindowFocusChange(windowId, Date.now()));
});

if (chrome.idle?.onStateChanged) {
  // Default idle threshold is 60 s; bump explicitly to be sure.
  try {
    // 30 s instead of Chrome's 60 s default. Catches the "walked away
     // for a coffee" case sooner. Combined with the existing 30 s
     // blur-grace window, an AFK visit emits at ~1 minute total, not 2.
    chrome.idle.setDetectionInterval?.(30);
  } catch {
    // Stub in tests / older builds.
  }
  chrome.idle.onStateChanged.addListener((state) => {
    void withSwLock(() => handleIdleStateChanged(state, Date.now()));
  });
}

chrome.runtime.onStartup.addListener(() => {
  void flushOutbox();
  void reconcileHeartbeatOnBoot();
  void refreshToolbarIcon();
});

// Reconcile on every SW boot (not just chrome.runtime.onStartup, which
// only fires when Chrome starts — not when the SW is woken from being
// idle). Doing this at module load means a freshly-reactivated SW
// re-registers the heartbeat script if it was previously enabled.
void reconcileHeartbeatOnBoot();
void refreshToolbarIcon();

// ---------- right-click context menu ----------
//
// Two quick actions that previously required a popup round-trip:
//   "Ignore this domain"     → adds the host to the Tier 3 list
//   "Categorize this as…"    → submenu of CATEGORY_VOCAB slugs
// The contextMenus permission is back in the manifest specifically for
// this. We register the menu at SW boot and on install.

const MENU_ROOT = "fulcra-attention-root";
const MENU_IGNORE = "fulcra-attention-ignore";
const MENU_CATEGORY_PREFIX = "fulcra-attention-cat:";

const CATEGORY_VOCAB = [
  "search", "webmail", "ai-chat", "dm", "doc-editor", "reddit-thread",
  "calendar", "banking", "brokerage", "crypto", "tax", "healthcare",
  "password-manager", "mental-health", "dating", "adult", "job-hunting",
];

function ensureContextMenus(): void {
  if (!chrome.contextMenus) return;
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU_ROOT,
      title: "Fulcra Attention",
      contexts: ["page"],
    });
    chrome.contextMenus.create({
      id: MENU_IGNORE,
      parentId: MENU_ROOT,
      title: "Ignore this domain",
      contexts: ["page"],
    });
    for (const slug of CATEGORY_VOCAB) {
      chrome.contextMenus.create({
        id: MENU_CATEGORY_PREFIX + slug,
        parentId: MENU_ROOT,
        title: `Categorize as: ${slug}`,
        contexts: ["page"],
      });
    }
  });
}

if (chrome.contextMenus) {
  chrome.contextMenus.onClicked.addListener(async (info, tab) => {
    if (!info.menuItemId || !tab?.url) return;
    let host: string;
    try { host = new URL(tab.url).hostname; } catch { return; }
    if (info.menuItemId === MENU_IGNORE) {
      const { loadIgnoreList, saveIgnoreList } = await import("./storage");
      const list = await loadIgnoreList();
      if (!list.some((e) => e.pattern === host)) {
        await saveIgnoreList([
          ...list,
          { pattern: host, addedAt: new Date().toISOString() },
        ]);
      }
      return;
    }
    const id = String(info.menuItemId);
    if (id.startsWith(MENU_CATEGORY_PREFIX)) {
      const slug = id.slice(MENU_CATEGORY_PREFIX.length);
      const { loadCategoryMap, saveCategoryMap } = await import("./storage");
      const map = await loadCategoryMap();
      const existing = map.findIndex((m) => m.pattern === host);
      if (existing >= 0) map[existing] = { pattern: host, category: slug };
      else map.push({ pattern: host, category: slug });
      await saveCategoryMap(map);
    }
  });
}

// ---------- toolbar icon state machine ----------
//
// Three visual states surface different operational realities:
//   active    → default mark, full color (icons/icon-*.png)
//   paused    → desaturated; clear "I'm not capturing"
//   error     → red overlay; relay unreachable / 401 / etc.
//
// We re-evaluate on every storage change + on each handler tick.

type IconState = "active" | "paused" | "error";

async function currentIconState(): Promise<IconState> {
  const settings = await loadSettings();
  if (!settings.enabled) return "paused";
  if (isPaused(settings, Date.now())) return "paused";
  const r = await chrome.storage.local.get("lastIngestError");
  if (r.lastIngestError) return "error";
  return "active";
}

export async function refreshToolbarIcon(): Promise<void> {
  const state = await currentIconState();
  const path = {
    16:  `icons/icon-${state}-16.png`,
    32:  `icons/icon-${state}-32.png`,
    48:  `icons/icon-${state}-48.png`,
    128: `icons/icon-${state}-128.png`,
  };
  try {
    await chrome.action.setIcon({ path });
    await chrome.action.setTitle({
      title: state === "active" ? "Fulcra Attention"
           : state === "paused" ? "Fulcra Attention — paused"
           : "Fulcra Attention — error (relay unreachable?)",
    });
  } catch {
    // setIcon throws if a variant file is missing. Fall back to the
    // default manifest icon by clearing the override.
    try { await chrome.action.setIcon({ path: "icons/icon-active-32.png" }); } catch { /* ignore */ }
  }
}

chrome.storage.onChanged.addListener(() => {
  void refreshToolbarIcon();
});

// ---------- heartbeat ingest ----------
//
// The optional heartbeat content script (registered dynamically when the
// user opts in) posts {kind:"heartbeat", t} for every input event on its
// page, debounced to ~5 s. The sender.tab.id tells us which tab. We just
// update visits[tabId].lastHeartbeat — the sweep does the AFK decision.
//
// The message is intentionally tiny: NO url, NO selection, NO page text.
// The framing is "did something happen on this tab" — that's it.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.kind !== "heartbeat") return false;
  const tabId = sender.tab?.id;
  if (tabId === undefined) {
    sendResponse({ ok: false });
    return false;
  }
  void withSwLock(async () => {
    const visits = await loadVisits();
    const v = visits[tabId];
    if (v) {
      visits[tabId] = { ...v, lastHeartbeat: Date.now() };
      await saveVisits(visits);
    }
    sendResponse({ ok: true });
  });
  return true;  // tells Chrome we'll call sendResponse asynchronously
});

// Auto-open the onboarding wizard on first install. Skipped on
// update/upgrade so existing users don't get spammed with a tab on
// every refresh.
chrome.runtime.onInstalled.addListener((details) => {
  ensureContextMenus();
  if (details.reason === "install") {
    void chrome.tabs.create({ url: chrome.runtime.getURL("wizard.html") });
  }
});

chrome.alarms.create(FLUSH_ALARM, { periodInMinutes: FLUSH_INTERVAL_MIN });
chrome.alarms.create(SWEEP_ALARM, { periodInMinutes: SWEEP_INTERVAL_MIN });
chrome.alarms.onAlarm.addListener((alarm) => {
  // flushOutbox doesn't touch visits, only its own outbox key; safe
  // outside the mutex. sweepStaleBlurred mutates visits → must be locked.
  if (alarm.name === FLUSH_ALARM) void flushOutbox();
  if (alarm.name === SWEEP_ALARM) void withSwLock(() => sweepStaleBlurred(Date.now()));
});
