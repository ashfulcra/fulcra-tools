// chrome/src/storage.ts
import type {
  Settings, IgnoreEntry, CategoryMapping, OutboxEntry, Visit, BackfillRun,
} from "./types";
import { DEFAULT_SETTINGS } from "./types";

// chrome.storage.local — per-machine
export async function loadSettings(): Promise<Settings> {
  const r = await chrome.storage.local.get("settings");
  return { ...DEFAULT_SETTINGS, ...(r.settings as Partial<Settings> | undefined) };
}
export async function saveSettings(s: Settings): Promise<void> {
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
