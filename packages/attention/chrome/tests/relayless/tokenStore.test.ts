// chrome/tests/relayless/tokenStore.test.ts
import { describe, test, expect, vi } from "vitest";
import { TokenStore, EXPIRY_SKEW_MS } from "../../src/relayless/tokenStore";
import { memStorage } from "./memStorage";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const NOW = 1_000_000_000_000;

describe("get/set/clear", () => {
  test("set then get round-trips; clear removes", async () => {
    const ts = new TokenStore({ storage: memStorage(), now: () => NOW });
    await ts.set({
      accessToken: "AT",
      refreshToken: "RT",
      idToken: "IT",
      expiresAt: NOW + 1000,
    });
    expect(await ts.get()).toEqual({
      accessToken: "AT",
      refreshToken: "RT",
      idToken: "IT",
      expiresAt: NOW + 1000,
    });
    await ts.clear();
    expect(await ts.get()).toBeNull();
  });

  test("setFromTokenSet anchors expires_in to absolute expiry", async () => {
    const ts = new TokenStore({ storage: memStorage(), now: () => NOW });
    const stored = await ts.setFromTokenSet({
      access_token: "AT",
      refresh_token: "RT",
      expires_in: 3600,
    });
    expect(stored.expiresAt).toBe(NOW + 3600_000);
    expect((await ts.get())?.accessToken).toBe("AT");
  });

  test("setFromTokenSet keeps prior refresh token when omitted", async () => {
    const ts = new TokenStore({ storage: memStorage(), now: () => NOW });
    await ts.set({ accessToken: "old", refreshToken: "RT-keep", expiresAt: NOW });
    const stored = await ts.setFromTokenSet({
      access_token: "new",
      expires_in: 3600,
    });
    expect(stored.refreshToken).toBe("RT-keep");
  });

  test("setFromTokenSet stores the id_token from the token set", async () => {
    const ts = new TokenStore({ storage: memStorage(), now: () => NOW });
    const stored = await ts.setFromTokenSet({
      access_token: "AT",
      refresh_token: "RT",
      id_token: "ID-TOK",
      expires_in: 3600,
    });
    expect(stored.idToken).toBe("ID-TOK");
    expect((await ts.get())?.idToken).toBe("ID-TOK");
  });

  test("setFromTokenSet keeps prior id_token when a refresh omits it", async () => {
    const ts = new TokenStore({ storage: memStorage(), now: () => NOW });
    await ts.set({
      accessToken: "old",
      refreshToken: "RT",
      idToken: "ID-keep",
      expiresAt: NOW,
    });
    const stored = await ts.setFromTokenSet({
      access_token: "new",
      expires_in: 3600,
    });
    expect(stored.idToken).toBe("ID-keep");
  });
});

describe("getIdToken", () => {
  test("returns the stored id_token", async () => {
    const ts = new TokenStore({ storage: memStorage(), now: () => NOW });
    await ts.set({
      accessToken: "AT",
      refreshToken: "RT",
      idToken: "ID-TOK",
      expiresAt: NOW + 1000,
    });
    expect(await ts.getIdToken()).toBe("ID-TOK");
  });

  test("returns null when never signed in", async () => {
    const ts = new TokenStore({ storage: memStorage(), now: () => NOW });
    expect(await ts.getIdToken()).toBeNull();
  });

  test("returns null for a legacy stored token with no idToken field", async () => {
    const ts = new TokenStore({ storage: memStorage(), now: () => NOW });
    // Simulate a pre-existing record written before idToken existed.
    await ts.set({
      accessToken: "AT",
      refreshToken: "RT",
      expiresAt: NOW + 1000,
    });
    expect(await ts.getIdToken()).toBeNull();
  });
});

describe("getValidAccessToken", () => {
  test("returns null when not signed in", async () => {
    const ts = new TokenStore({ storage: memStorage(), now: () => NOW });
    expect(await ts.getValidAccessToken()).toBeNull();
  });

  test("returns the cached token when fresh (no fetch)", async () => {
    const ts = new TokenStore({ storage: memStorage(), now: () => NOW });
    await ts.set({
      accessToken: "AT",
      refreshToken: "RT",
      expiresAt: NOW + 10 * 60_000,
    });
    const fetchFn = vi.fn();
    const tok = await ts.getValidAccessToken({
      fetch: fetchFn as unknown as typeof fetch,
    });
    expect(tok).toBe("AT");
    expect(fetchFn).not.toHaveBeenCalled();
  });

  test("refreshes when expired and persists the rotated set", async () => {
    const storage = memStorage();
    const ts = new TokenStore({ storage, now: () => NOW });
    await ts.set({
      accessToken: "OLD",
      refreshToken: "RT",
      expiresAt: NOW - 1, // already expired
    });
    const fetchFn = vi.fn(async () =>
      jsonResponse(200, {
        access_token: "NEW",
        refresh_token: "RT2",
        expires_in: 3600,
      }),
    );
    const tok = await ts.getValidAccessToken({
      fetch: fetchFn as unknown as typeof fetch,
    });
    expect(tok).toBe("NEW");
    const stored = await ts.get();
    expect(stored?.accessToken).toBe("NEW");
    expect(stored?.refreshToken).toBe("RT2");
    expect(stored?.expiresAt).toBe(NOW + 3600_000);
  });

  test("refreshes when within the expiry skew margin", async () => {
    const ts = new TokenStore({ storage: memStorage(), now: () => NOW });
    await ts.set({
      accessToken: "OLD",
      refreshToken: "RT",
      expiresAt: NOW + EXPIRY_SKEW_MS - 1, // inside the skew window
    });
    const fetchFn = vi.fn(async () =>
      jsonResponse(200, { access_token: "NEW", expires_in: 3600 }),
    );
    const tok = await ts.getValidAccessToken({
      fetch: fetchFn as unknown as typeof fetch,
    });
    expect(tok).toBe("NEW");
    expect(fetchFn).toHaveBeenCalledTimes(1);
  });

  test("keeps the prior refresh token when refresh omits a new one", async () => {
    const ts = new TokenStore({ storage: memStorage(), now: () => NOW });
    await ts.set({ accessToken: "OLD", refreshToken: "RT", expiresAt: NOW - 1 });
    const fetchFn = vi.fn(async () =>
      jsonResponse(200, { access_token: "NEW", expires_in: 3600 }),
    );
    await ts.getValidAccessToken({ fetch: fetchFn as unknown as typeof fetch });
    expect((await ts.get())?.refreshToken).toBe("RT");
  });

  test("throws when expired and there is no refresh token", async () => {
    const ts = new TokenStore({ storage: memStorage(), now: () => NOW });
    await ts.set({ accessToken: "OLD", refreshToken: null, expiresAt: NOW - 1 });
    await expect(ts.getValidAccessToken()).rejects.toThrow(/re-authenticate/);
  });

  test("force:true refreshes even when the token is still fresh", async () => {
    const ts = new TokenStore({ storage: memStorage(), now: () => NOW });
    await ts.set({
      accessToken: "FRESH-BUT-REVOKED",
      refreshToken: "RT",
      expiresAt: NOW + 10 * 60_000, // not stale
    });
    const fetchFn = vi.fn(async () =>
      jsonResponse(200, { access_token: "REFRESHED", expires_in: 3600 }),
    );
    const tok = await ts.getValidAccessToken({
      fetch: fetchFn as unknown as typeof fetch,
      force: true,
    });
    expect(tok).toBe("REFRESHED");
    expect(fetchFn).toHaveBeenCalledTimes(1);
    expect((await ts.get())?.accessToken).toBe("REFRESHED");
  });

  test("force:true with no stored token returns null (not signed in)", async () => {
    const ts = new TokenStore({ storage: memStorage(), now: () => NOW });
    expect(await ts.getValidAccessToken({ force: true })).toBeNull();
  });

  test("force:true with no refresh token throws (re-authenticate)", async () => {
    const ts = new TokenStore({ storage: memStorage(), now: () => NOW });
    await ts.set({
      accessToken: "AT",
      refreshToken: null,
      expiresAt: NOW + 10 * 60_000,
    });
    await expect(ts.getValidAccessToken({ force: true })).rejects.toThrow(
      /re-authenticate/,
    );
  });
});
