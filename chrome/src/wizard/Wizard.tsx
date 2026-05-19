import { useEffect, useMemo, useState } from "react";
import "./wizard.css";
import markUrl from "../assets/fulcra-mark.png";
import { loadSettings, saveSettings, loadIgnoreList, saveIgnoreList } from "../storage";
import type { IgnoreEntry } from "../types";
import {
  EXCLUSION_PRESETS, fetchAndGroupHistory, matchesAnyPattern, buildIgnoreList,
} from "./history";
import type { DomainGroup } from "./history";
import { backfillHistory } from "./backfill";

type Step = "welcome" | "token" | "scan" | "filter" | "ingest" | "done";

function FulcrumMark() {
  return <img className="logo" src={markUrl} alt="Fulcra" />;
}

export function Wizard() {
  const [step, setStep] = useState<Step>("welcome");

  // ---- token step state ----
  const [token, setToken] = useState("");

  // ---- scan step state ----
  const [daysBack, setDaysBack] = useState(30);
  const [maxResults, setMaxResults] = useState(2000);
  const [groups, setGroups] = useState<DomainGroup[] | null>(null);
  const [scanError, setScanError] = useState<string | null>(null);

  // ---- filter step state ----
  const [selectedPresetIds, setSelectedPresetIds] = useState<string[]>([]);
  const [manuallyExcluded, setManuallyExcluded] = useState<Set<string>>(new Set());
  const [existingPatterns, setExistingPatterns] = useState<string[]>([]);
  const [search, setSearch] = useState("");

  // ---- ingest step state ----
  const [ingestProgress, setIngestProgress] = useState<{ done: number; total: number } | null>(null);
  const [ingestEnabled, setIngestEnabled] = useState(true);
  const [ingestComplete, setIngestComplete] = useState(false);
  const [ingestRunning, setIngestRunning] = useState(false);
  const [ingestError, setIngestError] = useState<string | null>(null);

  // Pre-populate manuallyExcluded with anything already on the Tier 3 list
  // so the wizard starts from "here's what you already excluded".
  useEffect(() => {
    void (async () => {
      const existing = await loadIgnoreList();
      setExistingPatterns(existing.map((e) => e.pattern));
    })();
    void loadSettings().then((s) => setToken(s.bearerToken ?? ""));
  }, []);

  // ---- handlers ----

  async function saveTokenAndAdvance() {
    const cur = await loadSettings();
    await saveSettings({ ...cur, bearerToken: token.trim() || null });
    setStep("scan");
  }

  async function runScan() {
    setScanError(null);
    setGroups(null);
    try {
      const g = await fetchAndGroupHistory({ daysBack, maxResults });
      setGroups(g);
      setStep("filter");
    } catch (e) {
      setScanError((e as Error).message ?? String(e));
    }
  }

  function toggleHost(host: string) {
    const next = new Set(manuallyExcluded);
    if (next.has(host)) next.delete(host);
    else next.add(host);
    setManuallyExcluded(next);
  }

  function togglePreset(id: string) {
    setSelectedPresetIds(selectedPresetIds.includes(id)
      ? selectedPresetIds.filter((x) => x !== id)
      : [...selectedPresetIds, id]);
  }

  /** Patterns that would be active if the wizard committed now. */
  const livePatterns = useMemo(
    () => buildIgnoreList(selectedPresetIds, [...manuallyExcluded], existingPatterns),
    [selectedPresetIds, manuallyExcluded, existingPatterns],
  );

  async function applyExclusionsAndAdvance() {
    // Merge selected presets + manual hosts into the existing Tier-3 list.
    const merged = livePatterns;
    const now = new Date().toISOString();
    // Preserve existing addedAt timestamps; only timestamp new ones.
    const existing = await loadIgnoreList();
    const existingMap = new Map<string, IgnoreEntry>();
    for (const e of existing) existingMap.set(e.pattern, e);
    const out: IgnoreEntry[] = merged.map((pattern) => (
      existingMap.get(pattern) ?? { pattern, addedAt: now }
    ));
    await saveIgnoreList(out);
    setStep("ingest");
  }

  async function runIngest() {
    if (ingestRunning) return;  // double-click guard
    if (!groups) return;
    setIngestRunning(true);
    setIngestError(null);
    setIngestProgress({ done: 0, total: 0 });
    try {
      if (!ingestEnabled) {
        // Skip the actual backfill — go straight to done.
        await markOnboarded();
        setIngestComplete(true);
        setStep("done");
        return;
      }
      // Filter out groups whose host is covered by ANY current exclusion
      // (preset, manual, or pre-existing). The wizard already wrote
      // those into storage in applyExclusionsAndAdvance, but recompute
      // here so this function stays correct in isolation.
      const keep = groups.filter((g) => !matchesAnyPattern(g.host, livePatterns));
      const count = await backfillHistory(keep, {
        onProgress: (done, total) => setIngestProgress({ done, total }),
      });
      await markOnboarded();
      setIngestComplete(true);
      setIngestProgress({ done: count, total: count });
      setStep("done");
    } catch (e) {
      // Surface the error so the user knows why nothing advanced —
      // most likely the relay is unreachable or the bearer token
      // is missing/wrong. Events stay in the outbox and will retry
      // on the next chrome.alarms tick, so this isn't data loss.
      setIngestError((e as Error).message ?? String(e));
    } finally {
      setIngestRunning(false);
    }
  }

  async function markOnboarded() {
    const cur = await loadSettings();
    await saveSettings({ ...cur, onboarded: true });
  }

  async function skipWizard() {
    await markOnboarded();
    setStep("done");
  }

  // ---- render ----

  const tagForStep: Record<Step, string> = {
    welcome: "Step 1 of 6",
    token: "Step 2 of 6",
    scan: "Step 3 of 6",
    filter: "Step 4 of 6",
    ingest: "Step 5 of 6",
    done: "All set",
  };

  return (
    <div className="wizard">
      <header className="wizard-header">
        <FulcrumMark />
        <h1>Fulcra Attention — Setup</h1>
        <span className="step-tag">{tagForStep[step]}</span>
      </header>

      {step === "welcome" && (
        <WelcomeStep
          onNext={() => setStep("token")}
          onSkip={() => void skipWizard()}
        />
      )}

      {step === "token" && (
        <TokenStep
          token={token}
          setToken={setToken}
          onNext={() => void saveTokenAndAdvance()}
        />
      )}

      {step === "scan" && (
        <ScanStep
          daysBack={daysBack} setDaysBack={setDaysBack}
          maxResults={maxResults} setMaxResults={setMaxResults}
          scanError={scanError}
          onNext={() => void runScan()}
        />
      )}

      {step === "filter" && groups && (
        <FilterStep
          groups={groups}
          selectedPresetIds={selectedPresetIds}
          togglePreset={togglePreset}
          manuallyExcluded={manuallyExcluded}
          toggleHost={toggleHost}
          search={search}
          setSearch={setSearch}
          existingPatterns={existingPatterns}
          livePatterns={livePatterns}
          onNext={() => void applyExclusionsAndAdvance()}
          onBack={() => setStep("scan")}
        />
      )}

      {step === "ingest" && (
        <IngestStep
          groups={groups ?? []}
          livePatterns={livePatterns}
          ingestEnabled={ingestEnabled}
          setIngestEnabled={setIngestEnabled}
          progress={ingestProgress}
          running={ingestRunning}
          error={ingestError}
          onStart={() => void runIngest()}
          onBack={() => setStep("filter")}
          onSkip={async () => { await markOnboarded(); setStep("done"); }}
        />
      )}

      {step === "done" && (
        <DoneStep ingestComplete={ingestComplete} />
      )}
    </div>
  );
}

// ============= step components =============

function WelcomeStep({ onNext, onSkip }: { onNext: () => void; onSkip: () => void }) {
  return (
    <>
      <h2>Welcome</h2>
      <p>
        Fulcra Attention captures what you read so you can look it up later.
        This setup takes a couple of minutes:
      </p>
      <ol>
        <li>Paste your relay's bearer token (from <code>~/.config/fulcra-attention/relay.json</code>)</li>
        <li>Scan your recent browser history</li>
        <li>Pick sites and categories to exclude (banking, healthcare, etc.)</li>
        <li>Optionally back-fill the kept history into Fulcra</li>
      </ol>
      <p className="muted">
        You can change everything later from the popup or skip this wizard
        and rely on real-time capture only.
      </p>
      <div className="action-row">
        <div className="spacer" />
        <button onClick={onSkip}>Skip wizard</button>
        <button className="primary" onClick={onNext}>Get started →</button>
      </div>
    </>
  );
}

function TokenStep(props: {
  token: string;
  setToken: (s: string) => void;
  onNext: () => void;
}) {
  return (
    <>
      <h2>Connect to your relay</h2>
      <p>
        The relay runs on this machine at <code>http://127.0.0.1:8771</code>.
        Paste the bearer token from <code>~/.config/fulcra-attention/relay.json</code>:
      </p>
      <input
        type="password"
        placeholder="Bearer token"
        value={props.token}
        onChange={(e) => props.setToken(e.target.value)}
        style={{ width: "100%", boxSizing: "border-box" }}
      />
      <p className="muted">
        Don't have one yet? Print it with:
        <br />
        <code>cat ~/.config/fulcra-attention/relay.json</code>
        <br />
        — the <code>bearer_token</code> field is what you want. If that file
        doesn't exist, run setup first (from inside the fulcra-attention
        venv):
        <br />
        <code>.venv/bin/fulcra-attention setup</code>
      </p>
      <div className="action-row">
        <div className="spacer" />
        <button className="primary" onClick={props.onNext} disabled={!props.token.trim()}>
          Continue →
        </button>
      </div>
    </>
  );
}

function ScanStep(props: {
  daysBack: number; setDaysBack: (n: number) => void;
  maxResults: number; setMaxResults: (n: number) => void;
  scanError: string | null;
  onNext: () => void;
}) {
  return (
    <>
      <h2>Scan your history</h2>
      <p>
        We'll read browsing history from the last N days, group it by site,
        and let you decide what to keep. Nothing is sent anywhere yet.
      </p>
      <div className="toolbar">
        <label>
          Days back:&nbsp;
          <input
            type="number"
            min={1} max={365}
            value={props.daysBack}
            onChange={(e) => props.setDaysBack(Math.max(1, Math.min(365, Number(e.target.value) || 0)))}
            style={{ width: 80 }}
          />
        </label>
        <label>
          Max items:&nbsp;
          <input
            type="number"
            min={100} max={10000} step={100}
            value={props.maxResults}
            onChange={(e) => props.setMaxResults(Math.max(100, Math.min(10000, Number(e.target.value) || 0)))}
            style={{ width: 100 }}
          />
        </label>
      </div>
      {props.scanError && (
        <p style={{ color: "var(--fa-danger)" }}>Scan failed: {props.scanError}</p>
      )}
      <div className="action-row">
        <div className="spacer" />
        <button className="primary" onClick={props.onNext}>Scan history →</button>
      </div>
    </>
  );
}

function FilterStep(props: {
  groups: DomainGroup[];
  selectedPresetIds: string[];
  togglePreset: (id: string) => void;
  manuallyExcluded: Set<string>;
  toggleHost: (host: string) => void;
  search: string;
  setSearch: (s: string) => void;
  existingPatterns: string[];
  livePatterns: string[];
  onNext: () => void;
  onBack: () => void;
}) {
  const filteredGroups = useMemo(() => {
    const q = props.search.trim().toLowerCase();
    if (!q) return props.groups;
    return props.groups.filter((g) => g.host.toLowerCase().includes(q));
  }, [props.groups, props.search]);

  const totalKept = useMemo(() => props.groups
    .filter((g) => !matchesAnyPattern(g.host, props.livePatterns))
    .reduce((sum, g) => sum + g.count, 0), [props.groups, props.livePatterns]);

  const totalDropped = useMemo(() => props.groups
    .filter((g) => matchesAnyPattern(g.host, props.livePatterns))
    .reduce((sum, g) => sum + g.count, 0), [props.groups, props.livePatterns]);

  return (
    <>
      <h2>Choose what to exclude</h2>
      <p>
        Anything you check here is added to your Tier 3 ignore list — those
        sites won't be logged now or in the future, and won't be back-filled.
      </p>

      <h3>Bulk exclusions</h3>
      <div className="presets">
        {EXCLUSION_PRESETS.map((p) => {
          const on = props.selectedPresetIds.includes(p.id);
          return (
            <div
              key={p.id}
              className={`preset-card ${on ? "on" : ""}`}
              onClick={() => props.togglePreset(p.id)}
              role="checkbox"
              aria-checked={on}
              tabIndex={0}
              onKeyDown={(e) => { if (e.key === " " || e.key === "Enter") props.togglePreset(p.id); }}
            >
              <span className="pname">
                <input
                  type="checkbox"
                  checked={on}
                  onChange={() => props.togglePreset(p.id)}
                  onClick={(e) => e.stopPropagation()}
                  style={{ marginRight: 8 }}
                />
                {p.label}
              </span>
              <span className="pdesc">{p.description}</span>
            </div>
          );
        })}
      </div>

      <h3>Per-site exclusions ({props.groups.length} sites in your history)</h3>
      <div className="toolbar">
        <input
          className="search"
          type="text"
          placeholder="Filter by domain…"
          value={props.search}
          onChange={(e) => props.setSearch(e.target.value)}
        />
        <span className="muted">
          {totalKept.toLocaleString()} visits to keep · {totalDropped.toLocaleString()} excluded
        </span>
      </div>
      <div className="scrollbox">
        <table className="domain-table">
          <thead>
            <tr>
              <th style={{ width: 32 }}></th>
              <th>Host</th>
              <th>Source</th>
              <th>Visits</th>
            </tr>
          </thead>
          <tbody>
            {filteredGroups.map((g) => {
              const presetMatch = matchesAnyPattern(g.host, presetPatternsFor(props.selectedPresetIds));
              const existingMatch = matchesAnyPattern(g.host, props.existingPatterns);
              const manualMatch = props.manuallyExcluded.has(g.host);
              const excluded = presetMatch || existingMatch || manualMatch;
              const rowClass = excluded
                ? (manualMatch ? "excluded" : "excluded-by-preset")
                : "";
              return (
                <tr key={g.host} className={rowClass}>
                  <td>
                    <input
                      type="checkbox"
                      checked={manualMatch}
                      disabled={presetMatch || existingMatch}
                      onChange={() => props.toggleHost(g.host)}
                      title={presetMatch
                        ? "Covered by a selected preset"
                        : existingMatch ? "Already on your ignore list" : ""}
                    />
                  </td>
                  <td>{g.host}</td>
                  <td>
                    {presetMatch && <span className="badge">preset</span>}
                    {existingMatch && !presetMatch && <span className="badge">existing</span>}
                    {manualMatch && !presetMatch && !existingMatch && <span className="badge">manual</span>}
                  </td>
                  <td className="count">{g.count}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="action-row">
        <button onClick={props.onBack}>← Back</button>
        <div className="spacer" />
        <button className="primary" onClick={props.onNext}>
          Apply exclusions →
        </button>
      </div>
    </>
  );
}

function presetPatternsFor(ids: string[]): string[] {
  const out: string[] = [];
  for (const id of ids) {
    const p = EXCLUSION_PRESETS.find((x) => x.id === id);
    if (p) out.push(...p.patterns);
  }
  return out;
}

function IngestStep(props: {
  groups: DomainGroup[];
  livePatterns: string[];
  ingestEnabled: boolean;
  setIngestEnabled: (b: boolean) => void;
  progress: { done: number; total: number } | null;
  running: boolean;
  error: string | null;
  onStart: () => void;
  onBack: () => void;
  onSkip: () => void;
}) {
  const kept = props.groups
    .filter((g) => !matchesAnyPattern(g.host, props.livePatterns))
    .reduce((sum, g) => sum + g.urls.length, 0);

  // Use the running flag from the parent for the disable check — it's
  // true the entire time runIngest is in flight, even before any
  // progress callback has fired. Without this, the button stayed clickable
  // on empty-keep / instant-done flows.
  const inProgress = props.running;
  const pct = props.progress && props.progress.total > 0
    ? Math.round((props.progress.done / props.progress.total) * 100)
    : 0;

  return (
    <>
      <h2>Back-fill your history (optional)</h2>
      <p>
        You can ship the {kept.toLocaleString()} kept URLs into Fulcra as
        synthetic 60-second visits. They'll be tagged with{" "}
        <code>fulcra-attention-chrome-backfill/0.1.0</code> so you can
        tell them apart from real-time captures.
      </p>
      <p className="muted">
        Skip this if you only want real-time tracking from here on. You can
        always re-run the wizard from the popup later.
      </p>
      <label style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
        <input
          type="checkbox"
          checked={props.ingestEnabled}
          onChange={(e) => props.setIngestEnabled(e.target.checked)}
          disabled={inProgress}
        />
        Send the {kept.toLocaleString()} kept visits to Fulcra
      </label>

      {props.progress !== null && (
        <>
          <div className="progress-track">
            <div className="progress-fill" style={{ width: `${pct}%` }} />
          </div>
          <div className="progress-label">
            {props.progress.done.toLocaleString()} / {props.progress.total.toLocaleString()} queued ({pct}%)
          </div>
        </>
      )}

      {props.error && (
        <div style={{
          marginTop: 12, padding: "10px 12px",
          border: "1px solid var(--fa-danger)",
          background: "rgba(217, 38, 56, 0.05)",
          borderRadius: 6,
          color: "var(--fa-danger)",
          fontSize: 13,
        }}>
          <strong>Backfill failed.</strong> {props.error}
          <br />
          <span style={{ color: "var(--fa-muted)" }}>
            Most likely cause: relay isn't reachable on{" "}
            <code>127.0.0.1:8771</code>, or the bearer token in settings
            doesn't match the one in <code>relay.json</code>. Queued events
            stay in the outbox and will retry automatically — you can finish
            the wizard and check the popup's Recent stream later.
          </span>
        </div>
      )}

      <div className="action-row">
        <button onClick={props.onBack} disabled={inProgress}>← Back</button>
        <div className="spacer" />
        {props.error && (
          <button onClick={props.onSkip}>
            Finish anyway →
          </button>
        )}
        <button className="primary" onClick={props.onStart} disabled={inProgress}>
          {inProgress
            ? "Sending…"
            : props.ingestEnabled ? "Send to Fulcra →" : "Finish setup →"}
        </button>
      </div>
    </>
  );
}

function DoneStep({ ingestComplete }: { ingestComplete: boolean }) {
  return (
    <>
      <h2>You're set up</h2>
      <div className="done-card">
        <p style={{ margin: 0 }}>
          ✓ Real-time capture is running. Foreground browsing will start
          showing up in Fulcra immediately, tagged with{" "}
          <code>service:web</code> + <code>identity:&lt;your account&gt;</code>{" "}
          + (when categorized) <code>category:&lt;slug&gt;</code>.
        </p>
        {ingestComplete && (
          <p style={{ margin: "10px 0 0" }}>
            ✓ Back-filled history queued for relay. The outbox will drain in the background.
          </p>
        )}
      </div>
      <p>
        Next stop: your context dashboard.
      </p>
      <div className="action-row">
        <div className="spacer" />
        <a
          href="https://context.fulcradynamics.com/"
          target="_blank"
          rel="noreferrer"
          style={{ textDecoration: "none" }}
        >
          <button className="primary">
            Open Fulcra Context →
          </button>
        </a>
      </div>
      <p className="muted" style={{ marginTop: 14 }}>
        The popup (toolbar icon) stays your day-to-day control surface — pause
        capture, edit your ignore list, swap your identity label.
      </p>
    </>
  );
}
