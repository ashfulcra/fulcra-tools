/**
 * Workaround for Node 26 + @vercel/sandbox@2.0.2: the SDK consumes response.body
 * as raw bytes but doesn't decode brotli, so every Vercel API stream call fails
 * with "Expected a stream of command data". Force Accept-Encoding: identity on
 * every outbound request so the API returns plain (uncompressed) NDJSON the SDK
 * can read directly.
 *
 * Also note: there's a second SDK bug (response headers come back as {} on Node
 * 26, so the strict content-type check still throws even on uncompressed
 * responses). Until that lands upstream, the SDK's content-type check is patched
 * locally to tolerate null content-type — see docs/ARCHITECTURE.md.
 *
 * Long-term fix: pin Node 22 LTS (where the SDK works clean, no shims).
 */
const _origFetch = globalThis.fetch;
globalThis.fetch = (input, init = {}) => {
  const headers = new Headers(init.headers || (input && input.headers) || {});
  headers.set('accept-encoding', 'identity');
  return _origFetch(input, { ...init, headers });
};
