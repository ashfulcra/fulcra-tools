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
from typing import Any, Optional


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
