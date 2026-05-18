// chrome/src/background.ts
//
// MV3 service worker. Owns:
//  - chrome.webNavigation.onCommitted + onHistoryStateUpdated subscribers
//  - Per-tab active-visit state machine (open on nav, close on next nav,
//    tab close, or window blur)
//  - Outbox flush on every event and on chrome.alarms ticks

import { scrubUrl } from "./scrub";
import { isIgnored } from "./ignore";
import { categorize } from "./categorize";
import { getChromeIdentity } from "./identity";
import type { PageMeta } from "./content";
import { addToOutbox, flushOutbox } from "./outbox";
import {
  loadActiveVisits, saveActiveVisits, loadSettings,
} from "./storage";
import type { AttentionEvent, ActiveVisit } from "./types";
import { CLIENT } from "./types";

const ALARM_NAME = "fulcra-attention-flush";
const FLUSH_INTERVAL_MIN = 1;

// ---------- helpers ----------

function isHttpScheme(url: string): boolean {
  return url.startsWith("http://") || url.startsWith("https://");
}

function toIsoSecondZ(ms: number): string {
  return new Date(Math.floor(ms / 1000) * 1000).toISOString().replace(".000", "");
}

// ---------- payload builder ----------

interface BuildPayloadInput {
  visit: ActiveVisit;
  category: string | null;
  endTime: number;
  meta: PageMeta;
}

export async function buildPayload(inp: BuildPayloadInput): Promise<AttentionEvent> {
  const identity = await getChromeIdentity();
  const isCategorized = inp.category !== null;
  return {
    url: isCategorized ? null : inp.visit.scrubbedUrl,
    title: isCategorized ? null : inp.meta.title,
    og_description: isCategorized ? null : inp.meta.og_description,
    favicon_url: isCategorized ? null : inp.meta.favicon_url,
    category: inp.category,
    chrome_identity: identity,
    og_type: isCategorized ? null : inp.meta.og_type,
    lang: isCategorized ? null : inp.meta.lang,
    start_time: toIsoSecondZ(inp.visit.startTime),
    end_time: toIsoSecondZ(inp.endTime),
    client: CLIENT,
  };
}

// ---------- page meta fetch ----------

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

// ---------- close + emit ----------

async function closeVisit(tabId: number, endTime: number): Promise<void> {
  const visits = await loadActiveVisits();
  const visit = visits[tabId];
  if (!visit) return;
  delete visits[tabId];
  await saveActiveVisits(visits);

  const category = await categorize(visit.scrubbedUrl);
  const meta = category
    ? { title: null, og_description: null, og_type: null, favicon_url: null, lang: null }
    : await fetchPageMeta(tabId);

  const payload = await buildPayload({ visit, category, endTime, meta });
  await addToOutbox(payload);
  await flushOutbox();
}

// ---------- handlers ----------

export interface NavInput {
  tabId: number;
  url: string;
  frameId: number;
  timeStamp: number;
}

export async function handleNavigation(n: NavInput): Promise<void> {
  if (n.frameId !== 0) return;
  if (!isHttpScheme(n.url)) return;

  const settings = await loadSettings();
  if (!settings.enabled) return;

  const visits = await loadActiveVisits();
  if (visits[n.tabId]) {
    await closeVisit(n.tabId, n.timeStamp);
  }

  if (await isIgnored(n.url)) return;

  const scrubbed = scrubUrl(n.url);
  const cur = await loadActiveVisits();
  cur[n.tabId] = {
    tabId: n.tabId,
    scrubbedUrl: scrubbed,
    startTime: n.timeStamp,
  };
  await saveActiveVisits(cur);
}

export async function handleTabClose(tabId: number): Promise<void> {
  await closeVisit(tabId, Date.now());
}

export async function handleWindowFocusChange(windowId: number): Promise<void> {
  if (windowId !== chrome.windows.WINDOW_ID_NONE) return;
  const visits = await loadActiveVisits();
  const now = Date.now();
  for (const k of Object.keys(visits)) {
    await closeVisit(Number(k), now);
  }
}

// ---------- wire to chrome APIs at SW boot ----------

chrome.webNavigation.onCommitted.addListener((details) => {
  void handleNavigation({
    tabId: details.tabId, url: details.url,
    frameId: details.frameId, timeStamp: details.timeStamp,
  });
});

chrome.webNavigation.onHistoryStateUpdated.addListener((details) => {
  void handleNavigation({
    tabId: details.tabId, url: details.url,
    frameId: details.frameId, timeStamp: details.timeStamp,
  });
});

chrome.tabs.onRemoved.addListener((tabId) => {
  void handleTabClose(tabId);
});

chrome.windows.onFocusChanged.addListener((windowId) => {
  void handleWindowFocusChange(windowId);
});

chrome.runtime.onStartup.addListener(() => {
  void flushOutbox();
});

chrome.alarms.create(ALARM_NAME, { periodInMinutes: FLUSH_INTERVAL_MIN });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) void flushOutbox();
});
