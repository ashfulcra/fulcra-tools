import { useEffect, useState } from "react";
import { loadCategoryMap, saveCategoryMap } from "../storage";
import type { CategoryMapping } from "../types";

// Mirrored from the Python side (fulcra_attention/fulcra.py CATEGORY_VOCAB).
// Add new slugs in BOTH places — the relay pre-creates the tag at
// bootstrap time, so unfamiliar slugs won't have a tag UUID to bind to.
const VOCAB = [
  "search", "webmail", "ai-chat", "dm", "doc-editor", "reddit-thread",
  "calendar", "banking", "brokerage", "crypto", "tax", "healthcare",
  "password-manager", "mental-health", "dating", "adult", "job-hunting",
];

/**
 * Tier 2 category mapping editor — pattern → slug. Used to collapse
 * a noisy domain (chatgpt.com, reddit.com/r/foo, etc.) into a single
 * category event instead of capturing every URL verbatim.
 *
 * v1 popup shipped this as view-only. This is the inline editor: add /
 * edit / delete from the popup directly.
 */
export function CategoryEditor() {
  const [list, setList] = useState<CategoryMapping[]>([]);
  const [pattern, setPattern] = useState("");
  const [slug, setSlug] = useState<string>(VOCAB[0]);

  async function refresh() {
    setList(await loadCategoryMap());
  }
  useEffect(() => { void refresh(); }, []);

  async function add() {
    const pat = pattern.trim();
    if (!pat) return;
    const cur = await loadCategoryMap();
    const existing = cur.findIndex((m) => m.pattern === pat);
    if (existing >= 0) cur[existing] = { pattern: pat, category: slug };
    else cur.push({ pattern: pat, category: slug });
    await saveCategoryMap(cur);
    setPattern("");
    await refresh();
  }

  async function remove(pat: string) {
    const cur = await loadCategoryMap();
    await saveCategoryMap(cur.filter((m) => m.pattern !== pat));
    await refresh();
  }

  return (
    <div className="section">
      <h2>Categories</h2>
      <div className="muted" style={{ marginBottom: 6 }}>
        Map a host pattern to a category. URLs that match are logged
        as the category (no URL captured).
      </div>
      <div className="row" style={{ gap: 6 }}>
        <input
          type="text"
          placeholder="example.com or *.example.com"
          value={pattern}
          onChange={(e) => setPattern(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") void add(); }}
          style={{ flex: 2 }}
        />
        <select value={slug} onChange={(e) => setSlug(e.target.value)} style={{ flex: 1 }}>
          {VOCAB.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <button onClick={() => void add()}>Add</button>
      </div>
      <div className="ignore-list" style={{ marginTop: 8 }}>
        {list.length === 0 && <div className="muted">(no mappings)</div>}
        {list.map((m) => (
          <div key={m.pattern} className="ignore-row">
            <span style={{ flex: 1 }}>{m.pattern}</span>
            <span className="tag cat">{m.category}</span>
            <button onClick={() => void remove(m.pattern)} aria-label={`Remove ${m.pattern}`}>
              ×
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
