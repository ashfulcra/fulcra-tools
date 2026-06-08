import { useEffect, useState } from "react";
import { loadIgnoreList, saveIgnoreList } from "../storage";
import type { IgnoreEntry } from "../types";

export function IgnoreList() {
  const [list, setList] = useState<IgnoreEntry[]>([]);
  const [draft, setDraft] = useState("");

  async function refresh() {
    setList(await loadIgnoreList());
  }

  useEffect(() => { void refresh(); }, []);

  async function add() {
    const pat = draft.trim();
    if (pat === "") return;
    const cur = await loadIgnoreList();
    if (cur.some((e) => e.pattern === pat)) {
      setDraft("");
      return;
    }
    await saveIgnoreList([...cur, { pattern: pat, addedAt: new Date().toISOString() }]);
    setDraft("");
    await refresh();
  }

  async function remove(pat: string) {
    const cur = await loadIgnoreList();
    await saveIgnoreList(cur.filter((e) => e.pattern !== pat));
    await refresh();
  }

  return (
    <div className="section">
      <h2>Ignore list</h2>
      <div className="muted" style={{ marginBottom: 6 }}>
        Dropped entirely. Syncs across your Chrome profiles.
      </div>
      <div className="row">
        <input
          type="text"
          placeholder='example.com or *.example.com'
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") void add(); }}
        />
        <button onClick={() => void add()}>Add</button>
      </div>
      <div className="ignore-list">
        {list.length === 0 && <div className="muted">(empty)</div>}
        {list.map((e) => (
          <div key={e.pattern} className="ignore-row">
            <span style={{ flex: 1 }}>{e.pattern}</span>
            <button onClick={() => void remove(e.pattern)} aria-label={`Remove ${e.pattern}`}>×</button>
          </div>
        ))}
      </div>
    </div>
  );
}
