// chrome/tests/relayless/signIn.test.ts
import { describe, test, expect, vi } from "vitest";
import { startDeviceSignIn } from "../../src/relayless/signIn";
import { TokenStore } from "../../src/relayless/tokenStore";
import {
  DEVICE_CODE_URL,
  TOKEN_URL,
} from "../../src/relayless/config";
import { memStorage, mockFetch } from "./memStorage";

const DEVICE_RESP = {
  device_code: "DEV-CODE",
  user_code: "WXYZ-1234",
  verification_uri: "https://fulcra.us.auth0.com/activate",
  verification_uri_complete: "https://fulcra.us.auth0.com/activate?code=WXYZ-1234",
  expires_in: 900,
  interval: 5,
};

const TOKEN_RESP = {
  access_token: "ACCESS",
  refresh_token: "REFRESH",
  expires_in: 3600,
  token_type: "Bearer",
};

describe("startDeviceSignIn", () => {
  test("happy path: prompts, polls, stores tokens", async () => {
    const storage = memStorage();
    let pollCount = 0;
    const fetchFn = mockFetch(async (input) => {
      const url = String(input);
      if (url === DEVICE_CODE_URL) {
        return new Response(JSON.stringify(DEVICE_RESP), { status: 200 });
      }
      if (url === TOKEN_URL) {
        pollCount += 1;
        // First poll: pending; second: success.
        if (pollCount === 1) {
          return new Response(JSON.stringify({ error: "authorization_pending" }), { status: 400 });
        }
        return new Response(JSON.stringify(TOKEN_RESP), { status: 200 });
      }
      throw new Error(`unexpected ${url}`);
    });

    const onPrompt = vi.fn();
    const sleep = vi.fn(async () => undefined);

    const res = await startDeviceSignIn({
      onPrompt,
      fetch: fetchFn,
      storage,
      sleep,
    });

    expect(res.ok).toBe(true);
    // onPrompt called with the complete verification URI + user code.
    expect(onPrompt).toHaveBeenCalledWith(
      DEVICE_RESP.verification_uri_complete,
      DEVICE_RESP.user_code,
    );
    // Tokens persisted.
    const store = new TokenStore({ storage });
    const stored = await store.get();
    expect(stored?.accessToken).toBe("ACCESS");
    expect(stored?.refreshToken).toBe("REFRESH");
    expect(stored?.expiresAt).toBeGreaterThan(Date.now());
    // Polled twice (pending then success).
    expect(pollCount).toBe(2);
  });

  test("rejects when the user denies", async () => {
    const storage = memStorage();
    const fetchFn = mockFetch(async (input) => {
      const url = String(input);
      if (url === DEVICE_CODE_URL) {
        return new Response(JSON.stringify(DEVICE_RESP), { status: 200 });
      }
      return new Response(JSON.stringify({ error: "access_denied" }), { status: 400 });
    });
    await expect(
      startDeviceSignIn({ onPrompt: vi.fn(), fetch: fetchFn, storage, sleep: async () => undefined }),
    ).rejects.toThrow();
    // No tokens stored.
    const store = new TokenStore({ storage });
    expect(await store.get()).toBeNull();
  });
});
