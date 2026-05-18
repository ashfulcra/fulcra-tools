import "./options.css";

function FulcrumMark() {
  return (
    <svg
      className="logo"
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      <path d="M3 17h18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
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

export function Options() {
  return (
    <div className="options">
      <header className="options-header">
        <FulcrumMark />
        <h1>Fulcra Attention</h1>
        <span className="sub">v0.1 · Options</span>
      </header>

      <h2>Status</h2>
      <p>
        For now, the popup is the source of truth for all settings —
        bearer token, ignore list, identity label, and the live event
        stream all live there.
      </p>

      <h2>What's coming in v1.5</h2>
      <p>
        Tier 2 category editor (full <code>domain → category</code> rule
        management), preset packs (banking, healthcare, etc.), and
        import/export of your config so it can travel between machines.
      </p>

      <h2>Privacy model</h2>
      <p className="muted">
        Tier 1 (always-on) strips a small denylist of auth-bearing URL
        parameters. Tier 2 categories collapse a URL down to a single
        slug — useful for "I read 4 things on Reddit today" without
        keeping the specific threads. Tier 3 ignore list drops the
        event entirely.
      </p>

      <h2>Project</h2>
      <p>
        <a href="https://github.com/ashfulcra/fulcra-attention" target="_blank" rel="noreferrer">
          github.com/ashfulcra/fulcra-attention
        </a>
        {" · "}
        <a href="https://fulcra.ai" target="_blank" rel="noreferrer">fulcra.ai</a>
      </p>
    </div>
  );
}
