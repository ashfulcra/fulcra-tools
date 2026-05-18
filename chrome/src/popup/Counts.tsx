import { useEffect, useState } from "react";

interface CountsState { logged: number; categorized: number; ignored: number; }

export function Counts() {
  const [c, setC] = useState<CountsState>({ logged: 0, categorized: 0, ignored: 0 });

  useEffect(() => {
    let stopped = false;
    async function refresh() {
      const r = await chrome.storage.local.get("counts");
      const today = new Date().toISOString().slice(0, 10);
      const raw = r.counts as { date: string; logged: number; categorized: number; ignored: number } | undefined;
      const cur = raw && raw.date === today ? raw : { date: today, logged: 0, categorized: 0, ignored: 0 };
      if (!stopped) setC({ logged: cur.logged, categorized: cur.categorized, ignored: cur.ignored });
    }
    void refresh();
    const id = setInterval(refresh, 2000);
    return () => { stopped = true; clearInterval(id); };
  }, []);

  return (
    <div className="section">
      <div className="muted">Today: {c.logged} logged · {c.categorized} categorized · {c.ignored} ignored</div>
    </div>
  );
}
