import { useEffect, useState } from "react";
import { loadSettings, saveSettings } from "../storage";

const PRESETS_MIN: Array<{ label: string; minutes: number | "indefinite" }> = [
  { label: "15 minutes", minutes: 15 },
  { label: "30 minutes", minutes: 30 },
  { label: "1 hour", minutes: 60 },
  { label: "Until I resume", minutes: "indefinite" },
];

function formatRemaining(ms: number): string {
  if (ms <= 0) return "any moment";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

/**
 * Pause UI. When not paused: dropdown picker + Pause button.
 * When paused: "Paused — resumes in X (Resume now)" pill.
 *
 * Pause is machine-local for v1; cross-machine pause sync is a future
 * iteration. Indefinite pause = pausedUntil set to Number.MAX_SAFE_INTEGER
 * so the same `pausedUntil > now` check works uniformly.
 */
export function PauseControl() {
  const [pausedUntil, setPausedUntil] = useState<number | null>(null);
  const [pick, setPick] = useState<number | "indefinite">(15);
  const [tick, setTick] = useState(0);  // forces refresh of the countdown

  useEffect(() => {
    void loadSettings().then((s) => setPausedUntil(s.pausedUntil));
  }, []);

  // Cheap countdown — every second, force a re-render so the
  // "resumes in 14m 22s" pill updates.
  useEffect(() => {
    if (pausedUntil === null) return;
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, [pausedUntil]);

  // Lazy auto-resume in case the SW's sweep hasn't cleared it yet.
  useEffect(() => {
    if (pausedUntil !== null && Date.now() >= pausedUntil) {
      void resume();
    }
  }, [pausedUntil, tick]);

  async function pause(): Promise<void> {
    const cur = await loadSettings();
    const until = pick === "indefinite"
      ? Number.MAX_SAFE_INTEGER
      : Date.now() + (pick as number) * 60_000;
    await saveSettings({ ...cur, pausedUntil: until });
    setPausedUntil(until);
  }

  async function resume(): Promise<void> {
    const cur = await loadSettings();
    await saveSettings({ ...cur, pausedUntil: null });
    setPausedUntil(null);
  }

  if (pausedUntil !== null && Date.now() < pausedUntil) {
    const remaining = pausedUntil - Date.now();
    const label = pausedUntil === Number.MAX_SAFE_INTEGER
      ? "indefinitely"
      : `in ${formatRemaining(remaining)}`;
    return (
      <div className="section">
        <h2>Capture</h2>
        <div className="row" style={{ gap: 8 }}>
          <span className="tag cat" style={{ padding: "2px 10px" }}>Paused</span>
          <span className="muted" style={{ flex: 1 }}>
            Resumes {label}
          </span>
          <button onClick={() => void resume()}>Resume now</button>
        </div>
      </div>
    );
  }

  return (
    <div className="section">
      <h2>Capture</h2>
      <div className="row" style={{ gap: 8 }}>
        <select
          value={pick === "indefinite" ? "indefinite" : String(pick)}
          onChange={(e) => {
            const v = e.target.value;
            setPick(v === "indefinite" ? "indefinite" : Number(v));
          }}
          style={{ flex: 1 }}
        >
          {PRESETS_MIN.map((p) => (
            <option key={String(p.minutes)} value={p.minutes}>
              Pause for {p.label}
            </option>
          ))}
        </select>
        <button onClick={() => void pause()}>Pause</button>
      </div>
    </div>
  );
}
