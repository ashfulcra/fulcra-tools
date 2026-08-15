// chrome/src/safari/popup.tsx
//
// The Safari popup. Deliberately almost empty.
//
// The Chrome popup carries sign-in, onboarding, category editing, an ignore
// list, pause controls, a live stream and a backfill wizard. None of that
// belongs here: the native app owns auth, and the wizard's history backfill
// cannot run on iOS at all (chrome.history does not exist there). Shipping it
// would mean a popup whose buttons do nothing on the platform it ships to.
//
// So this reports one fact — whether the containing app is holding a token —
// and points the user at the app when it is not.

import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

import { SAFARI_AUTH_QUERY } from "./protocol";

type State = "checking" | "signed-in" | "signed-out";

function App(): JSX.Element {
  const [state, setState] = useState<State>("checking");

  useEffect(() => {
    let alive = true;
    chrome.runtime
      .sendMessage({ type: SAFARI_AUTH_QUERY })
      .then((r: { signedIn?: boolean } | undefined) => {
        if (alive) setState(r?.signedIn ? "signed-in" : "signed-out");
      })
      // An unreachable background worker is indistinguishable from being
      // signed out FROM HERE, and claiming "signed in" on an error would be
      // the misleading direction — so degrade to signed-out.
      .catch(() => alive && setState("signed-out"));
    return () => {
      alive = false;
    };
  }, []);

  return (
    <main style={{ font: "13px -apple-system, system-ui", padding: 16, width: 240 }}>
      <h1 style={{ fontSize: 15, margin: "0 0 8px" }}>Fulcra Attention</h1>
      {state === "checking" && <p>Checking…</p>}
      {state === "signed-in" && <p>Capturing. Signed in via the Fulcra Attention app.</p>}
      {state === "signed-out" && (
        <p>
          Not signed in. Open the <strong>Fulcra Attention</strong> app to sign in — it
          holds the account, not this extension.
        </p>
      )}
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
