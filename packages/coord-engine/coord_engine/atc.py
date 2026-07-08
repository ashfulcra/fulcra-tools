"""ATC — cross-subscription cap ledger folds (fulcra-agent-atc).

Pure functions over injected rows + clock (the presence/continuity-audit
pattern): accounts declared by the operator, usage shards written after
spend, folded into per-account/window headroom. A throttled shard zeroes
the window's headroom (observed ground truth beats declared caps) and
flags the account for cap calibration.
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta
from importlib.resources import files
from typing import Any, Optional


# Frozen capability taxonomy. A tag outside this set in the packaged default
# map is a packaging bug (raises); in operator overlay content it is dropped
# and reported.
TAXONOMY = frozenset({
    "code", "architecture", "writing", "long-context",
    "vision", "fast", "tool-use",
})


def _read_default_models_text() -> str:
    """Read the packaged default model map as text (seam for tests to inject a
    hand-broken map via monkeypatch)."""
    return (files("coord_engine").joinpath("default_models.json")
            .read_text(encoding="utf-8"))


def _validate_default_entry(mid: str, entry: Any) -> None:
    """Validate one entry of the packaged default map. Raises ValueError on any
    violation — the default map is a coord release artifact, so bad content is a
    packaging bug that must surface loudly, not be silently repaired."""
    if not isinstance(entry, dict):
        raise ValueError(f"default model {mid!r}: entry is not an object")
    tags = entry.get("tags")
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ValueError(f"default model {mid!r}: 'tags' must be a list of strings")
    unknown = [t for t in tags if t not in TAXONOMY]
    if unknown:
        raise ValueError(
            f"default model {mid!r}: unknown tag(s) {sorted(unknown)} "
            "outside the frozen taxonomy")
    cost_rank = entry.get("cost_rank")
    if (not isinstance(cost_rank, int) or isinstance(cost_rank, bool)
            or not (1 <= cost_rank <= 9)):
        raise ValueError(f"default model {mid!r}: 'cost_rank' must be an int 1-9")
    harnesses = entry.get("harnesses")
    if not isinstance(harnesses, list) or not all(isinstance(h, str) for h in harnesses):
        raise ValueError(
            f"default model {mid!r}: 'harnesses' must be a list of strings")


def load_default_models() -> dict[str, Any]:
    """Load + validate the packaged default model map.

    Returns ``{"map_version": str, "models": {id: entry}}``. Unknown top-level
    keys (``_comment``, ``_watch_items``) are ignored. Raises ValueError on any
    invalid entry — the packaged map is trusted release data."""
    raw = json.loads(_read_default_models_text())
    if not isinstance(raw, dict):
        raise ValueError("default model map is not a JSON object")
    map_version = raw.get("map_version")
    if not isinstance(map_version, str) or not map_version:
        raise ValueError("default model map missing a non-empty 'map_version'")
    models = raw.get("models")
    if not isinstance(models, dict):
        raise ValueError("default model map missing a 'models' object")
    for mid, entry in models.items():
        _validate_default_entry(mid, entry)
    return {"map_version": map_version, "models": models}


def merge_models(defaults: dict[str, Any],
                 overlay: Optional[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """Fold an operator overlay (accounts.json ``models`` key, shape ``{id: entry}``)
    over the default map. Overlay entries replace defaults per model id; new ids
    are allowed. Overlay content NEVER raises — unknown tags are dropped and
    reported, malformed entries (non-dict, or missing a valid ``tags`` list) are
    skipped and reported. Returns ``(merged_map, reports)``."""
    merged: dict[str, Any] = dict(defaults.get("models") or {})
    reports: list[str] = []
    for mid, entry in (overlay or {}).items():
        if not isinstance(entry, dict):
            reports.append(f"model {mid}: overlay entry is not an object; skipped")
            continue
        tags = entry.get("tags")
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            reports.append(
                f"model {mid}: overlay entry missing a valid 'tags' list; skipped")
            continue
        kept: list[str] = []
        for t in tags:
            if t in TAXONOMY:
                kept.append(t)
            else:
                reports.append(f"model {mid}: unknown tag '{t}' dropped")
        clean = dict(entry)
        clean["tags"] = kept
        merged[mid] = clean
    return {"map_version": defaults.get("map_version"), "models": merged}, reports


def parse_accounts(text: Optional[str]) -> dict[str, Any]:
    if not text:
        return {"accounts": [], "tiers": {}}
    try:
        d = json.loads(text)
        accounts = d.get("accounts") or []
        tiers = d.get("tiers") or {}
        if not isinstance(accounts, list) or not isinstance(tiers, dict):
            raise ValueError("accounts must be a list, tiers a dict")
        valid = [a for a in accounts
                 if isinstance(a, dict) and isinstance(a.get("id"), str) and a["id"]]
        out: dict[str, Any] = {"accounts": valid, "tiers": tiers}
        dropped = len(accounts) - len(valid)
        if dropped:
            out["error"] = (
                f"skipped {dropped} account entr{'y' if dropped == 1 else 'ies'} "
                "missing a non-empty string 'id'")
        return out
    except (ValueError, TypeError) as e:
        return {"accounts": [], "tiers": {}, "error": str(e)}


def headroom(accounts: list[dict[str, Any]], shards: list[dict[str, Any]],
             now: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for acct in accounts:
        acct_shards = [s for s in shards if s.get("account") == acct.get("id")]
        for win in acct.get("windows") or []:
            hours, cap = win.get("hours"), win.get("cap")
            if not isinstance(hours, (int, float)) or not isinstance(cap, (int, float)):
                continue
            cutoff = now - timedelta(hours=hours)
            in_win = [s for s in acct_shards if s.get("ts") and s["ts"] >= cutoff]
            used = sum(max(0, int(s.get("units") or 0)) for s in in_win)
            throttled = any(s.get("throttled") for s in in_win)
            head = 0 if throttled else max(0, int(cap) - used)
            rows.append({
                "account": acct["id"], "window_hours": int(hours), "cap": int(cap),
                "used": used, "headroom": head,
                "pct": round(head * 100.0 / cap, 1) if cap else 0.0,
                "throttled": throttled, "calibrate": throttled,
            })
    return sorted(rows, key=lambda r: (r["account"], r["window_hours"]))
