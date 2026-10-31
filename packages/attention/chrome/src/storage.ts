// chrome/src/storage.ts
import {strictV1Settings} from "./relayless/v1AuthStorage";
import {v1HeldMode,v1Command} from "./relayless/v1AuthClient";
import type {
  Settings, IgnoreEntry, CategoryMapping, OutboxEntry, Visit, BackfillRun,
} from "./types";
import { DEFAULT_SETTINGS } from "./types";

// chrome.storage.local — per-machine
export async function loadSettings(): Promise<Settings> {
  if(await v1HeldMode())return strictV1Settings(await v1Command("settings-read"));
  const r = await chrome.storage.local.get("settings");
  return { ...DEFAULT_SETTINGS, ...(r.settings as Partial<Settings> | undefined) };
}
export async function saveSettings(s: Settings): Promise<void> {
  if(await v1HeldMode()){await v1Command("settings",s);return;}
  await chrome.storage.local.set({ settings: s });
}

export async function loadOutbox(): Promise<OutboxEntry[]> {
  const r = await chrome.storage.local.get("outbox");
  return (r.outbox as OutboxEntry[] | undefined) ?? [];
}
export async function saveOutbox(entries: OutboxEntry[]): Promise<void> {
  await chrome.storage.local.set({ outbox: entries });
}

export async function loadCategoryMap(): Promise<CategoryMapping[]> {
  const r = await chrome.storage.local.get("categoryMap");
  return (r.categoryMap as CategoryMapping[] | undefined) ?? [];
}
export async function saveCategoryMap(m: CategoryMapping[]): Promise<void> {
  await chrome.storage.local.set({ categoryMap: m });
}

// chrome.storage.sync — propagates across Chrome profiles via Google sync
export async function loadIgnoreList(): Promise<IgnoreEntry[]> {
  const r = await chrome.storage.sync.get("ignoreList");
  return (r.ignoreList as IgnoreEntry[] | undefined) ?? [];
}
export async function saveIgnoreList(entries: IgnoreEntry[]): Promise<void> {
  await chrome.storage.sync.set({ ignoreList: entries });
}

// chrome.storage.local — a random id minted once per machine/profile.
// Lets the wizard tell "a backfill run from another machine" apart from
// its own.
export async function getMachineId(): Promise<string> {
  const r = await chrome.storage.local.get("machineId");
  let id = r.machineId as string | undefined;
  if (!id) {
    id = Math.random().toString(36).slice(2) + Date.now().toString(36);
    await chrome.storage.local.set({ machineId: id });
  }
  return id;
}

// chrome.storage.sync — backfill runs propagate across synced machines so
// a second machine's wizard can warn before re-backfilling shared history.
const BACKFILL_RUNS_CAP = 10;
export async function loadBackfillRuns(): Promise<BackfillRun[]> {
  const r = await chrome.storage.sync.get("backfillRuns");
  return (r.backfillRuns as BackfillRun[] | undefined) ?? [];
}
export async function recordBackfillRun(machineId: string): Promise<void> {
  const runs = await loadBackfillRuns();
  runs.push({ machineId, at: new Date().toISOString() });
  while (runs.length > BACKFILL_RUNS_CAP) runs.shift();
  await chrome.storage.sync.set({ backfillRuns: runs });
}

// chrome.storage.session — in-memory, cleared on browser restart.
// A single map keyed by tabId. At most one entry has state="focused".
export async function loadVisits(): Promise<Record<number, Visit>> {
  const r = await chrome.storage.session.get("visits");
  return (r.visits as Record<number, Visit> | undefined) ?? {};
}
export async function saveVisits(v: Record<number, Visit>): Promise<void> {
  await chrome.storage.session.set({ visits: v });
}

/** Read-only worker retention projection. A missing/error response is UNKNOWN,
 * never an empty or uploaded result. No page opens capture/journal authority. */
export interface CaptureStatus {
 status:'held';drains:'disabled';retained:number;held:number;active:number;prepared:number;enqueued:number;
 legacyOriginals:number;verifiedUploaded:0;serializedBytes:number;budgetBytes:number;legacyObservation:'pending'|'complete'|'unknown';
}
export async function loadCaptureStatus():Promise<CaptureStatus>{
 const r=await chrome.runtime.sendMessage({type:'attention-capture-v1',version:1,command:'status',payload:null});
 const s=r?.result;
 if(r?.version!==1||r?.ok!==true||s?.status!=='held'||s?.drains!=='disabled'||s?.verifiedUploaded!==0
  ||!['retained','held','active','prepared','enqueued','legacyOriginals','serializedBytes','budgetBytes'].every(k=>Number.isSafeInteger(s[k])&&s[k]>=0)
  ||s.retained!==s.held+s.active+s.prepared+s.enqueued||!['pending','complete','unknown'].includes(s.legacyObservation))throw new Error('Attention capture retention UNKNOWN');
 return s;
}
