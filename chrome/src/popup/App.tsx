import { BearerForm } from "./BearerForm";
import { LiveStream } from "./LiveStream";
import { IgnoreList } from "./IgnoreList";
import { Counts } from "./Counts";
import { IdentityLabel } from "./IdentityLabel";

/**
 * Fulcra "fulcrum" mark — a stylised pivot. Inlined as SVG so the popup
 * doesn't need an image asset; tint follows currentColor (`fa-ink`).
 * Two short bars on either side of a triangle / wedge gives the lever
 * feel and reads at 18px without aliasing.
 */
function FulcrumMark() {
  return (
    <svg
      className="logo"
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      <path
        d="M3 17h18"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
      <path
        d="M12 4 L18 16 L6 16 Z"
        fill="#56d6b7"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function App() {
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
    </div>
  );
}
