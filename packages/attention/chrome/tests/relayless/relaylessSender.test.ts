// chrome/tests/relayless/relaylessSender.test.ts
import { describe, test, expect, vi } from "vitest";
import { sendBatch } from "../../src/relayless/relaylessSender";
import { SentSet } from "../../src/relayless/sentSet";
import { INGEST_BATCH_URL } from "../../src/relayless/config";
import { memStorage, mockFetch } from "./memStorage";
import type { AttentionEvent } from "../../src/types";

const CTX = { definitionId: "def-1", tagIds: ["t-attn", "t-web"] };

function ev(url: string, start = "2026-05-18T14:00:00Z"): AttentionEvent {
  return {
    url,
    title: "T",
    og_description: null,
    favicon_url: null,
    category: null,
    chrome_identity: null,
    og_type: null,
    lang: null,
    start_time: start,
    end_time: "2026-05-18T14:05:00Z",
    client: "c",
  };
}

const okResp = () => new Response("{}", { status: 200 });

describe("sendBatch", () => {
  test("POSTs survivors to /ingest/v1/record/batch with Bearer + records on 2xx", async () => {
    const sentSet = new SentSet({ storage: memStorage() });
    const fetchFn = mockFetch(async () => okResp());
    const getToken = vi.fn(async () => "TOK");
    const res = await sendBatch([ev("https://a.com/"), ev("https://b.com/")], {
      getToken,
      fetch: fetchFn,
      context: CTX,
      sentSet,
    });
    expect(res.ok).toBe(true);
    expect(res.sent).toHaveLength(2);
    expect(fetchFn).toHaveBeenCalledTimes(1);
    const [url, init] = fetchFn.mock.calls[0];
    expect(url).toBe(INGEST_BATCH_URL);
    expect((init as RequestInit).headers).toMatchObject({
      Authorization: "Bearer TOK",
      "Content-Type": "application/x-jsonl",
    });
    // Body is JSONL: 2 newline-joined records.
    expect(((init as RequestInit).body as string).split("\n")).toHaveLength(2);
    // Both source_ids now recorded.
    for (const sid of res.sent) expect(await sentSet.has(sid)).toBe(true);
  });

  test("skips events whose source_id is already in the sent-set", async () => {
    const storage = memStorage();
    const sentSet = new SentSet({ storage });
    // Pre-seed with the source_id of the 'a.com' event by sending it once.
    const fetchFn = mockFetch(async () => okResp());
    const getToken = vi.fn(async () => "TOK");
    const first = await sendBatch([ev("https://a.com/")], {
      getToken,
      fetch: fetchFn,
      context: CTX,
      sentSet,
    });
    expect(first.sent).toHaveLength(1);

    // Second flush includes the same a.com event plus a new b.com event.
    const fetch2 = mockFetch(async () => okResp());
    const res = await sendBatch([ev("https://a.com/"), ev("https://b.com/")], {
      getToken,
      fetch: fetch2,
      context: CTX,
      sentSet,
    });
    expect(res.skipped).toHaveLength(1); // a.com skipped
    expect(res.sent).toHaveLength(1); // only b.com sent
    // Only b.com on the wire — one record.
    expect(((fetch2.mock.calls[0][1] as RequestInit).body as string).split("\n")).toHaveLength(1);
  });

  test("de-dupes duplicate source_ids within a single flush (claim-once)", async () => {
    const sentSet = new SentSet({ storage: memStorage() });
    const fetchFn = mockFetch(async () => okResp());
    const res = await sendBatch(
      [ev("https://a.com/"), ev("https://a.com/")], // same url+start -> same sid
      {
        getToken: async () => "TOK",
        fetch: fetchFn,
        context: CTX,
        sentSet,
      },
    );
    expect(res.sent).toHaveLength(1);
    expect(((fetchFn.mock.calls[0][1] as RequestInit).body as string).split("\n")).toHaveLength(1);
  });

  test("refreshes the token once on 401 and retries", async () => {
    const sentSet = new SentSet({ storage: memStorage() });
    let call = 0;
    const fetchFn = mockFetch(async () => {
      call += 1;
      return call === 1 ? new Response(null, { status: 401 }) : okResp();
    });
    const getToken = vi.fn(async (opts?: { force?: boolean }) =>
      opts?.force ? "FRESH" : "STALE",
    );
    const res = await sendBatch([ev("https://a.com/")], {
      getToken,
      fetch: fetchFn,
      context: CTX,
      sentSet,
    });
    expect(res.ok).toBe(true);
    expect(fetchFn).toHaveBeenCalledTimes(2);
    // Second call carried the forced-fresh token.
    expect((fetchFn.mock.calls[1][1] as RequestInit).headers).toMatchObject({
      Authorization: "Bearer FRESH",
    });
    expect(getToken).toHaveBeenCalledWith({ force: true });
  });

  test("does NOT record on a non-2xx (5xx) — retries next flush", async () => {
    const sentSet = new SentSet({ storage: memStorage() });
    const fetchFn = mockFetch(async () => new Response(null, { status: 502 }));
    const res = await sendBatch([ev("https://a.com/")], {
      getToken: async () => "TOK",
      fetch: fetchFn,
      context: CTX,
      sentSet,
    });
    expect(res.ok).toBe(false);
    expect(res.failureStatus).toBe(502);
    expect(res.sent).toHaveLength(0);
    expect(await sentSet.size()).toBe(0);
  });

  test("ok with nothing sent when not signed in (no token)", async () => {
    const sentSet = new SentSet({ storage: memStorage() });
    const fetchFn = mockFetch(async () => okResp());
    const res = await sendBatch([ev("https://a.com/")], {
      getToken: async () => null,
      fetch: fetchFn,
      context: CTX,
      sentSet,
    });
    expect(res.ok).toBe(false);
    expect(res.failureStatus).toBe(0);
    expect(fetchFn).not.toHaveBeenCalled();
    expect(await sentSet.size()).toBe(0);
  });

  test("empty event list is a no-op success", async () => {
    const fetchFn = mockFetch(async () => okResp());
    const res = await sendBatch([], {
      getToken: async () => "TOK",
      fetch: fetchFn,
      context: CTX,
      sentSet: new SentSet({ storage: memStorage() }),
    });
    expect(res.ok).toBe(true);
    expect(fetchFn).not.toHaveBeenCalled();
  });

  test("transport error reports ok=false without recording", async () => {
    const sentSet = new SentSet({ storage: memStorage() });
    const fetchFn = mockFetch(async () => {
      throw new TypeError("network");
    });
    const res = await sendBatch([ev("https://a.com/")], {
      getToken: async () => "TOK",
      fetch: fetchFn,
      context: CTX,
      sentSet,
    });
    expect(res.ok).toBe(false);
    expect(res.failureStatus).toBe(0);
    expect(await sentSet.size()).toBe(0);
  });
});
