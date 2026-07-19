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
from typing import Any, Callable, Optional

from . import budget, config
from . import transport as _transport

DEFAULT_FORGE_SWEEP_BUDGET = 60.0


def _forge_sweep_budget(override: Optional[float] = None) -> float:
    """Whole direct feedback-sweep budget."""
    return config.env_float(
        "COORD_FORGE_SWEEP_BUDGET",
        DEFAULT_FORGE_SWEEP_BUDGET,
        override=override,
    )


_PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_SLUG = re.compile(r"[^a-z0-9]+")
_NODE = re.compile(r"[^A-Za-z0-9_.-]+")


def pr_slug(url: Optional[str]) -> Optional[str]:
    """Stable ``owner-repo-number`` slug for a PR url — the shared key for the
    watch registry doc and the feedback shard directory. Deterministic, so a
    re-run converges onto the same paths. Returns None for a non-PR url."""
    m = _PR_URL.search(str(url or ""))
    if not m:
        return None
    return _SLUG.sub("-", f"{m.group(1)}-{m.group(2)}".lower()).strip("-")


def _node_seg(node: str) -> str:
    """Path-safe filename segment for a GitHub node id (kept case-sensitive —
    node ids are). Deterministic → idempotent shard naming."""
    return _NODE.sub("-", str(node)).strip("-")


def review_artifact(fm: dict[str, Any]) -> Any:
    """The PR-bearing field of a review doc: ``of`` (the key the real
    ``review request`` verb writes the artifact under) first, falling back to
    ``artifact`` (older hand-written docs). Reading only ``artifact`` missed
    every review opened through the CLI — a pre-existing discovery bug."""
    got = fm.get("of")
    return got if got is not None else fm.get("artifact")


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
    """Run gh; None on any failure (missing binary, non-zero, timeout).

    Uses the hard-bounded runner (own process group + group SIGKILL on timeout)
    so a ``gh`` that spawns a wedged helper can't stretch the call past the
    bound — the same descendant-leak/drain hole ``transport`` closes."""
    if not shutil.which(args[0]):
        return None
    try:
        rc, out, _err = _transport.run_bounded(args, 30.0)
        return out if rc == 0 else None
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
        url = parse_pr_url(review_artifact(fm), repo=repo)
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


# --- three-surface feedback sweep -----------------------------------------
#
# The motivating failure: a formal GitHub review went unseen because a watcher
# polled conversation comments only. A single surface is not enough — a PR
# carries feedback on THREE distinct surfaces, each with its own gh call and its
# own JSON shape:
#   review  — `gh pr view <url> --json author,reviews`  (author.login / id / submittedAt)
#   inline  — `gh api repos/<o>/<r>/pulls/<n>/comments`  (user.login / node_id / created_at)
#   comment — `gh pr view <url> --json comments`         (author.login / id / createdAt)
# Each item is mirrored to an idempotent shard keyed by its GitHub node id, so a
# re-run converges. Items authored by the PR author are skipped (self-comments
# are not feedback). Per-PR gh failure is reported and the pass continues.


def _login(obj: Any) -> Optional[str]:
    return obj.get("login") if isinstance(obj, dict) else None


def _parse_reviews(raw: Optional[str]) -> tuple[Optional[str], list[dict[str, Any]]]:
    """(pr_author, [items]) from the reviews call. A COMMENTED review with an
    empty body is just the wrapper around inline comments (captured separately),
    so it is dropped; formal verdicts (APPROVED / CHANGES_REQUESTED / …) are kept
    even when their body is empty."""
    if not raw:
        return None, []
    try:
        got = json.loads(raw)
    except Exception:
        return None, []
    if not isinstance(got, dict):
        return None, []
    pr_author = _login(got.get("author"))
    out: list[dict[str, Any]] = []
    for r in got.get("reviews") or []:
        if not isinstance(r, dict):
            continue
        body = str(r.get("body") or "")
        state = str(r.get("state") or "")
        if not body and state.upper() in ("", "COMMENTED"):
            continue
        out.append({"node_id": r.get("id"), "author": _login(r.get("author")),
                    "submitted_at": r.get("submittedAt"),
                    "body": body or f"Review: {state}", "state": state})
    return pr_author, out


def _parse_inline(raw: Optional[str]) -> list[dict[str, Any]]:
    """Inline review comments from the REST array (different shape: node_id,
    user.login, created_at)."""
    if not raw:
        return []
    try:
        got = json.loads(raw)
    except Exception:
        return []
    if not isinstance(got, list):
        return []
    out: list[dict[str, Any]] = []
    for c in got:
        if not isinstance(c, dict):
            continue
        out.append({"node_id": c.get("node_id"), "author": _login(c.get("user")),
                    "submitted_at": c.get("created_at"), "body": str(c.get("body") or "")})
    return out


def _parse_comments(raw: Optional[str]) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        got = json.loads(raw)
    except Exception:
        return []
    if not isinstance(got, dict):
        return []
    out: list[dict[str, Any]] = []
    for c in got.get("comments") or []:
        if not isinstance(c, dict):
            continue
        out.append({"node_id": c.get("id"), "author": _login(c.get("author")),
                    "submitted_at": c.get("createdAt"), "body": str(c.get("body") or "")})
    return out


def _discover_prs(
    transport: Any, team: str, repo: Optional[str], *, deadline: budget.Deadline
) -> tuple[dict[str, str], dict[str, int | bool]]:
    """Discover ``{pr_url: pr_slug}`` within the shared deadline.

    PRs may come from review artifacts or the watch registry. The metadata
    reports scanned/total/skipped discovery documents so callers can distinguish
    a genuinely empty registry from a partial fold.
    """
    from . import okf
    from .transport import TransportError

    prs: dict[str, str] = {}
    scanned = total = skipped = 0
    degraded = False
    review_prefix = f"team/{team}/review/"
    try:
        if deadline.expired():
            return prs, {"scanned": 0, "total": 0, "skipped": 0, "degraded": True}
        review_entries = transport.list_dir(review_prefix)
        if deadline.expired():
            return prs, {"scanned": 0, "total": 0, "skipped": 0, "degraded": True}
        review_entries = [
            e for e in review_entries
            if not e.get("is_dir") and (e.get("name") or "").endswith(".md")
            and (e.get("name") or "") != "index.md"
        ]
        total += len(review_entries)
        for i, e in enumerate(review_entries):
            if deadline.expired():
                degraded = True
                skipped += len(review_entries) - i
                break
            n = e.get("name") or ""
            raw = transport.read(review_prefix + n)
            scanned += 1
            unreadable = raw is None
            if unreadable:
                skipped += 1
                degraded = True
            if deadline.expired():
                degraded = True
                if not unreadable:
                    skipped += 1
                skipped += len(review_entries) - i - 1
                break
            fm = okf.parse_frontmatter(raw) or {}
            url = parse_pr_url(review_artifact(fm), repo=repo)
            if url:
                prs[url] = pr_slug(url)
    except TransportError:
        degraded = True
    watch_prefix = f"team/{team}/_coord/forge/watch/"
    try:
        if deadline.expired():
            return prs, {"scanned": scanned, "total": total,
                         "skipped": skipped, "degraded": True}
        watch_entries = transport.list_dir(watch_prefix)
        if deadline.expired():
            return prs, {"scanned": scanned, "total": total,
                         "skipped": skipped, "degraded": True}
        watch_entries = [
            e for e in watch_entries
            if not e.get("is_dir") and (e.get("name") or "").endswith(".md")
        ]
        total += len(watch_entries)
        for i, e in enumerate(watch_entries):
            if deadline.expired():
                degraded = True
                skipped += len(watch_entries) - i
                break
            n = e.get("name") or ""
            raw = transport.read(watch_prefix + n)
            scanned += 1
            unreadable = raw is None
            if unreadable:
                skipped += 1
                degraded = True
            if deadline.expired():
                degraded = True
                if not unreadable:
                    skipped += 1
                skipped += len(watch_entries) - i - 1
                break
            fm = okf.parse_frontmatter(raw) or {}
            url = parse_pr_url(fm.get("url"), repo=repo)
            if url:
                prs[url] = pr_slug(url)
    except TransportError:
        degraded = True
    return prs, {"scanned": scanned, "total": total,
                 "skipped": skipped, "degraded": degraded}


def _sweep_pr(
    transport: Any, team: str, slug: str, url: str, *,
    runner: Callable[[list[str]], Optional[str]],
    deadline: Optional[budget.Deadline] = None,
) -> dict[str, Any]:
    """Sweep one PR's three surfaces → shards. Returns {items, gh_ok,
    author_unknown}. gh_ok is False only when EVERY call returned None (a gh
    failure for this PR). author_unknown is True when items were ingested while
    the PR author was unknown (reviews call failed) — self-skip could not be
    applied, so it is reported rather than silently dropped."""
    from . import okf

    m = _PR_URL.search(url)
    owner_repo, num = (m.group(1), m.group(2)) if m else (None, None)
    reviews_raw = runner(["gh", "pr", "view", url, "--json", "author,reviews"])
    if deadline is not None and deadline.expired():
        return {"items": 0, "gh_ok": False, "unavailable": [],
                "author_unknown": False, "budget_exhausted": True}
    inline_raw = runner(["gh", "api", f"repos/{owner_repo}/pulls/{num}/comments"])
    if deadline is not None and deadline.expired():
        return {"items": 0, "gh_ok": False, "unavailable": [],
                "author_unknown": False, "budget_exhausted": True}
    comments_raw = runner(["gh", "pr", "view", url, "--json", "comments"])
    if deadline is not None and deadline.expired():
        return {"items": 0, "gh_ok": False, "unavailable": [],
                "author_unknown": False, "budget_exhausted": True}
    # Per-surface availability, not just the aggregate. A partial failure (one
    # surface None while others return) previously reported clean, silently
    # recreating the motivating blind spot (a persistently failing reviews
    # surface goes unseen). Track each raw-is-None so the caller can note the
    # unavailable surface while still ingesting the healthy ones.
    raw_by_surface = (
        ("review", reviews_raw), ("inline", inline_raw), ("comment", comments_raw),
    )
    unavailable = [name for name, raw in raw_by_surface if raw is None]
    gh_ok = len(unavailable) < len(raw_by_surface)  # False only when ALL three None

    pr_author, review_items = _parse_reviews(reviews_raw)
    surfaces = (
        ("review", review_items),
        ("inline", _parse_inline(inline_raw)),
        ("comment", _parse_comments(comments_raw)),
    )
    items = 0
    for surface, lst in surfaces:
        for it in lst:
            if deadline is not None and deadline.expired():
                return {"items": items, "gh_ok": gh_ok,
                        "unavailable": unavailable,
                        "author_unknown": pr_author is None and items > 0,
                        "budget_exhausted": True}
            node = it.get("node_id")
            if not node:
                continue  # can't key it without a node id
            author = it.get("author")
            if pr_author and author and str(author).lower() == str(pr_author).lower():
                continue  # self-comment — not feedback
            body = str(it.get("body") or "")
            stem = f"{surface}-{_node_seg(node)}"
            path = f"team/{team}/_coord/forge/feedback/{slug}/{stem}.md"
            # Key ORDERING here is load-bearing: `excerpt` MUST stay last.
            # okf's block-scalar rendering has a pre-existing limitation where a
            # `---` line inside a value's body truncates the parsed value (but
            # nothing after it survives). Keeping the free-text `excerpt` — the
            # only field that can contain an embedded `---` — as the final key
            # confines that truncation to the excerpt and protects every
            # structured field above it. Do not reorder.
            fm = {
                "type": "Feedback", "source": "forge", "surface": surface,
                "author": author, "submitted_at": it.get("submitted_at"),
                "pr": url, "node_id": str(node),
                "state": it.get("state"), "excerpt": body[:400],
            }
            if transport.write(path, okf.render_frontmatter(fm) + "\n" + body + "\n"):
                items += 1
            if deadline is not None and deadline.expired():
                return {"items": items, "gh_ok": gh_ok,
                        "unavailable": unavailable,
                        "author_unknown": pr_author is None and items > 0,
                        "budget_exhausted": True}
    return {"items": items, "gh_ok": gh_ok, "unavailable": unavailable,
            "author_unknown": pr_author is None and items > 0,
            "budget_exhausted": False}


def feedback_sweep(
    transport: Any, team: str, *,
    runner: Callable[[list[str]], Optional[str]] = default_runner,
    repo: Optional[str] = None,
    deadline_seconds: Optional[float] = None,
) -> dict[str, Any]:
    """One budgeted three-surface sweep over every discovered PR.

    Returns ``{prs, items, skipped, notes, degraded}``. A per-PR failure adds a
    report line and the pass continues; exhausting the shared discovery + forge
    deadline stops new work and returns a fail-visible degraded marker. Shards
    deliberately carry no wall-clock (node-id keyed → idempotent), so no
    ``now`` is threaded through. ``notes`` records non-fatal observations.
    """
    dl = budget.Deadline.open(_forge_sweep_budget(deadline_seconds))
    prs, discovery = _discover_prs(transport, team, repo, deadline=dl)
    prs_checked = items_written = 0
    skipped: list[str] = []
    notes: list[str] = []
    degraded = bool(discovery["degraded"])
    ordered = sorted(prs.items())
    for i, (url, slug) in enumerate(ordered):
        if dl.expired():
            degraded = True
            skipped.extend(f"{s}: budget exhausted" for _u, s in ordered[i:])
            break
        prs_checked += 1
        try:
            res = _sweep_pr(transport, team, slug, url, runner=runner, deadline=dl)
        except Exception as ex:  # never-crash: isolate one PR's blast radius
            skipped.append(f"{slug}: {type(ex).__name__}")
            continue
        items_written += res["items"]
        if res.get("budget_exhausted"):
            degraded = True
            skipped.append(f"{slug}: budget exhausted")
            skipped.extend(f"{s}: budget exhausted" for _u, s in ordered[i + 1:])
            break
        if not res["gh_ok"]:
            skipped.append(f"{slug}: gh unavailable")
            continue
        # A partially-failed sweep (some surfaces healthy) still lands its
        # healthy items, but each unavailable surface is noted so the blind spot
        # is visible rather than reported clean.
        for surface in res.get("unavailable", []):
            notes.append(f"{slug}: {surface} surface unavailable")
        if res.get("author_unknown"):
            notes.append(f"{slug}: author unknown — self-skip not applied")
    marker = None
    if degraded:
        discovery_skipped = int(discovery["skipped"])
        # Count skipped discovery documents as unknown PR work units. This
        # keeps the fold internally consistent (never "1 skipped of 0") while
        # remaining conservative when a timed-out document might duplicate a
        # PR that was already discovered from the other source.
        total = len(ordered) + discovery_skipped
        marker = budget.degraded_row(
            "forge-sweep-degraded",
            prs_checked,
            total,
            len(skipped) + discovery_skipped,
        )
    return {
        "prs": prs_checked,
        "items": items_written,
        "skipped": skipped,
        "notes": notes,
        "degraded": marker,
    }
