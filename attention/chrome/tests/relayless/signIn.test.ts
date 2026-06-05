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

  test("awaits the Auth0 Origin-strip DNR registration before the device-code POST — Bug A4", async () => {
    const storage = memStorage();
    const order: string[] = [];
    let releaseRegister!: () => void;
    const registerGate = new Promise<void>((r) => {
      releaseRegister = r;
    });
    const registerOriginStrip = vi.fn(async () => {
      await registerGate; // stays pending until we release it
      order.push("registered");
    });

    const fetchFn = mockFetch(async (input) => {
      const url = String(input);
      if (url === DEVICE_CODE_URL) {
        order.push("device-code");
        return new Response(JSON.stringify(DEVICE_RESP), { status: 200 });
      }
      if (url === TOKEN_URL) {
        return new Response(JSON.stringify(TOKEN_RESP), { status: 200 });
      }
      throw new Error(`unexpected ${url}`);
    });

    const promise = startDeviceSignIn({
      onPrompt: vi.fn(),
      fetch: fetchFn,
      storage,
      sleep: async () => undefined,
      registerOriginStrip,
    });

    // Let any microtasks run: the device-code POST must NOT have fired yet
    // because the (still-pending) registration is awaited first.
    await Promise.resolve();
    await Promise.resolve();
    expect(registerOriginStrip).toHaveBeenCalledTimes(1);
    expect(order).not.toContain("device-code");

    // Release the registration → the flow proceeds.
    releaseRegister();
    await promise;

    // Registration completed strictly before the device-code request.
    expect(order).toEqual(["registered", "device-code"]);
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
