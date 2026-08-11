import { describe, expect, it, vi } from "vitest";

import {
  NATIVE_AUTH_STATE,
  NATIVE_INGEST,
  nativeAuthState,
  sendBatchViaNative,
} from "../../src/relayless/nativeTransport";
import type { AttentionEvent } from "../../src/types";

const event = (url = "https://a.example/1"): AttentionEvent => ({
  url,
  title: "t",
  og_description: null,
  favicon_url: null,
  category: null,
  chrome_identity: null,
  og_type: null,
  lang: null,
  start_time: "2026-06-01T00:00:00.000Z",
  end_time: "2026-06-01T00:01:00.000Z",
  client: "fulcra-attention-safari/0.1.0",
});

describe("sendBatchViaNative", () => {
  it("sends the ingest message with the events passed through unchanged", async () => {
    // The outbox already stores the snake_case wire shape, and the Swift side
    // decodes exactly those keys. Any remapping here is a chance to lose a
    // field silently, so the test pins the pass-through.
    const send = vi.fn().mockResolvedValue({ ok: true, sent: 1, skipped: 0 });
    await sendBatchViaNative([event()], send);

    expect(send).toHaveBeenCalledTimes(1);
    const msg = send.mock.calls[0][0] as { type: string; events: AttentionEvent[] };
    expect(msg.type).toBe(NATIVE_INGEST);
    expect(msg.events[0]).toEqual(event());
    expect(msg.events[0]).toHaveProperty("start_time");
    expect(msg.events[0]).not.toHaveProperty("startTime");
  });

  it("reports ok with the counts the native side returned", async () => {
    const send = vi.fn().mockResolvedValue({ ok: true, sent: 3, skipped: 2 });
    await expect(sendBatchViaNative([event()], send)).resolves.toEqual({
      kind: "ok",
      sent: 3,
      skipped: 2,
    });
  });

  it("treats needs_sign_in as unauthorized even when ok is absent", async () => {
    // The one outcome the user can act on. Folding it into a generic failure
    // would have the extension back off silently while the fix is a tap.
    const send = vi.fn().mockResolvedValue({ ok: false, needs_sign_in: true, error: "no token" });
    await expect(sendBatchViaNative([event()], send)).resolves.toMatchObject({
      kind: "unauthorized",
    });
  });

  it("treats ok:false as unreachable so the caller RETAINS the outbox", async () => {
    const send = vi.fn().mockResolvedValue({ ok: false, error: "HTTP 500" });
    await expect(sendBatchViaNative([event()], send)).resolves.toMatchObject({
      kind: "unreachable",
    });
  });

  it("does NOT read a missing ok as success", async () => {
    // An extension host that returns nothing looks identical to one that
    // quietly dropped the batch. Defaulting to success here would clear events
    // the native side never accepted.
    for (const reply of [{}, null, undefined, { sent: 5 }]) {
      const send = vi.fn().mockResolvedValue(reply);
      await expect(sendBatchViaNative([event()], send)).resolves.toMatchObject({
        kind: "unreachable",
      });
    }
  });

  it("reports a thrown transport error as unreachable rather than propagating", async () => {
    const send = vi.fn().mockRejectedValue(new Error("host not found"));
    await expect(sendBatchViaNative([event()], send)).resolves.toMatchObject({
      kind: "unreachable",
    });
  });

  it("reports unreachable when native messaging is unavailable", async () => {
    await expect(sendBatchViaNative([event()], null)).resolves.toMatchObject({
      kind: "unreachable",
    });
  });

  it("treats an empty batch as success without a round trip", async () => {
    const send = vi.fn();
    await expect(sendBatchViaNative([], send)).resolves.toEqual({
      kind: "ok",
      sent: 0,
      skipped: 0,
    });
    expect(send).not.toHaveBeenCalled();
  });
});

describe("nativeAuthState", () => {
  it("reports the native side's signed_in flag", async () => {
    const send = vi.fn().mockResolvedValue({ ok: true, signed_in: true });
    await expect(nativeAuthState(send)).resolves.toBe(true);
    expect((send.mock.calls[0][0] as { type: string }).type).toBe(NATIVE_AUTH_STATE);
  });

  it("reports not-signed-in when the bridge is unavailable or throws", async () => {
    await expect(nativeAuthState(null)).resolves.toBe(false);
    await expect(
      nativeAuthState(vi.fn().mockRejectedValue(new Error("nope"))),
    ).resolves.toBe(false);
  });
});
