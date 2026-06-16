"""Render a compiled doc as a session-bootstrap context block. Empty output
for missing/empty docs is a contract: the injector must NEVER break a session
start (SPEC.md errors & edges)."""
from __future__ import annotations


def _safe_key(key: str) -> str:
    """Keys render on a single line; neutralize control chars so a crafted key
    (the schema accepts any non-empty string) can't forge extra preference
    lines in the bootstrap block. Printable chars pass through unchanged."""
    return "".join(
        ch if ch.isprintable() else ch.encode("unicode_escape").decode("ascii")
        for ch in key
    )


def render_block(doc: dict | None, platform: str) -> str:
    if not doc or not doc.get("keys"):
        return ""
    lines = [f"# User preferences (fulcra-prefs) — {platform}, "
             f"compiled {doc['compiled_at'][:10]}", ""]
    for key in sorted(doc["keys"]):
        e = doc["keys"][key]
        stale = " (stale)" if e.get("stale") else ""
        lines.append(f"- {_safe_key(key)}: {e['value']!r} "
                     f"[{e['weight']:+.2f}]{stale}")
    lines.append("")
    lines.append("Apply these as standing user preferences. Weights in [-1,1]; "
                 "negative = aversion. Capture new/changed preferences via "
                 "fulcra-prefs (see the fulcra-prefs skill).")
    return "\n".join(lines)
