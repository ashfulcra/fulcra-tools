import { useEffect, useState } from "react";
import { loadSettings, saveSettings } from "../storage";

export function BearerForm() {
  const [token, setToken] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    void loadSettings().then((s) => {
      setToken(s.bearerToken ?? "");
      setEnabled(s.enabled);
    });
  }, []);

  async function save() {
    const cur = await loadSettings();
    await saveSettings({ ...cur, bearerToken: token || null, enabled });
    // Clear any stale ingest error — the user just fixed their token,
    // so we shouldn't keep showing a Reconnect banner. The next outbox
    // flush either succeeds (stays clear) or re-raises a fresh error.
    await chrome.storage.local.remove("lastIngestError");
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
  }

  return (
    <div>
      <div className="row">
        <label>
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          Enabled
        </label>
      </div>
      <div className="row">
        <input
          type="password"
          placeholder="Paste bearer token from relay.json"
          value={token}
          onChange={(e) => setToken(e.target.value)}
        />
        <button className="primary" onClick={save}>Save</button>
      </div>
      {saved && <span className="saved-flash">✓ Saved</span>}
    </div>
  );
}
