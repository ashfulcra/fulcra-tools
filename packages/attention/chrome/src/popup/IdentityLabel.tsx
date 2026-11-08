import {v1HeldMode} from "../relayless/v1AuthClient";
import { useEffect, useState } from "react";
import { loadSettings, saveSettings } from "../storage";
import { TokenStore } from "../relayless/tokenStore";
import { updateIdentity, type EnsureOpts } from "../relayless/ensureDefinition";

/**
 * "This browser" section. Shows the per-browser identity label and lets the
 * user rename it. On save it persists the label, then re-tags the cached
 * Attention resolution (updateIdentity) so future records carry the new
 * machine:<slug> tag. Gracefully handles the not-yet-onboarded case:
 * updateIdentity no-ops (returns null) when there's no cached definition, so
 * we still persist the label.
 *
 * Deps are injectable so the popup test drives without chrome.* / network.
 */
export function IdentityLabel(props: {
  tokenStore?: TokenStore;
  update?: typeof updateIdentity;
}) {
  const tokenStore = props.tokenStore ?? new TokenStore();
  const update = props.update ?? updateIdentity;

  const [label, setLabel] = useState("");
  const [saved, setSaved] = useState(false);
  const [error,setError]=useState<string|null>(null);

  useEffect(() => {
    void loadSettings().then((s) => setLabel(s.identityLabel ?? "")).catch(()=>setError("Settings held"));
  }, []);

  async function save() {
    setError(null);try{
    const trimmed = label.trim();
    const next = trimmed === "" ? null : trimmed;
    const cur = await loadSettings();
    await saveSettings({ ...cur, identityLabel: next });
    if(await v1HeldMode()){setSaved(true);setTimeout(()=>setSaved(false),1500);return;}
    // Re-tag the cached Attention resolution. No-op (null) when not yet
    // onboarded — the persisted label is still picked up on the next flush.
    const ensureOpts: EnsureOpts = {
      getToken: (o?: { force?: boolean }) =>
        tokenStore.getValidAccessToken({ force: o?.force }),
    };
    try {
      await update(ensureOpts, next);
    } catch {
      // Re-tag is best-effort from the popup; the label is already persisted.
    }
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
    }catch(e){setError(e instanceof Error?e.message:"Settings held");}
  }

  return (
    <div className="section">
      <h2>This browser</h2>
      <div className="muted" style={{ marginBottom: 6 }}>
        All your browsers share one Attention line; this label is how you tell
        them apart.
      </div>
      <div className="row">
        <input
          type="text"
          placeholder="e.g. Work MBP — Chrome"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
        />
        <button onClick={() => void save()}>Save</button>
      </div>
      {error&&<span role="alert">{error}</span>}
      {saved && <span className="saved-flash">✓ Saved</span>}
    </div>
  );
}
