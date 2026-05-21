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
      <h2>Identity label</h2>
      <div className="muted" style={{ marginBottom: 6 }}>
        Overrides your Google account email. Blank = use Google email.
      </div>
      <div className="row">
        <input
          type="text"
          placeholder='e.g. "Acme Corp", "Personal"'
          value={label}
          onChange={(e) => setLabel(e.target.value)}
        />
        <button onClick={save}>Save</button>
      </div>
      {saved && <span className="saved-flash">✓ Saved</span>}
    </div>
  );
}
