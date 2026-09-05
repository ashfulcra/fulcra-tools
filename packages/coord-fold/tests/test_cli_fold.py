import json
from coord_fold import checkpoint as cp
from coord_fold.cli import main
from coord_fold_fakes import FakeReader, FakeStore, FakeWriter

CFG = "team/r/_coord/bus-v4/records.json"
CFG_DOC = json.dumps({"data_type": "MomentAnnotation/x", "api_version": "v1alpha1"})


def _rec(kind, slug, at, rid, to="me", sender="boss", ptr=None):
    p = {"v": 1, "at": at, "from": sender, "to": to, "kind": kind, "slug": slug, "pri": "P1", "ptr": ptr or f"team/r/task/{slug}.md"}
    return {"id": rid, "recorded_at": at, "note": json.dumps(p)}


def _team(events):
    return FakeStore({CFG: CFG_DOC}, events)


def _run(st, *extra, now="2026-09-04T11:00:00Z"):
    return main(["fold", "r", "--agent", "me", "--now", now, *extra], reader=FakeReader(st), writer=FakeWriter(st))


def _ckpt(st):
    return json.loads(st.saved[cp.path("r", "me")])


def test_fold_from_fresh_applies_open_events_and_stores_the_checkpoint():
    st = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1"), _rec("open", "b", "2026-09-04T10:01:00Z", "2")])
    assert _run(st) == 0 and set(_ckpt(st)["open"]) == {"a", "b"}


def test_close_after_open_removes_the_row():
    st = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1"), _rec("close", "a", "2026-09-04T10:05:00Z", "2")])
    _run(st); assert _ckpt(st)["open"] == {}


def test_events_for_someone_else_do_not_land_but_broadcast_does():
    st = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1", to="them"), _rec("open", "b", "2026-09-04T10:00:00Z", "2", to="all")])
    _run(st); assert set(_ckpt(st)["open"]) == {"b"}


def test_cursor_is_the_last_applied_event_never_now():
    """G26 / Ruling 1."""
    st = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1")]); _run(st)
    assert _ckpt(st)["cursor"] == "2026-09-04T10:00:00Z"
    st.events.append(_rec("close", "a", "2026-09-04T11:30:00Z", "2"))
    _run(st, now="2026-09-04T12:00:00Z"); assert _ckpt(st)["open"] == {} and _ckpt(st)["cursor"] == "2026-09-04T11:30:00Z"
    _run(st, now="2026-09-04T13:00:00Z"); assert _ckpt(st)["cursor"] == "2026-09-04T11:30:00Z"


def test_rerunning_from_the_stored_cursor_yields_the_same_open_set():
    """G26's checkable consequence."""
    st = _team([_rec("open", f"s{i}", f"2026-09-04T10:{i:02d}:00Z", str(i)) for i in range(6)] + [_rec("close", "s2", "2026-09-04T10:07:00Z", "x")])
    _run(st); first = _ckpt(st)["open"]
    _run(st, now="2026-09-04T12:00:00Z"); assert _ckpt(st)["open"] == first == {f"s{i}": first[f"s{i}"] for i in (0, 1, 3, 4, 5)}


def test_other_agents_traffic_after_my_last_event_is_not_reread():
    """G31 / codex-coder round 7: without this, an agent with no recent addressed events rereads everyone else's traffic forever."""
    st = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1")]); _run(st)
    st.events.extend(_rec("open", f"o{i}", f"2026-09-04T10:{i // 60 + 1:02d}:{i % 60:02d}Z", f"o{i}", to="them") for i in range(3000))
    _run(st, now="2026-09-04T12:00:00Z"); assert _ckpt(st)["cursor"] == "2026-09-04T10:50:59Z" and set(_ckpt(st)["open"]) == {"a"}
    r = FakeReader(st); yielded = []; orig = r.read_events
    r.read_events = lambda ch, since: (yielded.append(x) or x for x in orig(ch, since))
    assert main(["fold", "r", "--agent", "me", "--now", "2026-09-04T13:00:00Z"], reader=r, writer=FakeWriter(st)) == 0
    assert len(yielded) < 10 and set(_ckpt(st)["open"]) == {"a"}


def test_a_failed_event_read_does_not_advance_the_cursor_and_exits_3(capsys):
    st = _team([]); st.fail_events = True
    assert _run(st) == 3 and cp.path("r", "me") not in st.saved and "degraded" not in capsys.readouterr().out.lower()


def test_a_capped_pass_is_a_remainder_not_an_error(capsys):
    """G25 / Ruling 4: exit 0, cursor at the last applied event, unread_events is the bounded remainder."""
    st = _team([_rec("open", f"s{i}", f"2026-09-04T10:{i:02d}:00Z", str(i)) for i in range(7)])
    assert _run(st, "--max-events", "5") == 0
    c = _ckpt(st); assert c["unread_events"] == 2 and len(c["open"]) == 5 and c["cursor"] == "2026-09-04T10:04:00Z"
    out = capsys.readouterr(); assert "2 events remain" in out.out and "degraded" not in (out.out + out.err).lower()
    assert _run(st, "--max-events", "5", now="2026-09-04T12:00:00Z") == 0 and len(_ckpt(st)["open"]) == 7 and _ckpt(st)["unread_events"] == 0


def test_zero_progress_with_events_present_is_the_only_error(capsys):
    st = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1")])
    assert _run(st, "--max-events", "0") == 2 and "no progress" in capsys.readouterr().err and cp.path("r", "me") not in st.saved


def test_a_concurrent_writer_is_refused_by_name_and_nothing_is_overwritten(capsys):
    """G27 / Ruling 2: the generation moves between load and the re-read before write."""
    st = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1")]); _run(st)
    assert _ckpt(st)["generation"] == 1 and _ckpt(st)["writer"].startswith("me:")
    r = FakeReader(st); calls = []; orig = r.read_classified
    def bumping(path):
        body, state = orig(path)
        if path == cp.path("r", "me"):
            calls.append(path)
            if len(calls) == 2:
                other = json.loads(body); other["generation"] += 1; other["writer"] = "me:other-host"; body = json.dumps(other)
        return body, state
    r.read_classified = bumping
    before = st.saved[cp.path("r", "me")]
    st.events.append(_rec("close", "a", "2026-09-04T11:30:00Z", "2"))
    assert main(["fold", "r", "--agent", "me", "--now", "2026-09-04T12:00:00Z"], reader=r, writer=FakeWriter(st)) == 2
    err = capsys.readouterr().err
    assert "acting twice" in err and "me:other-host" in err and st.saved[cp.path("r", "me")] == before


def test_corrupt_checkpoint_is_refused_and_untouched(capsys):
    st = _team([]); st.docs[cp.path("r", "me")] = "{not json"
    assert _run(st) == 2 and cp.path("r", "me") not in st.saved and "corrupt" in capsys.readouterr().err


def test_unresolved_channel_is_refused():
    assert _run(FakeStore({}, [])) == 2


def test_verify_pointers_records_an_absent_pointer_and_default_reads_none():
    st = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1", ptr="team/r/task/gone.md")])
    assert _run(st, "--verify-pointers") == 3 and _ckpt(st)["unreadable_pointers"] == ["a"]
    st2 = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1")]); reads = []
    r = FakeReader(st2); orig = r.read_classified; r.read_classified = lambda p: (reads.append(p), orig(p))[1]
    main(["fold", "r", "--agent", "me", "--now", "2026-09-04T11:00:00Z"], reader=r, writer=FakeWriter(st2))
    assert not any("/task/" in p for p in reads)
