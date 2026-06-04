import { useEffect, useState } from "react";
import { loadOutbox } from "../storage";
import { flushOutbox } from "../outbox";
import type { TransportMode } from "../types";

interface IngestError { kind: "unauthorized" | "unreachable"; at: number; }

export interface BannerProps {
  /** Drives the copy for the `unauthorized` state: relay mode talks about
   * re-pairing a daemon token; relayless mode routes to "Sign in to Fulcra".
   * Defaults to "relay" so existing callers are unaffected. */
  transportMode?: TransportMode;
}

/**
 * Top-of-popup status banner. Rolls several states into one:
 *
 *   - 401 (unauthorized):
 *       relay      → "Reconnect — your token doesn't match" (re-pair daemon)
 *       relayless  → "Sign in to Fulcra" (routes to the sign-in surface below)
 *   - repeated network failures → "… unreachable; N events queued"
 *   - normal outbox depth → "N events waiting to ship" + Flush Now
 *
 * On a healthy run with no queued events, the banner renders nothing.
 */
export function Banner(props: BannerProps = {}) {
  const transportMode = props.transportMode ?? "relay";
  const [err, setErr] = useState<IngestError | null>(null);
  const [queued, setQueued] = useState(0);
  const [flushing, setFlushing] = useState(false);

  useEffect(() => {
    let stopped = false;
    async function refresh() {
      const [r, outbox] = await Promise.all([
        chrome.storage.local.get("lastIngestError"),
        loadOutbox(),
      ]);
      if (stopped) return;
      setErr((r.lastIngestError as IngestError | undefined) ?? null);
      setQueued(outbox.length);
    }
    void refresh();
    const id = setInterval(refresh, 2_000);
    return () => { stopped = true; clearInterval(id); };
  }, []);

  async function flushNow(): Promise<void> {
    setFlushing(true);
    try {
      await flushOutbox();
    } finally {
      setFlushing(false);
    }
  }

  if (err?.kind === "unauthorized") {
    if (transportMode === "relayless") {
      return (
        <div className="banner banner-error">
          <strong>Sign in to Fulcra.</strong> Your events are queued and will
          ship once you sign in below.
        </div>
      );
    }
    return (
      <div className="banner banner-error">
        <strong>Reconnect.</strong> Fulcra Collect rejected your bearer token.
        Re-pair from the Collect app (<em>Attention → Pair extension</em>) to
        get a fresh token, then paste it into the field below and click Save.
      </div>
    );
  }
  if (err?.kind === "unreachable") {
    const what = transportMode === "relayless" ? "Fulcra Cloud" : "Fulcra Collect";
    return (
      <div className="banner banner-warn">
        <strong>{what} unreachable.</strong> {queued} event{queued === 1 ? "" : "s"} queued; will retry every minute.
        <button onClick={() => void flushNow()} disabled={flushing}
                style={{ marginLeft: 8 }}>
          {flushing ? "…" : "Retry now"}
        </button>
      </div>
    );
  }
  if (queued > 0) {
    return (
      <div className="banner banner-info">
        {queued} event{queued === 1 ? "" : "s"} waiting to ship.
        <button onClick={() => void flushNow()} disabled={flushing}
                style={{ marginLeft: 8 }}>
          {flushing ? "…" : "Flush now"}
        </button>
      </div>
    );
  }
  return null;
}
