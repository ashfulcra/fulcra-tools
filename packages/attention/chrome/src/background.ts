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

import {handleV1AuthCommand,workerAuthMode,saveWorkerSettings,loadWorkerSettings as loadSettings,initializeWorkerAuth,readWorkerCaptureDescriptor,enqueueWorkerCapture,workerCaptureEpoch} from "./relayless/v1AuthWorker";
import { scrubUrl,DENYLIST } from "./scrub";
import { isIgnored } from "./ignore";
import { categorize } from "./categorize";
import { getChromeIdentity } from "./identity";
import { extractPageMeta, type PageMeta } from "./content";
import { addToOutbox, flushOutbox } from "./outbox";
import { loadVisits, saveVisits as saveSessionVisits } from "./storage";
import {V1CaptureIngress} from "./relayless/v1CaptureIngress";
import {snapshotV1JSON} from "./relayless/v1Http";
import {sha256Hex} from "./relayless/sha256";
import type { AttentionEvent, Visit, Counts } from "./types";
import { BLUR_GRACE_MS, CLIENT, HEARTBEAT_STALE_MS } from "./types";
import { reconcileHeartbeatOnBoot } from "./heartbeat-control";
import { scrubTitle } from "./title-scrub";
import { mergeDefaults } from "./default-categories";
import { registerAuth0OriginStrip } from "./relayless/authOriginFix";

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

const captureWorkerId=crypto.randomUUID();
const ingressFactory=globalThis.indexedDB;
const ingress=()=>new V1CaptureIngress(ingressFactory);
let captureHealth:'pending'|'complete'|'unknown'='pending';
const captureReady=workerAuthMode().then(async mode=>{
 if(mode!=='v1-held')return;
 for(const row of await ingress().rows())if(row.state==='active')await ingress().hold(row.id,'restart-gap-unknown');
});
void captureReady.catch(()=>{captureHealth='unknown';console.warn('[attention] capture retention UNKNOWN; preserve active input');});
function identifyVisit(v:Visit):void{
 if(v.captureId)return;
 v.captureId=`legacy-visit:${v.tabId}:${v.windowId}:${v.startTime}`;
 v.captureDescriptor=null;v.captureIdentity=null;v.captureWorker=null;
}
function visitRaw(v:Visit,now:number){
 return {kind:'visit',url:v.category===null?v.scrubbedUrl:null,category:v.category,tabId:v.tabId,windowId:v.windowId,
  startTime:v.startTime,state:v.state,focusEpoch:v.focusEpoch,accumulatedFocusMs:v.accumulatedFocusMs,
  observedFocusMs:(v.captureObservedFocusMs??v.accumulatedFocusMs)+(v.state==='focused'?Math.max(0,now-v.focusEpoch):0),
  continuityUnknown:v.captureContinuityUnknown??false,captureConsentEpoch:v.captureConsentEpoch??null,
  blurredAt:v.blurredAt,lastHeartbeat:v.lastHeartbeat,checkpointAt:now,focusIntervals:v.captureIntervals??[],
  currentInterval:v.state==='focused'?[v.focusEpoch,now]:null};
}
async function checkpointVisit(v:Visit,now:number){
 await captureReady;identifyVisit(v);
 const raw=visitRaw(v,now),store=ingress(),original=await store.read(v.captureId!);
 if(original&&original.state!=='active'&&JSON.stringify(original.raw)!==JSON.stringify(raw)){
  // A surviving session can contain observations newer than a restart/failed
  // cleanup seal. Preserve them as unowned evidence; never rewrite that seal
  // or pretend the uncertain gap constitutes another Attention event.
  const id=v.captureId!+':late:'+await sha256Hex(JSON.stringify(raw));
  await store.checkpointPortion({id,origin:'visit',linkage:{store:'session.visits',parent:v.captureId!,observationOnly:true},descriptor:null,sourceContext:original.sourceContext??null,raw});
  await store.sealPortion(id,null,'late-observation-held');return original;
 }
 return store.checkpointPortion({id:v.captureId!,origin:'visit',linkage:{store:'session.visits',tabId:v.tabId,startTime:v.startTime},descriptor:v.captureDescriptor??null,sourceContext:v.captureId!.startsWith('visit:')&&Object.hasOwn(v,'captureIdentity')?{version:1,client:CLIENT,identityLabel:v.captureIdentity??null}:null,raw});
}
async function saveVisits(visits:Record<number,Visit>,now:number):Promise<void>{
 if(await workerAuthMode()!=='v1-held'){await saveSessionVisits(visits);return;}
 for(const v of Object.values(visits))identifyVisit(v);
 // Session recovery precedes a potentially refused durable write. Cleanup is
 // performed only after emit's durable seal; never delete on a failed receipt.
 await saveSessionVisits(visits);
 try{for(const v of Object.values(visits))await checkpointVisit(v,now);}catch(error){captureHealth='unknown';throw error;}
}
const emptyMeta=():PageMeta=>({title:null,og_description:null,og_type:null,favicon_url:null,lang:null});
async function emitHeld(visit:Visit,endTime:number):Promise<void>{
 identifyVisit(visit);
 const epoch=visit.captureConsentEpoch,currentLifetime=()=>epoch!==undefined&&epoch===workerCaptureEpoch();
 // Retain active evidence even if settings/auth cannot currently be read.
 const retained=await checkpointVisit(visit,endTime);if(retained.state!=='active')return;
 if(!retained.sourceContext){await ingress().sealPortion(visit.captureId!,null,'capture-context-unknown');return;}
 let settings;try{settings=await loadSettings();}catch{await ingress().sealPortion(visit.captureId!,null,'consent-unknown');return;}
 if(!settings.enabled||isPaused(settings,endTime)){await ingress().sealPortion(visit.captureId!,null,'consent-closed');return;}
 if(visit.captureWorker!==captureWorkerId||visit.captureContinuityUnknown||visit.state==='focused'&&endTime-visit.focusEpoch>MAX_FOCUS_PERIOD_MS){
  await ingress().sealPortion(visit.captureId!,null,'continuity-unknown');return;
 }
 if(visit.captureConsentEpoch!==workerCaptureEpoch()){await ingress().sealPortion(visit.captureId!,null,'capture-scope-changed');return;}
 if(await isIgnored(visit.url)){await ingress().sealPortion(visit.captureId!,null,'ignored-now');return;}
 const focusedMs=focusedDurationMs(visit,endTime);
 if(focusedMs<1000){await ingress().sealPortion(visit.captureId!,null,'below-threshold');return;}
 let meta=emptyMeta();
 // Navigation may already have replaced this tab. Do not enrich the original
 // URL with a different page, or fetch metadata after consent/ignore changed.
 if(visit.category===null){
  const tab=await chrome.tabs.get(visit.tabId).catch(()=>null);
  if(tab?.url===visit.url){
   const current=await loadSettings();
   if(current.enabled&&!isPaused(current,endTime)&&!await isIgnored(visit.url))meta=await fetchPageMeta(visit.tabId,visit.url,epoch);
  }
 }
 const categorized=visit.category!==null;
 let host:string|null=null;try{host=new URL(visit.scrubbedUrl).hostname;}catch{/* no host */}
 const payload:AttentionEvent={url:categorized?null:visit.scrubbedUrl,title:categorized?null:scrubTitle(host,meta.title),
  og_description:categorized?null:meta.og_description,favicon_url:categorized?null:meta.favicon_url,category:visit.category,
  chrome_identity:retained.sourceContext.identityLabel,og_type:categorized?null:meta.og_type,lang:categorized?null:meta.lang,
  start_time:new Date(visit.startTime).toISOString(),end_time:new Date(visit.startTime+focusedMs).toISOString(),client:retained.sourceContext.client};
 if(visit.captureConsentEpoch!==workerCaptureEpoch()){await ingress().sealPortion(visit.captureId!,null,'capture-scope-changed');return;}
 await ingress().sealPortion(visit.captureId!,payload,visit.captureDescriptor?'descriptor-held':'unattributed',currentLifetime);
 await enqueueWorkerCapture(visit.captureId!);
}

// ---------- helpers ----------

function capturedUrlIsPrivate(url:string):boolean{
 try{const u=new URL(url);return !isHttpScheme(url)||!!u.hash||!!u.username||!!u.password||[...u.searchParams.keys()].some(k=>DENYLIST.has(k.toLowerCase()));}catch{return true;}
}
function isHttpScheme(url: string): boolean {
  return url.startsWith("http://") || url.startsWith("https://");
}

/**
 * Check whether capture is currently paused. `pausedUntil` semantics:
 *   null   → not paused
 *   Number → paused until that ms epoch; values in the past mean the
 *            pause already expired and the SW just hasn't cleared the
 *            field yet (it gets cleared lazily on next sweep). Sentinel
 *            value Number.MAX_SAFE_INTEGER = pause indefinitely.
 *            (POSITIVE_INFINITY would JSON-roundtrip to null via
 *            chrome.storage and break the gate.)
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

async function fetchPageMeta(tabId: number,expectedDocumentUrl?:string,captureEpoch?:string): Promise<PageMeta> {
  try {
    const tab = await chrome.tabs.get(tabId);
    const url = tab.url ?? "";
    if(expectedDocumentUrl!==undefined&&url!==expectedDocumentUrl)return emptyMeta();
    if(captureEpoch!==undefined&&captureEpoch!==workerCaptureEpoch())return emptyMeta();
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      // extractPageMeta is self-contained (see content.ts): executeScript
      // serializes it to source and runs it in the page, reading the page's
      // global `document`. The unit test in tests/content.test.ts exercises
      // this exact function, so this injection path can't drift untested.
      func: extractPageMeta,
      args: expectedDocumentUrl===undefined?[url]:[url,expectedDocumentUrl],
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
  if(await workerAuthMode()==='v1-held'){await emitHeld(visit,endTime);return;}
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
    ...(v.captureId?{captureIntervals:[...(v.captureIntervals??[]),[v.focusEpoch,now] as [number,number]],captureObservedFocusMs:(v.captureObservedFocusMs??v.accumulatedFocusMs)+Math.max(0,now-v.focusEpoch),captureContinuityUnknown:v.captureContinuityUnknown||now-v.focusEpoch>MAX_FOCUS_PERIOD_MS}:{}),
    blurredAt: now,
  };
}

/**
 * Move a blurred visit back to focused. Resets focusEpoch to `now`.
 *
 * Also bumps `lastHeartbeat` to `now`. Otherwise: a visit that was
 * blurred at 25s of staleness, then resumed via tab return within
 * grace, would have a stale lastHeartbeat (still 25s+ old). The very
 * next sweep tick (≤60s) would see `now - lastHeartbeat > HEARTBEAT_STALE_MS`
 * for a "focused" visit and immediately re-freeze it with a
 * truncated duration. Resetting on thaw makes the heartbeat sweep
 * behave consistently with the underlying user-presence signal.
 */
function thaw(v: Visit, now: number): Visit {
  if (v.state === "focused") return v;
  return { ...v, state: "focused", focusEpoch: now, blurredAt: null, lastHeartbeat: now };
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
    await saveVisits(visits, now);
    return;
  }

  let tab: chrome.tabs.Tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch {
    await saveVisits(visits, now);
    return;
  }
  const url = tab.url ?? "";
  if (!isHttpScheme(url)) {
    await saveVisits(visits, now);
    return;
  }
  if (await isIgnored(url)) {
    await saveVisits(visits, now);
    return;
  }
  const scrubbed = scrubUrl(url);
  const category = await categorize(scrubbed);

  const heldMode=await workerAuthMode()==='v1-held';
  if(heldMode){const current=await loadSettings();if(!current.enabled||isPaused(current,now)){await saveVisits(visits,now);return;}}
  const existing = visits[tabId];
  if(existing&&await workerAuthMode()==='v1-held'&&(existing.captureWorker!==captureWorkerId||existing.captureConsentEpoch!==workerCaptureEpoch())){await emit(existing,existing.blurredAt??now);delete visits[tabId];}
  if (visits[tabId] && existing && existing.scrubbedUrl === scrubbed) {
    if (existing.state === "blurred") {
      // Within grace? Resume. Beyond grace? Sweep should already have
      // emitted it; treat as fresh visit.
      if (existing.blurredAt !== null && now - existing.blurredAt <= BLUR_GRACE_MS) {
        visits[tabId] = thaw(existing, now);
        await saveVisits(visits, now);
        return;
      }
      // Stale — emit and replace.
      await emit(existing, existing.blurredAt ?? now);
      delete visits[tabId];
    } else {
      // Already focused, same URL — no-op.
      await saveVisits(visits, now);
      return;
    }
  } else if (visits[tabId] && existing) {
    // Same tab, different URL. Emit the old visit; start a new one.
    const endTime = existing.state === "blurred" ? (existing.blurredAt ?? now) : now;
    await emit(existing, endTime);
    delete visits[tabId];
  }

  const held=await workerAuthMode()==='v1-held';
  const descriptor=held?await readWorkerCaptureDescriptor():null;
  const captureSettings=held?await loadSettings():settings;
  if(held&&(!captureSettings.enabled||isPaused(captureSettings,now))){await saveVisits(visits,now);return;}
  visits[tabId] = {
    ...(held?{captureId:`visit:${crypto.randomUUID()}:0`,captureDescriptor:descriptor,captureIdentity:captureSettings.identityLabel?.trim()||null,captureWorker:captureWorkerId,captureConsentEpoch:workerCaptureEpoch(),captureIntervals:[],captureObservedFocusMs:0,captureContinuityUnknown:false}:{}),
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
  await saveVisits(visits, now);
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
  if (changed) await saveVisits(visits, now);
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
    await saveWorkerSettings({ ...settings, pausedUntil: null });
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
  if (changed || await workerAuthMode()==='v1-held') await saveVisits(visits, now);
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

  // Ignored targets never open visits. Legacy mode counts the rule hit;
  // held mode preserves original legacy counters while sealing prior input.
  if (await isIgnored(n.url)) {
    if(await workerAuthMode()==='v1-held'){const visits=await loadVisits();const v=visits[n.tabId];if(v){await emit(v,n.timeStamp);delete visits[n.tabId];await saveVisits(visits,n.timeStamp);}}
    else await bumpIgnoredCount();
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
    await saveVisits(visits, n.timeStamp);
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
    await saveVisits(visits, n.timeStamp);
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
  await saveVisits(visits, now);
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
    if (changed) await saveVisits(visits, now);
    return;
  }
  // idle | locked → freeze all focused visits as of NOW. The blur
  // grace timer starts now; if the user returns within grace,
  // we resume via the 'active' transition above.
  await blurAll(now);
}


/** Observe originals without deleting/rewriting legacy stores or guessing owner. */
function legacyProjection(value:unknown):AttentionEvent|null{
 if(!value||typeof value!=='object'||Array.isArray(value))return null;
 const p=value as AttentionEvent;
 if(Object.keys(p).sort().join(',')!=='category,chrome_identity,client,end_time,favicon_url,lang,og_description,og_type,start_time,title,url')return null;
 if(!['category','chrome_identity','favicon_url','lang','og_description','og_type','title','url'].every(k=>p[k as keyof AttentionEvent]===null||typeof p[k as keyof AttentionEvent]==='string'))return null;
 if(typeof p.client!=='string'||typeof p.start_time!=='string'||typeof p.end_time!=='string')return null;
 if(p.category!==null){if(p.url!==null||p.title!==null||p.og_description!==null||p.favicon_url!==null||p.og_type!==null||p.lang!==null)return null;}
 else if(typeof p.url!=='string'||capturedUrlIsPrivate(p.url))return null;
 return p;
}
export async function checkpointLegacyArrivals(observed:readonly unknown[]=[]):Promise<void>{
 // Detach supplied oldValue/newValue before the first await; the durable
 // current key can change again before this observer gets scheduled.
 const incoming=observed.map(row=>snapshotV1JSON(row,1024*1024));
 if(await workerAuthMode()!=='v1-held')return;
 await captureReady;
 const legacy=await chrome.storage.local.get('outbox');
 if(legacy.outbox!==undefined&&!Array.isArray(legacy.outbox))throw new Error('Attention capture retention UNKNOWN; legacy listing unreadable');
 const current=new Set<string>(),all=new Map<string,unknown>();
 for(const original of legacy.outbox??[]){const captured=snapshotV1JSON(original,1024*1024),hash=await sha256Hex(JSON.stringify(captured));current.add(hash);all.set(hash,captured);}
 for(const original of incoming)all.set(await sha256Hex(JSON.stringify(original)),original);
 let incomplete=false;
 for(const [hash,original] of all){
  const row=original as {id?:unknown;payload?:unknown},payload=legacyProjection(row?.payload);
  if(!payload&&!current.has(hash)){incomplete=true;captureHealth='unknown';}
  const id='legacy:'+hash;
  await ingress().checkpointPortion({id,origin:'legacy',linkage:{store:'local.outbox',objectHash:hash,originalIdHash:await sha256Hex(JSON.stringify(row?.id??null))},descriptor:null,
   raw:payload?{kind:'legacy-projection',payload,objectHash:hash,projection:'existing-attention-fields'}:{kind:'legacy-link',objectHash:hash,retention:'original-store-only'}});
  if(!payload&&!current.has(hash))await ingress().noteLegacyLoss(id);
  else await ingress().sealPortion(id,null,'legacy-owner-unproved');
 }
 // A prior loss-note transaction may have failed. Recheck durable link-only
 // evidence against this observed original-store snapshot on every sweep, so
 // a quiet pass/restart retries the fact rather than forgetting the failure.
 for(const retained of await ingress().rows())if(retained.origin==='legacy'&&retained.raw.kind==='legacy-link'&&!current.has(retained.raw.objectHash as string)){
  incomplete=true;captureHealth='unknown';await ingress().noteLegacyLoss(retained.id);
 }
 captureHealth=incomplete?'unknown':'complete';
}
export async function handleCaptureCommand(message:unknown):Promise<unknown>{
 const epoch=workerCaptureEpoch(),currentLifetime=()=>epoch===workerCaptureEpoch();
 try{
  if(await workerAuthMode()!=='v1-held')throw new Error('MODE_MISMATCH');
  if(!message||typeof message!=='object'||Array.isArray(message))throw new Error('PROTOCOL_MISMATCH');
  const m=message as Record<string,unknown>;
  if(Object.keys(m).sort().join(',')!=='command,payload,type,version'||m.type!=='attention-capture-v1'||m.version!==1)throw new Error('PROTOCOL_MISMATCH');
  if(m.command==='status'&&m.payload===null){const status=await ingress().status();return {version:1,ok:true,result:{...status,legacyObservation:status.legacyRetentionUnknown>0?'unknown':captureHealth}};}
  if(m.command!=='backfill'||!m.payload||typeof m.payload!=='object')throw new Error('PROTOCOL_MISMATCH');
  const p=m.payload as {id:string;raw:Record<string,unknown>;matchingUrl:string};
  if(Object.keys(p).sort().join(',')!=='id,matchingUrl,raw'||typeof p.id!=='string'||!/^backfill:[a-f0-9]{64}$/.test(p.id)||typeof p.matchingUrl!=='string'||!isHttpScheme(p.matchingUrl)||capturedUrlIsPrivate(p.matchingUrl))throw new Error('PROTOCOL_MISMATCH');
  const raw=p.raw;
  if(!raw||Object.keys(raw).sort().join(',')!=='category,endTime,kind,startTime,syntheticDurationMs,title,url'||raw.kind!=='backfill'||!Number.isFinite(raw.startTime)||!Number.isFinite(raw.endTime)||raw.syntheticDurationMs!==60000
   ||(raw.endTime as number)-(raw.startTime as number)!==60000||!(raw.category===null||typeof raw.category==='string')||!(raw.title===null||typeof raw.title==='string')
   ||(raw.category!==null?(raw.url!==null||raw.title!==null):(raw.url!==p.matchingUrl)))throw new Error('PROTOCOL_MISMATCH');
  const settings=await loadSettings();if(!settings.enabled||isPaused(settings,Date.now()))throw new Error('CONSENT_CLOSED');
  if(await isIgnored(p.matchingUrl))throw new Error('IGNORED_NOW');
  const category=await categorize(p.matchingUrl);
  if(category!==raw.category)throw new Error('CATEGORY_CHANGED');
  // Canonical source key is captured by the existing page scrub boundary;
  // no second normalization is allowed here. Worker rechecks current consent.
  let consentCurrent=false;try{const current=await loadSettings();consentCurrent=current.enabled&&!isPaused(current,Date.now());}catch{/* unknown retains original raw, never admits preparation */}
  const originalLifetime=()=>consentCurrent&&currentLifetime();
  const descriptor=originalLifetime()?await readWorkerCaptureDescriptor():null;
  await captureReady;
  const existing=await ingress().read(p.id);
  const sourceContext={version:1 as const,client:'fulcra-attention-chrome-backfill/0.1.0',identityLabel:settings.identityLabel};
  if(existing&&JSON.stringify(existing.raw)!==JSON.stringify(raw)){
   const conflictId=p.id+':input:'+await sha256Hex(JSON.stringify(raw));
   await ingress().checkpointPortion({id:conflictId,origin:'backfill',linkage:{kind:'history-selection-conflict',parent:p.id},descriptor:null,sourceContext,raw});
   const sealed=await ingress().sealPortion(conflictId,null,'ingress-input-conflict',originalLifetime);
   if(!originalLifetime()||sealed.reason==='capture-scope-changed')return {version:1,ok:false,code:'CAPTURE_SCOPE_CHANGED',retained:{id:conflictId,durable:true,status:'held'}};
   return {version:1,ok:true,receipt:{id:p.id,checkpointId:conflictId,durable:true,status:'held',uploaded:false,drains:'disabled'}};
  }
  const prior=await ingress().checkpointPortion({id:p.id,origin:'backfill',linkage:{kind:'history-selection',rowId:p.id},descriptor:existing?existing.descriptor:descriptor,sourceContext,raw});
  if(prior.state==='active'){
   if(!prior.sourceContext){await ingress().sealPortion(p.id,null,'capture-context-unknown');return {version:1,ok:false,code:'CAPTURE_CONTEXT_UNKNOWN',retained:{id:p.id,durable:true,status:'held'}};}
   const event:AttentionEvent={url:raw.url as string|null,title:raw.title as string|null,category:raw.category as string|null,
    og_description:null,favicon_url:null,og_type:null,lang:null,chrome_identity:prior.sourceContext.identityLabel,
    start_time:new Date(raw.startTime as number).toISOString(),end_time:new Date(raw.endTime as number).toISOString(),client:prior.sourceContext.client};
   const sealed=await ingress().sealPortion(p.id,event,prior.descriptor?'descriptor-held':'unattributed',originalLifetime);
   if(sealed.reason==='capture-scope-changed')return {version:1,ok:false,code:'CAPTURE_SCOPE_CHANGED',retained:{id:p.id,durable:true,status:'held'}};
  }
  if(!originalLifetime())return {version:1,ok:false,code:'CAPTURE_SCOPE_CHANGED',retained:{id:p.id,durable:true,status:'held'}};
  await enqueueWorkerCapture(p.id);
  if(!originalLifetime())return {version:1,ok:false,code:'CAPTURE_SCOPE_CHANGED',retained:{id:p.id,durable:true,status:'held'}};
  return {version:1,ok:true,receipt:{id:p.id,durable:true,status:'held',uploaded:false,drains:'disabled'}};
 }catch{return {version:1,ok:false,code:'CAPTURE_RETENTION_UNKNOWN'};}
}
chrome.runtime.onMessage.addListener((message,sender,sendResponse)=>{
 if(message?.type!=='attention-capture-v1')return false;
 if(sender.url!==chrome.runtime.getURL('wizard.html')&&sender.url!==chrome.runtime.getURL('index.html')&&sender.url!==chrome.runtime.getURL('options.html')){sendResponse({version:1,ok:false,code:'SENDER_REFUSED'});return false;}
 void withSwLock(()=>handleCaptureCommand(message)).then(sendResponse);return true;
});

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
  void withSwLock(() => reconcileHeartbeatOnBoot(saveWorkerSettings,loadSettings));
  void refreshToolbarIcon();
});

// Reconcile on every SW boot (not just chrome.runtime.onStartup, which
// only fires when Chrome starts — not when the SW is woken from being
// idle). Doing this at module load means a freshly-reactivated SW
// re-registers the heartbeat script if it was previously enabled.
// Both paths go through the SW lock so a user-driven popup toggle
// firing concurrently can't race the boot-time registration.
void withSwLock(() => reconcileHeartbeatOnBoot(saveWorkerSettings,loadSettings));
void refreshToolbarIcon();

// Strip the chrome-extension Origin header off the extension's own POSTs to
// the Auth0 host. Without this, the relayless device-flow sign-in 403s at
// the very first call (Auth0 "Allowed Web Origins" check). Registered at SW
// boot so the rule is in place before the user ever clicks sign-in.
void registerAuth0OriginStrip();

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
//   error     → red overlay; Fulcra API unreachable / 401 / etc.
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
  let state:IconState;try{state=await currentIconState();}catch{state="error";}
  let held=false;try{held=await workerAuthMode()==='v1-held';}catch{state="error";}
  const path = {
    16:  `icons/icon-${state}-16.png`,
    32:  `icons/icon-${state}-32.png`,
    48:  `icons/icon-${state}-48.png`,
    128: `icons/icon-${state}-128.png`,
  };
  try {
    await chrome.action.setIcon({ path });
    await chrome.action.setTitle({
      title: held ? (captureHealth==='unknown'?"Fulcra Attention — retention UNKNOWN; both drains disabled":"Fulcra Attention — held locally; both drains disabled")
           : state === "active" ? "Fulcra Attention"
           : state === "paused" ? "Fulcra Attention — paused"
           : "Fulcra Attention — error (Fulcra API unreachable?)",
    });
  } catch {
    // setIcon throws if a variant file is missing. Fall back to the
    // default manifest icon by clearing the override.
    try { await chrome.action.setIcon({ path: "icons/icon-active-32.png" }); } catch { /* ignore */ }
  }
}

chrome.storage.onChanged.addListener((changes,area) => {
  if(area==='local'&&changes.outbox)void checkpointLegacyArrivals([...(changes.outbox.oldValue??[]),...(changes.outbox.newValue??[])]).catch(()=>{captureHealth='unknown';console.warn('[attention] legacy observation UNKNOWN; originals preserved');});
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
// Auth messages are accepted only in the versioned worker protocol. Old UI
// auth/setup helpers fail closed in held mode; raw capture migration is Slice B.
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if(message?.type!=="attention-auth-v1")return false;
  const pages=["index.html","wizard.html","options.html"].map(page=>chrome.runtime.getURL(page));
  if(!sender.url||!pages.includes(sender.url)){sendResponse({version:1,ok:false,code:"SENDER_REFUSED"});return false;}
  void handleV1AuthCommand(message).then(sendResponse);return true;
});
// Initialize the immutable worker mode before any drain admission. A malformed
// mode fails closed through every flush; it never falls back to legacy.
void workerAuthMode().then(()=>initializeWorkerAuth()).catch(()=>{});
void captureReady.then(()=>checkpointLegacyArrivals()).catch(()=>{captureHealth='unknown';console.warn('[attention] ingress startup UNKNOWN; originals preserved');});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.kind !== "heartbeat") return false;
  const tabId = sender.tab?.id;
  if (tabId === undefined) {
    sendResponse({ ok: false });
    return false;
  }
  void withSwLock(async () => {
    // Re-check the gate INSIDE the mutex. If the user revoked the host
    // permission while a heartbeat was in flight, the content script
    // is unregistered but already-fired messages can still arrive.
    // Drop those without mutating storage so the disabled state stays
    // clean.
    const settings = await loadSettings();
    if (!settings.heartbeatEnabled) {
      sendResponse({ ok: false });
      return;
    }
    const visits = await loadVisits();
    const v = visits[tabId];
    if (v) {
      visits[tabId] = { ...v, lastHeartbeat: Date.now() };
      await saveVisits(visits, Date.now());
    }
    sendResponse({ ok: true });
  });
  return true;  // tells Chrome we'll call sendResponse asynchronously
});

// Bug A3: the service worker is the ONE context allowed to drain the outbox.
// Page contexts (popup SignIn/Banner, wizard backfill) ask us to flush via
// requestFlush() → { type: "flushOutbox" }. Running flushOutbox() HERE keeps
// every real flush in this single context, where outbox.ts's module-scope
// single-flight guard can actually serialize against the alarm-driven flush.
// (The alarm handler still calls flushOutbox() directly — same context.)
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type !== "flushOutbox") return false;
  // Keep the message channel open until the flush settles (async handler).
  flushOutbox()
    .catch((e) => {
      console.error("[background] flushOutbox: requested flush failed", e);
    })
    .finally(() => {
      try { sendResponse({ ok: true }); } catch { /* channel may be closed */ }
    });
  return true;  // we'll call sendResponse asynchronously
});

// Auto-open the onboarding wizard on first install. Skipped on
// update/upgrade so existing users don't get spammed with a tab on
// every refresh.
chrome.runtime.onInstalled.addListener(async (details) => {
  ensureContextMenus();
  // Re-assert the Auth0 Origin-strip rule on install/update. Idempotent
  // (removeRuleIds clears the prior rule first), so this is safe alongside
  // the SW-boot registration above.
  void registerAuth0OriginStrip();
  if (details.reason === "install") {
    // Seed the default category map on fresh installs so high-traffic
    // sites (Gmail, Calendar, Docs, AI chats, DM apps) collapse to a
    // single category event from the start instead of leaking
    // per-email / per-conversation titles + URLs.
    const { loadCategoryMap, saveCategoryMap } = await import("./storage");
    const existing = await loadCategoryMap();
    await saveCategoryMap(mergeDefaults(existing));
    void chrome.tabs.create({ url: chrome.runtime.getURL("wizard.html") });
  }
});

chrome.alarms.create(FLUSH_ALARM, { periodInMinutes: FLUSH_INTERVAL_MIN });
chrome.alarms.create(SWEEP_ALARM, { periodInMinutes: SWEEP_INTERVAL_MIN });
chrome.alarms.onAlarm.addListener((alarm) => {
  // flushOutbox doesn't touch visits, only its own outbox key, so it stays
  // outside the visits mutex. It has its OWN single-flight guard (see
  // outbox.ts) so this alarm tick can't overlap the backfill's flush and
  // double-send. sweepStaleBlurred mutates visits → must be locked.
  if (alarm.name === FLUSH_ALARM) void flushOutbox();
  if (alarm.name === SWEEP_ALARM) void withSwLock(async()=>{await checkpointLegacyArrivals();await sweepStaleBlurred(Date.now());});
});
