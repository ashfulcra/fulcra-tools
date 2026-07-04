"""Forge mirror — GitHub signals folded into review evidence (fulcra-agent-forge).

For each review doc whose ``artifact`` is a GitHub PR URL, ask ``gh`` for the
PR's state and mirror it onto the team store: an idempotent evidence shard per
state (``_coord/evidence/<slug>/state-<STATE>.md``) and, on merge, an automatic
``verdicts/forge.md`` approval — so ``review status`` reflects reality even when
no human reviewer files a verdict. Degrades to a clear no-op without ``gh``.

The ``runner`` is injectable (tests fake it; prod shells out to ``gh``).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any, Callable, Optional

_PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")


def parse_pr_url(artifact: Optional[str], *, repo: Optional[str] = None) -> Optional[str]:
    """Return the canonical PR URL, or None if the artifact isn't a GitHub PR.
    With ``repo`` (owner/name), URLs for any OTHER repo return None — the
    allowlist that stops a mis-set artifact from driving a wrong-repo
    auto-approval (review finding)."""
    m = _PR_URL.search(str(artifact or ""))
    if not m:
        return None
    if repo and m.group(1).lower() != repo.lower():
        return None
    return f"https://github.com/{m.group(1)}/pull/{m.group(2)}"


def default_runner(args: list[str]) -> Optional[str]:
    """Run gh; None on any failure (missing binary, non-zero, timeout)."""
    if not shutil.which(args[0]):
        return None
    try:
        cp = subprocess.run(args, capture_output=True, text=True, timeout=30)
        return cp.stdout if cp.returncode == 0 else None
    except Exception:
        return None


def pr_state(runner: Callable[[list[str]], Optional[str]], url: str) -> Optional[dict[str, Any]]:
    raw = runner(["gh", "pr", "view", url, "--json", "state,mergedAt,reviewDecision"])
    if not raw:
        return None
    try:
        got = json.loads(raw)
        return got if isinstance(got, dict) else None
    except Exception:
        return None


def mirror(
    transport: Any,
    team: str,
    *,
    now: str,
    runner: Callable[[list[str]], Optional[str]] = default_runner,
    repo: Optional[str] = None,
) -> dict[str, Any]:
    """One mirror pass. Returns {checked, mirrored, verdicts, skipped}."""
    from . import okf
    from .transport import TransportError

    checked = mirrored = verdicts = 0
    prefix = f"team/{team}/review/"
    try:
        entries = transport.list_dir(prefix)
    except TransportError:
        return {"checked": 0, "mirrored": 0, "verdicts": 0, "error": "review dir unreadable"}
    for e in entries:
        n = e.get("name") or ""
        if e.get("is_dir") or not n.endswith(".md") or n == "index.md":
            continue
        slug = n[:-3]
        fm = okf.parse_frontmatter(transport.read(prefix + n)) or {}
        url = parse_pr_url(fm.get("artifact"), repo=repo)
        if not url:
            continue
        checked += 1
        state = pr_state(runner, url)
        if state is None:
            continue  # gh unavailable or PR unreadable — leave untouched
        label = "MERGED" if state.get("mergedAt") else str(state.get("state") or "UNKNOWN").upper()
        shard = f"team/{team}/_coord/evidence/{slug}/state-{label}.md"
        if transport.read(shard) is None:  # idempotent per state transition
            if transport.write(shard, okf.render_frontmatter({
                "type": "Evidence", "source": "forge", "state": label,
                "artifact": url, "timestamp": now,
                "review_decision": state.get("reviewDecision"),
            }) + f"\nPR is {label} as of {now}.\n"):
                mirrored += 1  # count only landed writes (failed ones retry next pass)
        if label == "MERGED":
            vpath = f"team/{team}/review/{slug}/verdicts/forge.md"
            if transport.read(vpath) is None:
                if transport.write(vpath, okf.render_frontmatter({
                    "type": "Verdict", "reviewer": "forge", "verdict": "approve",
                    "timestamp": now,
                }) + f"\nAuto-approved: PR merged on the forge ({url}).\n"):
                    verdicts += 1
    return {"checked": checked, "mirrored": mirrored, "verdicts": verdicts}
