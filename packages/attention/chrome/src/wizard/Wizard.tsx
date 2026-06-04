import { useEffect, useMemo, useState } from "react";
import "./wizard.css";
import markUrl from "../assets/fulcra-mark.png";
import {
  loadSettings, saveSettings, loadIgnoreList, saveIgnoreList,
  loadBackfillRuns, recordBackfillRun, getMachineId,
} from "../storage";
import type { IgnoreEntry, BackfillRun } from "../types";
import {
  EXCLUSION_PRESETS, fetchAndGroupHistory, matchesAnyPattern, buildIgnoreList,
} from "./history";
import type { DomainGroup } from "./history";
import { backfillHistory } from "./backfill";
import { setHeartbeatEnabled, hasHeartbeatPermission } from "../heartbeat-control";
import { SignIn } from "../popup/SignIn";
import type { TransportMode } from "../types";

type Step = "welcome" | "token" | "scan" | "filter" | "heartbeat" | "ingest" | "done";

function FulcrumMark() {
  return <img className="logo" src={markUrl} alt="Fulcra" />;
}

export function Wizard() {
  const [step, setStep] = useState<Step>("welcome");

  // ---- token / auth step state ----
  const [token, setToken] = useState("");
  // Drives the auth step: "relayless" (default) shows device-flow sign-in,
  // "relay" keeps the daemon token-paste form. Null until loadSettings resolves.
  const [transportMode, setTransportMode] = useState<TransportMode | null>(null);

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
  // A backfill already run on a *different* machine. When set, the
  // backfill defaults OFF — re-backfilling synced history duplicates
  // events. Null when no other machine has backfilled.
  const [priorBackfill, setPriorBackfill] = useState<BackfillRun | null>(null);

  // Pre-populate manuallyExcluded with anything already on the Tier 3 list
  // so the wizard starts from "here's what you already excluded".
  useEffect(() => {
    void (async () => {
      const existing = await loadIgnoreList();
      setExistingPatterns(existing.map((e) => e.pattern));
    })();
    void loadSettings().then((s) => {
      setToken(s.bearerToken ?? "");
      setTransportMode(s.transportMode);
    });
    // If another machine on this Chrome profile already backfilled,
    // default the backfill OFF and surface a warning — re-backfilling
    // synced history would duplicate every event.
    void (async () => {
      const [runs, myId] = await Promise.all([loadBackfillRuns(), getMachineId()]);
      const fromOthers = runs.filter((r) => r.machineId !== myId);
      if (fromOthers.length > 0) {
        const latest = fromOthers.reduce((a, b) => (a.at > b.at ? a : b));
        setPriorBackfill(latest);
        setIngestEnabled(false);
      }
    })();
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
    setStep("heartbeat");
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
      // Record the run so other machines on this Chrome profile warn
      // before backfilling the same synced history again.
      await recordBackfillRun(await getMachineId());
      await markOnboarded();
      setIngestComplete(true);
      setIngestProgress({ done: count, total: count });
      setStep("done");
    } catch (e) {
      // Surface the error so the user knows why nothing advanced —
      // most likely the Fulcra Collect daemon is unreachable or the bearer
      // token is missing/wrong. Events stay in the outbox and will retry
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
    welcome: "Step 1 of 7",
    token: "Step 2 of 7",
    scan: "Step 3 of 7",
    filter: "Step 4 of 7",
    heartbeat: "Step 5 of 7",
    ingest: "Step 6 of 7",
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

      {step === "token" && transportMode === "relayless" && (
        <SignInStep onSignedIn={() => setStep("scan")} />
      )}

      {step === "token" && transportMode === "relay" && (
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

      {step === "heartbeat" && (
        <HeartbeatStep
          onBack={() => setStep("filter")}
          onNext={() => setStep("ingest")}
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
          priorBackfill={priorBackfill}
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
        <li>Paste the bearer token from Fulcra Collect (<em>Attention → Pair extension</em>)</li>
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

function SignInStep(props: { onSignedIn: () => void }) {
  return (
    <>
      <h2>Sign in to Fulcra</h2>
      <p>
        Connect this browser straight to Fulcra Cloud — no local app needed.
        We'll open a confirmation page; approve it and we'll bring you back
        here to scan your history.
      </p>
      {/*
        Reuse the popup's relayless sign-in surface. onSignedIn fires once the
        device flow completes (or immediately via the "Continue" button when a
        valid token already exists), advancing the wizard to the scan step.
      */}
      <SignIn onSignedIn={props.onSignedIn} />
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
      <h2>Connect to Fulcra Collect</h2>
      <p>
        Events go to the Fulcra Collect daemon running on this machine at{" "}
        <code>http://127.0.0.1:9292/api/extension/attention</code>. Paste the
        bearer token Collect issued when you paired the extension:
      </p>
      <input
        type="password"
        placeholder="Bearer token"
        value={props.token}
        onChange={(e) => props.setToken(e.target.value)}
        style={{ width: "100%", boxSizing: "border-box" }}
      />
      <p className="muted">
        Don't have one yet? Open the Fulcra Collect app, go to the{" "}
        <strong>Attention</strong> plugin, and click{" "}
        <strong>Pair extension</strong>. Collect issues a bearer token and
        shows it for you to paste here. Re-running that step re-issues the
        token if you ever need a fresh one.
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

function HeartbeatStep(props: { onBack: () => void; onNext: () => void }) {
  const [enabling, setEnabling] = useState(false);
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [denied, setDenied] = useState(false);

  // Reflect the current real state on mount so a user re-running the
  // wizard sees the right toggle.
  useEffect(() => {
    void (async () => {
      const s = await loadSettings();
      const hasPerm = await hasHeartbeatPermission();
      setEnabled(s.heartbeatEnabled && hasPerm);
    })();
  }, []);

  async function enable(): Promise<void> {
    setEnabling(true);
    setDenied(false);
    try {
      const ok = await setHeartbeatEnabled(true);
      setEnabled(ok);
      if (!ok) setDenied(true);
    } finally {
      setEnabling(false);
    }
  }

  return (
    <>
      <h2>Sharper AFK detection (optional)</h2>
      <p>
        By default we use Chrome's system-level idle signal — keyboard,
        mouse, screen lock. That misses cases where you've walked away
        from your desk but Chrome thinks you're still active (no clicks
        for a few minutes).
      </p>
      <p>
        Turn this on and we'll also watch for <em>any</em> mouse movement,
        scroll, or keypress inside the tab you're reading.
      </p>
      <div style={{
        border: "1px solid var(--fa-edge)",
        background: "var(--fa-surface)",
        borderRadius: 8, padding: "12px 14px", margin: "14px 0",
      }}>
        <strong style={{ display: "block", marginBottom: 6 }}>
          What this script reads
        </strong>
        <ul style={{ margin: "0 0 6px 22px", padding: 0 }}>
          <li><strong>No</strong> page content, DOM, text, or forms</li>
          <li><strong>No</strong> URLs, titles, or selected text</li>
          <li><em>Only</em> whether you're interacting with the page (event types, not values)</li>
        </ul>
        <span className="muted">
          Chrome will ask you to grant "read and change all your data on websites you visit."
          That permission is needed for the watchdog to load on every page,
          but the script itself reads no page data.
        </span>
      </div>

      {enabled && (
        <p className="muted" style={{ color: "var(--fa-mint-2)" }}>
          ✓ Enabled. You can flip this off anytime from the popup.
        </p>
      )}
      {denied && (
        <p className="muted" style={{ color: "var(--fa-danger)" }}>
          Permission declined. The default chrome.idle signal still works —
          you can flip this on later from the popup.
        </p>
      )}

      <div className="action-row">
        <button onClick={props.onBack}>← Back</button>
        <div className="spacer" />
        <button onClick={props.onNext}>Skip this</button>
        {!enabled && (
          <button className="primary" onClick={() => void enable()} disabled={enabling}>
            {enabling ? "Asking Chrome…" : "Enable sharper AFK"}
          </button>
        )}
        {enabled && (
          <button className="primary" onClick={props.onNext}>
            Continue →
          </button>
        )}
      </div>
    </>
  );
}

function IngestStep(props: {
  groups: DomainGroup[];
  livePatterns: string[];
  ingestEnabled: boolean;
  setIngestEnabled: (b: boolean) => void;
  progress: { done: number; total: number } | null;
  running: boolean;
  error: string | null;
  priorBackfill: BackfillRun | null;
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

      {props.priorBackfill && (
        <div style={{
          border: "1px solid var(--fa-edge)",
          background: "var(--fa-surface)",
          borderRadius: 8, padding: "12px 14px", margin: "12px 0",
        }}>
          <strong style={{ display: "block", marginBottom: 4 }}>
            Another machine already backfilled
          </strong>
          <span className="muted">
            History was backfilled from a different machine on{" "}
            {new Date(props.priorBackfill.at).toLocaleDateString()}. If your
            Chrome history syncs across machines, this machine sees the same
            pages — backfilling again would create duplicate events, so it's
            left unchecked. If this machine keeps its <em>own</em> separate
            history, check the box to back-fill it.
          </span>
        </div>
      )}

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
            Most likely cause: the Fulcra Collect daemon isn't reachable on{" "}
            <code>127.0.0.1:9292</code>, or the bearer token in settings
            doesn't match the one Collect issued when you paired the extension
            (<em>Attention → Pair extension</em>). Queued events stay in the
            outbox and will retry automatically — you can finish the wizard and
            check the popup's Recent stream later.
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
            ✓ Back-filled history queued for Fulcra Collect. The outbox will drain in the background.
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
