// chrome/tests/outbox-relayless.test.ts
//
// The relayless transport branch of the outbox flush. Uses the global
// chrome.storage mock (setup.ts) because TokenStore / SentSet /
// ensureAttentionDefinitionAndTags all default to chrome.storage.local. We
// seed the token set + the resolved-attention cache directly and mock the
// global fetch so no network is touched.

import { describe, test, expect, beforeEach, vi } from "vitest";
import { addToOutbox, flushOutbox } from "../src/outbox";
import { loadOutbox, saveSettings } from "../src/storage";
import { DEFAULT_SETTINGS } from "../src/types";
import { INGEST_BATCH_URL, TOKEN_URL } from "../src/relayless/config";
import { mockFetch } from "./relayless/memStorage";
import type { AttentionEvent } from "../src/types";

function makeEvent(url = "https://x.com/"): AttentionEvent {
  return {
    url, title: "T", og_description: null, favicon_url: null,
    category: null, chrome_identity: null, og_type: null, lang: null,
    start_time: "2026-05-18T14:00:00Z", end_time: "2026-05-18T14:05:00Z",
    client: "fulcra-attention-chrome/0.1.0",
  };
}

/** Seed a non-expired token set into the global chrome.storage.local under
 * the TokenStore key. */
async function seedToken() {
  await chrome.storage.local.set({
    relaylessTokens: {
      accessToken: "ACCESS",
      refreshToken: "REFRESH",
      expiresAt: Date.now() + 3_600_000,
    },
  });
}

/** Seed the resolved attention cache so ensure makes no network calls. */
async function seedResolved() {
  await chrome.storage.local.set({
    relaylessResolvedAttention: {
      definitionId: "def-1",
      tagIds: ["tag-attn", "tag-web"],
    },
  });
}

async function getError() {
  const r = await chrome.storage.local.get("lastIngestError");
  return r.lastIngestError as { kind: string } | undefined;
}

beforeEach(async () => {
  await chrome.storage.local.clear();
  await saveSettings({ ...DEFAULT_SETTINGS, transportMode: "relayless" });
});

describe("flushOutbox — relayless mode", () => {
  test("with a valid token + resolved ids, sends to ingest and clears the outbox", async () => {
    await seedToken();
    await seedResolved();
    await addToOutbox(makeEvent("https://a.com/"));
    await addToOutbox(makeEvent("https://b.com/"));

    const f = mockFetch(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", f);

    await flushOutbox();

    // POSTed to the cloud ingest endpoint (not the localhost relay).
    const ingestCalls = f.mock.calls.filter(
      (c): c is [RequestInfo | URL, RequestInit] => String(c[0]) === INGEST_BATCH_URL,
    );
    expect(ingestCalls.length).toBe(1);
    const init = ingestCalls[0][1];
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer ACCESS");
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/x-jsonl");
    // Both events in one JSONL batch.
    expect((init.body as string).split("\n")).toHaveLength(2);

    // Outbox cleared; error state cleared.
    expect(await loadOutbox()).toHaveLength(0);
    expect(await getError()).toBeUndefined();
  });

  test("no token → needs-sign-in (unauthorized), events retained, no ingest POST", async () => {
    // No seedToken().
    await seedResolved();
    await addToOutbox(makeEvent());
    const f = mockFetch(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", f);

    await flushOutbox();

    expect(await loadOutbox()).toHaveLength(1); // retained
    expect((await getError())?.kind).toBe("unauthorized");
    const ingestCalls = f.mock.calls.filter((c) => String(c[0]) === INGEST_BATCH_URL);
    expect(ingestCalls.length).toBe(0);
  });

  test("ingest 401 → unauthorized, events retained", async () => {
    await seedToken();
    await seedResolved();
    await addToOutbox(makeEvent());
    const f = mockFetch(async () => new Response("", { status: 401 }));
    vi.stubGlobal("fetch", f);

    await flushOutbox();

    expect(await loadOutbox()).toHaveLength(1);
    expect((await getError())?.kind).toBe("unauthorized");
  });

  test("ingest network failure → unreachable, events retained", async () => {
    await seedToken();
    await seedResolved();
    await addToOutbox(makeEvent());
    const f = mockFetch(async () => { throw new TypeError("network"); });
    vi.stubGlobal("fetch", f);

    await flushOutbox();

    expect(await loadOutbox()).toHaveLength(1);
    expect((await getError())?.kind).toBe("unreachable");
  });

  test("empty outbox with token clears a stale unreachable", async () => {
    await seedToken();
    await seedResolved();
    await chrome.storage.local.set({ lastIngestError: { kind: "unreachable", at: 1 } });
    const f = mockFetch(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", f);

    await flushOutbox();

    expect(await getError()).toBeUndefined();
  });

  test("ingest 401 on a still-fresh token → force-refresh + retry succeeds, outbox cleared", async () => {
    // Token is NOT stale by the local clock, but the server has revoked it:
    // the first ingest POST returns 401. The outbox getToken adapter must
    // honor force:true and refresh via the refresh grant, then the retry POST
    // (carrying the refreshed token) succeeds. Exactly one real refresh.
    await seedToken(); // fresh, non-expired
    await seedResolved();
    await addToOutbox(makeEvent());

    let ingestCalls = 0;
    let refreshCalls = 0;
    const f = mockFetch(async (input, init) => {
      const url = String(input);
      if (url === TOKEN_URL) {
        refreshCalls += 1;
        return new Response(
          JSON.stringify({ access_token: "REFRESHED", expires_in: 3600 }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url === INGEST_BATCH_URL) {
        ingestCalls += 1;
        const auth = (init?.headers as Record<string, string>).Authorization;
        // First POST uses the stale-but-fresh token and is rejected; the
        // forced refresh swaps it for REFRESHED, which the API accepts.
        return auth === "Bearer REFRESHED"
          ? new Response("{}", { status: 200 })
          : new Response("", { status: 401 });
      }
      return new Response("{}", { status: 200 });
    });
    vi.stubGlobal("fetch", f);

    await flushOutbox();

    expect(ingestCalls).toBe(2); // 401 then retry
    expect(refreshCalls).toBe(1); // exactly one real refresh
    expect(await loadOutbox()).toHaveLength(0); // sent, cleared
    expect(await getError()).toBeUndefined();
    // The refreshed token is persisted for next time.
    const stored = (await chrome.storage.local.get("relaylessTokens"))
      .relaylessTokens as { accessToken: string };
    expect(stored.accessToken).toBe("REFRESHED");
  });

  test("ensure failure (network) when uncached → unreachable, events retained", async () => {
    await seedToken();
    // No seedResolved() → ensure must hit the API, which fails.
    await addToOutbox(makeEvent());
    const f = mockFetch(async () => { throw new TypeError("network"); });
    vi.stubGlobal("fetch", f);

    await flushOutbox();

    expect(await loadOutbox()).toHaveLength(1);
    expect((await getError())?.kind).toBe("unreachable");
  });
});
