"""--json purity (Phase-1 part 3): under --json, stdout is ALWAYS one parseable
JSON value — no prose, ever. Every degraded/notice line becomes a JSON row or a
reserved key, or goes to stderr. A consumer piping `--json` must never have a
result corrupted by a human-facing notice.

Induced across every fold verb (status, board, needs-me, inbox, briefing, threads)
by forcing each into a degraded path. `threads` is the known leak (it streamed
JSON-Lines — N values, not one); the rest are guarded so the new blocked-on-human
section cannot regress them.
"""

import json

from coord_engine import cli, reconcile
from coord_engine_test_helpers import FakeTransport


def _one_json_value(out):
    """Parse stdout as exactly ONE JSON value; raise if it is JSON-Lines / prose."""
    return json.loads(out)  # json.loads rejects trailing data (JSONL, prose)


def _corrupt_index(t):
    """summaries.json present but UNPARSEABLE -> `_load_rows_status` ok=False, the
    read-degraded path, on every aggregate-backed read."""
    t.put("team/r/_coord/summaries.json", "{ this is not json")


def test_status_json_one_value_under_read_degraded(capsys):
    t = FakeTransport(); _corrupt_index(t)
    capsys.readouterr()
    assert cli.main(["status", "r", "--json"], transport=t) == 0
    v = _one_json_value(capsys.readouterr().out)
    assert "read-degraded" in v


def test_board_json_one_value_under_read_degraded(capsys):
    t = FakeTransport(); _corrupt_index(t)
    capsys.readouterr()
    assert cli.main(["board", "r", "--json"], transport=t) == 0
    v = _one_json_value(capsys.readouterr().out)
    assert "read-degraded" in v


def test_needs_me_json_one_value_under_read_degraded(capsys):
    # Contract 2: an unreadable task authority is UNKNOWN health -> rc 3
    # (OC3/E4; rc was previously forge-only). Still exactly ONE JSON value.
    t = FakeTransport(); _corrupt_index(t)
    capsys.readouterr()
    assert cli.main(["needs-me", "r", "--agent", "alice", "--json"], transport=t) == 3
    v = _one_json_value(capsys.readouterr().out)
    assert v["health"] == "UNKNOWN"
    assert any(r.get("type") == "read-degraded" for r in v["rows"])


def test_inbox_json_one_value_under_read_degraded(capsys):
    t = FakeTransport(); _corrupt_index(t)
    capsys.readouterr()
    assert cli.main(["inbox", "r", "--agent", "alice", "--json"], transport=t) == 0
    v = _one_json_value(capsys.readouterr().out)
    assert any(r.get("type") == "inbox-degraded" for r in v)


def test_briefing_json_one_value_under_read_degraded(capsys):
    t = FakeTransport(); _corrupt_index(t)
    capsys.readouterr()
    assert cli.main(["briefing", "r", "--agent", "alice", "--json"], transport=t) == 0
    v = _one_json_value(capsys.readouterr().out)
    assert "read_degraded" in v


def test_needs_me_json_pure_under_review_budget_pressure(capsys, monkeypatch):
    # The brief's named leak: needs-me under budget pressure must stay one value.
    monkeypatch.setenv("COORD_REVIEW_FOLD_BUDGET", "0.0001")
    t = FakeTransport()
    t.put("team/r/task/a.md",
          "---\ntype: Task\ntitle: A\nstatus: active\nassignee: alice\n"
          "timestamp: 2026-07-01T00:00:00Z\n---\nb")
    for i in range(4):
        t.put(f"team/r/review/pr{i}.md",
              "---\ntype: Review\nrequired: [alice]\n---\nr")
        t.put(f"team/r/review/pr{i}/verdicts/bob.md",
              "---\ntype: Verdict\nverdict: approve\n---\nv")
    reconcile.reconcile(t, "r", now="2026-07-20T00:00:00Z", today="2026-07-20", host="h")
    capsys.readouterr()
    assert cli.main(["needs-me", "r", "--agent", "alice", "--json"], transport=t) == 3
    v = _one_json_value(capsys.readouterr().out)
    # Contract 2: still ONE value — now the envelope object; budget pressure is
    # DEGRADED health and rc follows it (OC3/E4).
    assert isinstance(v, dict) and v["health"] == "DEGRADED"
    assert isinstance(v["rows"], list)


def test_threads_json_is_one_value(capsys):
    # KNOWN leak (red today): `threads --json` streamed one JSON object PER LINE, so
    # with 2+ dropped threads stdout was N values and `json.loads(out)` raised.
    t = FakeTransport()
    for name in ("a", "b"):
        t.put(f"team/r/task/{name}.md",
              f"---\ntype: Task\ntitle: {name}\nstatus: active\nassignee: ash\n"
              f"timestamp: 2020-01-01T00:00:00Z\ntags: []\n---\nb")
    reconcile.reconcile(t, "r", now="2026-07-20T00:00:00Z", today="2026-07-20", host="h")
    capsys.readouterr()
    assert cli.main(["threads", "r", "--for", "ash", "--json"], transport=t) == 0
    out = capsys.readouterr().out
    v = _one_json_value(out)  # must not raise
    assert isinstance(v, list) and len(v) >= 2


def test_threads_json_degraded_marker_is_in_the_value(capsys):
    # The threads degraded marker must ride INSIDE the single value, not as a
    # trailing extra JSON document. An intent candidate whose task doc read fails
    # leaves the intent window UNKNOWN -> the threads source degrades visibly.
    class T(FakeTransport):
        armed = False

        def read(self, path):
            if path == "team/r/task/a.md" and self.armed:
                return None  # intent window UNKNOWN -> ok=False
            return super().read(path)

    t = T()
    # Fresh timestamp so bounded retention does not archive it before the fold runs.
    t.put("team/r/task/a.md",
          "---\ntype: Task\ntitle: A\nstatus: proposed\nassignee: ash\n"
          "timestamp: 2026-07-20T00:00:00Z\ntags: [\"intent:ash\"]\n---\nb")
    reconcile.reconcile(t, "r", now="2026-07-20T00:00:00Z", today="2026-07-20", host="h")
    t.armed = True
    capsys.readouterr()
    assert cli.main(["threads", "r", "--for", "ash", "--json"], transport=t) == 0
    out = capsys.readouterr().out
    v = _one_json_value(out)
    assert any(o.get("type") == "threads-degraded" for o in v), v


# --- close the class, discovered from the PARSER ----------------------------
# coord-boss filed the leak 2026-07-21 against `threads` and `needs-me`. Both
# were fixed and pinned. My first attempt at "close the class" hand-listed
# thirteen verbs and claimed completeness; codex-reviewer rejected it because the
# parser defines TWENTY-EIGHT `--json` paths. Widening the sweep to all of them
# immediately found a LIVE leak the thirteen missed: `headroom --json` printed
# prose on its no-accounts early return, which predates the `--json` branch.
#
# So the registry is no longer hand-maintained as a claim. It is checked AGAINST
# PARSER DISCOVERY: a `--json` path added tomorrow fails this suite until it is
# either pinned or explicitly exempted with a reason.

import argparse  # noqa: E402

import pytest  # noqa: E402

#: path -> argv that reaches it. Every one of these must print exactly one JSON
#: value AND print something: a verb regressing to silence would otherwise pass
#: a "parses if non-empty" check while emitting no result at all.
_JSON_PINNED = {
    "agents": ["agents", "r"],
    "annotate status": ["annotate", "status", "r"],
    "asks": ["asks", "r"],
    "atc report": ["atc", "report", "r"],
    "board": ["board", "r"],
    "briefing": ["briefing", "r", "--agent", "alice"],
    "continuity resume": ["continuity", "resume", "r", "alice"],
    "digest": ["digest", "r"],
    "engagement sweep": ["engagement", "sweep", "r"],
    "headroom": ["headroom", "r"],
    "health": ["health", "r"],
    "inbox": ["inbox", "r", "--agent", "alice"],
    "needs-me": ["needs-me", "r", "--agent", "alice"],
    "obligations": ["obligations", "r", "--agent", "alice"],
    "presence show": ["presence", "show", "r"],
    "queue": ["queue", "r", "--agent", "alice"],
    "roles status": ["roles", "status", "r", "coord-maintainer"],
    "router shadow report": ["router", "shadow", "report", "r"],
    "search": ["search", "r", "a"],
    "stash list": ["stash", "list", "r"],
    "status": ["status", "r"],
    "threads": ["threads", "r", "--for", "alice"],
    # codex-reviewer, PR 568 r2: these six were exempt in r1 and should not have
    # been. Every "unsafe / emits nothing" justification I wrote was really my own
    # WRONG INVOCATION — `route` takes --needs not --task, `review status` needs a
    # slug that exists, and the three mutating paths all have dry-run/once/shadow
    # modes built for exactly this. Called correctly, all six print one JSON value.
    #
    # MUTATION, measured rather than assumed: `bus-v3 migrate --dry-run` and
    # `router execute --once --dry-run` write NOTHING. `router run --once
    # --shadow` DOES write three paths (a router cursor plus shadow marks and a
    # shadow decision) — it suppresses live delivery, not persistence. That is
    # safe here ONLY because every fixture is a per-test in-memory FakeTransport;
    # it would advance a real cursor against a real store. Do not lift these argv
    # into a smoke script that points at the live bus.
    "bus-v3 migrate": ["bus-v3", "migrate", "r", "--dry-run"],
    "router execute": ["router", "execute", "r", "--once", "--dry-run"],
    "router run": ["router", "run", "r", "--once", "--shadow"],
    "engagement gate": ["engagement", "gate", "r"],
    "review status": ["review", "status", "r", "pr1"],
    "route": ["route", "r", "--needs", "review"],
}

#: Paths deliberately NOT smoke-run, each with the reason. EMPTY BY DESIGN today:
#: every parser-discovered `--json` path is pinned. The hatch stays because a
#: future path may genuinely contract no output — but an entry here is a claim
#: someone must justify in review, not a place to park a path that was merely
#: awkward to invoke. That is what it degenerated into in r1.
_JSON_EXEMPT: dict[str, str] = {}


def _json_paths_from_parser():
    """Every `--json` path the parser actually defines, discovered by walking it."""
    found = []

    def walk(parser, path=()):
        has_json = any("--json" in (a.option_strings or []) for a in parser._actions)
        subs = [a for a in parser._actions
                if isinstance(a, argparse._SubParsersAction)]
        if has_json:
            found.append(" ".join(path))
        for sub in subs:
            for name, sp in sub.choices.items():
                walk(sp, path + (name,))

    walk(cli.build_parser())
    return {p for p in found if p}


def test_every_json_path_in_the_parser_is_pinned_or_explicitly_exempt():
    """The completeness gate. A new `--json` path fails here until represented —
    which is what makes the AGENTS.md rule enforceable rather than aspirational."""
    discovered = _json_paths_from_parser()
    covered = set(_JSON_PINNED) | set(_JSON_EXEMPT)
    assert discovered, "parser discovery found nothing — the walk is broken"
    missing = discovered - covered
    assert not missing, (
        f"{len(missing)} --json path(s) are neither pinned nor exempt: "
        f"{sorted(missing)}. Add them to _JSON_PINNED, or to _JSON_EXEMPT with a "
        f"reason.")
    stale = covered - discovered
    assert not stale, f"registry names paths the parser no longer has: {sorted(stale)}"


_ALL_BUDGETS = (
    "COORD_BRIEFING_BUDGET", "COORD_FORGE_SWEEP_BUDGET", "COORD_OBLIGATION_BUDGET",
    "COORD_OVERLAY_BUDGET", "COORD_PROJECTION_BUILD_BUDGET",
    "COORD_REVIEW_FOLD_BUDGET", "COORD_ROLE_FOLD_BUDGET",
    "COORD_THREADS_FOLD_BUDGET",
)


def _seeded_store():
    """Enough reviews/roles/intents that a squeezed budget actually truncates a
    fold — an empty store cannot produce the markers under test."""
    t = FakeTransport()
    t.put("team/r/task/a.md",
          "---\ntype: Task\ntitle: A\nstatus: active\nassignee: alice\n"
          "timestamp: 2026-07-20T00:00:00Z\ntags: [\"intent:alice\"]\n---\nb")
    for i in range(6):
        t.put(f"team/r/review/pr{i}.md", "---\ntype: Review\nrequired: [alice]\n---\nr")
        t.put(f"team/r/review/pr{i}/verdicts/bob.md",
              "---\ntype: Verdict\nverdict: approve\n---\nv")
    for i in range(4):
        t.put(f"team/r/roles/role{i}.md", "---\ntype: Role\nholder: alice\n---\nx")
    reconcile.reconcile(t, "r", now="2026-07-20T00:00:00Z", today="2026-07-20",
                        host="h")
    return t


def _assert_one_json_value_and_non_empty(out, path):
    # NOT "skip when empty": a verb regressing to no payload at all would sail
    # through that, while the contract under test is exactly one JSON value.
    assert out.strip(), (
        f"`{path} --json` printed NOTHING; a JSON result is contracted here. If "
        f"this path legitimately emits nothing, move it to _JSON_EXEMPT with a "
        f"reason rather than letting silence pass.")
    _one_json_value(out)


@pytest.mark.parametrize("path", sorted(_JSON_PINNED), ids=sorted(_JSON_PINNED))
def test_pinned_json_path_under_budget_pressure(path, capsys, monkeypatch):
    for var in _ALL_BUDGETS:
        monkeypatch.setenv(var, "0.0001")
    t = _seeded_store()
    capsys.readouterr()
    try:
        cli.main(_JSON_PINNED[path] + ["--json"], transport=t)
    except SystemExit:
        pass
    _assert_one_json_value_and_non_empty(capsys.readouterr().out, path)


@pytest.mark.parametrize("path", sorted(_JSON_PINNED), ids=sorted(_JSON_PINNED))
def test_pinned_json_path_under_read_degraded(path, capsys):
    # Seeded THEN corrupted: some pinned paths need a real slug/review to have
    # anything to say, and an empty store would make them vacuously "silent"
    # rather than exercising the degraded read.
    t = _seeded_store()
    _corrupt_index(t)
    capsys.readouterr()
    try:
        cli.main(_JSON_PINNED[path] + ["--json"], transport=t)
    except SystemExit:
        pass
    _assert_one_json_value_and_non_empty(capsys.readouterr().out, path)
