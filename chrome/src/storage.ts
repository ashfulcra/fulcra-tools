// chrome/src/storage.ts
import type {
  Settings, IgnoreEntry, CategoryMapping, OutboxEntry, ActiveVisit,
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

// chrome.storage.session — in-memory, cleared on browser restart
export async function loadActiveVisits(): Promise<Record<number, ActiveVisit>> {
  const r = await chrome.storage.session.get("activeVisits");
  return (r.activeVisits as Record<number, ActiveVisit> | undefined) ?? {};
}
export async function saveActiveVisits(v: Record<number, ActiveVisit>): Promise<void> {
  await chrome.storage.session.set({ activeVisits: v });
}
