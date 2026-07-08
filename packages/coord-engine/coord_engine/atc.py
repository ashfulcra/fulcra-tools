"""ATC — cross-subscription cap ledger folds (fulcra-agent-atc).

Pure functions over injected rows + clock (the presence/continuity-audit
pattern): accounts declared by the operator, usage shards written after
spend, folded into per-account/window headroom. A throttled shard zeroes
the window's headroom (observed ground truth beats declared caps) and
flags the account for cap calibration.
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
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


def route(accounts: dict[str, Any], models: dict[str, Any], needs: list[str],
          shards: list[dict[str, Any]], *,
          demotions: Optional[dict[str, list[str]]] = None,
          now: Optional[datetime] = None) -> dict[str, Any]:
    """Rank the models that cover ALL requested needs, each bound to its
    best-headroom eligible account, in a deterministic cost/headroom order.

    Inputs:
      * ``accounts`` — a parsed-accounts dict (``parse_accounts`` output shape:
        ``{"accounts": [...], "tiers": {...}}``).
      * ``models`` — a merged model map (``merge_models`` / ``load_default_models``
        output shape: ``{"map_version": str, "models": {id: entry}}``). Overlay
        entries reach here with unvalidated ``cost_rank``/``harnesses`` by design;
        this fold coerces them defensively and never crashes.
      * ``needs`` — requested capability tags; every one must be in ``TAXONOMY``.
      * ``shards`` — usage rows in the ``headroom`` fold's input shape.
      * ``demotions`` — ``{model_id: [needs...]}`` (Task 3 fold; default ``{}``):
        a model demoted for ANY requested need sorts below all non-demoted ones.

    Algorithm: (1) unknown need short-circuits to ``reason="unknown need: x"``;
    (2) coverage keeps models whose tags ⊇ needs; (3) each covering model binds
    to its highest-min-window-headroom account whose harnesses intersect the
    model's and whose EVERY window has headroom > 0 (throttle-zeroed windows
    exclude the account); (4) demotions push below non-demoted; (5) sort:
    non-demoted first, cost_rank DESC, headroom-% DESC, model id ASC.

    Returns ``{"candidates": [...], "map_version": str, "reason": str|None,
    "dropped_unknown_tags": [str, ...]}``. ``dropped_unknown_tags`` carries this
    fold's defensive-coercion notes (bad cost_rank / harnesses).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    demotions = demotions or {}
    map_version = models.get("map_version")

    # (1) unknown need -> bail (CLI maps this to exit 2).
    for n in needs:
        if n not in TAXONOMY:
            return {"candidates": [], "map_version": map_version,
                    "reason": f"unknown need: {n}", "dropped_unknown_tags": []}

    accounts_list = accounts.get("accounts") or []
    model_map = models.get("models") or {}
    reports: list[str] = []

    # Per-account eligibility: EVERY declared window must have headroom > 0
    # (throttle-zeroed windows fail this). An eligible account's routing score
    # is its worst (min) window headroom-%.
    hrows = headroom(accounts_list, shards, now)
    by_acct: dict[str, list[dict[str, Any]]] = {}
    for r in hrows:
        by_acct.setdefault(r["account"], []).append(r)
    acct_pct: dict[str, float] = {}
    for a in accounts_list:
        aid = a.get("id")
        declared = a.get("windows") or []
        rows = by_acct.get(aid, [])
        if not declared:
            # An account declaring ZERO windows is "uncapped" — the operator's
            # local-ollama case: no declared caps means unlimited headroom, so it
            # is ELIGIBLE at 100.0%, not ineligible. KNOWN CONSERVATIVE GAP: with
            # no windows there is nothing for a throttled shard to zero, so an
            # uncapped account can never be throttle-excluded and always routes.
            acct_pct[aid] = 100.0
        elif rows and all(r["headroom"] > 0 for r in rows):
            acct_pct[aid] = min(r["pct"] for r in rows)

    need_set = set(needs)
    candidates: list[dict[str, Any]] = []
    coverage_count = 0
    for mid, entry in model_map.items():
        tags = entry.get("tags")
        if not isinstance(tags, list):
            tags = []
        if not need_set <= set(tags):
            continue
        coverage_count += 1

        # Defensive coercion — overlay content reaches here unvalidated.
        harnesses = entry.get("harnesses")
        if not isinstance(harnesses, list) or not all(isinstance(h, str) for h in harnesses):
            reports.append(f"model {mid}: 'harnesses' not a list of strings; "
                           "treated as unroutable")
            harnesses = []
        cost_rank = entry.get("cost_rank")
        if (isinstance(cost_rank, bool) or not isinstance(cost_rank, int)
                or not (1 <= cost_rank <= 9)):
            reports.append(f"model {mid}: invalid cost_rank {cost_rank!r}; "
                           "treated as 5 (mid)")
            cost_rank = 5

        hset = set(harnesses)
        eligible = [
            (acct_pct[a["id"]], a["id"]) for a in accounts_list
            if a.get("id") in acct_pct
            and (hset & set(a["harnesses"] if isinstance(a.get("harnesses"), list) else []))
        ]
        if not eligible:
            continue  # covers needs but no account has headroom + a shared harness
        best_pct, best_acct = sorted(eligible, key=lambda t: (-t[0], t[1]))[0]

        demoted_needs = [n for n in needs if n in set(demotions.get(mid) or [])]
        candidates.append({
            "model": mid, "account": best_acct, "headroom_pct": best_pct,
            "tags": list(tags), "cost_rank": cost_rank, "demoted": demoted_needs,
        })

    # (5) deterministic sort: non-demoted first, cost_rank DESC, headroom-% DESC,
    # model id ASC.
    candidates.sort(key=lambda c: (bool(c["demoted"]), -c["cost_rank"],
                                   -c["headroom_pct"], c["model"]))

    if candidates:
        reason: Optional[str] = None
    elif coverage_count == 0:
        reason = "no model covers needs"
    else:
        reason = "no account headroom"

    return {"candidates": candidates, "map_version": map_version,
            "reason": reason, "dropped_unknown_tags": reports}


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


# Trailing-window demotion policy (frozen): a (model, task_class) pair is demoted
# when recent work goes badly. BAD outcomes are rework/escalated; the window is
# the trailing 5 outcome-bearing shards by ts; and — the strict insufficient-
# evidence rule — a pair demotes ONLY when at least DEMOTE_MIN outcome-bearing
# shards exist for it AND at least DEMOTE_MIN of the trailing window are bad
# (so 2-of-2 bad never demotes; 3-of-3 does).
_DEMOTE_WINDOW = 5
_DEMOTE_MIN = 3
_BAD_OUTCOMES = frozenset({"rework", "escalated"})


def demotions(shards: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Fold usage shards into the set of demoted ``(model, task_class)`` pairs.

    Pure over the injected rows. Only shards carrying ALL of ``model`` +
    ``task_class`` + ``outcome`` participate — v1 shards (and partial rows
    missing any of the three) are ignored silently, so pre-task-3 data flows
    through untouched. For each pair the outcome-bearing shards are ordered by
    ``ts`` ascending (stable), the trailing ``_DEMOTE_WINDOW`` (5) are taken, and
    the pair is DEMOTED iff ≥``_DEMOTE_MIN`` (3) outcome shards exist for it AND
    ≥``_DEMOTE_MIN`` of that trailing window are ``rework``/``escalated``.

    Returns ``{(model, task_class): {"bad": n, "of": m, "window": 5}}`` for the
    demoted pairs ONLY (a recovered pair — later clean shards pulling the trailing
    ratio under threshold — simply drops out of the mapping). ``of`` is the size
    of the trailing window actually inspected (``min(5, total)``); ``window`` is
    the policy window size."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for s in shards:
        model, tc, outcome = s.get("model"), s.get("task_class"), s.get("outcome")
        if not model or not tc or not outcome:
            continue  # v1 / partial shard — outside the outcome ledger
        groups.setdefault((model, tc), []).append(s)

    out: dict[tuple[str, str], dict[str, Any]] = {}
    for key, group in groups.items():
        # Stable sort by ts asc -> deterministic trailing window even for shards
        # sharing a ts (input order breaks the tie).
        ordered = sorted(group, key=lambda s: s["ts"])
        trailing = ordered[-_DEMOTE_WINDOW:]
        bad = sum(1 for s in trailing if s.get("outcome") in _BAD_OUTCOMES)
        if len(ordered) >= _DEMOTE_MIN and bad >= _DEMOTE_MIN:
            out[key] = {"bad": bad, "of": len(trailing), "window": _DEMOTE_WINDOW}
    return out


def _demotions_for_route(demo_map: dict[tuple[str, str], dict[str, Any]]
                         ) -> dict[str, list[str]]:
    """Adapt ``demotions`` output to the shape ``route`` consumes.

    ``demotions`` keys by ``(model, task_class)``; ``route`` wants
    ``{model_id: [demoted tags...]}``. task_class values are TAXONOMY tags (the
    CLI validates ``--task-class`` against the taxonomy), so a pair demoted for
    ``(model, "code")`` demotes that model for the ``code`` need. Tags are sorted
    for a deterministic list."""
    out: dict[str, list[str]] = {}
    for (model, tc) in demo_map:
        out.setdefault(model, []).append(tc)
    return {m: sorted(tags) for m, tags in out.items()}
