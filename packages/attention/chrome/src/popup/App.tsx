import { useEffect, useState } from "react";
import { SignIn } from "./SignIn";
import { LiveStream } from "./LiveStream";
import { IgnoreList } from "./IgnoreList";
import { Counts } from "./Counts";
import { IdentityLabel } from "./IdentityLabel";
import { PauseControl } from "./PauseControl";
import { HeartbeatToggle } from "./HeartbeatToggle";
import { Banner } from "./Banner";
import { CategoryEditor } from "./CategoryEditor";
import {v1HeldMode} from "../relayless/v1AuthClient";
import {loadCaptureStatus,type CaptureStatus} from "../storage";
import { loadSettings } from "../storage";

import markUrl from "../assets/fulcra-mark.png";

function FulcrumMark() {
  // The official Fulcra mark (hexagon + spiral). Bundled at build time
  // by Vite, served from the extension's own origin so the popup works
  // offline.
  return <img className="logo" src={markUrl} alt="Fulcra" />;
}

function openWizard() {
  void chrome.tabs.create({ url: chrome.runtime.getURL("wizard.html") });
}

function HeldCaptureStatus(){
 const [status,setStatus]=useState<CaptureStatus|null>(null),[unknown,setUnknown]=useState(false);
 useEffect(()=>{let stopped=false;void loadCaptureStatus().then(value=>{if(!stopped)setStatus(value);}).catch(()=>{if(!stopped)setUnknown(true);});return ()=>{stopped=true;};},[]);
 return <div role="status" className="section"><h2>Capture held locally</h2><p>Both drains are disabled. No upload is confirmed.</p>{unknown?<p>Retention UNKNOWN; preserve active input.</p>:status?<p>{status.retained} locally retained: {status.held} held, {status.active} active, {status.prepared} awaiting journal acknowledgement, {status.enqueued} journal-verified pending.{status.legacyObservation!=='complete'?' Legacy observation is incomplete.':''}</p>:<p>Loading retention status…</p>}</div>;
}
export function App() {
  const [heldMode,setHeldMode]=useState<boolean|null>(null);
  const [settingsHeld,setSettingsHeld]=useState(false);
  const [onboarded, setOnboarded] = useState<boolean | null>(null);

  useEffect(() => {
    void Promise.all([loadSettings(),v1HeldMode()]).then(([s,held]) => {
      setHeldMode(held);setOnboarded(s.onboarded);
    }).catch(()=>setSettingsHeld(true));
  }, []);

  if(settingsHeld)return <div role="alert">Attention held: settings are unavailable.</div>;
  if(onboarded===null)return <div role="status">Loading settings…</div>;

  // First-run prompt: until the wizard has been completed, show a
  // single-purpose card that does the bare-minimum (open the wizard
  // in a new tab). The full popup UI returns after onboarding.
  if (onboarded === false) {
    return (
      <div className="app">
        <header className="app-header">
          <FulcrumMark />
          <h1>Fulcra Attention</h1>
          <span className="sub">v0.1</span>
        </header>
        <p style={{ margin: "0 0 12px" }}>
          Welcome — let's get you set up.
        </p>
        <div className="row" style={{ gap: 8 }}>
          <button className="primary" style={{ flex: 1 }} onClick={openWizard}>
            Open setup wizard
          </button>
        </div>
        <p className="muted" style={{ marginTop: 10 }}>
          The wizard runs in a regular tab — it has room to show your history
          and let you pick what to exclude.
        </p>
      </div>
    );
  }

  return (
    <div className="app">
      <header className="app-header">
        <FulcrumMark />
        <h1>Fulcra Attention</h1>
        <span className="sub">v0.1</span>
      </header>
      {heldMode?<HeldCaptureStatus/>:<Banner />}
      <SignIn />
      <PauseControl />
      {!heldMode&&<><Counts /><LiveStream /></>}
      <CategoryEditor />
      <IgnoreList />
      <HeartbeatToggle />
      <IdentityLabel />
      <div className="section">
        <button onClick={openWizard} style={{ width: "100%" }}>
          Re-run setup wizard
        </button>
      </div>
    </div>
  );
}
