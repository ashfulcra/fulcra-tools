import { useEffect, useState } from "react";
import { loadOutbox } from "../storage";
import { flushOutbox } from "../outbox";

interface IngestError { kind: "unauthorized" | "unreachable"; at: number; }

/**
 * Top-of-popup status banner. Three things rolled into one:
 *
 *   - 401 from the relay → "Reconnect — your token doesn't match"
 *   - repeated network failures → "Relay unreachable; N events queued"
 *   - normal outbox depth → "N events waiting to ship" + Flush Now
 *
 * On a healthy run with no queued events, the banner renders nothing.
 */
export function Banner() {
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
    return (
      <div className="banner banner-error">
        <strong>Reconnect.</strong> The relay rejected your bearer token —
        paste the current one from <code>~/.config/fulcra-attention/relay.json</code>
        into the field below and click Save.
      </div>
    );
  }
  if (err?.kind === "unreachable") {
    return (
      <div className="banner banner-warn">
        <strong>Relay unreachable.</strong> {queued} event{queued === 1 ? "" : "s"} queued; will retry every minute.
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
