"""Same-second verdicts order by chronology, not digest; `review status --json` exposes the exact
winning shard per reviewer.

codex-reviewer reproduced, on coord-fold-plan-r2-65761fbd round 12 (2026-09-05): an earlier APPROVE
whose digest (feb86aee) sorted after a later CHANGES (058ddb93) in the same second, so the fold kept
the APPROVE and a ship gate would have read stale approving evidence. Both reviewers: fix the typed
surface, expose the winning envelope, do not refold filenames downstream.
"""
from __future__ import annotations

import json

from coord_engine import cli, okf, review
from coord_engine_test_helpers import FakeTransport

TEAM, SLUG, HEAD, REVIEWER = "r", "pr-1-thing", "a" * 40, "codex-reviewer"
PREFIX = f"team/{TEAM}/review/{SLUG}/verdicts/"
SECOND = "2026-09-05T01:32:10"


def _open_review(t, monkeypatch):
    monkeypatch.setenv("FULCRA_COORD_AGENT", "asker")
    assert cli.main(["review", "request", TEAM, SLUG, "--of", "PR #1", "--reviewer", REVIEWER, "--head", HEAD], transport=t) == 0


def _shard(t, digest, verdict, fraction):
    name = review.verdict_filename(REVIEWER, head=HEAD, ts=f"{SECOND}Z", digest=digest)
    fm = {"type": "Verdict", "reviewer": REVIEWER, "head": HEAD, "verdict": verdict}
    if fraction is not None:
        fm["ts"] = f"{SECOND}.{fraction}Z"
    t.store[PREFIX + name] = okf.render_frontmatter(fm) + f"\n{verdict}\n"
    return name


# --- the key ---------------------------------------------------------------------------

def test_canonical_key_takes_the_second_from_the_name_and_the_fraction_only_within_it():
    assert review.canonical_sort_key(f"{SECOND}Z", f"{SECOND}.900000Z", None) == f"{SECOND}.900000Z"
    assert review.canonical_sort_key(f"{SECOND}Z", "2026-09-05T01:32:11.900000Z", None) == f"{SECOND}.000000Z"   # frontmatter cannot move across seconds
    assert review.canonical_sort_key(f"{SECOND}Z", "", None) == f"{SECOND}.000000Z"                             # legacy shard: no fraction
    assert review.canonical_sort_key(None, f"{SECOND}.5Z", None) == f"{SECOND}.500000Z"                        # plain shard: frontmatter is all there is
    assert review.canonical_sort_key(None, None, f"{SECOND}Z") == f"{SECOND}.000000Z"                          # mtime fallback
    assert review.canonical_sort_key(None, None, None) == ""


def test_legacy_and_new_shards_compare_in_one_format():
    legacy = review.canonical_sort_key(f"{SECOND}Z", "", None)
    newer = review.canonical_sort_key(f"{SECOND}Z", f"{SECOND}.000001Z", None)
    assert legacy < newer and len(legacy) == len(newer)


# --- the fold -----------------------------------------------------------------------------

def _row(digest, verdict, fraction):
    name = review.verdict_filename(REVIEWER, head=HEAD, ts=f"{SECOND}Z", digest=digest)
    fm_ts = f"{SECOND}.{fraction}Z" if fraction is not None else ""
    return {"reviewer": REVIEWER, "name": name, "verdict": verdict,
            "sort_key": review.canonical_sort_key(f"{SECOND}Z", fm_ts, None)}


def test_same_second_reverse_digest_the_later_changes_wins():
    earlier_approve = _row("feb86aee", "approve", "100000")   # larger digest, EARLIER
    later_changes = _row("058ddb93", "changes", "900000")     # smaller digest, LATER
    kept, folded = review.fold_newest_per_reviewer([earlier_approve, later_changes])
    assert kept == [later_changes] and folded == 1


def test_without_fractions_the_tie_still_breaks_on_the_name_and_the_plan_says_so():
    """The legacy caveat, pinned: two same-second shards with NO frontmatter ts order by name."""
    a = _row("feb86aee", "approve", None)
    b = _row("058ddb93", "changes", None)
    kept, _ = review.fold_newest_per_reviewer([a, b])
    assert kept == [a]


# --- the typed surface --------------------------------------------------------------------

def test_review_status_json_exposes_the_exact_winning_shard(monkeypatch, capsys):
    t = FakeTransport()
    _open_review(t, monkeypatch)
    _shard(t, "feb86aee", "approve", "100000")
    later = _shard(t, "058ddb93", "changes", "900000")
    assert cli.main(["review", "status", TEAM, SLUG, "--json"], transport=t) == 0
    out = json.loads([l for l in capsys.readouterr().out.splitlines() if l.startswith("{")][-1])
    assert out["state"] == "CHANGES"
    assert out["winning"][REVIEWER]["name"] == later
    assert out["winning"][REVIEWER]["verdict"] == "changes"
    assert out["winning"][REVIEWER]["sort_key"] == f"{SECOND}.900000Z"


def test_the_verb_writes_a_microsecond_ts_in_frontmatter(monkeypatch):
    t = FakeTransport()
    _open_review(t, monkeypatch)
    monkeypatch.setenv("FULCRA_COORD_AGENT", REVIEWER)
    assert cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD, "--verdict", "approve", "--note", "tree: " + "1" * 40], transport=t) == 0
    shards = sorted(k for k in t.store if k.startswith(PREFIX + f"{HEAD}--{REVIEWER}--"))
    assert shards
    fm = okf.parse_frontmatter(t.store[shards[-1]])
    name_second = shards[-1].rsplit("--", 1)[1][:19]
    assert str(fm["ts"])[:19] == name_second and str(fm["ts"]).endswith("Z")
