import json
from coord_fold import checkpoint as cp
from coord_fold.cli import main
from coord_fold_fakes import FakeReader, FakeStore, FakeWriter
CFG = "team/r/_coord/bus-v4/records.json"
CFG_DOC = json.dumps({"data_type": "MomentAnnotation/x", "api_version": "v1alpha1"})


def _m(st, argv):
    return main(argv, reader=FakeReader(st), writer=FakeWriter(st))


def test_emit_writes_one_v1_open_record():
    st = FakeStore({CFG: CFG_DOC}, [])
    rc = _m(st, ["emit", "r", "--from", "boss", "--to", "me", "--kind", "open", "--slug", "p1-x", "--pri", "P1", "--ptr", "team/r/task/p1-x.md", "--at", "2026-09-04T13:45:00Z"])
    assert rc == 0 and len(st.written) == 1 and set(st.written[0]["payload"]) == {"v", "at", "from", "to", "kind", "slug", "pri", "ptr"}


def test_emit_open_without_ptr_is_refused_and_writes_nothing(capsys):
    st = FakeStore({CFG: CFG_DOC}, [])
    assert _m(st, ["emit", "r", "--from", "boss", "--to", "me", "--kind", "open", "--slug", "s", "--pri", "P1"]) == 2
    assert not st.written and "ptr is required" in capsys.readouterr().err


def test_emit_then_fold_sees_the_event():
    st = FakeStore({CFG: CFG_DOC}, [])
    _m(st, ["emit", "r", "--from", "boss", "--to", "me", "--kind", "open", "--slug", "s", "--pri", "P2", "--ptr", "x.md", "--at", "2026-09-04T13:45:00Z"])
    _m(st, ["fold", "r", "--agent", "me", "--now", "2026-09-04T14:00:00Z"])
    assert "s" in json.loads(st.saved[cp.path("r", "me")])["open"]


def test_a_failed_write_exits_3_not_0():
    st = FakeStore({CFG: CFG_DOC}, []); w = FakeWriter(st); w.write_event = lambda *a, **k: False
    assert main(["emit", "r", "--from", "boss", "--to", "me", "--kind", "note", "--slug", "s", "--pri", "P3"], reader=FakeReader(st), writer=w) == 3
