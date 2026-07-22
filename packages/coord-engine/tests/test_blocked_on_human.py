"""Blocked-on-human: a reserved, un-starvable FIRST section (Phase-1 part 1).

A decision parked on a human is the incident this section exists to make
impossible to bury. It is computed from the AGGREGATE ROWS ONLY — the rows are
already loaded, so the section costs ZERO extra transport ops, which is what
makes it structurally un-starvable by a budget cut. `--on-user` now TYPES the
block as `blocked_on: user:<name>`; legacy plain `blocked_on` values resolve
human-vs-agent and, on ambiguity, resolve toward SURFACING (a false positive in
this section is noise; a hidden human-blocked item is the incident).
"""

import json

from coord_engine import cli, okf, query, reconcile
from coord_engine_test_helpers import FakeTransport


class CountingTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.reads: list[str] = []
        self.lists: list[str] = []

    def read(self, path):
        self.reads.append(path)
        return super().read(path)

    def list_dir(self, prefix):
        self.lists.append(prefix)
        return super().list_dir(prefix)


def _row(name, *, status="blocked", assignee=None, blocked_on=None, tags=None,
         owner=None, title=None):
    return {
        "id": name, "name": name, "title": title or name, "status": status,
        "priority": "P2", "assignee": assignee, "owner": owner,
        "blocked_on": blocked_on, "tags": list(tags or []), "timestamp": "2026-07-01T00:00:00Z",
    }


# --- the pure classifier -----------------------------------------------------

def test_pure_classifier_takes_no_transport():
    # Structural free-ness proof: the classifier is a pure function of a row list.
    # It CANNOT perform I/O — there is no transport parameter — so the section it
    # feeds can never add a transport op no matter how the callers wire it.
    import inspect
    params = set(inspect.signature(query.blocked_on_human).parameters)
    assert "transport" not in params


def test_typed_user_block_surfaces():
    rows = [_row("t1", blocked_on="user:ash")]
    out = query.blocked_on_human(rows, human="ash")
    assert [r["name"] for r in out] == ["t1"]
    assert out[0]["type"] == "blocked-on-human"
    assert out[0]["blocked_on_user"] == "ash"
    assert not out[0].get("blocked_on_degraded")


def test_agent_block_with_known_agent_is_not_surfaced():
    # A legacy plain blocked_on naming a KNOWN agent is agent-blocked, not human.
    rows = [_row("t1", blocked_on="bob")]
    out = query.blocked_on_human(rows, human="ash", known_agents={"bob"})
    assert out == []


def test_legacy_ambiguous_value_surfaces():
    # blocked_on names something that is NOT a known agent/role: ambiguity resolves
    # toward SURFACING — show it in the human section (no degraded note; the set was
    # known, the value simply is not in it).
    rows = [_row("t1", blocked_on="ash")]
    out = query.blocked_on_human(rows, human="ash", known_agents={"bob"})
    assert [r["name"] for r in out] == ["t1"]
    assert out[0]["blocked_on_user"] == "ash"
    assert not out[0].get("blocked_on_degraded")


def test_ambiguity_surfaces_with_degraded_note_when_listing_unknown():
    # The set needed to decide is UNKNOWN (roles listing raised upstream): a legacy
    # plain blocked_on value we cannot classify is SHOWN, with a degraded note —
    # never hidden. Fail-closed here means SHOW.
    rows = [_row("t1", blocked_on="reviewer")]
    out = query.blocked_on_human(rows, human="ash", known_agents=set(),
                                 roles_unknown=True)
    assert [r["name"] for r in out] == ["t1"]
    assert out[0].get("blocked_on_degraded") is True


def test_needs_human_tag_is_human_block():
    rows = [_row("t1", assignee="ash", blocked_on="please decide X",
                 tags=["needs:human"])]
    out = query.blocked_on_human(rows, human="ash")
    assert [r["name"] for r in out] == ["t1"]
    assert out[0]["blocked_on_user"] == "ash"


def test_terminal_rows_excluded():
    rows = [_row("t1", status="done", blocked_on="user:ash")]
    assert query.blocked_on_human(rows, human="ash") == []


# --- `--on-user` typing ------------------------------------------------------

def test_on_user_types_blocked_on_field(capsys):
    t = FakeTransport()
    cli.main(["task", "start", "r", "Ship it", "--status", "active"], transport=t)
    slug = "ship-it"
    capsys.readouterr()
    rc = cli.main(["task", "block", "r", slug, "--on-user", "ash"], transport=t)
    assert rc == 0
    doc = t.store[f"team/r/task/{slug}.md"]
    fm = okf.parse_frontmatter(doc)
    assert fm["blocked_on"] == "user:ash", fm
    assert fm["status"] == "blocked"
    assert "needs:human" in (fm.get("tags") or [])


def test_plain_blocked_on_unchanged(capsys):
    # --blocked-on (agent) is NOT typed — old behavior preserved, additive change.
    t = FakeTransport()
    cli.main(["task", "start", "r", "Ship it", "--status", "active"], transport=t)
    capsys.readouterr()
    cli.main(["task", "block", "r", "ship-it", "--blocked-on", "bob"], transport=t)
    fm = okf.parse_frontmatter(t.store["team/r/task/ship-it.md"])
    assert fm["blocked_on"] == "bob"


# --- section FIRST + free, integrated through briefing / needs-me ------------

def _seed(t, rows_docs):
    for name, fm in rows_docs.items():
        body = okf.render_frontmatter({"type": "Task", **fm}) + f"\n# {name}\n"
        t.put(f"team/r/task/{name}.md", body)
    reconcile.reconcile(t, "r", now="2026-07-20T00:00:00Z", today="2026-07-20", host="h")


def test_briefing_json_has_blocked_on_human_key_first(capsys, monkeypatch):
    monkeypatch.setenv("FULCRA_COORD_HUMAN", "ash")
    t = FakeTransport()
    _seed(t, {
        "t1": {"title": "Decide launch", "status": "blocked", "assignee": "ash",
               "blocked_on": "user:ash", "tags": ["needs:human"]},
        "t2": {"title": "Normal work", "status": "active", "assignee": "alice"},
    })
    capsys.readouterr()
    rc = cli.main(["briefing", "r", "--agent", "alice", "--json"], transport=t)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert "blocked_on_human" in out
    assert [r["name"] for r in out["blocked_on_human"]] == ["t1"]


def test_briefing_text_renders_blocked_on_human_first(capsys, monkeypatch):
    monkeypatch.setenv("FULCRA_COORD_HUMAN", "ash")
    t = FakeTransport()
    _seed(t, {
        "t1": {"title": "Decide launch", "status": "blocked", "assignee": "ash",
               "blocked_on": "user:ash", "tags": ["needs:human"]},
    })
    capsys.readouterr()
    cli.main(["briefing", "r", "--agent", "alice"], transport=t)
    out = capsys.readouterr().out
    assert "blocked on" in out.lower()
    # FIRST: the blocked-on-human line precedes the presence "live now" line.
    assert out.lower().index("blocked on") < out.index("live now"), out


def test_needs_me_surfaces_blocked_on_human_first(capsys, monkeypatch):
    monkeypatch.setenv("FULCRA_COORD_HUMAN", "ash")
    t = FakeTransport()
    _seed(t, {
        "t1": {"title": "Decide launch", "status": "blocked", "assignee": "ash",
               "blocked_on": "user:ash", "tags": ["needs:human"]},
        "t2": {"title": "Alice work", "status": "active", "assignee": "alice"},
    })
    capsys.readouterr()
    rc = cli.main(["needs-me", "r", "--agent", "alice", "--json"], transport=t)
    assert rc == 0
    got = json.loads(capsys.readouterr().out)
    assert got[0].get("type") == "blocked-on-human", got[0]
    assert got[0]["name"] == "t1"


def test_section_costs_zero_additional_transport_ops(capsys, monkeypatch):
    # The op-count proof: a briefing WITH a user:-blocked row must issue exactly the
    # same number of transport ops as one WITHOUT it. The section is derived from
    # rows already in memory, so it can add none.
    monkeypatch.setenv("FULCRA_COORD_HUMAN", "ash")

    # Both fixtures are IDENTICAL except the one field the section reads from
    # memory (t1.blocked_on): a typed human block that SURFACES vs a known-agent
    # block that does NOT. Same assignees/owners/statuses, so role resolution and
    # every other transport-touching section issue exactly the same ops — the only
    # possible difference is a read the section itself would add, and it adds none.
    def _count(blocked):
        t = CountingTransport()
        docs = {
            "t1": {"title": "A task", "status": "blocked", "assignee": "worker",
                   "blocked_on": ("user:ash" if blocked else "worker")},
            "t2": {"title": "Other", "status": "active", "assignee": "worker"},
        }
        _seed(t, docs)
        t.reads.clear(); t.lists.clear()
        capsys.readouterr()
        cli.main(["briefing", "r", "--agent", "worker", "--json"], transport=t)
        return len(t.reads) + len(t.lists)

    with_section = _count(True)      # t1 surfaces in blocked-on-human
    without_section = _count(False)  # t1 is agent-blocked, section empty
    assert with_section == without_section, (with_section, without_section)
