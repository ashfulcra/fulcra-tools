// chrome/src/relayless/oidc.ts
//
// Auth0 OIDC device-authorization flow, ported from
// fulcra_api/oidc.py (FulcraOIDCProvider). Lets the extension authenticate a
// user against Fulcra without a localhost daemon: request a device code,
// show the user a verification URL, poll until they approve, then hold the
// resulting token set. `fetch` is injectable so tests never hit the network.
//
// Request shapes match the Python exactly:
//   - POST /oauth/device/code  with form {client_id, audience, scope}
//   - POST /oauth/token        with form {client_id, grant_type, ...}
// Both use Content-Type: application/x-www-form-urlencoded.
//
// The Python `authorize_via_device_flow` swallows every polling error and
// retries until a timeout; here we implement the proper RFC 8628 state
// machine so the caller (a popup UI) can distinguish "still pending" from
// "user denied" / "code expired" and react accordingly.

import {
  DEVICE_CODE_URL,
  TOKEN_URL,
  OIDC_CLIENT_ID,
  OIDC_AUDIENCE,
  OIDC_SCOPE,
  DEVICE_CODE_GRANT,
} from "./config";

export type FetchFn = typeof fetch;

/** Parsed response of POST /oauth/device/code. */
export interface DeviceCodeResponse {
  device_code: string;
  user_code: string;
  verification_uri: string;
  verification_uri_complete: string;
  expires_in: number;
  interval: number;
}

/** A token set as returned by POST /oauth/token. */
export interface TokenSet {
  access_token: string;
  refresh_token?: string | null;
  /** The OIDC id_token JWT. Auth0 returns this whenever the scope includes
   * `openid` (ours does); unlike the API-audience access token it carries the
   * `name`/`email` display claims. A refresh response may omit it. */
  id_token?: string | null;
  expires_in: number;
  token_type?: string;
  scope?: string;
}

/** Error thrown when the device flow cannot complete. `code` carries the
 * Auth0 error string for terminal failures (expired_token / access_denied)
 * or a synthetic code for transport problems. */
export class OidcError extends Error {
  constructor(
    message: string,
    readonly code: string,
  ) {
    super(message);
    this.name = "OidcError";
  }
}

function formBody(fields: Record<string, string>): string {
  return new URLSearchParams(fields).toString();
}

const FORM_HEADERS = { "Content-Type": "application/x-www-form-urlencoded" };

export class FulcraOidc {
  private readonly fetchFn: FetchFn;

  constructor(opts: { fetch?: FetchFn } = {}) {
    // Bind so a passed-in `fetch` that relies on `this` (rare) still works,
    // and so the default picks up the global at call time.
    this.fetchFn = opts.fetch ?? ((...a: Parameters<FetchFn>) => fetch(...a));
  }

  /** POST /oauth/device/code → parsed device-code response. */
  async requestDeviceCode(): Promise<DeviceCodeResponse> {
    const resp = await this.fetchFn(DEVICE_CODE_URL, {
      method: "POST",
      headers: FORM_HEADERS,
      body: formBody({
        client_id: OIDC_CLIENT_ID,
        audience: OIDC_AUDIENCE,
        scope: OIDC_SCOPE,
      }),
    });
    if (!resp.ok) {
      throw new OidcError(
        `device code request failed: HTTP ${resp.status}`,
        "device_code_failed",
      );
    }
    return (await resp.json()) as DeviceCodeResponse;
  }

  /** One token-endpoint exchange. Returns either a parsed TokenSet (on 200)
   * or the Auth0 error code string (on non-200, e.g. "authorization_pending").
   * Network errors throw an OidcError(code="network"). */
  private async tokenExchange(
    fields: Record<string, string>,
  ): Promise<{ ok: true; token: TokenSet } | { ok: false; error: string }> {
    let resp: Response;
    try {
      resp = await this.fetchFn(TOKEN_URL, {
        method: "POST",
        headers: FORM_HEADERS,
        body: formBody({
          client_id: OIDC_CLIENT_ID,
          ...fields,
        }),
      });
    } catch (e) {
      throw new OidcError(
        `token request transport error: ${String(e)}`,
        "network",
      );
    }
    if (resp.ok) {
      const token = (await resp.json()) as TokenSet;
      if (!token.access_token) {
        throw new OidcError("token response missing access_token", "invalid");
      }
      return { ok: true, token };
    }
    // Auth0 returns the OAuth error code in a JSON body on 4xx.
    let error = `http_${resp.status}`;
    try {
      const body = (await resp.json()) as { error?: string };
      if (body && typeof body.error === "string") error = body.error;
    } catch {
      // Non-JSON error body — keep the synthetic http_<status> code.
    }
    return { ok: false, error };
  }

  /**
   * Poll POST /oauth/token with the device_code grant until the user
   * approves, the code is denied, or it expires. Implements the RFC 8628 /
   * Auth0 polling rules:
   *   - authorization_pending → wait `interval` and retry
   *   - slow_down             → increase the interval by 5s, then retry
   *   - expired_token         → fail (OidcError code="expired_token")
   *   - access_denied         → fail (OidcError code="access_denied")
   * `sleep` is injectable so tests advance the clock without real delay.
   * `deviceCode` plus the starting `intervalSec` come from
   * requestDeviceCode().
   */
  async pollForToken(
    deviceCode: string,
    intervalSec: number,
    opts: {
      sleep?: (ms: number) => Promise<void>;
      maxAttempts?: number;
    } = {},
  ): Promise<TokenSet> {
    const sleep =
      opts.sleep ?? ((ms: number) => new Promise((r) => setTimeout(r, ms)));
    // A generous default ceiling so a stuck loop can't spin forever; the
    // caller can override. The real bound is the device code's expires_in,
    // surfaced as the expired_token terminal error.
    const maxAttempts = opts.maxAttempts ?? 600;
    let interval = Math.max(1, intervalSec);

    for (let attempt = 0; attempt < maxAttempts; attempt++) {
      await sleep(interval * 1000);
      const result = await this.tokenExchange({
        grant_type: DEVICE_CODE_GRANT,
        device_code: deviceCode,
      });
      if (result.ok) return result.token;
      switch (result.error) {
        case "authorization_pending":
          continue;
        case "slow_down":
          // Per RFC 8628 §3.5: increase the polling interval by 5 seconds.
          interval += 5;
          continue;
        case "expired_token":
        case "access_denied":
          throw new OidcError(
            `device authorization failed: ${result.error}`,
            result.error,
          );
        default:
          // Any other error (invalid_grant, http_5xx, etc.) is terminal —
          // continuing would just hammer the endpoint.
          throw new OidcError(
            `device authorization failed: ${result.error}`,
            result.error,
          );
      }
    }
    throw new OidcError("device authorization timed out", "timeout");
  }

  /** POST /oauth/token with grant_type=refresh_token. Returns a fresh token
   * set. The refreshed response may or may not include a new refresh_token
   * (Auth0 rotation policy dependent); callers should keep the old one if
   * the response omits it. */
  async refresh(refreshToken: string): Promise<TokenSet> {
    const result = await this.tokenExchange({
      grant_type: "refresh_token",
      refresh_token: refreshToken,
      scope: OIDC_SCOPE,
    });
    if (!result.ok) {
      throw new OidcError(`token refresh failed: ${result.error}`, result.error);
    }
    return result.token;
  }
}
