// chrome/src/popup/ConnectionMode.tsx
//
// Transport-mode selector. Lets the user choose between:
//   relayless — "Fulcra Cloud (no app needed)": OIDC sign-in, direct ingest.
//   relay     — "Local Collect app": the localhost daemon paste-token path.
//
// Persisted via the existing settings save. `relay` stays the default for
// existing installs (DEFAULT_SETTINGS.transportMode); switching is explicit —
// we never auto-flip anyone. Emits onChange so the parent can re-render the
// surface (sign-in vs paste-token) immediately on toggle.

import { useEffect, useState } from "react";
import { loadSettings, saveSettings } from "../storage";
import type { TransportMode } from "../types";

export interface ConnectionModeProps {
  /** Notified after a successful persist so the parent can swap surfaces. */
  onChange?: (mode: TransportMode) => void;
}

export function ConnectionMode(props: ConnectionModeProps) {
  const [mode, setMode] = useState<TransportMode | null>(null);

  useEffect(() => {
    void loadSettings().then((s) => setMode(s.transportMode));
  }, []);

  async function choose(next: TransportMode): Promise<void> {
    if (next === mode) return;
    const cur = await loadSettings();
    await saveSettings({ ...cur, transportMode: next });
    setMode(next);
    props.onChange?.(next);
  }

  if (mode === null) return null;

  return (
    <div className="section">
      <h2>Connection</h2>
      <div className="row" role="radiogroup" aria-label="Connection mode" style={{ gap: 8 }}>
        <button
          role="radio"
          aria-checked={mode === "relayless"}
          className={mode === "relayless" ? "primary" : ""}
          style={{ flex: 1 }}
          onClick={() => void choose("relayless")}
        >
          Fulcra Cloud (no app needed)
        </button>
        <button
          role="radio"
          aria-checked={mode === "relay"}
          className={mode === "relay" ? "primary" : ""}
          style={{ flex: 1 }}
          onClick={() => void choose("relay")}
        >
          Local Collect app
        </button>
      </div>
    </div>
  );
}
