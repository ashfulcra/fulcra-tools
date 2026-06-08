// chrome/tests/relayless/whoami.test.ts
import { describe, test, expect } from "vitest";
import { whoami } from "../../src/relayless/whoami";
import { mockFetch } from "./memStorage";
import { API_BASE } from "../../src/relayless/config";

/** Build a fake JWT with the given payload (base64url, unsigned — whoami
 * never verifies). */
function jwt(payload: Record<string, unknown>): string {
  const b64url = (s: string) =>
    btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64url('{"alg":"none"}')}.${b64url(JSON.stringify(payload))}.sig`;
}

describe("whoami", () => {
  test("prefers the name claim over email in the id_token JWT (no network)", async () => {
    const f = mockFetch(async () => new Response("{}", { status: 200 }));
    const r = await whoami(jwt({ name: "Ash Kalb", email: "a@b.com", sub: "x" }), {
      fetch: f,
    });
    expect(r.label).toBe("Ash Kalb");
    expect(f).not.toHaveBeenCalled();
  });

  test("reads the email claim when there is no name claim (no network)", async () => {
    const f = mockFetch(async () => new Response("{}", { status: 200 }));
    const r = await whoami(jwt({ email: "a@b.com", sub: "x" }), { fetch: f });
    expect(r.label).toBe("a@b.com");
    expect(f).not.toHaveBeenCalled();
  });

  test("falls back to the namespaced email claim when name/email absent", async () => {
    const f = mockFetch(async () => new Response("{}", { status: 200 }));
    const r = await whoami(
      jwt({ "https://fulcradynamics.com/email": "ns@b.com", sub: "x" }),
      { fetch: f },
    );
    expect(r.label).toBe("ns@b.com");
    expect(f).not.toHaveBeenCalled();
  });

  test("falls back to GET /user/v1alpha1/info when the JWT has no name/email", async () => {
    const f = mockFetch(async (input) => {
      expect(String(input)).toBe(`${API_BASE}/user/v1alpha1/info`);
      return new Response(JSON.stringify({ userid: "uid-123" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    const r = await whoami(jwt({ sub: "x" }), { fetch: f });
    expect(r.label).toBe("uid-123");
  });

  test("returns null label when the JWT is malformed and /info fails", async () => {
    const f = mockFetch(async () => new Response("", { status: 500 }));
    const r = await whoami("not-a-jwt", { fetch: f });
    expect(r.label).toBeNull();
  });

  test("never throws on a network error", async () => {
    const f = mockFetch(async () => {
      throw new TypeError("network");
    });
    const r = await whoami(jwt({ sub: "x" }), { fetch: f });
    expect(r.label).toBeNull();
  });
});
