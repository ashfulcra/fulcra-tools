import json
from coord_fold import checkpoint as cp
from coord_fold.cli import main
from fakes import FakeReader, FakeStore, FakeWriter


def _st(state):
    st = FakeStore({}, []); st.docs[cp.path("r", "me")] = json.dumps(state); return st


def _m(st):
    return main(["status", "r", "--agent", "me"], reader=FakeReader(st), writer=FakeWriter(st))


def test_status_prints_open_rows_and_exits_0(capsys):
    s = cp.empty("T"); s["open"]["s"] = {"pri": "P1", "from": "boss", "ptr": "x.md", "at": "T"}
    assert _m(_st(s)) == 0 and "[P1] s" in capsys.readouterr().out


def test_status_exits_3_only_on_an_unknown_and_reports_a_remainder_at_0(capsys):
    s = cp.empty("T"); s["unread_events"] = 12; assert _m(_st(s)) == 0 and "12 events remain" in capsys.readouterr().out
    s = cp.empty("T"); s["unreadable_pointers"] = ["s9"]; assert _m(_st(s)) == 3 and "pointer for s9" in capsys.readouterr().err


def test_status_never_folded_exits_2_and_reads_no_events(capsys):
    assert _m(FakeStore({}, [])) == 2 and "never folded" in capsys.readouterr().err
    st = _st(cp.empty("T")); r = FakeReader(st); r.read_events = lambda *a: (_ for _ in ()).throw(AssertionError("status read events"))
    assert main(["status", "r", "--agent", "me"], reader=r, writer=FakeWriter(st)) == 0
