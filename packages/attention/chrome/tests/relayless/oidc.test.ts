// chrome/tests/relayless/oidc.test.ts
import { describe, test, expect } from "vitest";
import { FulcraOidc, OidcError } from "../../src/relayless/oidc";
import {
  DEVICE_CODE_URL,
  OIDC_CLIENT_ID,
  DEVICE_CODE_GRANT,
} from "../../src/relayless/config";
import { mockFetch } from "./memStorage";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const noSleep = async () => {};

function formParams(init?: RequestInit): URLSearchParams {
  return new URLSearchParams((init as RequestInit).body as string);
}

describe("requestDeviceCode", () => {
  test("POSTs form body with client_id/audience/scope and parses response", async () => {
    const fetchFn = mockFetch(async () =>
      jsonResponse(200, {
        device_code: "DEV",
        user_code: "WXYZ-1234",
        verification_uri: "https://fulcra.us.auth0.com/activate",
        verification_uri_complete:
          "https://fulcra.us.auth0.com/activate?user_code=WXYZ-1234",
        expires_in: 900,
        interval: 5,
      }),
    );
    const oidc = new FulcraOidc({ fetch: fetchFn });
    const dc = await oidc.requestDeviceCode();
    expect(dc.device_code).toBe("DEV");
    expect(dc.interval).toBe(5);

    const [url, init] = fetchFn.mock.calls[0];
    expect(url).toBe(DEVICE_CODE_URL);
    expect((init as RequestInit).method).toBe("POST");
    const params = formParams(init);
    expect(params.get("client_id")).toBe(OIDC_CLIENT_ID);
    expect(params.get("audience")).toBe("https://api.fulcradynamics.com/");
    expect(params.get("scope")).toContain("offline_access");
    expect((init as RequestInit).headers).toMatchObject({
      "Content-Type": "application/x-www-form-urlencoded",
    });
  });

  test("throws OidcError on non-200", async () => {
    const fetchFn = mockFetch(async () => new Response(null, { status: 500 }));
    const oidc = new FulcraOidc({ fetch: fetchFn });
    await expect(oidc.requestDeviceCode()).rejects.toThrow(OidcError);
  });
});

describe("pollForToken state machine", () => {
  test("pending then success resolves with the token set", async () => {
    let call = 0;
    const fetchFn = mockFetch(async () => {
      call += 1;
      if (call < 3) return jsonResponse(400, { error: "authorization_pending" });
      return jsonResponse(200, {
        access_token: "AT",
        refresh_token: "RT",
        expires_in: 86400,
      });
    });
    const oidc = new FulcraOidc({ fetch: fetchFn });
    const token = await oidc.pollForToken("DEV", 5, { sleep: noSleep });
    expect(token.access_token).toBe("AT");
    expect(token.refresh_token).toBe("RT");
    expect(fetchFn).toHaveBeenCalledTimes(3);
    const params = formParams(fetchFn.mock.calls[0][1]);
    expect(params.get("grant_type")).toBe(DEVICE_CODE_GRANT);
    expect(params.get("device_code")).toBe("DEV");
    expect(params.get("client_id")).toBe(OIDC_CLIENT_ID);
  });

  test("slow_down increases the polling interval by 5s", async () => {
    const sleeps: number[] = [];
    const sleep = async (ms: number) => {
      sleeps.push(ms);
    };
    let call = 0;
    const fetchFn = mockFetch(async () => {
      call += 1;
      if (call === 1) return jsonResponse(400, { error: "authorization_pending" });
      if (call === 2) return jsonResponse(429, { error: "slow_down" });
      return jsonResponse(200, { access_token: "AT", expires_in: 3600 });
    });
    const oidc = new FulcraOidc({ fetch: fetchFn });
    await oidc.pollForToken("DEV", 5, { sleep });
    // Sleep before attempt1=5s, attempt2=5s, then slow_down bumps to 10s.
    expect(sleeps).toEqual([5000, 5000, 10000]);
  });

  test("expired_token rejects with code expired_token", async () => {
    const fetchFn = mockFetch(async () =>
      jsonResponse(400, { error: "expired_token" }),
    );
    const oidc = new FulcraOidc({ fetch: fetchFn });
    await expect(
      oidc.pollForToken("DEV", 5, { sleep: noSleep }),
    ).rejects.toMatchObject({ code: "expired_token" });
  });

  test("access_denied rejects with code access_denied", async () => {
    const fetchFn = mockFetch(async () =>
      jsonResponse(403, { error: "access_denied" }),
    );
    const oidc = new FulcraOidc({ fetch: fetchFn });
    await expect(
      oidc.pollForToken("DEV", 5, { sleep: noSleep }),
    ).rejects.toMatchObject({ code: "access_denied" });
  });

  test("gives up after maxAttempts with code timeout", async () => {
    const fetchFn = mockFetch(async () =>
      jsonResponse(400, { error: "authorization_pending" }),
    );
    const oidc = new FulcraOidc({ fetch: fetchFn });
    await expect(
      oidc.pollForToken("DEV", 5, { sleep: noSleep, maxAttempts: 3 }),
    ).rejects.toMatchObject({ code: "timeout" });
    expect(fetchFn).toHaveBeenCalledTimes(3);
  });
});

describe("refresh", () => {
  test("POSTs grant_type=refresh_token and returns a fresh token set", async () => {
    const fetchFn = mockFetch(async () =>
      jsonResponse(200, {
        access_token: "AT2",
        refresh_token: "RT2",
        expires_in: 3600,
      }),
    );
    const oidc = new FulcraOidc({ fetch: fetchFn });
    const token = await oidc.refresh("RT");
    expect(token.access_token).toBe("AT2");
    const params = formParams(fetchFn.mock.calls[0][1]);
    expect(params.get("grant_type")).toBe("refresh_token");
    expect(params.get("refresh_token")).toBe("RT");
    expect(params.get("client_id")).toBe(OIDC_CLIENT_ID);
  });

  test("throws OidcError when the refresh is rejected", async () => {
    const fetchFn = mockFetch(async () =>
      jsonResponse(403, { error: "invalid_grant" }),
    );
    const oidc = new FulcraOidc({ fetch: fetchFn });
    await expect(oidc.refresh("RT")).rejects.toMatchObject({
      code: "invalid_grant",
    });
  });
});
