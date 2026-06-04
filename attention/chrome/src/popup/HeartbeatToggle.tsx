import { useEffect, useState } from "react";
import { loadSettings } from "../storage";
import {
  setHeartbeatEnabled, hasHeartbeatPermission,
} from "../heartbeat-control";

/**
 * Opt-in toggle for the "sharper AFK detection" content script. Mirrors
 * the wizard step but available at any time post-onboarding.
 *
 * The toggle reflects BOTH the saved setting and the actual host
 * permission, since Chrome lets users revoke optional permissions
 * outside our UI. If they're out of sync we trust the permission and
 * resync the setting on next interaction.
 */
export function HeartbeatToggle() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void (async () => {
      const [s, hasPerm] = await Promise.all([
        loadSettings(),
        hasHeartbeatPermission(),
      ]);
      // The honest state: enabled = settings says yes AND Chrome agrees.
      setEnabled(s.heartbeatEnabled && hasPerm);
    })();
  }, []);

  async function toggle(want: boolean): Promise<void> {
    setBusy(true);
    try {
      const actual = await setHeartbeatEnabled(want);
      setEnabled(actual);
    } finally {
      setBusy(false);
    }
  }

  if (enabled === null) return null;  // still loading

  return (
    <div className="section">
      <h2>AFK detection</h2>
      <div className="muted" style={{ marginBottom: 6 }}>
        Default uses Chrome's keyboard/mouse signal. Turn this on to also
        watch for scroll + tab focus inside the page. Reads no page
        content — only whether you're interacting.
      </div>
      <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => void toggle(e.target.checked)}
          disabled={busy}
        />
        Sharper AFK detection
        {enabled && <span className="saved-flash">✓ active</span>}
      </label>
    </div>
  );
}
