import { useEffect, useState } from "react";
import type { AttentionEvent } from "../types";

function ShortTime({ iso }: { iso: string }) {
  const d = new Date(iso);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return <span className="ts">{hh}:{mm}</span>;
}

function Row({ ev }: { ev: AttentionEvent }) {
  if (ev.category !== null) {
    return (
      <div className="stream-row">
        <ShortTime iso={ev.end_time} />
        <span className="tag cat">{ev.category}</span>
      </div>
    );
  }
  return (
    <div className="stream-row">
      <ShortTime iso={ev.end_time} />
      <span className="tag url">{ev.title || ev.url}</span>
    </div>
  );
}

export function LiveStream() {
  const [recent, setRecent] = useState<AttentionEvent[]>([]);

  useEffect(() => {
    let stopped = false;
    async function refresh() {
      const r = await chrome.storage.local.get("recentEmitted");
      const list = (r.recentEmitted as AttentionEvent[] | undefined) ?? [];
      if (!stopped) setRecent(list.slice(0, 5));
    }
    void refresh();
    const id = setInterval(refresh, 2000);
    return () => { stopped = true; clearInterval(id); };
  }, []);

  if (recent.length === 0) {
    return (
      <div className="section">
        <h2>Recent</h2>
        <div className="muted">No events yet — visit a page in another tab.</div>
      </div>
    );
  }
  return (
    <div className="section">
      <h2>Recent</h2>
      {recent.map((ev, i) => <Row key={`${ev.end_time}-${i}`} ev={ev} />)}
    </div>
  );
}
