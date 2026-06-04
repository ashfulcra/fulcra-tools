// chrome/src/relayless/tokenStore.ts
//
// Persistence + freshness for the relayless token set. Tokens live in the
// extension's local storage area under a single key. `getValidAccessToken`
// returns a non-expired access token, transparently refreshing via the OIDC
// refresh grant when the stored one is expired or within a safety margin of
// expiry, and persisting the rotated tokens.
//
// We store an ABSOLUTE expiry (ms epoch) rather than the relative expires_in
// so freshness checks don't depend on when the record was written. The clock
// is injectable for tests.

import { FulcraOidc, type FetchFn, type TokenSet } from "./oidc";
import {
  type StorageArea,
  defaultLocalStorageArea,
} from "./storageArea";

const TOKEN_KEY = "relaylessTokens";

/** Refresh when the access token is within this margin of expiry, so an
 * in-flight ingest POST doesn't race the expiry boundary. */
export const EXPIRY_SKEW_MS = 60_000;

/** The persisted token record. */
export interface StoredTokens {
  accessToken: string;
  refreshToken: string | null;
  /** Absolute expiry, ms epoch. */
  expiresAt: number;
}

export interface TokenStoreOpts {
  storage?: StorageArea;
  now?: () => number;
}

/** Convert an OIDC token response into the persisted shape, anchoring the
 * relative expires_in to an absolute timestamp. `existingRefresh` is kept
 * when the response omits a rotated refresh token. */
export function toStored(
  token: TokenSet,
  now: number,
  existingRefresh: string | null = null,
): StoredTokens {
  return {
    accessToken: token.access_token,
    refreshToken: token.refresh_token ?? existingRefresh,
    expiresAt: now + Math.max(0, token.expires_in) * 1000,
  };
}

export class TokenStore {
  private readonly storage: StorageArea;
  private readonly now: () => number;

  constructor(opts: TokenStoreOpts = {}) {
    this.storage = opts.storage ?? defaultLocalStorageArea();
    this.now = opts.now ?? (() => Date.now());
  }

  /** Read the persisted token set, or null if not signed in. */
  async get(): Promise<StoredTokens | null> {
    const r = await this.storage.get(TOKEN_KEY);
    return (r[TOKEN_KEY] as StoredTokens | undefined) ?? null;
  }

  /** Persist a token set. */
  async set(tokens: StoredTokens): Promise<void> {
    await this.storage.set({ [TOKEN_KEY]: tokens });
  }

  /** Persist a fresh OIDC token response (e.g. right after the device flow
   * completes), anchoring its expiry. Keeps the prior refresh token if the
   * new response omits one. */
  async setFromTokenSet(token: TokenSet): Promise<StoredTokens> {
    const existing = await this.get();
    const stored = toStored(token, this.now(), existing?.refreshToken ?? null);
    await this.set(stored);
    return stored;
  }

  /** Remove the token set (sign out). */
  async clear(): Promise<void> {
    await this.storage.remove(TOKEN_KEY);
  }

  /** True when `tokens` is expired or within EXPIRY_SKEW_MS of expiry. */
  private isStale(tokens: StoredTokens): boolean {
    return tokens.expiresAt - this.now() <= EXPIRY_SKEW_MS;
  }

  /**
   * Return a usable access token. If the stored token is fresh, return it as
   * is. If it is expired / near-expiry, refresh it (persisting the rotated
   * set) and return the new one. Returns null when there is no stored token
   * (not signed in). Throws if a refresh is required but there is no refresh
   * token, or the refresh itself fails.
   */
  async getValidAccessToken(opts: { fetch?: FetchFn } = {}): Promise<
    string | null
  > {
    const tokens = await this.get();
    if (!tokens) return null;
    if (!this.isStale(tokens)) return tokens.accessToken;

    if (!tokens.refreshToken) {
      throw new Error(
        "access token expired and no refresh token available; re-authenticate",
      );
    }
    const oidc = new FulcraOidc({ fetch: opts.fetch });
    const refreshed = await oidc.refresh(tokens.refreshToken);
    const stored = toStored(refreshed, this.now(), tokens.refreshToken);
    await this.set(stored);
    return stored.accessToken;
  }
}
