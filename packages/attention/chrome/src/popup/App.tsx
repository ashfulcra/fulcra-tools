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

export function App() {
  const [onboarded, setOnboarded] = useState<boolean | null>(null);

  useEffect(() => {
    void loadSettings().then((s) => {
      setOnboarded(s.onboarded);
    });
  }, []);

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
      <Banner />
      <SignIn />
      <PauseControl />
      <Counts />
      <LiveStream />
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
