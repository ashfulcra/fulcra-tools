"""Closed compiler profiles and the physical-read ledger; no native tools run."""
import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest

sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
import bundle_provenance_stages as s


def qmodule():
    assert importlib.util.find_spec('bundle_compiler_qualifiers'), 'typed compiler observer is missing'
    return importlib.import_module('bundle_compiler_qualifiers')


def test_profiles_are_closed_and_cross_bound(tmp_path):
    source=tmp_path/'packages/menubar/scripts/cli_launcher.c'
    source.parent.mkdir(parents=True)
    source.write_bytes((Path(__file__).parents[1]/'scripts/cli_launcher.c').read_bytes())
    recipe={'compiler_baseline':{'schema':2,'profile':'cloud-xcode16.4-arm64-v1','source':str(source)}}
    # Existing implementation rejects this reviewed selector: causal RED precedes the new module.
    assert s._compiler_baseline(recipe)['output_sha256']=='41cec1c57154dbab34c1a8bc7c9813cca21591efe71141f54fe58c21c347f3c8'
    q=qmodule()
    selected=q.select_compiler_qualifier(recipe,tmp_path)
    recipe['compiler_producer']={'python':{'path':str(tmp_path/'python'),'sha256':'a'*64},'workflow':{'path':str(tmp_path/'.github/workflows/macos.yml'),'sha256':'b'*64},'environment':{}}
    tools=tuple(s.ToolIdentity(p,h) for p,_,h in q.WRAPPERS)+(s.ToolIdentity(str(tmp_path/'python'),'a'*64),)
    q.assert_profile_tools(selected,tools,recipe)
    for bad in (tools[:-1],tools+(tools[0],), (s.ToolIdentity('/usr/bin/clang','c'*64),)+tools[1:]):
        with pytest.raises(s.Unknown): q.assert_profile_tools(selected,bad,recipe)
    for field,value in [('profile','arbitrary'),('output_sha256','a'*64),('schema',True)]:
        bad=json.loads(json.dumps(recipe));bad['compiler_baseline'][field]=value
        with pytest.raises(s.Unknown): q.select_compiler_qualifier(bad,tmp_path)


def test_typed_os_reader_preserves_generic_limits(tmp_path):
    q=qmodule()
    path=tmp_path/'tool';path.write_bytes(b'ordinary');path.chmod(0o755)
    assert s._tool_identity(path)==s.digest(b'ordinary')
    with pytest.raises(s.Unknown): q.read_os_tool(str(path),q.CompilerReadBudget(()))
    info=SimpleNamespace(st_mode=0o100755,st_uid=0,st_nlink=5,st_size=264942528)
    q._tool_mode(info,typed=True)
    for attrs in ({'st_mode':0o100777},{'st_uid':999999},{'st_mode':0o100644},{'st_size':268435457}):
        bad=SimpleNamespace(**(vars(info)|attrs))
        with pytest.raises(s.Unknown): q._tool_mode(bad,typed=True)
    with pytest.raises(s.Unknown): q._tool_mode(info,typed=False)


def test_read_schedule_reserves_every_physical_pass(tmp_path):
    q=qmodule()
    tool=tmp_path/'python';tool.write_bytes(b'x'*100);tool.chmod(0o755)
    e=SimpleNamespace(invocation=SimpleNamespace(tools=(s.ToolIdentity(str(tool),s.digest(tool.read_bytes())),)))
    ledger=q.plan_compiler_reads(e,q.CLOUD)
    assert ledger.reservation==540040472
    assert len(ledger.slots)==18
    assert [slot.kind for slot in ledger.slots].count('ordinary')==6
    assert sum(slot.size for slot in ledger.slots if slot.kind=='os')==540039872
    with pytest.raises(s.Unknown): ledger.begin('ordinary',str(tool),101)
    huge=tmp_path/'large';huge.write_bytes(b'x');huge.chmod(0o755)
    with huge.open('r+b') as f:f.truncate(5064246)
    e.invocation.tools=(s.ToolIdentity(str(huge),'a'*64),)
    with pytest.raises(s.Unknown): q.plan_compiler_reads(e,q.CLOUD)


def test_installer_limit_precedes_any_write(tmp_path):
    import resource
    before=resource.getrlimit(resource.RLIMIT_FSIZE)
    output=tmp_path/'bounded'
    script="import pathlib,sys; p=pathlib.Path(sys.argv[1]); f=p.open('wb'); f.write(b'x'*2097153); f.close()"
    result=s._run_process((sys.executable,'-I','-B','-c',script,str(output)),timeout=10,cap=1024*1024,cwd=str(tmp_path),env={'PATH':'/usr/bin:/bin'},_compiler_limit=True)
    assert result.returncode!=0 and result.eof and result.reaped
    assert output.stat().st_size==2097152
    assert resource.getrlimit(resource.RLIMIT_FSIZE)==before


def test_parent_leader_evidence_does_not_claim_child_telemetry(tmp_path):
    q=qmodule();budget=q.CompilerReadBudget(())
    budget.command((sys.executable,'-I','-B','-c','pass'),cwd=str(tmp_path),env={'PATH':'/usr/bin:/bin'},producer=True)
    record=budget.commands[0]
    assert record['returncode']==0 and record['eof'] and record['reaped']
    assert set(record)=={'argv','returncode','stdout_bytes','stdout_sha256','stderr_bytes','stderr_sha256','eof','reaped'}


def test_observer_tail_and_output_drift_fail_closed(tmp_path,monkeypatch):
    q=qmodule();tool=tmp_path/'tiny';tool.write_bytes(b'x');tool.chmod(0o755)
    budget=q.CompilerReadBudget((q.ReadSlot('ordinary',str(tool),1),q.ReadSlot('ordinary',str(tool),1)))
    assert q.ordinary_tool_identity(str(tool),budget)==s.digest(b'x')
    with pytest.raises(s.Unknown):budget.complete()
    original=q.os.read
    def broken(fd,n):raise OSError('late read failure')
    monkeypatch.setattr(q.os,'read',broken)
    with pytest.raises((s.Unknown,OSError)):q.ordinary_tool_identity(str(tool),budget)
    assert budget.failed and budget.index==2
    with pytest.raises(s.Unknown):budget.complete()


def test_failed_and_shared_inode_reads_are_physically_charged(tmp_path,monkeypatch):
    q=qmodule();a=tmp_path/'a';b=tmp_path/'b';a.write_bytes(b'abc');a.chmod(0o755);os=__import__('os');os.link(a,b)
    budget=q.CompilerReadBudget((q.ReadSlot('ordinary',str(a),3),q.ReadSlot('ordinary',str(b),3)))
    q.ordinary_tool_identity(str(a),budget);q.ordinary_tool_identity(str(b),budget)
    budget.complete();assert budget.bytes==6 and len(budget.records)==2
    clock=[1.0];monkeypatch.setattr(q.time,'monotonic',lambda:clock[0])
    budget=q.CompilerReadBudget((q.ReadSlot('ordinary',str(a),3),))
    budget.begin('ordinary',str(a),3);budget.charge(1);clock[0]=61.0
    with pytest.raises(s.Unknown):budget.end(True)
    assert budget.failed and budget.bytes==1 and budget.seconds==60


def test_refused_installer_setup_has_no_producer_effect(tmp_path,monkeypatch):
    import resource
    target=tmp_path/'must-not-exist';original=resource.getrlimit(resource.RLIMIT_FSIZE)
    def failed():raise RuntimeError('injected preexec setup failure')
    monkeypatch.setattr(s,'_compiler_child_limits',failed)
    with pytest.raises(s.Unknown):
        s._run_process((sys.executable,'-I','-B','-c',"import pathlib,sys;pathlib.Path(sys.argv[1]).write_bytes(b'x')",str(target)),timeout=10,cap=1024,cwd=str(tmp_path),env={},_compiler_limit=True)
    assert not target.exists() and resource.getrlimit(resource.RLIMIT_FSIZE)==original


def test_tool_read_deadline_clamped_to_original_operation(monkeypatch):
    import bundle_compiler_qualifiers as q
    clock=[0.0];monkeypatch.setattr(q.time,'monotonic',lambda:clock[0])
    ledger=q.CompilerReadBudget((q.ReadSlot('ordinary','inert',1),))
    clock[0]=299.0
    assert ledger.begin('ordinary','inert',1)<=300.0
    clock[0]=301.0
    with pytest.raises(q.s.Unknown):ledger.charge(1)
    with pytest.raises(q.s.Unknown):ledger.end(True)
    assert ledger.bytes==1 and ledger.seconds==2.0 and ledger.failed
    assert not ledger.records[-1]['complete']
