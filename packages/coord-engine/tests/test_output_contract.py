"""The strict-consumer harness — the codex canary, moved into CI.

Every fixture here reads engine output the way the least-forgiving real
consumer does (the evidence pack's reader model,
`docs/coord/OUTPUT-CONTRACT.md`): at most 8 KiB of stdout, exactly ONE JSON
parse, no prose stripping, no history, no second lookup. The pack's twelve
incidents (C01–C12) are representation-boundary failures — a consumer that
parses less strictly turns UNKNOWN into false CLEAR, which is the class this
fleet keeps killing.

ENFORCED clauses fail CI. TARGET clauses come in two kinds (pr-633 r1):
probeable targets are EXECUTABLE tests marked ``xfail(strict=True)`` — the
moment their behavior lands, strict XPASS fails CI, forcing the same-PR
flip the contract's enforcement ladder demands; unprobeable targets sit in
``UNPROBEABLE_TARGETS``, a documentation-only registry that asserts nothing
and says so.
"""
from __future__ import annotations

import json

import pytest

from coord_engine import cli
from coord_engine_test_helpers import FakeTransport, needs_me_rows

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


def test_queue_version_warning_rides_stderr_never_stdout(capsys):
    # The C07 incident verb and the exact line: `queue --json` against a
    # legacy authority emits the VERSION WARNING. The engine's side of the
    # contract is purity per stream — the warning must be ON stderr and must
    # never share stdout. (FakeTransport carries no records API, so the read
    # itself errors after the warning; full queue-payload purity gets its
    # fixture when a records-capable stub lands with the OC2 flip.)
    t = FakeTransport()
    t.put("team/r/_coord/bus-v3/records.json",
          '{"data_type": "MomentAnnotation/x", "api_version": "v1alpha1"}')
    cli.main(["queue", "r", "--agent", "alice", "--json"], transport=t)
    captured = capsys.readouterr()
    assert "VERSION WARNING" in captured.err, "warning must be emitted, on stderr"
    assert "VERSION WARNING" not in captured.out
    assert not captured.out.strip() or strict_parse(captured.out) is not None


# --- OC10: typed degradation markers (ENFORCED vocabulary; C05, C06) -------


def test_degraded_rows_carry_type_and_coverage(capsys):
    # A transport whose listings fail must produce TYPED degradation the
    # strict parser can classify — never a clean-looking empty answer.
    t = FakeTransport()
    _seed_rows(t)
    t.fail_list = True
    cli.main(["needs-me", "r", "--agent", "alice", "--json"], transport=t)
    rows = needs_me_rows(strict_parse(capsys.readouterr().out))
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
    rows = needs_me_rows(strict_parse(capsys.readouterr().out))
    rendered = json.dumps(rows)
    assert "user:ash" in rendered
    assert "grant the permission" in rendered, (
        "unlock must render independently of blocked_on (C12; pr-625)")


# --- TARGET probes and registry -------------------------------------------
#
# Finding from pr-633 r1 (codex-reviewer): a descriptions-only registry cannot
# detect target behavior landing WITHOUT its fixture flip. So: every target
# that can be probed against today's behavior is an EXECUTABLE test marked
# xfail(strict=True) — it is expected to fail now, and the moment the
# behavior lands the xfail turns into XPASS which strict mode reports as a
# hard CI FAILURE, forcing the same-PR flip the contract demands. Targets
# that cannot be probed without infrastructure that does not exist yet stay
# in the registry below, which is explicitly documentation-only.


# --- OC2: envelope first, needs-me (ENFORCED — ladder PR 1; C03, C05, C06) --


def test_oc2_needs_me_envelope_leads_stdout(capsys):
    # LADDER PR 1 (was the strict-xfail probe): needs-me's stdout is ONE
    # envelope object — contract stamp, transport health, source, rows inside.
    # One parse decides everything; a truncated read becomes a loud parse
    # failure instead of a silently complete-looking array.
    t = FakeTransport()
    _seed_rows(t)
    rc = cli.main(["needs-me", "r", "--agent", "alice", "--json"], transport=t)
    value = strict_parse(capsys.readouterr().out)
    assert isinstance(value, dict), "envelope object must be the first value"
    assert value["contract"] == 2
    assert value["health"] in ("DATA", "CLEAR", "DEGRADED", "UNKNOWN")
    assert value["source"] in ("projection", "raw-scan"), (
        "the normative source enum, verbatim (pr-641 r1 finding 2)")
    assert isinstance(value["rows"], list)
    assert rc == (3 if value["health"] in ("DEGRADED", "UNKNOWN") else 0), (
        "rc must be a pure function of envelope health (OC3/E4)")


def test_oc2_health_rules_and_rc(capsys):
    # The r2 design's ordered health rules, driven end-to-end:
    # complete scan + rows -> DATA rc0; failing transport -> partial coverage
    # -> DEGRADED rc3 with a named basis (rows stay served as a floor).
    t = FakeTransport()
    head = "b" * 40
    cli.main(["review", "request", "r", "pr-hz", "--of", "https://x/pr/hz",
              "--reviewer", "alice", "--from", "boss", "--head", head],
             transport=t)
    capsys.readouterr()
    rc = cli.main(["needs-me", "r", "--agent", "alice", "--json"], transport=t)
    v = strict_parse(capsys.readouterr().out)
    assert (v["health"], rc) == ("DATA", 0)
    assert v["degraded"] == [] and v["basis"] == []

    t2 = FakeTransport()
    _seed_rows(t2)
    t2.fail_list = True
    rc2 = cli.main(["needs-me", "r", "--agent", "alice", "--json"],
                   transport=t2)
    v2 = strict_parse(capsys.readouterr().out)
    assert v2["health"] in ("DEGRADED", "UNKNOWN") and rc2 == 3
    assert v2["basis"], "a non-clean health must name its failure classes"


def test_oc2_envelope_source_enum_and_coverage():
    # pr-641 r1 findings 2+3: `source` is the NORMATIVE enum verbatim
    # (projection | raw-scan — never `raw`/`absent`), and coverage aggregates
    # marker scanned/total into the envelope where bounded work ran.
    env, rc = cli.class_a_envelope(
        [{"type": "review-fold-degraded", "scanned": 2, "total": 7},
         {"type": "forge-degraded", "scanned": 0, "total": 3},
         {"type": "needs-me-source", "source": "raw-scan", "reason": "stale"}],
        source_type="needs-me-source")
    assert env["source"] == "raw-scan"
    assert (env["scanned"], env["total"]) == (2, 10)
    assert (env["health"], rc) == ("DEGRADED", 3)

    env2, _rc2 = cli.class_a_envelope(
        [{"type": "needs-me-source", "source": "projection",
          "as_of": "2026-08-18T00:00:00Z"}],
        source_type="needs-me-source")
    assert env2["source"] == "projection"
    assert env2["as_of"] == "2026-08-18T00:00:00Z"
    assert "scanned" not in env2 and "total" not in env2

    env3, _rc3 = cli.class_a_envelope([], source_type="needs-me-source")
    assert env3["source"] == "raw-scan", (
        "no source row = the pre-projection raw path; the enum stays closed")


def test_oc2_inbox_envelope_leads_stdout(capsys):
    # LADDER PR 2: inbox joins contract 2 — same envelope, same health->rc law.
    t = FakeTransport()
    _seed_rows(t)
    rc = cli.main(["inbox", "r", "--agent", "alice", "--json"], transport=t)
    value = strict_parse(capsys.readouterr().out)
    assert isinstance(value, dict) and value["contract"] == 2
    assert value["source"] in ("projection", "raw-scan")
    assert isinstance(value["rows"], list)
    assert rc == (3 if value["health"] in ("DEGRADED", "UNKNOWN") else 0)


def test_oc2_inbox_unreadable_index_is_unknown_rc3(capsys):
    # The codex CRIT shape, now with the rc to match: an unreadable summaries
    # index is UNKNOWN (rows not actable), never a clean-[] exit 0.
    t = FakeTransport()
    t.put("team/r/_coord/summaries.json", "{not json")
    rc = cli.main(["inbox", "r", "--agent", "alice", "--json"], transport=t)
    v = strict_parse(capsys.readouterr().out)
    assert (v["health"], rc) == ("UNKNOWN", 3)
    assert "source-unreadable" in v["basis"]
    assert any(r.get("type") == "inbox-degraded" for r in v["rows"])


def test_oc2_asks_envelope_leads_stdout(capsys, monkeypatch):
    # LADDER PR 3: asks joins contract 2 — same envelope, same health->rc law.
    monkeypatch.setenv("FULCRA_COORD_HUMAN", "ash")
    t = FakeTransport()
    t.put("team/r/task/blocked-1.md",
          "---\ntype: Task\ntitle: Decide\nstatus: blocked\nowner: boss\n"
          "assignee: ash\nblocked_on: user:ash\n"
          "timestamp: 2026-08-15T00:00:00Z\n---\n")
    rc = cli.main(["asks", "r", "--json"], transport=t)
    value = strict_parse(capsys.readouterr().out)
    assert isinstance(value, dict) and value["contract"] == 2
    assert isinstance(value["rows"], list)
    assert rc == (3 if value["health"] in ("DEGRADED", "UNKNOWN") else 0)


def test_oc2_asks_unreadable_index_is_unknown_rc3(capsys):
    # An unreadable index must never read as "nothing waiting on the human".
    t = FakeTransport()
    t.put("team/r/_coord/summaries.json", "{not json")
    rc = cli.main(["asks", "r", "--human", "ash", "--json"], transport=t)
    v = strict_parse(capsys.readouterr().out)
    assert (v["health"], rc) == ("UNKNOWN", 3)
    assert "source-unreadable" in v["basis"]


def test_oc2_search_envelope_leads_stdout(capsys):
    # LADDER PR 4: search joins contract 2 — same envelope, same health->rc law.
    t = FakeTransport()
    _seed_rows(t)
    rc = cli.main(["search", "r", "One", "--json"], transport=t)
    value = strict_parse(capsys.readouterr().out)
    assert isinstance(value, dict) and value["contract"] == 2
    assert isinstance(value["rows"], list)
    assert rc == (3 if value["health"] in ("DEGRADED", "UNKNOWN") else 0)


def test_oc2_search_unreadable_index_is_unknown_rc3(capsys):
    # An unreadable index must never return a confident match set at rc 0.
    t = FakeTransport()
    t.put("team/r/_coord/summaries.json", "{not json")
    rc = cli.main(["search", "r", "anything", "--json"], transport=t)
    v = strict_parse(capsys.readouterr().out)
    assert (v["health"], rc) == ("UNKNOWN", 3)
    assert "source-unreadable" in v["basis"]


def test_oc2_invalid_source_token_is_not_promoted_to_provenance():
    # pr-641 r2, the remaining finding: a present source row with a token
    # OUTSIDE the closed enum is corrupt provenance — it must contribute
    # source-invalid (health UNKNOWN, rc 3) and emit an explicit null, never
    # be silently promoted to a clean raw-scan.
    env, rc = cli.class_a_envelope(
        [{"type": "needs-me-source", "source": "corrupt"}],
        source_type="needs-me-source")
    assert (env["health"], rc) == ("UNKNOWN", 3)
    assert "source-invalid" in env["basis"]
    assert env["source"] is None


def test_oc2_unclassified_degraded_marker_fails_closed():
    # pr-641 r1 finding 1: a degraded type the basis map does not know may be
    # an unreadable/invalid authority — it must become UNKNOWN (rows not
    # actable), never default into a coverage class that licenses a floor.
    env, rc = cli.class_a_envelope(
        [{"type": "future-source-degraded", "reason": "authority unreadable"}],
        source_type="needs-me-source")
    assert env["health"] == "UNKNOWN" and rc == 3
    assert "source-invalid" in env["basis"]


# --- OC5: act-on-it fields, review rows (ENFORCED — ladder flip 1; C01) ----


def test_oc5_review_row_carries_of_and_head(capsys):
    # LADDER FLIP 1 (was the strict-xfail probe): review-pending rows carry
    # the artifact URL and exact head, so the strict consumer can dispatch a
    # review with zero further lookups — C01's stall, closed.
    t = FakeTransport()
    head = "a" * 40
    cli.main(["review", "request", "r", "pr-9", "--of", "https://x/pr/9",
              "--reviewer", "alice", "--from", "boss", "--head", head],
             transport=t)
    capsys.readouterr()
    cli.main(["needs-me", "r", "--agent", "alice", "--json"], transport=t)
    rows = needs_me_rows(strict_parse(capsys.readouterr().out))
    pending = [r for r in rows if isinstance(r, dict)
               and r.get("type") == "review-pending"]
    assert pending, "reviewer must see the pending review"
    assert pending[0].get("of") == "https://x/pr/9"
    assert pending[0].get("head") == head


def test_oc5_review_row_keys_present_even_on_legacy_headless_register(capsys):
    # The register doc is the honest source: a legacy review with no `head:`
    # serves an EXPLICIT null, never an absent key — the strict consumer can
    # distinguish "register predates head-pinning" from "field dropped".
    t = FakeTransport()
    t.put("team/r/review/pr-old.md",
          "---\ntype: Review\nof: https://x/pr/old\nrequired: [alice]\n"
          "requested_by: boss\n---\nReview requested\n")
    cli.main(["needs-me", "r", "--agent", "alice", "--json"], transport=t)
    rows = needs_me_rows(strict_parse(capsys.readouterr().out))
    pending = [r for r in rows if isinstance(r, dict)
               and r.get("type") == "review-pending"]
    assert pending, "reviewer must see the pending review"
    assert "of" in pending[0] and "head" in pending[0]
    assert pending[0]["of"] == "https://x/pr/old"
    assert pending[0]["head"] is None


# Documentation-only registry: targets whose probe needs infrastructure that
# does not exist yet (yield tokens, capability stamps, cadence classes, a
# records-capable test transport). These entries assert nothing and say so.

UNPROBEABLE_TARGETS = [
    "OC3 rc-early / yield token (C04) — needs the token envelope design",
    "OC4 bounded rows / pagination (C03) — needs the continuation token",
    "OC6 canonical writer vs bus-only verdict (C02/C11) — needs a "
    "records-capable test transport",
    "OC7 capability identity (C08) — needs build/capability stamps",
    "OC8 artifact identity (C09) — needs register identity keys",
    "OC9 cadence-declared liveness (C10) — needs cadence classes in "
    "presence",
]


@pytest.mark.parametrize("reason", UNPROBEABLE_TARGETS)
def test_target_registry_documentation_only(reason):
    pytest.skip(f"TARGET registry (documentation-only, no probe yet): {reason}")
