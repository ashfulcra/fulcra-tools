# Fulcra Attention — Plan B: Chrome MV3 Extension

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Chrome MV3 extension that captures browsing attention and POSTs it to the fulcra-attention relay shipped in Plan A. Compatible end-to-end with the relay's wire format at `127.0.0.1:8771/attention`.

**Architecture:** TypeScript + Vite + React popup. MV3 service worker subscribes to `webNavigation.onCommitted` + `onHistoryStateUpdated`, runs a per-tab active-visit state machine, scrubs URLs (Tier 1 always-on, byte-identical to the Python sibling via a shared fixture), categorizes (Tier 2, opt-in) or ignores (Tier 3, opt-in), and posts via a write-ahead outbox.

**Tech Stack:** TypeScript 5.x strict, Vite 5 + `@crxjs/vite-plugin`, React 18, Vitest + jsdom, pnpm (npm fallback).

**Working directory:** `/Users/Scanning/Developer/fulcra-attention/chrome/` (new subdir of the Plan A repo).

**Companion docs:**
- Spec: `/Users/Scanning/Developer/FulcraMediaHelpers/docs/superpowers/specs/2026-05-18-fulcra-attention-v1-design.md`
- Plan A: `/Users/Scanning/Developer/FulcraMediaHelpers/docs/superpowers/plans/2026-05-18-fulcra-attention-plan-a-python.md`
- Cross-language scrub fixture (already shipped): `/Users/Scanning/Developer/fulcra-attention/tests/fixtures/scrub_cases.json`

**Validation target:** at end of Plan B the user can:
1. `cd chrome && pnpm install && pnpm build`
2. Load `chrome/dist/` as an unpacked extension at `chrome://extensions/`
3. Paste the bearer token from `~/.config/fulcra-attention/relay.json` into the popup
4. Visit a page → see post-scrub URL in the popup's "last 5" stream within 5s of nav-away
5. The annotation lands in Fulcra (verifiable via the manual smoke test in Plan A's Task 16)

---

## File Structure

```
fulcra-attention/
├── .gitignore                       # modify: add chrome/node_modules, chrome/dist
└── chrome/
    ├── package.json
    ├── tsconfig.json
    ├── vite.config.ts
    ├── manifest.json
    ├── index.html                   # popup entry
    ├── options.html                 # options entry
    ├── README.md
    ├── src/
    │   ├── types.ts                 # AttentionEvent + Settings shapes
    │   ├── scrub.ts                 # TS port of Python scrub.py
    │   ├── storage.ts               # chrome.storage.* typed wrappers
    │   ├── outbox.ts                # write-ahead queue
    │   ├── ignore.ts                # Tier 3 with wildcard subdomain
    │   ├── categorize.ts            # Tier 2 lookup
    │   ├── identity.ts              # chrome_identity capture
    │   ├── content.ts               # OG metadata scraper
    │   ├── background.ts            # MV3 SW: navigation + outbox flush
    │   ├── popup/
    │   │   ├── main.tsx
    │   │   ├── App.tsx
    │   │   ├── BearerForm.tsx
    │   │   ├── LiveStream.tsx
    │   │   ├── IgnoreList.tsx
    │   │   ├── Counts.tsx
    │   │   ├── IdentityLabel.tsx
    │   │   └── popup.css
    │   └── options/
    │       └── main.tsx
    └── tests/
        ├── setup.ts                 # vitest setup — minimal chrome stub
        ├── scrub.test.ts            # CROSS-LANGUAGE GATE
        ├── storage.test.ts
        ├── outbox.test.ts
        ├── ignore.test.ts
        ├── categorize.test.ts
        ├── identity.test.ts
        └── background.test.ts
```

---

## Task 1: Scaffold chrome/ subdir

**Files:**
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/package.json`
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/tsconfig.json`
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/vite.config.ts`
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/manifest.json`
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/index.html`
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/options.html`
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/tests/setup.ts`
- Modify: `/Users/Scanning/Developer/fulcra-attention/.gitignore`

- [ ] **Step 1: Add chrome/ ignores to top-level .gitignore**

Append two lines:

```gitignore
chrome/node_modules/
chrome/dist/
```

- [ ] **Step 2: Create chrome/package.json**

```json
{
  "name": "fulcra-attention-chrome",
  "version": "0.1.0",
  "description": "Capture browsing attention into your Fulcra account",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc --noEmit && vite build",
    "test": "vitest run",
    "test:watch": "vitest"
  },
  "dependencies": {
    "react": "^18.3.0",
    "react-dom": "^18.3.0"
  },
  "devDependencies": {
    "@crxjs/vite-plugin": "^2.0.0-beta.28",
    "@types/chrome": "^0.0.270",
    "@types/react": "^18.3.0",
    "@types/react-dom": "^18.3.0",
    "@vitejs/plugin-react": "^4.3.0",
    "jsdom": "^25.0.0",
    "typescript": "^5.6.0",
    "vite": "^5.4.0",
    "vitest": "^2.1.0"
  }
}
```

- [ ] **Step 3: Create chrome/tsconfig.json**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "resolveJsonModule": true,
    "isolatedModules": true,
    "types": ["chrome", "vitest/globals"]
  },
  "include": ["src", "tests"]
}
```

- [ ] **Step 4: Create chrome/vite.config.ts**

```typescript
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { crx } from "@crxjs/vite-plugin";
import manifest from "./manifest.json";

export default defineConfig({
  plugins: [
    react(),
    crx({ manifest }),
  ],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/setup.ts"],
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
```

- [ ] **Step 5: Create chrome/manifest.json**

```json
{
  "manifest_version": 3,
  "name": "Fulcra Attention",
  "version": "0.1.0",
  "description": "Capture browsing attention into your Fulcra account.",
  "permissions": [
    "webNavigation",
    "tabs",
    "storage",
    "alarms",
    "activeTab",
    "contextMenus",
    "identity",
    "scripting"
  ],
  "host_permissions": [
    "http://127.0.0.1:8771/*"
  ],
  "background": {
    "service_worker": "src/background.ts",
    "type": "module"
  },
  "action": {
    "default_popup": "index.html",
    "default_title": "Fulcra Attention"
  },
  "options_page": "options.html"
}
```

Note: NO `"incognito"` key (Chrome default = extension doesn't see private tabs). NO `<all_urls>` host permission.

- [ ] **Step 6: Create chrome/index.html (popup entry)**

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Fulcra Attention</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/popup/main.tsx"></script>
  </body>
</html>
```

- [ ] **Step 7: Create chrome/options.html (options entry stub)**

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <title>Fulcra Attention — Options</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/options/main.tsx"></script>
  </body>
</html>
```

- [ ] **Step 8: Create chrome/tests/setup.ts (minimal chrome stub)**

```typescript
// chrome/tests/setup.ts
// Minimal chrome.* API stub for Vitest + jsdom. Individual tests
// override pieces via vi.stubGlobal / vi.fn() as needed.

import { vi } from "vitest";

const memStore: Record<string, unknown> = {};

function makeArea() {
  return {
    get: vi.fn(async (keys?: string | string[] | Record<string, unknown> | null) => {
      if (keys == null) return { ...memStore };
      if (typeof keys === "string") return { [keys]: memStore[keys] };
      if (Array.isArray(keys)) {
        const out: Record<string, unknown> = {};
        for (const k of keys) out[k] = memStore[k];
        return out;
      }
      const out: Record<string, unknown> = {};
      for (const k of Object.keys(keys)) out[k] = memStore[k] ?? (keys as Record<string, unknown>)[k];
      return out;
    }),
    set: vi.fn(async (items: Record<string, unknown>) => {
      Object.assign(memStore, items);
    }),
    remove: vi.fn(async (keys: string | string[]) => {
      const arr = Array.isArray(keys) ? keys : [keys];
      for (const k of arr) delete memStore[k];
    }),
    clear: vi.fn(async () => {
      for (const k of Object.keys(memStore)) delete memStore[k];
    }),
  };
}

(globalThis as unknown as { chrome: unknown }).chrome = {
  storage: {
    local: makeArea(),
    sync: makeArea(),
    session: makeArea(),
  },
  alarms: {
    create: vi.fn(),
    clear: vi.fn(),
    onAlarm: { addListener: vi.fn() },
  },
  webNavigation: {
    onCommitted: { addListener: vi.fn() },
    onHistoryStateUpdated: { addListener: vi.fn() },
  },
  tabs: {
    get: vi.fn(),
    onRemoved: { addListener: vi.fn() },
  },
  windows: {
    onFocusChanged: { addListener: vi.fn() },
    WINDOW_ID_NONE: -1,
  },
  runtime: {
    onStartup: { addListener: vi.fn() },
    onSuspend: { addListener: vi.fn() },
    onMessage: { addListener: vi.fn() },
    sendMessage: vi.fn(),
  },
  scripting: {
    executeScript: vi.fn(),
  },
  identity: {
    getProfileUserInfo: vi.fn(),
  },
};
```

- [ ] **Step 9: Install dependencies**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm install || npm install
```

Expected: a `node_modules/` directory and either `pnpm-lock.yaml` or `package-lock.json`. Either is fine.

- [ ] **Step 10: Verify TypeScript compiles**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm exec tsc --noEmit || npx tsc --noEmit
```

Expected: zero errors. (No source files yet — should pass trivially.)

- [ ] **Step 11: Commit**

```bash
cd /Users/Scanning/Developer/fulcra-attention
git add .gitignore chrome/package.json chrome/tsconfig.json chrome/vite.config.ts chrome/manifest.json chrome/index.html chrome/options.html chrome/tests/setup.ts
git commit -m "chore(chrome): scaffold MV3 extension project (Vite + React + Vitest)"
```

---

## Task 2: types.ts — shared TS types

**Files:**
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/src/types.ts`

This is types-only — no tests needed for the types themselves (the tests in later tasks consume them).

- [ ] **Step 1: Write the types file**

```typescript
// chrome/src/types.ts
// Shared types. AttentionEvent matches Plan A's wire format byte-for-byte.

export const CLIENT = "fulcra-attention-chrome/0.1.0";

/**
 * The POST body sent to http://127.0.0.1:8771/attention.
 * Exactly one of {url, category} must be non-null.
 * start_time <= end_time <= now + 5min (enforced server-side).
 */
export interface AttentionEvent {
  url: string | null;
  title: string | null;
  og_description: string | null;
  favicon_url: string | null;
  category: string | null;
  chrome_identity: string | null;
  og_type: string | null;
  lang: string | null;
  start_time: string;  // ISO 8601 with trailing 'Z'
  end_time: string;
  client: string;      // CLIENT constant
}

/** Persistent settings in chrome.storage.local. */
export interface Settings {
  bearerToken: string | null;
  relayPort: number;       // default 8771
  enabled: boolean;        // master kill switch
  identityLabel: string | null;  // user override; null means use chrome.identity.getProfileUserInfo
}

export const DEFAULT_SETTINGS: Settings = {
  bearerToken: null,
  relayPort: 8771,
  enabled: true,
  identityLabel: null,
};

/** One entry in the Tier 3 ignore list (chrome.storage.sync). */
export interface IgnoreEntry {
  pattern: string;  // exact host like "example.com" or wildcard like "*.example.com"
  addedAt: string;  // ISO timestamp; informational
}

/** One Tier 2 mapping (chrome.storage.local). */
export interface CategoryMapping {
  pattern: string;     // same wildcard semantics as IgnoreEntry
  category: string;    // slug from the v1 vocabulary
}

/** An event queued for POST in chrome.storage.local. */
export interface OutboxEntry {
  id: string;          // sha1 of payload + nonce; used for dedup
  payload: AttentionEvent;
  queuedAt: number;    // Date.now()
  attempts: number;
}

/** Active visit being timed in chrome.storage.session. Keyed by tabId. */
export interface ActiveVisit {
  tabId: number;
  scrubbedUrl: string;
  startTime: number;   // Date.now()
}

/** Daily counters in chrome.storage.local for popup display. */
export interface Counts {
  date: string;        // YYYY-MM-DD
  logged: number;
  categorized: number;
  ignored: number;
}
```

- [ ] **Step 2: Verify it type-checks**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm exec tsc --noEmit || npx tsc --noEmit
```

Expected: zero errors.

- [ ] **Step 3: Commit**

```bash
cd /Users/Scanning/Developer/fulcra-attention
git add chrome/src/types.ts
git commit -m "feat(chrome): shared types (AttentionEvent matches Plan A wire format)"
```

---

## Task 3: scrub.ts — TS port + cross-language gate test

**Files:**
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/src/scrub.ts`
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/tests/scrub.test.ts`

**This is the critical quality gate.** The TS scrubber must produce byte-identical output to the Python sibling for every fixture entry.

- [ ] **Step 1: Write the failing cross-language fixture test**

```typescript
// chrome/tests/scrub.test.ts
import { describe, test, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { scrubUrl } from "../src/scrub";

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURE_PATH = join(__dirname, "..", "..", "tests", "fixtures", "scrub_cases.json");
const CASES: Array<{ input: string; expected: string }> = JSON.parse(
  readFileSync(FIXTURE_PATH, "utf-8"),
);

describe("scrub_url cross-language contract", () => {
  test.each(CASES)("scrubs $input -> $expected", ({ input, expected }) => {
    expect(scrubUrl(input)).toBe(expected);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm test
```
Expected: FAIL (Cannot find module '../src/scrub' or all 55 cases failing).

- [ ] **Step 3: Implement chrome/src/scrub.ts**

```typescript
// chrome/src/scrub.ts
//
// Tier 1 URL scrubbing — TypeScript port of fulcra_attention/scrub.py.
// CROSS-LANGUAGE CONTRACT: this must produce byte-identical output to the
// Python sibling for every entry in tests/fixtures/scrub_cases.json.
//
// Strips:
//   - Auth-bearing query params (access_token, code, etc.)
//   - Tracking params (utm_*, gclid, fbclid, etc.)
//   - One-click action tokens (unsubscribe, verify, reset, ...)
//   - The entire URL fragment (covers OAuth Implicit Flow + Slack/Notion
//     magic links that stuff tokens after #)
// Preserves the order of surviving query params.

export const DENYLIST: ReadonlySet<string> = new Set([
  // auth-bearing
  "access_token", "id_token", "refresh_token", "code", "state", "nonce",
  "client_secret", "assertion", "session", "sid", "sessionid", "auth",
  "authorization", "token", "apikey", "api_key", "key", "signature",
  "sig", "hmac", "x-amz-signature", "x-amz-credential",
  "x-amz-security-token", "expires", "password", "pwd", "pw", "otp",
  "magic", "share_token", "invite", "confirmation_token",
  "_csrf", "csrf_token", "xsrf", "ticket", "ott",
  // tracking
  "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
  "gclid", "fbclid", "msclkid", "mc_eid", "mc_cid", "_hsenc", "_hsmi",
  "igshid", "yclid", "ref", "ref_src", "ref_url",
  // one-click action
  "unsubscribe", "unsub", "verify", "reset", "confirm", "activate",
]);

export function scrubUrl(input: string): string {
  const u = new URL(input);
  // Preserve order of surviving query params.
  const kept = new URLSearchParams();
  for (const [k, v] of u.searchParams) {
    if (!DENYLIST.has(k.toLowerCase())) {
      kept.append(k, v);
    }
  }
  const queryStr = kept.toString();
  // Drop fragment entirely.
  const base = `${u.protocol}//${u.host}${u.pathname}`;
  return queryStr ? `${base}?${queryStr}` : base;
}
```

- [ ] **Step 4: Run the test to verify all 55 cases pass**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm test
```
Expected: 55 PASS. If any FAIL, that is a cross-language contract violation — investigate the specific case before proceeding.

Common pitfalls if a case fails:
- `URLSearchParams.toString()` uses `+` for space; Python's `urlencode` also uses `+` — these match.
- Trailing `/` on pathname: Python `urlsplit("https://x.com").path == ""` but TS `new URL("https://x.com").pathname == "/"`. If a fixture entry exposes this, the fix is `u.pathname || "/"`. The current fixtures don't trigger this.
- Case-sensitivity: denylist is lowercase; we lowercase the param name before comparing. Matches Python.

- [ ] **Step 5: Commit**

```bash
cd /Users/Scanning/Developer/fulcra-attention
git add chrome/src/scrub.ts chrome/tests/scrub.test.ts
git commit -m "feat(chrome): scrub.ts (TS port, byte-identical to Python via shared fixture)"
```

---

## Task 4: storage.ts — typed wrappers

**Files:**
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/src/storage.ts`
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/tests/storage.test.ts`

- [ ] **Step 1: Write failing tests**

```typescript
// chrome/tests/storage.test.ts
import { describe, test, expect, beforeEach } from "vitest";
import {
  loadSettings, saveSettings,
  loadOutbox, saveOutbox,
  loadIgnoreList, saveIgnoreList,
  loadCategoryMap, saveCategoryMap,
  loadActiveVisits, saveActiveVisits,
} from "../src/storage";
import { DEFAULT_SETTINGS } from "../src/types";

beforeEach(async () => {
  await chrome.storage.local.clear();
  await chrome.storage.sync.clear();
  await chrome.storage.session.clear();
});

describe("settings", () => {
  test("loadSettings returns defaults when empty", async () => {
    expect(await loadSettings()).toEqual(DEFAULT_SETTINGS);
  });
  test("saveSettings then loadSettings round-trips", async () => {
    await saveSettings({ bearerToken: "x", relayPort: 8771, enabled: false, identityLabel: "Work" });
    expect(await loadSettings()).toEqual({
      bearerToken: "x", relayPort: 8771, enabled: false, identityLabel: "Work",
    });
  });
});

describe("outbox", () => {
  test("loadOutbox returns [] when empty", async () => {
    expect(await loadOutbox()).toEqual([]);
  });
  test("save then load round-trips", async () => {
    const entry = {
      id: "abc",
      payload: {
        url: "https://x.com/", title: "T", og_description: null, favicon_url: null,
        category: null, chrome_identity: null, og_type: null, lang: null,
        start_time: "2026-05-18T14:00:00Z", end_time: "2026-05-18T14:05:00Z",
        client: "fulcra-attention-chrome/0.1.0",
      },
      queuedAt: 1700000000000,
      attempts: 0,
    };
    await saveOutbox([entry]);
    expect(await loadOutbox()).toEqual([entry]);
  });
});

describe("ignore list (sync)", () => {
  test("loadIgnoreList returns [] when empty", async () => {
    expect(await loadIgnoreList()).toEqual([]);
  });
  test("uses chrome.storage.sync, not local", async () => {
    await saveIgnoreList([{ pattern: "chase.com", addedAt: "2026-05-18T14:00:00Z" }]);
    const sync = await chrome.storage.sync.get(null);
    expect(sync).toHaveProperty("ignoreList");
  });
});

describe("category map (local)", () => {
  test("loadCategoryMap returns [] when empty", async () => {
    expect(await loadCategoryMap()).toEqual([]);
  });
});

describe("active visits (session)", () => {
  test("loadActiveVisits returns {} when empty", async () => {
    expect(await loadActiveVisits()).toEqual({});
  });
  test("uses chrome.storage.session", async () => {
    await saveActiveVisits({ 7: { tabId: 7, scrubbedUrl: "https://x.com/", startTime: 1700000000000 } });
    const session = await chrome.storage.session.get(null);
    expect(session).toHaveProperty("activeVisits");
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm test storage
```
Expected: FAIL (cannot resolve module).

- [ ] **Step 3: Implement chrome/src/storage.ts**

```typescript
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm test storage
```
Expected: 7 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/Scanning/Developer/fulcra-attention
git add chrome/src/storage.ts chrome/tests/storage.test.ts
git commit -m "feat(chrome): storage.ts typed wrappers around chrome.storage.{local,sync,session}"
```

---

## Task 5: ignore.ts + categorize.ts (Tier 3 + Tier 2 with wildcards)

**Files:**
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/src/ignore.ts`
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/src/categorize.ts`
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/tests/ignore.test.ts`
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/tests/categorize.test.ts`

- [ ] **Step 1: Write failing tests for ignore.ts**

```typescript
// chrome/tests/ignore.test.ts
import { describe, test, expect, beforeEach } from "vitest";
import { isIgnored, matchesPattern } from "../src/ignore";
import { saveIgnoreList } from "../src/storage";

beforeEach(async () => {
  await chrome.storage.sync.clear();
});

describe("matchesPattern", () => {
  test("exact host match", () => {
    expect(matchesPattern("example.com", "example.com")).toBe(true);
    expect(matchesPattern("other.com", "example.com")).toBe(false);
  });
  test("wildcard *.example.com matches subdomain", () => {
    expect(matchesPattern("mail.example.com", "*.example.com")).toBe(true);
    expect(matchesPattern("app.mail.example.com", "*.example.com")).toBe(true);
  });
  test("wildcard *.example.com does NOT match apex", () => {
    expect(matchesPattern("example.com", "*.example.com")).toBe(false);
  });
  test("wildcard does not match unrelated host", () => {
    expect(matchesPattern("other.com", "*.example.com")).toBe(false);
  });
});

describe("isIgnored", () => {
  test("returns false when ignore list is empty", async () => {
    expect(await isIgnored("https://example.com/page")).toBe(false);
  });
  test("returns true when host matches an exact entry", async () => {
    await saveIgnoreList([{ pattern: "chase.com", addedAt: "2026-05-18T14:00:00Z" }]);
    expect(await isIgnored("https://chase.com/login")).toBe(true);
    expect(await isIgnored("https://example.com/")).toBe(false);
  });
  test("returns true when host matches a wildcard", async () => {
    await saveIgnoreList([{ pattern: "*.bank.com", addedAt: "2026-05-18T14:00:00Z" }]);
    expect(await isIgnored("https://my.bank.com/account")).toBe(true);
    expect(await isIgnored("https://bank.com/")).toBe(false);
  });
});
```

- [ ] **Step 2: Write failing tests for categorize.ts**

```typescript
// chrome/tests/categorize.test.ts
import { describe, test, expect, beforeEach } from "vitest";
import { categorize } from "../src/categorize";
import { saveCategoryMap } from "../src/storage";

beforeEach(async () => {
  await chrome.storage.local.clear();
});

describe("categorize", () => {
  test("returns null when no mappings", async () => {
    expect(await categorize("https://example.com/")).toBeNull();
  });
  test("returns category slug on exact host match", async () => {
    await saveCategoryMap([{ pattern: "chatgpt.com", category: "ai-chat" }]);
    expect(await categorize("https://chatgpt.com/c/abc")).toBe("ai-chat");
  });
  test("returns category on wildcard match", async () => {
    await saveCategoryMap([{ pattern: "*.google.com", category: "search" }]);
    expect(await categorize("https://www.google.com/search?q=x")).toBe("search");
  });
  test("first matching rule wins (user-controlled order)", async () => {
    await saveCategoryMap([
      { pattern: "*.example.com", category: "first" },
      { pattern: "*.example.com", category: "second" },
    ]);
    expect(await categorize("https://x.example.com/")).toBe("first");
  });
});
```

- [ ] **Step 3: Run both tests to verify they fail**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm test ignore categorize
```
Expected: FAIL (modules don't exist).

- [ ] **Step 4: Implement chrome/src/ignore.ts**

```typescript
// chrome/src/ignore.ts
//
// Tier 3 — user-managed ignore list. Drops the event entirely. Persisted
// in chrome.storage.sync so it propagates across Chrome profiles via the
// user's Google sync.

import { loadIgnoreList } from "./storage";

/**
 * Match a host against a pattern. Supports:
 *   - Exact match: "example.com" matches only "example.com".
 *   - Wildcard subdomain: "*.example.com" matches "x.example.com" but
 *     NOT "example.com" itself (user can add both).
 */
export function matchesPattern(host: string, pattern: string): boolean {
  if (!pattern.startsWith("*.")) {
    return host === pattern;
  }
  const suffix = pattern.slice(1);  // ".example.com"
  return host.endsWith(suffix) && host !== suffix.slice(1);
}

export async function isIgnored(url: string): Promise<boolean> {
  let host: string;
  try {
    host = new URL(url).host;
  } catch {
    return false;
  }
  const list = await loadIgnoreList();
  return list.some((e) => matchesPattern(host, e.pattern));
}
```

- [ ] **Step 5: Implement chrome/src/categorize.ts**

```typescript
// chrome/src/categorize.ts
//
// Tier 2 — user-controlled category mapping. Replaces the URL/title with
// a category slug at ingest time. Empty by default. Stored in
// chrome.storage.local (per machine).

import { loadCategoryMap } from "./storage";
import { matchesPattern } from "./ignore";

export async function categorize(url: string): Promise<string | null> {
  let host: string;
  try {
    host = new URL(url).host;
  } catch {
    return null;
  }
  const map = await loadCategoryMap();
  for (const m of map) {
    if (matchesPattern(host, m.pattern)) return m.category;
  }
  return null;
}
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm test ignore categorize
```
Expected: All ignore + categorize tests PASS (8 + 4 = 12).

- [ ] **Step 7: Commit**

```bash
cd /Users/Scanning/Developer/fulcra-attention
git add chrome/src/ignore.ts chrome/src/categorize.ts chrome/tests/ignore.test.ts chrome/tests/categorize.test.ts
git commit -m "feat(chrome): Tier 3 ignore + Tier 2 categorize (wildcard subdomain)"
```

---

## Task 6: identity.ts — chrome_identity capture

**Files:**
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/src/identity.ts`
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/tests/identity.test.ts`

- [ ] **Step 1: Write failing tests**

```typescript
// chrome/tests/identity.test.ts
import { describe, test, expect, beforeEach, vi } from "vitest";
import { getChromeIdentity } from "../src/identity";
import { saveSettings } from "../src/storage";
import { DEFAULT_SETTINGS } from "../src/types";

beforeEach(async () => {
  await chrome.storage.local.clear();
  vi.mocked(chrome.identity.getProfileUserInfo).mockReset();
});

describe("getChromeIdentity", () => {
  test("returns Google account email when signed in", async () => {
    vi.mocked(chrome.identity.getProfileUserInfo).mockImplementation((_opts, cb) => {
      cb({ email: "ash@fulcradynamics.com", id: "google-id-123" });
    });
    expect(await getChromeIdentity()).toBe("ash@fulcradynamics.com");
  });

  test("returns popup-set label when not signed in to Google", async () => {
    vi.mocked(chrome.identity.getProfileUserInfo).mockImplementation((_opts, cb) => {
      cb({ email: "", id: "" });
    });
    await saveSettings({ ...DEFAULT_SETTINGS, identityLabel: "Side Project" });
    expect(await getChromeIdentity()).toBe("Side Project");
  });

  test("returns null when neither source available", async () => {
    vi.mocked(chrome.identity.getProfileUserInfo).mockImplementation((_opts, cb) => {
      cb({ email: "", id: "" });
    });
    expect(await getChromeIdentity()).toBeNull();
  });

  test("popup label overrides Google email when both set", async () => {
    vi.mocked(chrome.identity.getProfileUserInfo).mockImplementation((_opts, cb) => {
      cb({ email: "ash@fulcradynamics.com", id: "google-id" });
    });
    await saveSettings({ ...DEFAULT_SETTINGS, identityLabel: "Custom Label" });
    expect(await getChromeIdentity()).toBe("Custom Label");
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm test identity
```
Expected: FAIL (module not found).

- [ ] **Step 3: Implement chrome/src/identity.ts**

```typescript
// chrome/src/identity.ts
//
// Capture chrome_identity for the AttentionEvent payload.
// Order of preference (most specific first):
//   1. User-set label in Settings.identityLabel (free text from popup)
//   2. Google account email from chrome.identity.getProfileUserInfo()
//   3. null

import { loadSettings } from "./storage";

function profileUserInfo(): Promise<chrome.identity.UserInfo> {
  return new Promise((resolve) => {
    chrome.identity.getProfileUserInfo({ accountStatus: "ANY" }, (info) => resolve(info));
  });
}

export async function getChromeIdentity(): Promise<string | null> {
  const settings = await loadSettings();
  if (settings.identityLabel && settings.identityLabel.trim() !== "") {
    return settings.identityLabel.trim();
  }
  const info = await profileUserInfo();
  if (info.email && info.email !== "") return info.email;
  return null;
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm test identity
```
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/Scanning/Developer/fulcra-attention
git add chrome/src/identity.ts chrome/tests/identity.test.ts
git commit -m "feat(chrome): identity.ts (Google account email with free-text label override)"
```

---

## Task 7: outbox.ts — write-ahead queue with retry

**Files:**
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/src/outbox.ts`
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/tests/outbox.test.ts`

- [ ] **Step 1: Write failing tests**

```typescript
// chrome/tests/outbox.test.ts
import { describe, test, expect, beforeEach, vi } from "vitest";
import { addToOutbox, flushOutbox, OUTBOX_CAP } from "../src/outbox";
import { loadOutbox, saveSettings } from "../src/storage";
import type { AttentionEvent } from "../src/types";
import { DEFAULT_SETTINGS } from "../src/types";

function makeEvent(url = "https://x.com/"): AttentionEvent {
  return {
    url, title: "T", og_description: null, favicon_url: null,
    category: null, chrome_identity: null, og_type: null, lang: null,
    start_time: "2026-05-18T14:00:00Z", end_time: "2026-05-18T14:05:00Z",
    client: "fulcra-attention-chrome/0.1.0",
  };
}

beforeEach(async () => {
  await chrome.storage.local.clear();
  vi.stubGlobal("fetch", vi.fn());
});

describe("addToOutbox", () => {
  test("adds event with unique id, attempts=0", async () => {
    await addToOutbox(makeEvent("https://a.com/"));
    const ob = await loadOutbox();
    expect(ob).toHaveLength(1);
    expect(ob[0].attempts).toBe(0);
    expect(ob[0].id).toBeTruthy();
  });
  test("two adds get distinct ids", async () => {
    await addToOutbox(makeEvent("https://a.com/"));
    await addToOutbox(makeEvent("https://b.com/"));
    const ob = await loadOutbox();
    expect(ob).toHaveLength(2);
    expect(ob[0].id).not.toBe(ob[1].id);
  });
  test("drops oldest when over cap", async () => {
    // Pre-populate at capacity
    const seed = Array.from({ length: OUTBOX_CAP }, (_, i) => ({
      id: `seed-${i}`, payload: makeEvent(`https://x${i}.com/`),
      queuedAt: i, attempts: 0,
    }));
    await chrome.storage.local.set({ outbox: seed });
    await addToOutbox(makeEvent("https://new.com/"));
    const ob = await loadOutbox();
    expect(ob).toHaveLength(OUTBOX_CAP);
    expect(ob[0].id).toBe("seed-1");  // seed-0 dropped
    expect(ob[ob.length - 1].payload.url).toBe("https://new.com/");
  });
});

describe("flushOutbox", () => {
  test("no-op when outbox empty", async () => {
    const f = vi.mocked(fetch).mockResolvedValue(new Response(null, { status: 200 }));
    await flushOutbox();
    expect(f).not.toHaveBeenCalled();
  });

  test("no-op when no bearer token", async () => {
    await addToOutbox(makeEvent());
    const f = vi.mocked(fetch).mockResolvedValue(new Response(null, { status: 200 }));
    await flushOutbox();
    expect(f).not.toHaveBeenCalled();
    expect(await loadOutbox()).toHaveLength(1);  // still queued
  });

  test("POSTs each entry, removes on 200", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, bearerToken: "test-tok" });
    await addToOutbox(makeEvent("https://a.com/"));
    await addToOutbox(makeEvent("https://b.com/"));
    const f = vi.mocked(fetch).mockResolvedValue(new Response('{"posted":1}', { status: 200 }));
    await flushOutbox();
    expect(f).toHaveBeenCalledTimes(2);
    const [url, init] = f.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8771/attention");
    expect((init as RequestInit).headers).toMatchObject({
      "Authorization": "Bearer test-tok",
      "Content-Type": "application/json",
    });
    expect(await loadOutbox()).toHaveLength(0);
  });

  test("leaves entry in outbox and bumps attempts on non-200", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, bearerToken: "tok" });
    await addToOutbox(makeEvent());
    vi.mocked(fetch).mockResolvedValue(new Response(null, { status: 502 }));
    await flushOutbox();
    const ob = await loadOutbox();
    expect(ob).toHaveLength(1);
    expect(ob[0].attempts).toBe(1);
  });

  test("leaves entry on network error", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, bearerToken: "tok" });
    await addToOutbox(makeEvent());
    vi.mocked(fetch).mockRejectedValue(new TypeError("Network error"));
    await flushOutbox();
    expect(await loadOutbox()).toHaveLength(1);
  });

  test("drops entry on 400 (permanent failure — bad payload)", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, bearerToken: "tok" });
    await addToOutbox(makeEvent());
    vi.mocked(fetch).mockResolvedValue(new Response('{"error":"bad"}', { status: 400 }));
    await flushOutbox();
    // 400 = our bug or stale data; dropping is right (would loop forever otherwise)
    expect(await loadOutbox()).toHaveLength(0);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm test outbox
```
Expected: FAIL (module not found).

- [ ] **Step 3: Implement chrome/src/outbox.ts**

```typescript
// chrome/src/outbox.ts
//
// Write-ahead queue: every captured event lands here before POST. On 200 we
// delete the entry. On network failure / 5xx we leave it for retry. On 4xx
// we drop it (permanent failure — usually a bug or stale state). Cap at
// OUTBOX_CAP entries, dropping the oldest at overflow.

import { loadOutbox, saveOutbox, loadSettings } from "./storage";
import type { AttentionEvent, OutboxEntry } from "./types";

export const OUTBOX_CAP = 5000;
export const RELAY_URL = "http://127.0.0.1:8771/attention";

function genId(): string {
  // Random 16-char hex; collision-resistant enough for our scale.
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

export async function addToOutbox(payload: AttentionEvent): Promise<void> {
  const cur = await loadOutbox();
  const entry: OutboxEntry = {
    id: genId(),
    payload,
    queuedAt: Date.now(),
    attempts: 0,
  };
  cur.push(entry);
  // Drop oldest if over cap.
  while (cur.length > OUTBOX_CAP) cur.shift();
  await saveOutbox(cur);
}

export async function flushOutbox(): Promise<void> {
  const settings = await loadSettings();
  if (!settings.bearerToken) return;  // not configured — can't post
  let entries = await loadOutbox();
  if (entries.length === 0) return;

  const remaining: OutboxEntry[] = [];
  for (const entry of entries) {
    let ok = false;
    let permanentFail = false;
    try {
      const resp = await fetch(RELAY_URL, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${settings.bearerToken}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(entry.payload),
      });
      if (resp.status === 200) ok = true;
      else if (resp.status >= 400 && resp.status < 500) permanentFail = true;
    } catch {
      // Network error — keep for retry.
    }
    if (ok) continue;
    if (permanentFail) continue;  // drop
    remaining.push({ ...entry, attempts: entry.attempts + 1 });
  }
  await saveOutbox(remaining);
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm test outbox
```
Expected: 9 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/Scanning/Developer/fulcra-attention
git add chrome/src/outbox.ts chrome/tests/outbox.test.ts
git commit -m "feat(chrome): outbox.ts write-ahead queue (200=drop, 4xx=drop, 5xx/net=retry)"
```

---

## Task 8: content.ts — OG metadata + favicon scraper

**Files:**
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/src/content.ts`
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/tests/content.test.ts`

The content script runs INSIDE the page via `chrome.scripting.executeScript`. It must be self-contained (no imports of other src/ files) because crxjs builds it as a separate bundle.

- [ ] **Step 1: Write failing tests**

```typescript
// chrome/tests/content.test.ts
// content.ts is designed to run inside a page. We test the extractor
// function directly against jsdom-built documents.
import { describe, test, expect } from "vitest";
import { extractPageMeta } from "../src/content";

function makeDoc(html: string): Document {
  const parser = new DOMParser();
  return parser.parseFromString(`<!doctype html>${html}`, "text/html");
}

describe("extractPageMeta", () => {
  test("extracts title", () => {
    const d = makeDoc(`<html><head><title>My Page</title></head><body></body></html>`);
    expect(extractPageMeta(d, "https://example.com/p").title).toBe("My Page");
  });

  test("extracts og:description", () => {
    const d = makeDoc(`
      <html><head>
        <title>T</title>
        <meta property="og:description" content="A short summary." />
      </head></html>
    `);
    expect(extractPageMeta(d, "https://example.com/").og_description).toBe("A short summary.");
  });

  test("extracts og:type", () => {
    const d = makeDoc(`<html><head><meta property="og:type" content="article" /></head></html>`);
    expect(extractPageMeta(d, "https://example.com/").og_type).toBe("article");
  });

  test("extracts html lang", () => {
    const d = makeDoc(`<html lang="ja"><head></head></html>`);
    expect(extractPageMeta(d, "https://example.com/").lang).toBe("ja");
  });

  test("resolves favicon relative to page URL", () => {
    const d = makeDoc(`<html><head><link rel="icon" href="/favicon.ico" /></head></html>`);
    expect(extractPageMeta(d, "https://example.com/p/q").favicon_url)
      .toBe("https://example.com/favicon.ico");
  });

  test("falls back to /favicon.ico when no link tag", () => {
    const d = makeDoc(`<html><head></head></html>`);
    expect(extractPageMeta(d, "https://example.com/p/q").favicon_url)
      .toBe("https://example.com/favicon.ico");
  });

  test("returns null for missing optional fields", () => {
    const d = makeDoc(`<html><head><title>T</title></head></html>`);
    const m = extractPageMeta(d, "https://example.com/");
    expect(m.og_description).toBeNull();
    expect(m.og_type).toBeNull();
  });

  test("title is null when document.title is empty", () => {
    const d = makeDoc(`<html><head></head></html>`);
    expect(extractPageMeta(d, "https://example.com/").title).toBeNull();
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm test content
```
Expected: FAIL (module not found).

- [ ] **Step 3: Implement chrome/src/content.ts**

```typescript
// chrome/src/content.ts
//
// Content script. Runs inside the page (injected via
// chrome.scripting.executeScript) to read DOM metadata that's not visible
// to the service worker.
//
// Self-contained: no imports of other src/ files. crxjs builds this as a
// separate bundle that gets injected.

export interface PageMeta {
  title: string | null;
  og_description: string | null;
  og_type: string | null;
  favicon_url: string | null;
  lang: string | null;
}

function metaContent(doc: Document, prop: string): string | null {
  const el = doc.querySelector(`meta[property="${prop}"]`) as HTMLMetaElement | null;
  const c = el?.content?.trim();
  return c && c !== "" ? c : null;
}

function findFavicon(doc: Document, pageUrl: string): string {
  // Prefer <link rel="icon"> over <link rel="shortcut icon">.
  const candidates: HTMLLinkElement[] = Array.from(
    doc.querySelectorAll('link[rel="icon"], link[rel="shortcut icon"]'),
  ) as HTMLLinkElement[];
  const href = candidates[0]?.getAttribute("href");
  if (href && href !== "") {
    try {
      return new URL(href, pageUrl).toString();
    } catch {
      // fall through
    }
  }
  // Convention default.
  return new URL("/favicon.ico", pageUrl).toString();
}

export function extractPageMeta(doc: Document, pageUrl: string): PageMeta {
  const titleEl = doc.querySelector("title");
  const title = titleEl?.textContent?.trim() || null;
  const lang = doc.documentElement.getAttribute("lang");
  return {
    title: title === "" ? null : title,
    og_description: metaContent(doc, "og:description"),
    og_type: metaContent(doc, "og:type"),
    favicon_url: findFavicon(doc, pageUrl),
    lang: lang && lang !== "" ? lang : null,
  };
}

// Executed when the script is injected into a page by the service worker.
// We post the extracted meta back via window message; the SW listens via
// chrome.scripting.executeScript's returned-value mechanism (the last
// expression of the injected function is the return value).
declare const __INJECTED_INVOCATION__: undefined;
if (typeof __INJECTED_INVOCATION__ === "undefined" && typeof document !== "undefined") {
  // Not called at module load — the SW calls extractPageMeta directly via
  // executeScript({func: extractPageMeta, args: [document, location.href]}).
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm test content
```
Expected: 8 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/Scanning/Developer/fulcra-attention
git add chrome/src/content.ts chrome/tests/content.test.ts
git commit -m "feat(chrome): content.ts extractPageMeta (title, og_description, og_type, favicon, lang)"
```

---

## Task 9: background.ts — navigation listeners + active-visit state machine

**Files:**
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/src/background.ts`
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/tests/background.test.ts`

This is the largest task — the MV3 service worker that ties everything together. It subscribes to webNavigation events, runs the per-tab active-visit state machine, and on close emits the AttentionEvent.

- [ ] **Step 1: Write failing tests**

```typescript
// chrome/tests/background.test.ts
import { describe, test, expect, beforeEach, vi } from "vitest";
import {
  handleNavigation, handleTabClose, handleWindowFocusChange,
  buildPayload,
} from "../src/background";
import { saveSettings, loadOutbox, loadActiveVisits, saveIgnoreList, saveCategoryMap } from "../src/storage";
import { DEFAULT_SETTINGS } from "../src/types";

beforeEach(async () => {
  await chrome.storage.local.clear();
  await chrome.storage.sync.clear();
  await chrome.storage.session.clear();
  vi.mocked(chrome.identity.getProfileUserInfo).mockImplementation((_o, cb) => cb({ email: "", id: "" }));
  vi.mocked(chrome.tabs.get).mockResolvedValue({
    id: 1, url: "https://example.com/p", title: "Example", incognito: false,
  } as chrome.tabs.Tab);
  vi.mocked(chrome.scripting.executeScript).mockResolvedValue([
    { result: { title: "Example", og_description: null, og_type: null, favicon_url: null, lang: null }, frameId: 0 },
  ] as chrome.scripting.InjectionResult[]);
});

describe("handleNavigation", () => {
  test("opens an active visit on first nav", async () => {
    await handleNavigation({
      tabId: 1, url: "https://example.com/p", frameId: 0, timeStamp: 1_700_000_000_000,
    });
    const visits = await loadActiveVisits();
    expect(visits[1]).toBeDefined();
    expect(visits[1].scrubbedUrl).toBe("https://example.com/p");
  });

  test("ignores iframe navigations (frameId != 0)", async () => {
    await handleNavigation({
      tabId: 1, url: "https://example.com/p", frameId: 99, timeStamp: 1_700_000_000_000,
    });
    expect(await loadActiveVisits()).toEqual({});
  });

  test("scrubs URL before opening visit", async () => {
    await handleNavigation({
      tabId: 1, url: "https://example.com/p?access_token=secret&id=1", frameId: 0, timeStamp: 1_700_000_000_000,
    });
    const visits = await loadActiveVisits();
    expect(visits[1].scrubbedUrl).toBe("https://example.com/p?id=1");
  });

  test("drops nav entirely when host is on ignore list", async () => {
    await saveIgnoreList([{ pattern: "chase.com", addedAt: "2026-05-18T14:00:00Z" }]);
    await handleNavigation({
      tabId: 1, url: "https://chase.com/login", frameId: 0, timeStamp: 1_700_000_000_000,
    });
    expect(await loadActiveVisits()).toEqual({});
  });

  test("closes prior visit and opens new one on subsequent nav", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, bearerToken: "tok" });
    await handleNavigation({
      tabId: 1, url: "https://a.com/", frameId: 0, timeStamp: 1_700_000_000_000,
    });
    await handleNavigation({
      tabId: 1, url: "https://b.com/", frameId: 0, timeStamp: 1_700_000_300_000,
    });
    const ob = await loadOutbox();
    expect(ob).toHaveLength(1);  // visit to a.com was closed
    expect(ob[0].payload.url).toBe("https://a.com/");
    const visits = await loadActiveVisits();
    expect(visits[1].scrubbedUrl).toBe("https://b.com/");
  });

  test("skips non-http(s) schemes", async () => {
    await handleNavigation({
      tabId: 1, url: "chrome://settings/", frameId: 0, timeStamp: 1_700_000_000_000,
    });
    expect(await loadActiveVisits()).toEqual({});
  });

  test("when categorized, scrubbedUrl is null and category is the slug", async () => {
    await saveCategoryMap([{ pattern: "chatgpt.com", category: "ai-chat" }]);
    await saveSettings({ ...DEFAULT_SETTINGS, bearerToken: "tok" });
    await handleNavigation({
      tabId: 1, url: "https://chatgpt.com/c/abc", frameId: 0, timeStamp: 1_700_000_000_000,
    });
    await handleNavigation({
      tabId: 1, url: "https://other.com/", frameId: 0, timeStamp: 1_700_000_300_000,
    });
    const ob = await loadOutbox();
    expect(ob[0].payload.category).toBe("ai-chat");
    expect(ob[0].payload.url).toBeNull();
    expect(ob[0].payload.title).toBeNull();
  });
});

describe("handleTabClose", () => {
  test("closes the visit and queues an event", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, bearerToken: "tok" });
    await handleNavigation({
      tabId: 1, url: "https://x.com/", frameId: 0, timeStamp: 1_700_000_000_000,
    });
    await handleTabClose(1);
    const ob = await loadOutbox();
    expect(ob).toHaveLength(1);
    expect(ob[0].payload.url).toBe("https://x.com/");
    expect(await loadActiveVisits()).toEqual({});
  });
});

describe("handleWindowFocusChange", () => {
  test("WINDOW_ID_NONE closes ALL open visits", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, bearerToken: "tok" });
    await handleNavigation({
      tabId: 1, url: "https://a.com/", frameId: 0, timeStamp: 1_700_000_000_000,
    });
    await handleNavigation({
      tabId: 2, url: "https://b.com/", frameId: 0, timeStamp: 1_700_000_100_000,
    });
    await handleWindowFocusChange(chrome.windows.WINDOW_ID_NONE);
    const ob = await loadOutbox();
    expect(ob).toHaveLength(2);
    expect(await loadActiveVisits()).toEqual({});
  });
  test("non-NONE focus is a no-op", async () => {
    await handleNavigation({
      tabId: 1, url: "https://a.com/", frameId: 0, timeStamp: 1_700_000_000_000,
    });
    await handleWindowFocusChange(42);
    expect(await loadOutbox()).toEqual([]);
    expect(Object.keys(await loadActiveVisits())).toHaveLength(1);
  });
});

describe("buildPayload", () => {
  test("includes chrome_identity from popup label override", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, identityLabel: "Acme Corp" });
    const p = await buildPayload({
      visit: { tabId: 1, scrubbedUrl: "https://x.com/", startTime: 1_700_000_000_000 },
      category: null,
      endTime: 1_700_000_300_000,
      meta: { title: "T", og_description: "d", og_type: "article", favicon_url: "https://x.com/f.ico", lang: "en" },
    });
    expect(p.chrome_identity).toBe("Acme Corp");
    expect(p.og_type).toBe("article");
    expect(p.lang).toBe("en");
    expect(p.start_time).toBe("2023-11-14T22:13:20Z");
    expect(p.end_time).toBe("2023-11-14T22:18:20Z");
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm test background
```
Expected: FAIL (module not found).

- [ ] **Step 3: Implement chrome/src/background.ts**

```typescript
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
import { extractPageMeta, type PageMeta } from "./content";
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
        // This runs in the page. Inline the extractor so the SW doesn't
        // need to bundle content.ts here.
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
    // Page closed, restricted scheme, etc. — fall back to whatever we know.
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
  // For non-categorized events, fetch page meta. For categorized, skip.
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

  // Close any prior visit on this tab.
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

// ---------- wire to chrome APIs (at SW boot) ----------

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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm test background
```
Expected: 11 PASS.

- [ ] **Step 5: Run the WHOLE test suite to check no regressions**

```bash
pnpm test
```
Expected: All previous tests still PASS.

- [ ] **Step 6: Commit**

```bash
cd /Users/Scanning/Developer/fulcra-attention
git add chrome/src/background.ts chrome/tests/background.test.ts
git commit -m "feat(chrome): background SW (webNavigation + active-visit state machine + outbox)"
```

---

## Task 10: Popup — base shell + bearer token form

**Files:**
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/src/popup/main.tsx`
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/src/popup/App.tsx`
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/src/popup/BearerForm.tsx`
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/src/popup/popup.css`

UI tests for the popup are smoke-only (React mounts cleanly, the bearer form persists to storage). Don't over-test the rendering — Vitest + jsdom isn't a substitute for real Chrome.

- [ ] **Step 1: Create chrome/src/popup/popup.css**

```css
/* chrome/src/popup/popup.css */
:root { color-scheme: light dark; }
body { margin: 0; font-family: -apple-system, system-ui, sans-serif; width: 380px; }
.app { padding: 12px; }
.app h1 { font-size: 14px; margin: 0 0 12px 0; font-weight: 600; }
.row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
input[type="text"], input[type="password"] {
  flex: 1; padding: 6px 8px; border: 1px solid #ccc; border-radius: 4px;
  font: inherit; min-width: 0;
}
button {
  padding: 6px 10px; border: 1px solid #888; background: #f5f5f5;
  border-radius: 4px; cursor: pointer; font: inherit;
}
button:hover { background: #eaeaea; }
.muted { color: #777; font-size: 12px; }
.section { margin-top: 14px; padding-top: 10px; border-top: 1px solid #eee; }
.stream-row { font-size: 12px; padding: 3px 0; border-bottom: 1px dotted #eee; }
.stream-row .ts { color: #888; margin-right: 8px; }
.tag { display: inline-block; padding: 1px 5px; border-radius: 3px; font-size: 11px; margin-right: 4px; }
.tag.cat { background: #fcebd5; color: #7a4a00; }
.tag.url { background: #d9eef9; color: #0a4a6e; }
.ignore-list { max-height: 120px; overflow-y: auto; }
.ignore-row { display: flex; align-items: center; gap: 6px; font-size: 12px; padding: 2px 0; }
.ignore-row button { padding: 1px 5px; font-size: 11px; }
```

- [ ] **Step 2: Create chrome/src/popup/main.tsx**

```typescript
// chrome/src/popup/main.tsx
import React from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./popup.css";

const container = document.getElementById("root");
if (container) {
  createRoot(container).render(<App />);
}
```

- [ ] **Step 3: Create chrome/src/popup/BearerForm.tsx**

```typescript
// chrome/src/popup/BearerForm.tsx
import React, { useEffect, useState } from "react";
import { loadSettings, saveSettings } from "../storage";

export function BearerForm() {
  const [token, setToken] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    void loadSettings().then((s) => {
      setToken(s.bearerToken ?? "");
      setEnabled(s.enabled);
    });
  }, []);

  async function save() {
    const cur = await loadSettings();
    await saveSettings({ ...cur, bearerToken: token || null, enabled });
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
  }

  return (
    <div>
      <div className="row">
        <label>
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          Enabled
        </label>
      </div>
      <div className="row">
        <input
          type="password"
          placeholder="Paste bearer token from relay.json"
          value={token}
          onChange={(e) => setToken(e.target.value)}
        />
        <button onClick={save}>Save</button>
      </div>
      {saved && <div className="muted">Saved.</div>}
    </div>
  );
}
```

- [ ] **Step 4: Create chrome/src/popup/App.tsx (skeleton — will grow in later tasks)**

```typescript
// chrome/src/popup/App.tsx
import React from "react";
import { BearerForm } from "./BearerForm";

export function App() {
  return (
    <div className="app">
      <h1>Fulcra Attention</h1>
      <BearerForm />
    </div>
  );
}
```

- [ ] **Step 5: Verify the project builds**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm build
```
Expected: zero errors. `dist/` is populated with manifest.json, html files, JS bundles.

- [ ] **Step 6: Commit**

```bash
cd /Users/Scanning/Developer/fulcra-attention
git add chrome/src/popup/
git commit -m "feat(chrome): popup base (bearer token form + enabled toggle)"
```

---

## Task 11: Popup — live capture stream

**Files:**
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/src/popup/LiveStream.tsx`
- Modify: `/Users/Scanning/Developer/fulcra-attention/chrome/src/popup/App.tsx`

The live stream reads the outbox (and adds a "history" tail of recent emitted events). For v1 simplicity, we keep the last N events in a small `chrome.storage.local["recentEmitted"]` list — appended by `background.ts` whenever it calls `addToOutbox`.

- [ ] **Step 1: Modify chrome/src/background.ts to append to recentEmitted**

Find the `closeVisit` function. Right after `await addToOutbox(payload);` and before `await flushOutbox();`, insert:

```typescript
  // Maintain a small recent-emitted ring for the popup live stream.
  const r = await chrome.storage.local.get("recentEmitted");
  const recent: AttentionEvent[] = (r.recentEmitted as AttentionEvent[] | undefined) ?? [];
  recent.unshift(payload);
  while (recent.length > 10) recent.pop();
  await chrome.storage.local.set({ recentEmitted: recent });
```

- [ ] **Step 2: Run background tests to confirm no regressions**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm test background
```
Expected: 11 PASS still.

- [ ] **Step 3: Create chrome/src/popup/LiveStream.tsx**

```typescript
// chrome/src/popup/LiveStream.tsx
import React, { useEffect, useState } from "react";
import type { AttentionEvent } from "../types";

function ShortTime({ iso }: { iso: string }) {
  const d = new Date(iso);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return <span className="ts">{hh}:{mm}</span>;
}

function Row({ ev }: { ev: AttentionEvent }) {
  if (ev.category !== null) {
    return (
      <div className="stream-row">
        <ShortTime iso={ev.end_time} />
        <span className="tag cat">{ev.category}</span>
      </div>
    );
  }
  return (
    <div className="stream-row">
      <ShortTime iso={ev.end_time} />
      <span className="tag url">{ev.title || ev.url}</span>
    </div>
  );
}

export function LiveStream() {
  const [recent, setRecent] = useState<AttentionEvent[]>([]);

  useEffect(() => {
    let stopped = false;
    async function refresh() {
      const r = await chrome.storage.local.get("recentEmitted");
      const list = (r.recentEmitted as AttentionEvent[] | undefined) ?? [];
      if (!stopped) setRecent(list.slice(0, 5));
    }
    void refresh();
    const id = setInterval(refresh, 2000);
    return () => { stopped = true; clearInterval(id); };
  }, []);

  if (recent.length === 0) {
    return (
      <div className="section">
        <div className="muted">No events captured yet. Visit a page in another tab.</div>
      </div>
    );
  }
  return (
    <div className="section">
      <div className="muted">Last 5 captured (auto-refreshes)</div>
      {recent.map((ev, i) => <Row key={`${ev.end_time}-${i}`} ev={ev} />)}
    </div>
  );
}
```

- [ ] **Step 4: Update chrome/src/popup/App.tsx to include LiveStream**

```typescript
// chrome/src/popup/App.tsx
import React from "react";
import { BearerForm } from "./BearerForm";
import { LiveStream } from "./LiveStream";

export function App() {
  return (
    <div className="app">
      <h1>Fulcra Attention</h1>
      <BearerForm />
      <LiveStream />
    </div>
  );
}
```

- [ ] **Step 5: Verify build still works**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm build
```
Expected: zero errors.

- [ ] **Step 6: Commit**

```bash
cd /Users/Scanning/Developer/fulcra-attention
git add chrome/src/popup/LiveStream.tsx chrome/src/popup/App.tsx chrome/src/background.ts
git commit -m "feat(chrome): popup live capture stream (last 5 emitted events)"
```

---

## Task 12: Popup — ignore list editor, counts, identity label

**Files:**
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/src/popup/IgnoreList.tsx`
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/src/popup/Counts.tsx`
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/src/popup/IdentityLabel.tsx`
- Modify: `/Users/Scanning/Developer/fulcra-attention/chrome/src/popup/App.tsx`
- Modify: `/Users/Scanning/Developer/fulcra-attention/chrome/src/background.ts` (counts increment)

- [ ] **Step 1: Modify background.ts to maintain daily counts**

Find `closeVisit` in `background.ts`. After updating `recentEmitted` (added in Task 11) and BEFORE `await flushOutbox();`, add:

```typescript
  // Daily counts for the popup.
  const today = new Date().toISOString().slice(0, 10);
  const cRaw = await chrome.storage.local.get("counts");
  const c = (cRaw.counts as { date: string; logged: number; categorized: number; ignored: number } | undefined)
    ?? { date: today, logged: 0, categorized: 0, ignored: 0 };
  const isNewDay = c.date !== today;
  const counts = isNewDay
    ? { date: today, logged: 0, categorized: 0, ignored: 0 }
    : c;
  if (payload.category !== null) counts.categorized += 1;
  else counts.logged += 1;
  await chrome.storage.local.set({ counts });
```

Also, in `handleNavigation` where `isIgnored` returns true (returns without queuing), increment the `ignored` counter:

```typescript
  if (await isIgnored(n.url)) {
    const today = new Date().toISOString().slice(0, 10);
    const cRaw = await chrome.storage.local.get("counts");
    const c = (cRaw.counts as { date: string; logged: number; categorized: number; ignored: number } | undefined)
      ?? { date: today, logged: 0, categorized: 0, ignored: 0 };
    const counts = c.date !== today
      ? { date: today, logged: 0, categorized: 0, ignored: 1 }
      : { ...c, ignored: c.ignored + 1 };
    await chrome.storage.local.set({ counts });
    return;
  }
```

- [ ] **Step 2: Re-run background tests**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm test background
```
Expected: 11 PASS still (the count writes don't break any existing assertions).

- [ ] **Step 3: Create chrome/src/popup/Counts.tsx**

```typescript
// chrome/src/popup/Counts.tsx
import React, { useEffect, useState } from "react";

interface CountsState { logged: number; categorized: number; ignored: number; }

export function Counts() {
  const [c, setC] = useState<CountsState>({ logged: 0, categorized: 0, ignored: 0 });

  useEffect(() => {
    let stopped = false;
    async function refresh() {
      const r = await chrome.storage.local.get("counts");
      const today = new Date().toISOString().slice(0, 10);
      const raw = r.counts as { date: string; logged: number; categorized: number; ignored: number } | undefined;
      const cur = raw && raw.date === today ? raw : { date: today, logged: 0, categorized: 0, ignored: 0 };
      if (!stopped) setC({ logged: cur.logged, categorized: cur.categorized, ignored: cur.ignored });
    }
    void refresh();
    const id = setInterval(refresh, 2000);
    return () => { stopped = true; clearInterval(id); };
  }, []);

  return (
    <div className="section">
      <div className="muted">Today: {c.logged} logged · {c.categorized} categorized · {c.ignored} ignored</div>
    </div>
  );
}
```

- [ ] **Step 4: Create chrome/src/popup/IdentityLabel.tsx**

```typescript
// chrome/src/popup/IdentityLabel.tsx
import React, { useEffect, useState } from "react";
import { loadSettings, saveSettings } from "../storage";

export function IdentityLabel() {
  const [label, setLabel] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    void loadSettings().then((s) => setLabel(s.identityLabel ?? ""));
  }, []);

  async function save() {
    const cur = await loadSettings();
    await saveSettings({ ...cur, identityLabel: label.trim() === "" ? null : label.trim() });
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
  }

  return (
    <div className="section">
      <div className="muted">Identity label (overrides Google account email)</div>
      <div className="row">
        <input
          type="text"
          placeholder='e.g. "Acme Corp", "Personal" — blank uses Google email'
          value={label}
          onChange={(e) => setLabel(e.target.value)}
        />
        <button onClick={save}>Save</button>
      </div>
      {saved && <div className="muted">Saved.</div>}
    </div>
  );
}
```

- [ ] **Step 5: Create chrome/src/popup/IgnoreList.tsx**

```typescript
// chrome/src/popup/IgnoreList.tsx
import React, { useEffect, useState } from "react";
import { loadIgnoreList, saveIgnoreList } from "../storage";
import type { IgnoreEntry } from "../types";

export function IgnoreList() {
  const [list, setList] = useState<IgnoreEntry[]>([]);
  const [draft, setDraft] = useState("");

  async function refresh() {
    setList(await loadIgnoreList());
  }

  useEffect(() => { void refresh(); }, []);

  async function add() {
    const pat = draft.trim();
    if (pat === "") return;
    const cur = await loadIgnoreList();
    if (cur.some((e) => e.pattern === pat)) {
      setDraft("");
      return;
    }
    await saveIgnoreList([...cur, { pattern: pat, addedAt: new Date().toISOString() }]);
    setDraft("");
    await refresh();
  }

  async function remove(pat: string) {
    const cur = await loadIgnoreList();
    await saveIgnoreList(cur.filter((e) => e.pattern !== pat));
    await refresh();
  }

  return (
    <div className="section">
      <div className="muted">Ignore list (Tier 3 — dropped entirely; syncs across Chrome profiles)</div>
      <div className="row">
        <input
          type="text"
          placeholder='example.com or *.example.com'
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") void add(); }}
        />
        <button onClick={() => void add()}>Add</button>
      </div>
      <div className="ignore-list">
        {list.length === 0 && <div className="muted">(empty)</div>}
        {list.map((e) => (
          <div key={e.pattern} className="ignore-row">
            <span>{e.pattern}</span>
            <button onClick={() => void remove(e.pattern)}>×</button>
          </div>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 6: Update chrome/src/popup/App.tsx to wire all sections**

```typescript
// chrome/src/popup/App.tsx
import React from "react";
import { BearerForm } from "./BearerForm";
import { LiveStream } from "./LiveStream";
import { IgnoreList } from "./IgnoreList";
import { Counts } from "./Counts";
import { IdentityLabel } from "./IdentityLabel";

export function App() {
  return (
    <div className="app">
      <h1>Fulcra Attention</h1>
      <BearerForm />
      <Counts />
      <LiveStream />
      <IgnoreList />
      <IdentityLabel />
    </div>
  );
}
```

- [ ] **Step 7: Verify build**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm build
```
Expected: zero errors.

- [ ] **Step 8: Commit**

```bash
cd /Users/Scanning/Developer/fulcra-attention
git add chrome/src/popup/ chrome/src/background.ts
git commit -m "feat(chrome): popup ignore list editor + counts + identity label"
```

---

## Task 13: Options page stub

**Files:**
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/src/options/main.tsx`

- [ ] **Step 1: Create chrome/src/options/main.tsx**

```typescript
// chrome/src/options/main.tsx
//
// v1 placeholder. v1.5 will host the full Tier 2 category-mapping editor,
// preset import buttons (finance, healthcare, ai-chat, adult-via-oisd), and
// ignore-list import/export.

import React from "react";
import { createRoot } from "react-dom/client";

function Options() {
  return (
    <div style={{ fontFamily: "-apple-system, system-ui, sans-serif", padding: 20, maxWidth: 600 }}>
      <h1>Fulcra Attention — Options</h1>
      <p>
        The full Tier 2 category editor and preset importer ships in v1.5. For now, manage
        your ignore list and identity label from the popup.
      </p>
      <p>
        See the project README for the design spec and roadmap.
      </p>
    </div>
  );
}

const container = document.getElementById("root");
if (container) {
  createRoot(container).render(<Options />);
}
```

- [ ] **Step 2: Verify build**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm build
```
Expected: zero errors. `dist/options.html` and its bundle exist.

- [ ] **Step 3: Commit**

```bash
cd /Users/Scanning/Developer/fulcra-attention
git add chrome/src/options/
git commit -m "feat(chrome): options page stub (v1.5 placeholder)"
```

---

## Task 14: README + final validation

**Files:**
- Create: `/Users/Scanning/Developer/fulcra-attention/chrome/README.md`

- [ ] **Step 1: Create chrome/README.md**

```markdown
# fulcra-attention — Chrome MV3 extension

The browser-side half of [fulcra-attention](../README.md). Captures every page you visit (URL + title + OG description + favicon + time-on-page) and POSTs it to the loopback relay at `127.0.0.1:8771`, which forwards it into your Fulcra account.

## Develop

```bash
cd chrome
pnpm install        # or: npm install
pnpm dev            # Vite dev mode with hot reload
pnpm test           # Vitest run (cross-language scrub gate included)
pnpm build          # Production build to dist/
```

## Load into Chrome

1. Build: `pnpm build`
2. Open `chrome://extensions/`
3. Enable "Developer mode" (top right)
4. Click "Load unpacked"
5. Choose `chrome/dist/`
6. Open the extension popup and paste the bearer token from `~/.config/fulcra-attention/relay.json`

## Architecture

- `src/background.ts` — MV3 service worker. Subscribes to `webNavigation.onCommitted` + `onHistoryStateUpdated` (top frame only), runs a per-tab active-visit state machine, closes the visit on next nav / tab close / window blur, then queues an `AttentionEvent` in the outbox.
- `src/scrub.ts` — Tier 1 always-on URL scrubber. Byte-identical to the Python sibling via the shared fixture `../tests/fixtures/scrub_cases.json`. 55 contract tests must all pass.
- `src/categorize.ts` / `src/ignore.ts` — User-driven Tier 2 (categorize) and Tier 3 (ignore). Both default to empty.
- `src/identity.ts` — chrome_identity capture (Google account email or popup label override).
- `src/content.ts` — Page-meta extractor (title, og:description, og:type, favicon, html lang).
- `src/outbox.ts` — Write-ahead queue in `chrome.storage.local`. Retries on alarm ticks every 60s.
- `src/popup/` — React popup: bearer token, on/off, live last-5 stream, ignore list, identity label, daily counts.

## Storage map

| Where | What |
|---|---|
| `chrome.storage.local["settings"]` | bearer token, port, enabled toggle, identity label |
| `chrome.storage.local["outbox"]` | pending POST queue |
| `chrome.storage.local["categoryMap"]` | Tier 2 domain → category mappings |
| `chrome.storage.local["recentEmitted"]` | last 10 events for popup display |
| `chrome.storage.local["counts"]` | today's logged / categorized / ignored counters |
| `chrome.storage.sync["ignoreList"]` | Tier 3 — propagates across Chrome profiles |
| `chrome.storage.session["activeVisits"]` | open per-tab visit state |

## Manual smoke test

After install + paste bearer token:

1. Open a fresh tab → visit `https://example.com/`
2. Open another fresh tab → visit `https://news.ycombinator.com/`
3. Open the popup. The "Last 5 captured" stream should show one of those (depending on which closed first).
4. Run on the Plan A side: `fulcra get-records --type DurationAnnotation --start "5 minutes ago" | jq '.[] | select(.data.service == "web")'` and confirm the events landed in Fulcra.

## v2 roadmap

- OAuth (Auth0) direct from extension — drops the relay dependency. See `docs/superpowers/specs/2026-05-18-fulcra-browse-extension-auth0-app.md` in the sibling FulcraMediaHelpers repo.
- Highlights (text selection → annotation linked to parent visit)
- Retrieval surface (popup search bar)
- Tier 2 editor in options page (v1.5)
- Safari Web Extension wrapper (separate project)
```

- [ ] **Step 2: Run the full test suite as a final gate**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm test
```
Expected: All tests PASS. Total count: 55 (scrub) + 7 (storage) + 8 (ignore) + 4 (categorize) + 4 (identity) + 9 (outbox) + 8 (content) + 11 (background) = **106 tests**.

- [ ] **Step 3: Run production build as a final gate**

```bash
pnpm build
```
Expected: zero errors. `dist/` populated with `manifest.json`, `index.html`, `options.html`, and all JS bundles.

- [ ] **Step 4: Commit**

```bash
cd /Users/Scanning/Developer/fulcra-attention
git add chrome/README.md
git commit -m "docs(chrome): README with install + architecture + smoke test"
```

- [ ] **Step 5: Push**

```bash
cd /Users/Scanning/Developer/fulcra-attention
git push
```

---

## Task 15: Manual end-to-end validation (user-driven, not subagent)

This is a **hand-on validation gate** for the user (ash) to run when convenient. Not an automated task.

- [ ] **Step 1: Build the extension**

```bash
cd /Users/Scanning/Developer/fulcra-attention/chrome
pnpm install
pnpm build
```

- [ ] **Step 2: Confirm the relay is running**

```bash
curl http://127.0.0.1:8771/health
```
Expected: `{"ok":true,"definition_id":"<uuid>","received":N,"posted":M,"dropped":0}`.

If the relay isn't running, start it: `fulcra-attention setup` (one-time), then `launchctl load ~/Library/LaunchAgents/com.fulcra.attention.relay.plist` (macOS).

- [ ] **Step 3: Load the extension**

1. Open `chrome://extensions/`
2. Enable Developer mode
3. Click "Load unpacked"
4. Select `/Users/Scanning/Developer/fulcra-attention/chrome/dist/`

- [ ] **Step 4: Configure**

1. Click the Fulcra Attention extension icon (toolbar).
2. Paste the bearer token: `jq -r .bearer_token ~/.config/fulcra-attention/relay.json`
3. Click Save.
4. (Optional) Set an identity label like "Fulcra Work".

- [ ] **Step 5: Smoke test**

1. Open a new tab. Visit `https://example.com/`.
2. Visit `https://news.ycombinator.com/`.
3. Close the first tab.
4. Open the extension popup. Confirm:
   - The "Today" counter shows ≥ 1 logged.
   - The "Last 5 captured" stream shows the events with titles.

- [ ] **Step 6: Confirm in Fulcra**

```bash
fulcra get-records --type DurationAnnotation --start "5 minutes ago" \
  | jq '.[] | select(.data.service == "web") | {title: .data.title, url: .data.url, chrome_identity: .data.external_ids.chrome_identity}'
```
Expected: at least one record with `service: "web"`, the actual URL, the title, and the chrome_identity field populated.

- [ ] **Step 7: Test Tier 3 ignore**

1. In the popup, add `news.ycombinator.com` to the ignore list.
2. Visit `https://news.ycombinator.com/` again.
3. Wait for the next nav / tab close.
4. Confirm: the popup's "Today" counter shows +1 ignored, NOT +1 logged.

- [ ] **Step 8: Test Tier 1 scrubbing**

Visit `https://example.com/page?access_token=secret123&id=42`. Wait for the visit to close.

Check Fulcra:
```bash
fulcra get-records --type DurationAnnotation --start "1 minute ago" \
  | jq '.[] | .data.url'
```
Expected: `"https://example.com/page?id=42"` — the `access_token` param is gone.

- [ ] **Step 9: Mark validated**

Drop a note in your task tracker or commit an empty commit:
```bash
cd /Users/Scanning/Developer/fulcra-attention
git commit --allow-empty -m "smoke: Plan B end-to-end validated against live Fulcra"
git push
```

End of Plan B.
