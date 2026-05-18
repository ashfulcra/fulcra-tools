import { useEffect, useState } from "react";
import { loadSettings, saveSettings } from "../storage";

export function IdentityLabel() {
  const [label, setLabel] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    void loadSettings().then((s) => setLabel(s.identityLabel ?? ""));
  }, []);

  async function save() {
    const cur = await loadSettings();
    await saveSettings({ ...cur, identityLabel: label.trim() === "" ? null : label.trim() });
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
  }

  return (
    <div className="section">
      <div className="muted">Identity label (overrides Google account email)</div>
      <div className="row">
        <input
          type="text"
          placeholder='e.g. "Acme Corp", "Personal" — blank uses Google email'
          value={label}
          onChange={(e) => setLabel(e.target.value)}
        />
        <button onClick={save}>Save</button>
      </div>
      {saved && <div className="muted">Saved.</div>}
    </div>
  );
}
