import { useEffect, useState } from "react";
import { BearerForm } from "./BearerForm";
import { LiveStream } from "./LiveStream";
import { IgnoreList } from "./IgnoreList";
import { Counts } from "./Counts";
import { IdentityLabel } from "./IdentityLabel";
import { loadSettings } from "../storage";

function FulcrumMark() {
  return (
    <svg className="logo" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <path d="M3 17h18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
      <path d="M12 4 L18 16 L6 16 Z" fill="#56d6b7" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

function openWizard() {
  void chrome.tabs.create({ url: chrome.runtime.getURL("wizard.html") });
}

export function App() {
  const [onboarded, setOnboarded] = useState<boolean | null>(null);

  useEffect(() => {
    void loadSettings().then((s) => setOnboarded(s.onboarded));
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
      <BearerForm />
      <Counts />
      <LiveStream />
      <IgnoreList />
      <IdentityLabel />
      <div className="section">
        <button onClick={openWizard} style={{ width: "100%" }}>
          Re-run setup wizard
        </button>
      </div>
    </div>
  );
}
