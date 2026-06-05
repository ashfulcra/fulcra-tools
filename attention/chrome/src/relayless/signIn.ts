// chrome/src/relayless/signIn.ts
//
// Minimal programmatic sign-in entry point for the relayless transport. Runs
// the OIDC device-authorization flow end to end and stores the resulting
// tokens in the TokenStore. The polished popup UI (rendering the user code +
// "open this URL" affordance, copy buttons, status) is a separate task — this
// is the function that UI calls, with an onPrompt callback for showing the
// verification URL + user code while polling proceeds.

import { FulcraOidc, type FetchFn } from "./oidc";
import { TokenStore } from "./tokenStore";
import { registerAuth0OriginStrip } from "./authOriginFix";
import type { StorageArea } from "./storageArea";

export interface StartDeviceSignInOpts {
  /** Called once the device code is issued, BEFORE polling begins, so the UI
   * can show the user where to approve. `verificationUriComplete` embeds the
   * user code (open-and-go); `userCode` is shown for manual entry / display. */
  onPrompt: (verificationUriComplete: string, userCode: string) => void;
  fetch?: FetchFn;
  /** Injectable token storage (tests). Defaults to extension local storage. */
  storage?: StorageArea;
  /** Injectable sleep for the poll loop (tests advance without real delay). */
  sleep?: (ms: number) => Promise<void>;
  /** Cap on poll attempts (tests). */
  maxAttempts?: number;
  /** Register the Auth0 Origin-strip DNR rule. Awaited before the first Auth0
   * request so a cold-woken SW can't race the rule (Bug A4). Idempotent;
   * injectable for tests. Defaults to the real registerAuth0OriginStrip. */
  registerOriginStrip?: () => Promise<void>;
}

export interface SignInResult {
  /** True once tokens were stored. (Always true on the resolved path; the
   * function rejects on failure.) */
  ok: true;
}

/**
 * Run the device flow: request a device code, surface the verification URL +
 * user code via onPrompt, poll the token endpoint until the user approves,
 * then persist the token set via TokenStore. Rejects (OidcError) if the code
 * expires, the user denies, or the device-code request fails.
 */
export async function startDeviceSignIn(
  opts: StartDeviceSignInOpts,
): Promise<SignInResult> {
  // Ensure the Auth0 Origin-strip DNR rule is LIVE before the first Auth0
  // request. On a cold-woken SW the boot-time fire-and-forget registration may
  // not have settled yet; awaiting here (registration is idempotent) prevents
  // the device-code POST from racing it and getting a 403. Bug A4.
  const registerOriginStrip =
    opts.registerOriginStrip ?? (() => registerAuth0OriginStrip());
  await registerOriginStrip();

  const oidc = new FulcraOidc({ fetch: opts.fetch });
  const device = await oidc.requestDeviceCode();

  // Surface the verification URL + user code so the UI can prompt the user.
  opts.onPrompt(device.verification_uri_complete, device.user_code);

  const token = await oidc.pollForToken(device.device_code, device.interval, {
    sleep: opts.sleep,
    maxAttempts: opts.maxAttempts,
  });

  const store = new TokenStore({ storage: opts.storage });
  await store.setFromTokenSet(token);
  return { ok: true };
}
