"""Deterministic group-decision solver. Pure function; canonical ordering
(participants and options sorted) and a lexicographic tie-breaker make the
ranking reproducible. The trace is the product, not a debug aid — it is the
human-readable 'why' the spec promises."""
from __future__ import annotations

POLICIES = ("weighted-sum", "hard-veto")
VETO_THRESHOLD_DEFAULT = -0.5


def solve(options: list[dict], participant_docs: dict[str, dict],
          policy: str = "weighted-sum",
          veto_threshold: float = VETO_THRESHOLD_DEFAULT) -> dict:
    if policy not in POLICIES:
        raise ValueError(f"policy must be one of {POLICIES}, got {policy!r}")
    trace: list[str] = []
    participants = sorted(participant_docs)          # canonical ordering
    opts = sorted(options, key=lambda o: o["id"])
    scored, vetoed = [], []
    for opt in opts:
        total = 0.0
        veto = None
        for who in participants:
            keys = participant_docs[who].get("keys", {})
            for k in sorted(opt["keys"]):  # canonical: float add is non-associative
                if k not in keys:
                    continue
                w = keys[k]["weight"]
                total += w
                trace.append(f"{opt['id']}: {who} {k} weight {w:+.6f}")
                if policy == "hard-veto" and w < veto_threshold and veto is None:
                    veto = (who, k, w)
        if veto:
            who, k, w = veto
            vetoed.append(opt["id"])
            trace.append(f"{opt['id']}: veto by {who} on {k} ({w:+.6f} < {veto_threshold})")
        else:
            scored.append({"id": opt["id"], "score": round(total, 6)})
            trace.append(f"{opt['id']}: total {total:+.6f}")
    ranked = sorted(scored, key=lambda o: (-o["score"], o["id"]))
    trace.append("ranking: " + " > ".join(o["id"] for o in ranked)
                 + (f" | vetoed: {', '.join(vetoed)}" if vetoed else ""))
    return {"ranked": ranked, "vetoed": vetoed, "trace": trace}
