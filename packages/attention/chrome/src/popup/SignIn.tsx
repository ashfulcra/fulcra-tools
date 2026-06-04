// chrome/src/popup/SignIn.tsx
//
// The Fulcra sign-in surface (relayless device-flow OIDC). State machine:
//
//   idle      → "Sign in with Fulcra" button. Click → startDeviceSignIn.
//   prompting → device code issued: show the user code prominently + a button
//               that opens verification_uri_complete in a new tab. The poll
//               loop runs underneath ("waiting for approval…").
//   signedin  → "Signed in as <email>" (best-effort via whoami) + Sign out.
//   error     → message + "Try again".
//
// On mount we check the TokenStore for a valid token; if present we jump
// straight to signedin (resolving the label in the background).
//
// All side-effecting deps (sign-in runner, token store, whoami, tab opener)
// are injectable so the test can drive the flow without chrome.* / network.

import { useEffect, useRef, useState } from "react";
import { startDeviceSignIn } from "../relayless/signIn";
import { TokenStore } from "../relayless/tokenStore";
import { clearResolvedAttention } from "../relayless/ensureDefinition";
import { whoami } from "../relayless/whoami";
import { flushOutbox } from "../outbox";

type Phase =
  | { kind: "loading" }
  | { kind: "idle" }
  | { kind: "prompting"; url: string; userCode: string }
  | { kind: "signedin"; label: string | null }
  | { kind: "error"; message: string };

export interface SignInProps {
  /** Run the device flow. Defaults to the real startDeviceSignIn. */
  runSignIn?: typeof startDeviceSignIn;
  /** Token store (defaults to extension local storage). */
  tokenStore?: TokenStore;
  /** Resolve a display label for the signed-in user. */
  resolveLabel?: (accessToken: string) => Promise<string | null>;
  /** Open the verification URL. Defaults to chrome.tabs.create. */
  openUrl?: (url: string) => void;
  /** Clear the resolved-attention cache on sign-out. */
  clearResolved?: () => Promise<void>;
  /**
   * Called when the user is signed in (either freshly, after the device
   * flow completes, or because a valid token already existed on mount).
   * The popup leaves this unset (the surface stays put); the onboarding
   * wizard wires it to advance to the next step. When set, the signed-in
   * phase also renders a "Continue" affordance so an already-signed-in
   * user can proceed without re-authenticating.
   */
  onSignedIn?: () => void;
}

function defaultOpenUrl(url: string): void {
  void chrome.tabs.create({ url });
}

async function defaultResolveLabel(accessToken: string): Promise<string | null> {
  const r = await whoami(accessToken);
  return r.label;
}

export function SignIn(props: SignInProps) {
  const tokenStore = props.tokenStore ?? new TokenStore();
  const runSignIn = props.runSignIn ?? startDeviceSignIn;
  const resolveLabel = props.resolveLabel ?? defaultResolveLabel;
  const openUrl = props.openUrl ?? defaultOpenUrl;
  const clearResolved = props.clearResolved ?? clearResolvedDefault;
  const onSignedIn = props.onSignedIn;

  const [phase, setPhase] = useState<Phase>({ kind: "loading" });
  // Guards against setState after unmount (the poll loop can outlive the popup).
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // On mount: if there's already a valid token, show signed-in.
  useEffect(() => {
    let cancelled = false;
    async function check() {
      let token: string | null = null;
      try {
        token = await tokenStore.getValidAccessToken();
      } catch {
        token = null;
      }
      if (cancelled || !mounted.current) return;
      if (token) {
        setPhase({ kind: "signedin", label: null });
        void resolveLabel(token).then((label) => {
          if (!cancelled && mounted.current && label) {
            setPhase({ kind: "signedin", label });
          }
        });
      } else {
        setPhase({ kind: "idle" });
      }
    }
    void check();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function beginSignIn(): Promise<void> {
    try {
      await runSignIn({
        onPrompt: (verificationUriComplete, userCode) => {
          if (!mounted.current) return;
          setPhase({
            kind: "prompting",
            url: verificationUriComplete,
            userCode,
          });
          // Open the verification page for the user automatically.
          openUrl(verificationUriComplete);
        },
      });
      // Resolved → tokens are stored. Surface signed-in + resolve a label.
      if (!mounted.current) return;
      let token: string | null = null;
      try {
        token = await tokenStore.getValidAccessToken();
      } catch {
        token = null;
      }
      setPhase({ kind: "signedin", label: null });
      if (token) {
        const label = await resolveLabel(token);
        if (mounted.current && label) {
          setPhase({ kind: "signedin", label });
        }
      }
      // A successful sign-in clears a stale "needs sign-in" banner and kicks
      // a flush so queued events ship right away.
      await chrome.storage.local.remove("lastIngestError");
      void flushOutbox();
      // Let an embedder (the onboarding wizard) advance past the auth step.
      // The popup leaves onSignedIn unset, so this is a no-op there.
      if (mounted.current && onSignedIn) onSignedIn();
    } catch (e) {
      if (!mounted.current) return;
      setPhase({ kind: "error", message: errorMessage(e) });
    }
  }

  async function signOut(): Promise<void> {
    await tokenStore.clear();
    await clearResolved();
    // Re-assert needs-sign-in so the banner routes the user back here.
    await chrome.storage.local.set({
      lastIngestError: { kind: "unauthorized", at: Date.now() },
    });
    if (mounted.current) setPhase({ kind: "idle" });
  }

  if (phase.kind === "loading") {
    return <div className="signin muted">Checking sign-in…</div>;
  }

  if (phase.kind === "idle") {
    return (
      <div className="signin">
        <button className="primary" style={{ width: "100%" }}
                onClick={() => void beginSignIn()}>
          Sign in with Fulcra
        </button>
        <p className="muted" style={{ marginTop: 8 }}>
          Connect this browser straight to Fulcra Cloud — no local app needed.
        </p>
      </div>
    );
  }

  if (phase.kind === "prompting") {
    return (
      <div className="signin">
        <p style={{ margin: "0 0 6px" }}>
          Go to the page that opened and confirm code{" "}
          <strong className="usercode">{phase.userCode}</strong>.
        </p>
        <div className="row" style={{ gap: 8 }}>
          <button onClick={() => openUrl(phase.url)} style={{ flex: 1 }}>
            Open the confirmation page
          </button>
        </div>
        <p className="muted" style={{ marginTop: 8 }}>
          Waiting for approval…
        </p>
      </div>
    );
  }

  if (phase.kind === "signedin") {
    return (
      <div className="signin">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <span>
            {phase.label ? `Signed in as ${phase.label}` : "Signed in."}
          </span>
          <button onClick={() => void signOut()}>Sign out</button>
        </div>
        {onSignedIn && (
          <div className="action-row" style={{ marginTop: 10 }}>
            <div className="spacer" />
            <button className="primary" onClick={() => onSignedIn()}>
              Continue →
            </button>
          </div>
        )}
      </div>
    );
  }

  // error
  return (
    <div className="signin">
      <div className="banner banner-error" style={{ marginBottom: 8 }}>
        <strong>Sign-in failed.</strong> {phase.message}
      </div>
      <button className="primary" style={{ width: "100%" }}
              onClick={() => void beginSignIn()}>
        Try again
      </button>
    </div>
  );
}

function clearResolvedDefaultImpl(): Promise<void> {
  return clearResolvedAttention();
}
// Bound default so the prop can override it in tests without importing the
// chrome storage default eagerly at module init.
const clearResolvedDefault = clearResolvedDefaultImpl;

function errorMessage(e: unknown): string {
  if (e && typeof e === "object" && "message" in e) {
    const m = (e as { message?: unknown }).message;
    if (typeof m === "string" && m.length > 0) return m;
  }
  return "Please try again.";
}
