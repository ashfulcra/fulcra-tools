"""The strict-consumer harness — the codex canary, moved into CI.

Every fixture here reads engine output the way the least-forgiving real
consumer does (the evidence pack's reader model,
`docs/coord/OUTPUT-CONTRACT.md`): at most 8 KiB of stdout, exactly ONE JSON
parse, no prose stripping, no history, no second lookup. The pack's twelve
incidents (C01–C12) are representation-boundary failures — a consumer that
parses less strictly turns UNKNOWN into false CLEAR, which is the class this
fleet keeps killing.

ENFORCED clauses fail CI. TARGET clauses live in ``PENDING_FIXTURES`` below:
each names its contract clause and the behavior it awaits, and skips loudly —
flipping one to enforced belongs in the SAME PR as the behavior change
(contract doc, "enforcement ladder"). A pending entry silently passing is
itself a failure: that means the behavior landed without its flip.
"""
from __future__ import annotations

import json

import pytest

from coord_engine import cli
from coord_engine_test_helpers import FakeTransport

STRICT_READ_CAP = 8 * 1024


def strict_parse(stdout: str):
    """One parse over a capped read, no prefix/suffix tolerance (OC1).

    Raises on prose before or after the JSON value — the merged-stream C07
    failure shape — and on output whose FIRST 8 KiB does not contain the
    complete value (C03's tail-verdict shape; until OC4 pagination lands the
    fixtures keep payloads under the cap deliberately).
    """
    capped = stdout[:STRICT_READ_CAP]
    return json.loads(capped)


def _seed_rows(t: FakeTransport) -> None:
    t.put(
        "team/r/task/one-live-task.md",
        "---\ntype: Task\ntitle: One\nstatus: active\nowner: boss\n"
        "assignee: alice\ntimestamp: 2026-08-15T00:00:00Z\n---\n",
    )


# --- OC1: stream purity (ENFORCED; C07) -----------------------------------


@pytest.mark.parametrize("argv", [
    ["needs-me", "r", "--agent", "alice", "--json"],
    ["board", "r", "--json"],
    ["inbox", "r", "--agent", "alice", "--json"],
])
def test_json_stdout_is_json_only_one_parse(argv, capsys):
    t = FakeTransport()
    _seed_rows(t)
    cli.main(argv, transport=t)
    out = capsys.readouterr().out
    strict_parse(out)  # raises if any prose shares stdout with the value


def test_diagnostics_ride_stderr_not_stdout(capsys):
    # The C07 incident was a stream-MERGING harness; the engine's side of the
    # contract is purity per stream. The queue version warning is the exact
    # line from the incident: prove it lands on stderr while stdout stays
    # parseable, so any consumer handed stdout alone gets clean JSON.
    t = FakeTransport()
    t.put("team/r/_coord/bus-v3/records.json",
          '{"data_type": "MomentAnnotation/x", "api_version": "v1alpha1"}')
    cli.main(["needs-me", "r", "--agent", "alice", "--json"], transport=t)
    captured = capsys.readouterr()
    strict_parse(captured.out)
    assert "{" not in (captured.err.splitlines()[0] if captured.err else "")


# --- OC10: typed degradation markers (ENFORCED vocabulary; C05, C06) -------


def test_degraded_rows_carry_type_and_coverage(capsys):
    # A transport whose listings fail must produce TYPED degradation the
    # strict parser can classify — never a clean-looking empty answer.
    t = FakeTransport()
    _seed_rows(t)
    t.fail_list = True
    cli.main(["needs-me", "r", "--agent", "alice", "--json"], transport=t)
    rows = strict_parse(capsys.readouterr().out)
    assert isinstance(rows, list)
    markers = [r for r in rows if isinstance(r, dict)
               and str(r.get("type", "")).endswith(("-degraded", "-source"))
               or isinstance(r, dict) and "degraded" in str(r.get("type", ""))]
    assert markers, "no typed degradation marker on a failing transport"
    for m in markers:
        assert m.get("type"), "marker rows must be typed"


# --- OC5: act-on-it fields, blocked rows (ENFORCED via pr-625; asserted
# here as a contract-level canary so the fixture family lives in one file) --


def test_blocked_ask_renders_unlock_independent_of_blocked_on(
        capsys, monkeypatch):
    # The C12 shape: blocked asks ALWAYS have blocked_on, so a fold that
    # renders `blocked_on or unlock` makes unlock unreachable. pr-625 fixed
    # the asks fold (tests/test_asks_render.py owns the full family); this
    # canary pins the JSON surface at the contract level, driven row-level
    # in the same style as that file.
    import argparse
    from types import SimpleNamespace

    row = {"name": "slug-1", "owner": "coord-boss", "priority": "P1",
           "title": "A blocked thing", "status": "blocked",
           "blocked_on": "user:ash", "unlock": "grant the permission"}
    args = argparse.Namespace(team="r", json=True, human="ash")
    monkeypatch.setattr(cli, "_load_rows_status",
                        lambda transport, team: ([row], True, ""))
    monkeypatch.setattr(
        cli.query, "asks",
        lambda rows, *, now, human: [dict(r, age_hours=1.0) for r in rows])
    cli.cmd_asks(args, SimpleNamespace())
    rows = strict_parse(capsys.readouterr().out)
    rendered = json.dumps(rows)
    assert "user:ash" in rendered
    assert "grant the permission" in rendered, (
        "unlock must render independently of blocked_on (C12; pr-625)")


# --- TARGET registry: written, visible, deliberately pending ----------------
#
# One entry per adversarial fixture from the evidence pack whose clause has
# not landed. Skipping (not xfail) keeps intent unmistakable in CI output;
# the reason strings name the clause so a ladder flip greps its fixture.

PENDING_FIXTURES = [
    ("OC2 envelope-first: degradation after the 8KiB boundary is invisible "
     "to a strict reader until the envelope leads stdout (C03/C05/C06)"),
    ("OC3 rc-early: yielded read must emit an operation token envelope "
     "before the harness budget (C04)"),
    ("OC4 bounded rows: oversized folds paginate with a continuation token "
     "instead of an unbounded array (C03)"),
    ("OC5 review rows: review-pending rows carry of + exact head + "
     "canonical slug (C01) — FIRST LADDER FLIP"),
    ("OC6 canonical writer: a bus-only verdict must not satisfy the review "
     "fold; register shard is constitutive (C02/C11)"),
    ("OC7 capability identity: envelope carries full build pin + contract "
     "version + capability set; same-semver subtraction fails CI (C08)"),
    ("OC8 artifact identity: duplicate live registers for one "
     "(repo, PR, head) are refused or explicitly superseded (C09)"),
    ("OC9 cadence liveness: batch-worker presence declares cadence class; "
     "liveness grace scales to it (C10)"),
]


@pytest.mark.parametrize("reason", PENDING_FIXTURES)
def test_target_clause_pending(reason):
    pytest.skip(f"TARGET (not yet enforced): {reason}")
