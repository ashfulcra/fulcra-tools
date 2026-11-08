"""One original controlled deadline; pure clocks and inert local reads only."""
import importlib
from pathlib import Path
import sys
import pytest

sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))


def module():
    assert importlib.util.find_spec('bundle_operation_deadline') is not None, 'shared original operation bound is missing'
    return importlib.import_module('bundle_operation_deadline')


def test_nested_read_does_not_renew_operation(monkeypatch,tmp_path):
    d=module();clock=[0.0]
    monkeypatch.setattr(d.time,'monotonic',lambda:clock[0])
    with pytest.raises(d.OperationDeadlineExceeded), d.compiler_operation() as original:
        clock[0]=299.0
        with d.compiler_operation() as nested:
            assert nested.expires==original.expires==300.0
            assert nested.remaining()==1.0 and nested.clamp(359.0)==300.0
            import bundle_compiler_qualifiers as q
            ledger=q.CompilerReadBudget((q.ReadSlot('ordinary','inert',1),))
            assert ledger.begin('ordinary','inert',1)==300.0
            ledger.charge(1);ledger.end(True)
            assert ledger.seconds==0 and ledger.bytes==1
        assert d.current_operation_deadline() is original
        clock[0]=300.0
        with pytest.raises(d.OperationDeadlineExceeded):original.check()
    assert d.current_operation_deadline() is None
    with d.compiler_operation() as fresh:assert fresh.expires==600.0
    assert d.current_operation_deadline() is None


def test_read_and_pair_cannot_complete_after_original_deadline(monkeypatch,tmp_path):
    d=module();import bundle_provenance_stages as s
    import bundle_native_proof as n
    clock=[0.0];monkeypatch.setattr(d.time,'monotonic',lambda:clock[0])
    path=tmp_path/'inert';path.write_bytes(b'one')
    original=s.core.os.read
    def late(fd,n):
        value=original(fd,n);clock[0]=301.;return value
    with pytest.raises(d.OperationDeadlineExceeded), d.compiler_operation():
        clock[0]=299.
        monkeypatch.setattr(s.core.os,'read',late)
        with pytest.raises(s.Unknown):s.read(path)
        assert not n.prove_public_pair(b'one',b'one','raw').complete
    assert d.current_operation_deadline() is None


def test_command_cannot_start_without_original_reap_reserve(monkeypatch):
    d=module();import bundle_provenance_stages as s
    clock=[0.0];monkeypatch.setattr(d.time,'monotonic',lambda:clock[0])
    def forbidden(*args,**kwargs):pytest.fail('no process may start without teardown reserve')
    monkeypatch.setattr(s.subprocess,'Popen',forbidden)
    with d.compiler_operation():
        clock[0]=299.25
        with pytest.raises(s.Unknown):s._run_process(('/inert/tool',),timeout=30,cap=1024,cwd='/',env={})
