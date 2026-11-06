"""Invocation ownership, process-boundary fakes, and unchanged-byte oracles."""
from dataclasses import replace
import hashlib
import importlib
import json
import os
from pathlib import Path
import plistlib
import sys

import pytest

from test_bundle_public_inputs import inputs, sha
from test_bundle_native_proof import signing_pair

SCRIPTS = Path(__file__).parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))


def stages():
    try:
        return importlib.import_module('bundle_provenance_stages')
    except ModuleNotFoundError:
        pytest.fail('invocation-bound provenance stages are missing')


@pytest.fixture(autouse=True)
def synthetic_system_tools(tmp_path,monkeypatch):
    """Map only the identity boundary to files owned by this pure fixture.

    CODESIGN, production argv, source-policy rules and current-hash checks remain
    unchanged. No test needs a Darwin executable or platform-wide skip.
    """
    module=stages()
    production_identity=module._tool_identity
    folder=tmp_path/'synthetic-system-tools'
    folder.mkdir()
    files={}
    for path in ('/usr/bin/codesign','/usr/bin/clang'):
        fake=folder/Path(path).name
        fake.write_bytes(('synthetic non-executed tool: '+Path(path).name).encode())
        fake.chmod(0o755)
        files[path]=fake
    def identity(path):
        return production_identity(files.get(str(path),path))
    monkeypatch.setattr(module,'_tool_identity',identity)
    monkeypatch.setattr(module,'COMPILER_TOOL_SHA256',sha(files['/usr/bin/clang'].read_bytes()))
    return files,production_identity


def test_invocation_bound_stage_entry_exists():
    assert importlib.util.find_spec('bundle_provenance_stages') is not None, 'missing invocation-bound entry'


def make_run(inputs, tmp_path, *, native=False):
    p, archive, catalog, lock, destination, payload, make, _ = inputs
    if native:
        payload, _ = signing_pair()
        make(data=payload)
    s = stages()
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    source = workspace / 'source.txt'
    source.write_bytes(b'pinned source')
    recipe = workspace / 'recipe.json'
    recipe.write_text(json.dumps({'build_argv':[str(workspace/'briefcase'),'build','macOS'],'sign_argv':[],'signature_resources':[]}))
    app = workspace / 'app'
    tool = workspace / 'briefcase'
    tool.write_bytes(b'inert tool, never executed')
    tool.chmod(0o755)
    targets=(s.TargetPolicy('.', 'com.example.app', plistlib.dumps({}),0,'not set'),)
    if native:
        targets+=(s.TargetPolicy(destination,'native.example',plistlib.dumps({}),0,'not set'),)
    policy = s.SignaturePolicy(targets, 'adhoc')
    expected = s.start_invocation(tmp_path / 'private', workspace=workspace, app=app,
        source_files=(source,), lock=lock, recipe=recipe, archives={archive.name: archive},
        catalog_sha256=sha(catalog.read_bytes()), architecture='arm64',
        tools=(s.ToolIdentity(str(tool), sha(tool.read_bytes())), s.ToolIdentity('/usr/bin/codesign', s._tool_identity('/usr/bin/codesign'))), policy=policy,
        markers=('/public/marker',))
    return s, expected, source, recipe, app, destination, payload


@pytest.fixture
def run(inputs,tmp_path):
    return make_run(inputs,tmp_path)


def bound(run):
    s, e, _, _, app, destination, payload = run
    prepared = s.prepare_public_inputs(e)
    e = s.advance(e, prepared)
    target = app / destination
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    ref = s.bind_public_inputs(e, prepared, app)
    return s, s.advance(e, ref), ref


def checked(run,monkeypatch):
    s,e,ref=bound(run)
    def fake(argv,**kwargs):
        if '--verify' in argv or argv[0]!=s.CODESIGN: output=b''
        elif '--entitlements' in argv: output=plistlib.dumps({})
        else: output=b'Identifier=com.example.app\nSignature=adhoc\nTeamIdentifier=not set\nCodeDirectory v=20400 size=1 flags=0x0(none) hashes=1+0 location=embedded\n'
        return s.CommandResult(0,output,True,True)
    monkeypatch.setattr(s,'_run_process',fake)
    ref=s.sanitize_stage(e,ref,phase='raw',check_only=False); e=s.advance(e,ref)
    ref=s.controlled_build(e,ref,(str(Path(e.invocation.workspace)/'briefcase'),'build','macOS')); e=s.advance(e,ref)
    ref=s.sanitize_stage(e,ref,phase='postbuild',check_only=False); e=s.advance(e,ref)
    ref=s.sanitize_stage(e,ref,phase='postbuild',check_only=True); e=s.advance(e,ref)
    return s,e,ref


def test_prepare_is_before_create_and_bind_is_before_raw(run):
    s, e, _, _, app, _, _ = run
    assert not app.exists()
    prepared = s.prepare_public_inputs(e)
    assert prepared.stage == 'PREPARED' and prepared.generation == 1
    assert s.load_receipt(s.advance(e, prepared), prepared).stage == 'PREPARED'
    with pytest.raises(s.Unknown):
        s.bind_public_inputs(s.advance(e, prepared), prepared, app)


def test_bound_raw_progression_and_write_once(run):
    s, e, ref = bound(run)
    assert s.load_receipt(e, ref).stage == 'BOUND'
    raw = s.sanitize_stage(e, ref, phase='raw', check_only=False)
    assert raw.stage == 'RAW' and raw.generation == 3
    with pytest.raises(s.Unknown):
        s.sanitize_stage(e, ref, phase='raw', check_only=False)
    assert e.head.stage == 'BOUND'


@pytest.mark.parametrize('failure', ['missing-authority','old-run','older-generation','receipt-corrupt','inventory-corrupt','partial','source','lock','catalog','recipe','app','wrong-stage','predecessor','lost-authority'])
def test_receipts_never_supply_current_expectations(run, inputs, failure):
    s, e, ref = bound(run)
    original_head = e.head
    p, _, catalog, lock, _, _, _, _ = inputs
    if failure in {'missing-authority', 'lost-authority'}:
        e = None
    elif failure == 'old-run':
        e = replace(e, invocation=replace(e.invocation, nonce='f'*64))
    elif failure == 'older-generation':
        e = replace(e, head=replace(e.head, generation=99))
    elif failure == 'receipt-corrupt':
        ref.path.write_bytes(ref.path.read_bytes()+b'x')
    elif failure == 'inventory-corrupt':
        ref.path.with_suffix('.inventory.json').write_bytes(b'[]')
    elif failure == 'partial':
        ref.path.write_bytes(b'{')
    elif failure in {'source','lock','catalog','recipe'}:
        path = {'source':run[2], 'lock':lock,'catalog':catalog,'recipe':run[3]}[failure]
        path.write_bytes(path.read_bytes()+b'\n')
    elif failure == 'app':
        (run[4]/'inserted').write_bytes(b'marker-free')
    elif failure == 'wrong-stage':
        e = replace(e, head=replace(e.head, stage='SIGNED'))
    else:
        e = replace(e, head=replace(e.head, predecessor=s.Head('EMPTY',0,'0'*64)))
    with pytest.raises(s.Unknown):
        s.load_receipt(e, ref)
    assert original_head.stage == 'BOUND'


def test_no_provenance_is_strict_and_context_is_required(run):
    s, e, ref = bound(run)
    sanitizer = importlib.import_module('sanitize_bundle')
    with pytest.raises(RuntimeError):
        sanitizer.sanitize(e.app, home=Path('/public/marker'), check_only=True)
    with pytest.raises(RuntimeError):
        sanitizer.sanitize(e.app, phase='postbuild', provenance_receipt=ref.path, check_only=True)


def test_runtime_boundary_cannot_rebaseline_source_or_native_changes(run,monkeypatch):
    s, e, ref = checked(run,monkeypatch)
    before = s.boundary_before(e, ref, 'runtime')
    (e.app / run[5]).write_bytes(b'changed without marker')
    with pytest.raises(s.Unknown):
        s.boundary_after(e, before, s.CommandResult(0,b'',True,True), 'runtime')
    assert e.head.stage == 'POSTBUILD_CHECKED'


def test_runtime_cache_only_is_dirty_and_cannot_claim_signed(run,monkeypatch):
    s, e, ref = checked(run,monkeypatch)
    before = s.boundary_before(e, ref, 'runtime')
    cache = e.app / 'Contents/Resources/app_packages/example/__pycache__/x.cpython-313.pyc'
    cache.parent.mkdir()
    cache.write_bytes(b'cache')
    output = s.boundary_after(e, before, s.CommandResult(0,b'',True,True), 'runtime')
    result = s.load_receipt(s.advance(e, output), output)
    assert result.signature_state == 'DIRTY'
    with pytest.raises(s.Unknown):
        s.sanitize_stage(s.advance(e, output), output, phase='signed', check_only=True)


def test_verifier_derives_expected_map_before_observation(run, monkeypatch):
    s, e, ref = bound(run)
    # Real inventory and policy comparison; only native process boundary is fake.
    target = s.TargetPolicy('.', 'com.example.app', plistlib.dumps({}), 0, 'not set')
    policy = s.SignaturePolicy((target,), 'adhoc')
    e = replace(e, policy=policy, invocation=replace(e.invocation, policy_sha256=s.policy_digest(policy)))
    inventory = s.inventory(e.app)
    calls=[]
    def fake(argv, **kwargs):
        calls.append(tuple(argv))
        if '--verify' in argv:
            data=b''
        elif '--entitlements' in argv:
            data=plistlib.dumps({})
        else:
            data=b'Identifier=com.example.app\nSignature=adhoc\nTeamIdentifier=not set\nCodeDirectory v=20400 size=1 flags=0x0(none) hashes=1+0 location=embedded\n'
        return s.CommandResult(0,data,True,True)
    monkeypatch.setattr(s, '_run_process', fake)
    # Policy target enumeration rejects undeclared code and always includes app.
    assert s.verify_signature_policy(e, inventory, policy).complete
    assert all(c[0] == '/usr/bin/codesign' for c in calls)
    assert any('--strict' in c for c in calls)


@pytest.mark.parametrize('bad', ['timeout','nonzero','malformed','duplicate','entitlements-unreadable','entitlements-mismatch','identifier','flags','certificate','team','mutation','incomplete'])
def test_signature_observations_fail_closed(run, monkeypatch, bad):
    s, e, _ = bound(run)
    policy=s.SignaturePolicy((s.TargetPolicy('.', 'com.example.app', plistlib.dumps({}),0,'not set'),),'adhoc')
    e=replace(e,policy=policy,invocation=replace(e.invocation,policy_sha256=s.policy_digest(policy)))
    before=s.inventory(e.app)
    def fake(argv, **kwargs):
        if bad=='timeout':
            raise s.Unknown('UNKNOWN: command bound')
        if bad=='nonzero':
            return s.CommandResult(1,b'',True,True)
        if bad=='incomplete':
            return s.CommandResult(0,b'',False,False)
        if '--verify' in argv:
            data=b''
        elif '--entitlements' in argv:
            data= b'' if bad=='entitlements-unreadable' else plistlib.dumps({'evil':True} if bad=='entitlements-mismatch' else {})
        else:
            data=b'Identifier=com.example.app\nSignature=adhoc\nTeamIdentifier=not set\nCodeDirectory v=20400 size=1 flags=0x0(none) hashes=1+0 location=embedded\n'
            if bad=='malformed': data=b'garbage'
            if bad=='duplicate': data+=b'Identifier=com.example.app\n'
            if bad=='identifier': data=data.replace(b'com.example.app',b'other')
            if bad=='flags': data=data.replace(b'0x0(none)',b'0x10000(runtime)')
            if bad=='certificate': data=data.replace(b'adhoc',b'certificate')
            if bad=='team': data=data.replace(b'not set',b'OTHER')
            if bad=='mutation': (e.app/'inserted').write_bytes(b'clean')
        return s.CommandResult(0,data,True,True)
    monkeypatch.setattr(s,'_run_process',fake)
    with pytest.raises(s.Unknown): s.verify_signature_policy(e,before,policy)



def test_binding_allows_only_existing_optional_payload_cleanup(run):
    s,e,_,_,app,destination,payload=run
    prepared=s.prepare_public_inputs(e); e=s.advance(e,prepared)
    target=app/destination; target.parent.mkdir(parents=True); target.write_bytes(payload)
    script=app/'Contents/Resources/app_packages/bin/tool'
    script.parent.mkdir(); script.write_bytes(b'#!/public/marker/interpreter')
    ref=s.bind_public_inputs(e,prepared,app); e=s.advance(e,ref)
    raw=s.sanitize_stage(e,ref,phase='raw',check_only=False)
    assert raw.stage=='RAW' and not script.exists()
    assert s.load_receipt(s.advance(e,raw),raw).signature_state=='DIRTY'


def build_fake(s,e,native_path,*,tamper=None):
    def fake(argv,**kwargs):
        if argv[0]!='/usr/bin/codesign':
            origin,signed=signing_pair()
            if tamper=='payload': signed=signed[:224]+b'X'+signed[225:]
            (e.app/native_path).write_bytes(signed)
            if tamper=='extra': (e.app/'extra').write_bytes(b'marker-free')
            return s.CommandResult(0,b'build output',True,True)
        path=argv[-1]
        if '--verify' in argv: output=b''
        elif '--entitlements' in argv: output=plistlib.dumps({})
        else:
            identifier='native.example' if path.endswith(native_path) else 'com.example.app'
            output=('Identifier='+identifier+'\nSignature=adhoc\nTeamIdentifier=not set\nCodeDirectory v=20400 size=1 flags=0x0(none) hashes=1+0 location=embedded\n').encode()
        return s.CommandResult(0,output,True,True)
    return fake


@pytest.mark.parametrize('tamper',[None,'payload','extra'])
def test_controlled_build_owns_exact_output_before_postbuild(inputs,tmp_path,monkeypatch,tamper):
    run=make_run(inputs,tmp_path,native=True)
    s,e,ref=bound(run)
    ref=s.sanitize_stage(e,ref,phase='raw',check_only=False); e=s.advance(e,ref)
    monkeypatch.setattr(s,'_run_process',build_fake(s,e,run[5],tamper=tamper))
    argv=(str(Path(e.invocation.workspace)/'briefcase'),'build','macOS')
    if tamper:
        with pytest.raises(s.Unknown): s.controlled_build(e,ref,argv)
        assert e.head.stage=='RAW'
        return
    signed=s.controlled_build(e,ref,argv); e=s.advance(e,signed)
    assert signed.stage=='BUILD_SIGNED'
    post=s.sanitize_stage(e,signed,phase='postbuild',check_only=False); e=s.advance(e,post)
    checked=s.sanitize_stage(e,post,phase='postbuild',check_only=True)
    e=s.advance(e,checked)
    assert e.head.stage=='POSTBUILD_CHECKED'
    target=e.app/run[5]
    target.write_bytes(target.read_bytes()+b'marker-free insertion')
    with pytest.raises(s.Unknown): s.load_receipt(e,checked)


def test_nonpublic_native_transform_requires_before_byte_proof(inputs,tmp_path,monkeypatch):
    run=make_run(inputs,tmp_path,native=True)
    s,e,ref=bound(run)
    # Add native before binding would be necessary: independently pinned RAW
    # can only authorize later signature changes to its unchanged payload.
    ref=s.sanitize_stage(e,ref,phase='raw',check_only=False); e=s.advance(e,ref)
    monkeypatch.setattr(s,'_run_process',build_fake(s,e,run[5]))
    with pytest.raises(s.Unknown): s.controlled_build(e,ref,('/not/pinned','build'))
    assert e.head.stage=='RAW'


def test_cli_requires_independent_environment_and_help_is_inert(run,capsys,monkeypatch):
    s,e,ref=bound(run)
    assert s.main(['--help'])==0
    assert 'prepare-public-inputs' in capsys.readouterr().out
    monkeypatch.delenv('COLLECT_PROVENANCE_CONTEXT',raising=False)
    assert s.main(['bind-public-inputs'])==1
    assert 'UNKNOWN' in capsys.readouterr().err
    assert e.head.stage=='BOUND'


def test_context_roundtrip_does_not_adopt_candidate_head(run):
    s,e,ref=bound(run)
    restored=s.context_from_json(s.context_json(e))
    assert restored==e
    stale=replace(restored,head=replace(restored.head,generation=1))
    with pytest.raises(s.Unknown): s.load_receipt(stale,ref)


def test_policy_derivation_uses_pinned_recipe_and_entitlements_not_observation(run,monkeypatch):
    s,e,_,recipe,app,destination,payload=run
    config=Path(e.invocation.workspace)/'config.toml'
    config.write_text('[tool.briefcase]\nbundle="com.example"\n[tool.briefcase.app.app.macOS]\nuniversal_build=false\n')
    monkeypatch.setattr(s,'BRIEFCASE_SOURCE_SHA256',sha(run[2].read_bytes()))
    monkeypatch.setattr(s,'TEMPLATE_SHA256',sha(run[2].read_bytes()))
    recipe.write_text(json.dumps({'policy_schema':1,'mode':'adhoc','config':str(config),
        'config_sha256':sha(config.read_bytes()),'briefcase_source':str(run[2]),
        'briefcase_source_sha256':sha(run[2].read_bytes()),'template':str(run[2]),'template_sha256':sha(run[2].read_bytes()),
        'targets':[{'path':'.','identifier':'com.example.app','role':'app','flags':2,'team':'not set'},{'path':'Contents/MacOS/Fulcra Collect','identifier':'com.example.app','role':'app','flags':2,'team':'not set'}]}))
    derived=s.derive_signature_policy(recipe,expected_recipe_sha256=sha(recipe.read_bytes()))
    assert plistlib.loads(derived.targets[0].entitlements)=={
        'com.apple.security.cs.allow-unsigned-executable-memory':True,
        'com.apple.security.cs.disable-library-validation':True}
    recipe.write_bytes(recipe.read_bytes()+b' ')
    with pytest.raises(s.Unknown): s.derive_signature_policy(recipe,expected_recipe_sha256='0'*64)


@pytest.mark.parametrize('mode',['success','stdout-cap','stderr-cap','pipe-held'])
def test_process_boundary_requires_eof_reap_and_kills_group_on_bounds(run,monkeypatch,mode):
    s,e,*_=run
    state={'killed':False,'reaped':False}
    handles=[]
    class FakeProcess:
        pid=123456789
        def __init__(self,*args,**kwargs):
            assert kwargs['start_new_session'] is True
            self.stdout=stream('stdout'); self.stderr=stream('stderr')
        def wait(self,timeout=None):
            state['reaped']=True
            return 0
    def stream(which):
        r,w=os.pipe()
        if mode==which+'-cap': os.write(w,b'X'*33)
        elif mode=='success': os.write(w,b'out' if which=='stdout' else b'err')
        if mode=='pipe-held': handles.append(w)
        else: os.close(w)
        return os.fdopen(r,'rb',buffering=0)
    monkeypatch.setattr(s.subprocess,'Popen',FakeProcess)
    def kill(pid,sig):
        state['killed']=True
        assert pid==123456789 and sig==s.signal.SIGKILL
        for fd in handles: os.close(fd)
    monkeypatch.setattr(s.os,'killpg',kill)
    if mode=='success':
        result=s._run_process(('/inert/tool',),timeout=.1,cap=32,cwd=str(e.app.parent),env={})
        assert result.output==b'out' and result.stderr==b'err' and result.eof and result.reaped
        assert not state['killed']
    else:
        with pytest.raises(s.Unknown):
            s._run_process(('/inert/tool',),timeout=.01,cap=32,cwd=str(e.app.parent),env={})
        assert state['killed'] and state['reaped']


@pytest.mark.parametrize('profile',['local-v1','cloud-xcode16.4-arm64-v1'])
def test_build_shell_keeps_prepare_create_launchers_bind_raw_build_post_check_order(tmp_path,profile):
    import subprocess
    import shutil
    repo=tmp_path/'repo'; scripts=repo/'packages/menubar/scripts'; scripts.mkdir(parents=True)
    shutil.copy(SCRIPTS/'build_macos_app.sh',scripts/'build_macos_app.sh')
    tools=tmp_path/'tools'; tools.mkdir()
    log=tmp_path/'calls.log'
    py=tools/'python3'
    py.write_text('''#!/bin/sh
case "$1" in
  -c) echo "$FAKE_PROFILE";;
  *bundle_manifest.py) exit 0;;
  *bundle_provenance_stages.py)
    echo "stage:$2" >> "$FAKE_LOG"
    if [ "$2" = emit-completion ]; then
      "$REAL_PIPE_PYTHON" -I -c 'import os; raw=os.environ["COLLECT_PROVENANCE_CONTEXT"].encode(); os.write(int(os.environ["COLLECT_PROVENANCE_COMPLETION_FD"]),str(len(raw)).encode()+b"\\n"+raw+b"\\n")'
    else echo '{"context":"owned"}'; fi;;
  *sanitize_bundle.py) echo "sanitize:$*" >> "$FAKE_LOG"; echo '{"context":"owned"}';;
  *install_bundle_launchers.py) echo launchers >> "$FAKE_LOG";;
esac
'''); py.chmod(0o755)
    for tool in ('uv','uvx'):
        path=tools/tool
        path.write_text('#!/bin/sh\necho "'+tool+':$*" >> "$FAKE_LOG"\n')
        path.chmod(0o755)
    env=dict(os.environ,PATH=str(tools)+':/usr/bin:/bin',FAKE_LOG=str(log),FAKE_PROFILE=profile,
             COLLECT_PROVENANCE_REQUEST=str(tmp_path/'request.json'),COLLECT_PROVENANCE_RUN_DIR=str(tmp_path/'private'))
    import threading
    readfd,writefd=os.pipe(); frames=[]
    def drain():
        with os.fdopen(readfd,'rb') as stream: frames.append(stream.read(32785))
    thread=threading.Thread(target=drain); thread.start()
    env['COLLECT_PROVENANCE_COMPLETION_FD']=str(writefd)
    env['REAL_PIPE_PYTHON']=sys.executable
    process=subprocess.Popen(['/bin/bash',str(scripts/'build_macos_app.sh')],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,pass_fds=(writefd,))
    os.close(writefd)
    stdout,stderr=process.communicate(timeout=10)
    thread.join(timeout=2); assert not thread.is_alive()
    result=__import__('types').SimpleNamespace(returncode=process.returncode,stdout=stdout,stderr=stderr)
    prefix,rest=frames[0].split(b'\n',1)
    assert len(rest)==int(prefix)+1 and rest.endswith(b'\n')
    assert json.loads(rest[:-1])=={'context':'owned'}
    assert result.returncode==0,result.stderr
    calls=log.read_text().splitlines()
    stages_seen=[]
    for line in calls:
        if line.startswith('stage:'): stages_seen.append(line)
        elif line=='launchers': stages_seen.append(line)
        elif 'briefcase create' in line: stages_seen.append('create')
        elif line.startswith('sanitize:'):
            assert '--provenance-receipt' in line
            stages_seen.append('check' if '--check-only' in line else 'post' if '--phase postbuild' in line else 'raw')
    installer=['stage:controlled-install-and-bind'] if profile=='cloud-xcode16.4-arm64-v1' else ['launchers','stage:bind-public-inputs']
    assert stages_seen==['stage:start','stage:prepare-public-inputs','create',*installer,'raw','stage:controlled-build','post','check','stage:emit-completion']


@pytest.mark.parametrize('bad',[None,'missing-diagnostic','extra-diagnostic','wrong-path','nonzero','xml-vs-absence'])
def test_qualified_entitlement_absence_is_not_blank_or_unreadable(run,monkeypatch,bad):
    s,e,ref=bound(run)
    policy=s.SignaturePolicy((s.TargetPolicy('.','com.example.app',None,2,'not set'),),'adhoc')
    e=replace(e,policy=policy,invocation=replace(e.invocation,policy_sha256=s.policy_digest(policy)))
    def fake(argv,**kwargs):
        if '--verify' in argv: return s.CommandResult(0,b'',True,True)
        if '--verbose=4' in argv:
            return s.CommandResult(0,b'Identifier=com.example.app\nSignature=adhoc\nTeamIdentifier=not set\nCodeDirectory v=20400 size=1 flags=0x2(adhoc) hashes=1+0 location=embedded\n',True,True)
        diagnostic=('Executable='+str(e.app)+'\n'+"warning: Specifying ':' in the path is deprecated and will not work in a future release\n").encode()
        if bad=='missing-diagnostic': diagnostic=b''
        elif bad=='extra-diagnostic': diagnostic+=b'unreadable signature\n'
        elif bad=='wrong-path': diagnostic=diagnostic.replace(str(e.app).encode(),b'/wrong')
        return s.CommandResult(1 if bad=='nonzero' else 0,plistlib.dumps({}) if bad=='xml-vs-absence' else b'',True,True,diagnostic)
    monkeypatch.setattr(s,'_run_process',fake)
    if bad:
        with pytest.raises(s.Unknown): s.verify_signature_policy(e,s.inventory(e.app),policy)
    else: assert s.verify_signature_policy(e,s.inventory(e.app),policy).complete


def test_explicit_inside_out_sign_mints_only_actual_owned_output(inputs,tmp_path,monkeypatch):
    run=make_run(inputs,tmp_path,native=True)
    s,e,_,recipe,app,native_path,_=run
    entitlements=Path(e.invocation.workspace)/'Entitlements.plist'; entitlements.write_bytes(plistlib.dumps({}))
    targets=tuple(replace(t,flags=2) for t in e.policy.targets)
    policy=replace(e.policy,sign_targets=targets)
    ordered=sorted(targets,key=lambda t:(-len(Path(t.path).parts),t.path))
    plan=tuple((s.CODESIGN,str(app if t.path=='.' else app/t.path),'--force','--sign','-',
        '--identifier',t.identifier,'--options','0','--timestamp=none','--entitlements',str(entitlements)) for t in ordered)
    data=json.loads(recipe.read_text()); data['sign_argv']=[list(a) for a in plan]
    data['entitlements_inputs']={t.path:{'path':str(entitlements),'sha256':sha(entitlements.read_bytes())} for t in targets}
    recipe.write_text(json.dumps(data))
    e=replace(e,policy=policy,invocation=replace(e.invocation,policy_sha256=s.policy_digest(policy),recipe_sha256=sha(recipe.read_bytes())))
    s,e,ref=bound((s,e,*run[2:]))
    raw=s.sanitize_stage(e,ref,phase='raw',check_only=False); e=s.advance(e,raw)
    signed_mode={'value':False}; build=build_fake(s,e,native_path)
    def fake(argv,**kwargs):
        if '--sign' in argv:
            signed_mode['value']=True
            return s.CommandResult(0,b'',True,True)
        result=build(argv,**kwargs)
        if signed_mode['value'] and '--verbose=4' in argv:
            result=replace(result,output=result.output.replace(b'flags=0x0(none)',b'flags=0x2(adhoc)'))
        return result
    monkeypatch.setattr(s,'_run_process',fake)
    ref=s.controlled_build(e,raw,tuple(data['build_argv'])); e=s.advance(e,ref)
    ref=s.sanitize_stage(e,ref,phase='postbuild',check_only=False); e=s.advance(e,ref)
    ref=s.sanitize_stage(e,ref,phase='postbuild',check_only=True); e=s.advance(e,ref)
    signed=s.controlled_sign(e,ref,plan,policy); e=s.advance(e,signed)
    assert signed.stage=='SIGNED' and signed_mode['value']
    accepted=s.sanitize_stage(e,signed,phase='signed',check_only=True); e=s.advance(e,accepted)
    assert e.head.stage=='SIGNED_CHECKED'
    (e.app/native_path).write_bytes((e.app/native_path).read_bytes()+b'marker-free signature insertion')
    def must_not_verify(*args,**kwargs): pytest.fail('exact signer-output hash must fail before fresh native verification')
    monkeypatch.setattr(s,'_run_process',must_not_verify)
    with pytest.raises(s.Unknown): s.load_receipt(e,accepted)


@pytest.mark.parametrize('name',['__pycache__evil/private.py','__pycache__/private.py','__pycache__/program.dylib'])
def test_runtime_cache_boundary_rejects_nonbytecode_insertions(run,name,monkeypatch):
    s,e,ref=checked(run,monkeypatch)
    before=s.boundary_before(e,ref,'runtime')
    extra=e.app/'Contents/Resources/app_packages/example'/name
    extra.parent.mkdir(); extra.write_bytes(b'clean')
    with pytest.raises(s.Unknown): s.boundary_after(e,before,s.CommandResult(0,b'',True,True),'runtime')


@pytest.mark.parametrize('change',['root-mode','receipt-mode','inventory-mode'])
def test_mode_changes_cannot_restore_private_receipt_or_app_identity(run,change):
    s,e,ref=bound(run)
    if change=='root-mode': e.app.chmod(0o700)
    elif change=='receipt-mode': ref.path.chmod(0o644)
    else: ref.path.with_suffix('.inventory.json').chmod(0o644)
    with pytest.raises(s.Unknown): s.load_receipt(e,ref)


def test_entitlement_duplicate_keys_and_boolean_integer_confusion_are_unknown(run,monkeypatch):
    s,e,ref=bound(run)
    policy=s.SignaturePolicy((s.TargetPolicy('.','com.example.app',plistlib.dumps({'permission':True}),0,'not set'),),'adhoc')
    e=replace(e,policy=policy,invocation=replace(e.invocation,policy_sha256=s.policy_digest(policy)))
    for data in [b'<plist><dict><key>permission</key><false/><key>permission</key><true/></dict></plist>',
                 plistlib.dumps({'permission':1})]:
        def fake(argv,**kwargs):
            if '--verify' in argv: output=b''
            elif '--entitlements' in argv: output=data
            else: output=b'Identifier=com.example.app\nSignature=adhoc\nTeamIdentifier=not set\nCodeDirectory v=20400 size=1 flags=0x0(none) hashes=1+0 location=embedded\n'
            return s.CommandResult(0,output,True,True)
        monkeypatch.setattr(s,'_run_process',fake)
        with pytest.raises(s.Unknown): s.verify_signature_policy(e,s.inventory(e.app),policy)


@pytest.mark.parametrize('bad',[None,'info-identifier','missing-entitlements','entitlements-drift'])
def test_source_policy_binds_rendered_info_and_entitlements_before_build(run,monkeypatch,bad):
    s,e,source,recipe,app,destination,payload=run
    config=Path(e.invocation.workspace)/'config.toml'
    config.write_text('[tool.briefcase]\nbundle="com.example"\n[tool.briefcase.app.app]\nformal_name="Fulcra Collect"\n[tool.briefcase.app.app.macOS]\nuniversal_build=false\n')
    for name in ['BRIEFCASE_SOURCE_SHA256','TEMPLATE_SHA256']: monkeypatch.setattr(s,name,sha(source.read_bytes()))
    entitlement_bytes=plistlib.dumps({'com.apple.security.cs.allow-unsigned-executable-memory':True,
                                     'com.apple.security.cs.disable-library-validation':True})
    entitlement_file=Path(e.invocation.workspace)/'Entitlements.plist'
    data={'policy_schema':1,'mode':'adhoc','config':str(config),'config_sha256':sha(config.read_bytes()),
        'briefcase_source':str(source),'briefcase_source_sha256':sha(source.read_bytes()),
        'template':str(source),'template_sha256':sha(source.read_bytes()),
        'briefcase_entitlements':{'path':str(entitlement_file),'sha256':sha(entitlement_bytes)},
        'targets':[{'path':'.','identifier':'com.example.app','role':'app','flags':2,'team':'not set'},{'path':'Contents/MacOS/Fulcra Collect','identifier':'com.example.app','role':'app','flags':2,'team':'not set'}]}
    recipe.write_text(json.dumps(data)); policy=s.derive_signature_policy(recipe,expected_recipe_sha256=sha(recipe.read_bytes()))
    e=replace(e,policy=policy,invocation=replace(e.invocation,recipe_sha256=sha(recipe.read_bytes()),policy_sha256=s.policy_digest(policy),tools=e.invocation.tools+(s.ToolIdentity('/usr/bin/clang',s.COMPILER_TOOL_SHA256),)))
    ref=s.prepare_public_inputs(e); e=s.advance(e,ref)
    target=app/destination; target.parent.mkdir(parents=True); target.write_bytes(payload)
    stub=app/'Contents/MacOS/Stub'; stub.parent.mkdir(); stub.write_bytes(signing_pair()[0])
    info=app/'Contents/Info.plist'
    info.write_bytes(plistlib.dumps({'CFBundleIdentifier':'evil' if bad=='info-identifier' else 'com.example.app','CFBundleExecutable':'Fulcra Collect'}))
    if bad!='missing-entitlements': entitlement_file.write_bytes(plistlib.dumps({}) if bad=='entitlements-drift' else entitlement_bytes)
    if bad:
        with pytest.raises(s.Unknown): s.bind_public_inputs(e,ref,app)
        assert e.head.stage=='PREPARED'
    else: assert s.bind_public_inputs(e,ref,app).stage=='BOUND'


def test_same_run_older_receipt_cannot_replace_current_noop_generation(run):
    s,e,old=bound(run)
    new=s.sanitize_stage(e,old,phase='raw',check_only=False)
    e=s.advance(e,new)
    with pytest.raises(s.Unknown): s.load_receipt(e,old)
    assert e.head.stage=='RAW'


def _compiler_copy_binding_case(run,monkeypatch,bad):
    s,e,source,recipe,app,destination,payload=run
    origin,_=signing_pair()
    monkeypatch.setattr(s,'COMPILER_SOURCE_SHA256',sha(source.read_bytes()))
    monkeypatch.setattr(s,'COMPILER_OUTPUT_SHA256',sha(origin))
    monkeypatch.setattr(s.compiler,'SOURCE_SHA',sha(source.read_bytes()))
    monkeypatch.setattr(s.compiler,'LOCAL_TOOL',s.COMPILER_TOOL_SHA256)
    monkeypatch.setattr(s.compiler,'LOCAL_OUTPUT',sha(origin))
    monkeypatch.setattr(s.compiler,'LOCAL',replace(s.compiler.LOCAL,source_sha256=sha(source.read_bytes()),tool_sha256=s.COMPILER_TOOL_SHA256,output_sha256=sha(origin)))
    data=json.loads(recipe.read_text())
    data['compiler_baseline']={'source':str(source),'source_sha256':sha(source.read_bytes()),
        'tool_sha256':s.COMPILER_TOOL_SHA256,'output_sha256':sha(origin)}
    recipe.write_text(json.dumps(data))
    policy=replace(e.policy,targets=e.policy.targets+tuple(s.TargetPolicy(p,'fulcra-collect',None,0x20002,'not set') for p in s.COMPILER_PATHS))
    e=replace(e,policy=policy,invocation=replace(e.invocation,recipe_sha256=sha(recipe.read_bytes()),policy_sha256=s.policy_digest(policy),tools=e.invocation.tools+(s.ToolIdentity('/usr/bin/clang',s.COMPILER_TOOL_SHA256),)))
    ref=s.prepare_public_inputs(e); e=s.advance(e,ref)
    target=app/destination; target.parent.mkdir(parents=True); target.write_bytes(payload)
    for index,path in enumerate(s.COMPILER_PATHS):
        if bad=='missing-copy' and index==1: continue
        leaf=app/path; leaf.parent.mkdir(parents=True,exist_ok=True)
        leaf.write_bytes(origin+(b'x' if bad=='copy-changed' and index==1 else b''))
    if bad=='source-changed': source.write_bytes(b'drift')
    def fake(argv,**kwargs):
        if '--verify' in argv: return s.CommandResult(0,b'',True,True)
        if '--entitlements' in argv: return s.CommandResult(0,b'',True,True,s._entitlement_diagnostic(argv[-1]))
        return s.CommandResult(0,b'Identifier=fulcra-collect\nSignature=adhoc\nTeamIdentifier=not set\nCodeDirectory v=20400 size=1 flags=0x20002(adhoc,linker-signed) hashes=1+0 location=embedded\n',True,True)
    monkeypatch.setattr(s,'_run_process',fake)
    if bad:
        with pytest.raises(s.Unknown): s.bind_public_inputs(e,ref,app)
    else: assert s.bind_public_inputs(e,ref,app).stage=='BOUND'


@pytest.mark.parametrize('bad',[None,'copy-changed','missing-copy','source-changed'])
def test_compiler_copies_are_pinned_and_freshly_checked_before_binding(run,monkeypatch,bad):
    _compiler_copy_binding_case(run,monkeypatch,bad)


def test_signature_metadata_cannot_replace_codedirectory_with_unstructured_flags(run):
    s,e,*_=run
    target=s.TargetPolicy('.','com.example.app',None,2,'not set')
    malformed=b'Identifier=com.example.app\nSignature=adhoc\nTeamIdentifier=not set\nCodeDirectory arbitrary flags=0x2(adhoc) missing-geometry\n'
    with pytest.raises(s.Unknown): s._metadata(malformed,target)


@pytest.mark.parametrize('compiler',[False,True])
def test_pure_stage_fixtures_work_when_darwin_system_tools_are_missing(inputs,tmp_path,monkeypatch,compiler):
    """Fail if fixture construction or pure checks reach host Darwin tools."""
    system_tools={'/usr/bin/codesign','/usr/bin/clang'}
    original_bytes=Path.read_bytes
    original_read=inputs[0]._read_path
    original_stat=os.stat
    def missing(path):
        return str(path) in system_tools
    def guarded_bytes(path):
        if missing(path): raise FileNotFoundError('synthetic missing Darwin tool')
        return original_bytes(path)
    def guarded_read(path,*args,**kwargs):
        if missing(path): raise FileNotFoundError('synthetic missing Darwin tool')
        return original_read(path,*args,**kwargs)
    def guarded_stat(path,*args,**kwargs):
        if missing(path): raise FileNotFoundError('synthetic missing Darwin tool')
        return original_stat(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_bytes',guarded_bytes)
    monkeypatch.setattr(inputs[0],'_read_path',guarded_read)
    monkeypatch.setattr(os,'stat',guarded_stat)
    value=make_run(inputs,tmp_path)
    if compiler:
        _compiler_copy_binding_case(value,monkeypatch,None)
    else:
        s,e,ref=bound(value)
        ref=s.sanitize_stage(e,ref,phase='raw',check_only=False)
        assert s.advance(e,ref).head.stage=='RAW'



def test_default_tool_identity_fails_closed_when_absolute_system_tool_is_missing(inputs,monkeypatch,synthetic_system_tools):
    s=stages()
    _,production_identity=synthetic_system_tools
    real_read=inputs[0]._read_path
    def unavailable(path,*args,**kwargs):
        if str(path)=='/usr/bin/codesign': raise FileNotFoundError('synthetic missing tool')
        return real_read(path,*args,**kwargs)
    monkeypatch.setattr(inputs[0],'_read_path',unavailable)
    with pytest.raises(s.Unknown): production_identity('/usr/bin/codesign')
    with pytest.raises(s.Unknown): production_identity('codesign')


def test_changed_synthetic_tool_identity_does_not_advance_current_receipt(run,synthetic_system_tools):
    s,e,ref=bound(run)
    files,_=synthetic_system_tools
    files['/usr/bin/codesign'].write_bytes(b'changed synthetic tool')
    with pytest.raises(s.Unknown): s.load_receipt(e,ref)
    assert e.head.stage=='BOUND'


def alias_recipe(run,monkeypatch,kind='framework'):
    s,e,source,recipe,app,_,_=run
    config=Path(e.invocation.workspace)/'alias-config.toml'
    config.write_text('[tool.briefcase]\nbundle="com.example"\n[tool.briefcase.app.app]\nformal_name="Fulcra Collect"\n[tool.briefcase.app.app.macOS]\nuniversal_build=false\n')
    for key in ('BRIEFCASE_SOURCE_SHA256','TEMPLATE_SHA256'):
        monkeypatch.setattr(s,key,sha(source.read_bytes()))
    container='Contents/Frameworks/Python.framework' if kind.startswith('framework') else 'Contents/Resources/Helper.app'
    executable=container+('/Versions/3.13/Python' if kind=='framework' else '/Python' if kind=='framework-unversioned' else '/Contents/MacOS/Helper')
    identifier='org.python.Python' if kind.startswith('framework') else 'org.example.Helper'
    origin=Path(e.invocation.workspace)/'origin-Info.plist'
    origin.write_bytes(plistlib.dumps({'CFBundleIdentifier':identifier,'CFBundleExecutable':'Python' if kind.startswith('framework') else 'Helper'}))
    rule=lambda path,ident,role: {'path':path,'identifier':ident,'role':role,'flags':2,'team':'not set'}
    rules=[rule('.','com.example.app','app'),rule('Contents/MacOS/Fulcra Collect','com.example.app','app'),
           rule(container,identifier,'briefcase'),rule(executable,identifier,'briefcase')]
    data={'policy_schema':1,'mode':'adhoc','config':str(config),'config_sha256':sha(config.read_bytes()),
        'briefcase_source':str(source),'briefcase_source_sha256':sha(source.read_bytes()),
        'template':str(source),'template_sha256':sha(source.read_bytes()),
        'targets':rules,'sign_targets':rules,
        'bundle_info_inputs':{container:{'path':str(origin),'sha256':sha(origin.read_bytes()),'executable_path':executable}}}
    recipe.write_text(json.dumps(data))
    return data,container,executable,identifier,origin


@pytest.mark.parametrize('kind',['framework','framework-unversioned','app'])
def test_nested_container_main_executable_uses_final_source_policy(run,monkeypatch,kind):
    s,e,_,recipe,*_=run
    data,container,executable,identifier,origin=alias_recipe(run,monkeypatch,kind)
    policy=s.derive_signature_policy(recipe,expected_recipe_sha256=sha(recipe.read_bytes()))
    for targets in (policy.targets,policy.sign_targets):
        actual={t.path:t for t in targets}
        assert actual[executable].identifier==actual[container].identifier==identifier
        assert actual[executable].entitlements==actual[container].entitlements
        assert actual[executable].flags==actual[container].flags==2
        assert actual[executable].team==actual[container].team=='not set'
    assert any(a.container==container and a.executable==executable for a in policy.aliases)


@pytest.mark.parametrize('bad',['basename','missing-executable-path','escape-version','different-version'])
def test_nested_alias_origin_and_geometry_fail_before_operation(run,monkeypatch,bad):
    s,e,_,recipe,*_=run
    data,container,executable,identifier,origin=alias_recipe(run,monkeypatch)
    if bad=='basename':
        data['targets'][-1]['identifier']='Python'
    elif bad=='missing-executable-path':
        del data['bundle_info_inputs'][container]['executable_path']
    else:
        data['bundle_info_inputs'][container]['executable_path']=container+('/Versions/../Python' if bad=='escape-version' else '/Versions/3.12/Python')
    recipe.write_text(json.dumps(data))
    with pytest.raises(s.Unknown): s.derive_signature_policy(recipe,expected_recipe_sha256=sha(recipe.read_bytes()))


def test_old_basename_alias_policy_cannot_be_accepted(run,monkeypatch):
    s,e,_,recipe,*_=run
    data,container,executable,identifier,origin=alias_recipe(run,monkeypatch)
    del data['bundle_info_inputs'][container]['executable_path']
    data['targets'][-1]['identifier']='Python'
    data['sign_targets'][-1]['identifier']='Python'
    recipe.write_text(json.dumps(data))
    with pytest.raises(s.Unknown): s.derive_signature_policy(recipe,expected_recipe_sha256=sha(recipe.read_bytes()))


def complete_large_expected(run):
    s,e,*_=run
    files=[]
    for index in range(500):
        path=Path(e.invocation.workspace)/f'complete-source-input-{index:04d}.py'
        path.write_text(f'VALUE = {index}\n'); files.append(path)
    blob=plistlib.dumps({'com.apple.security.cs.allow-unsigned-executable-memory':True,
                        'com.apple.security.cs.disable-library-validation':True})
    targets=(s.TargetPolicy('.','com.example.app',blob,2,'not set'),s.TargetPolicy('Contents/MacOS/Fulcra Collect','com.example.app',blob,2,'not set'))+tuple(s.TargetPolicy(f'Contents/Resources/app_packages/library_{index:03d}/native.so',f'native_{index:03d}',blob,2,'not set') for index in range(158))
    policy=s.SignaturePolicy(targets,'adhoc',targets,(s.ContainerAlias('.','Contents/MacOS/Fulcra Collect','Contents/Info.plist',''),))
    return replace(e,source_files=tuple(files),policy=policy,invocation=replace(e.invocation,
        source_sha256=s._source_digest(tuple(files)),policy_sha256=s.policy_digest(policy)))


def test_complete_large_context_roundtrips_before_any_successful_transport(run):
    s=run[0]; expected=complete_large_expected(run)
    ref=s.prepare_public_inputs(expected); expected=s.advance(expected,ref)
    raw=s.context_json(expected)
    assert s.context_from_json(raw)==expected
    assert len(raw.encode())<=32768


@pytest.mark.parametrize('reason',['metadata-cap','exec-capacity'])
def test_untransportable_context_does_not_publish_a_receipt(run,monkeypatch,reason):
    s,e,*_=run
    if reason=='metadata-cap':
        markers=e.markers+('x'*(9*1024*1024),)
        e=replace(e,markers=markers,invocation=replace(e.invocation,markers_sha256=s.digest(s.encode(markers))))
    else:
        original=s.os.sysconf
        monkeypatch.setattr(s.os,'sysconf',lambda name:4096 if name=='SC_ARG_MAX' else original(name))
    with pytest.raises(s.Unknown): s.prepare_public_inputs(e)
    assert list(e.run_dir.glob('*.json'))==[]
    assert e.head.stage=='EMPTY'


def alias_bound_case(run,monkeypatch,kind='framework',bad_geometry=None):
    s,e,source,recipe,app,destination,payload=run
    data,container,executable,identifier,origin=alias_recipe(run,monkeypatch,kind)
    entitlements=Path(e.invocation.workspace)/'alias-Entitlements.plist'
    entitlements.write_bytes(plistlib.dumps({'com.apple.security.cs.allow-unsigned-executable-memory':True,
                                           'com.apple.security.cs.disable-library-validation':True}))
    data['briefcase_entitlements']={'path':str(entitlements),'sha256':sha(entitlements.read_bytes())}
    data['build_argv']=[str(Path(e.invocation.workspace)/'briefcase'),'build','macOS']
    policy=s.derive_signature_policy(recipe,expected_recipe_sha256=sha(recipe.read_bytes()))
    data['entitlements_inputs']={t.path:{'path':str(entitlements),'sha256':sha(entitlements.read_bytes())} for t in policy.sign_targets}
    data['sign_argv']=[[s.CODESIGN,str(app if t.path=='.' else app/t.path),'--force','--sign','-',
        '--identifier',t.identifier,'--options','0','--timestamp=none','--entitlements',str(entitlements)]
        for t in sorted(policy.sign_targets,key=lambda t:(-len(Path(t.path).parts),t.path))]
    recipe.write_text(json.dumps(data))
    e=replace(e,policy=policy,invocation=replace(e.invocation,recipe_sha256=sha(recipe.read_bytes()),policy_sha256=s.policy_digest(policy),tools=e.invocation.tools+(s.ToolIdentity('/usr/bin/clang',s.COMPILER_TOOL_SHA256),)))
    ref=s.prepare_public_inputs(e); e=s.advance(e,ref)
    target=app/destination; target.parent.mkdir(parents=True); target.write_bytes(payload)
    stub=app/'Contents/MacOS/Stub'; stub.parent.mkdir(); stub.write_bytes(signing_pair()[0])
    (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'com.example.app','CFBundleExecutable':'Fulcra Collect'}))
    leaf=app/executable; leaf.parent.mkdir(parents=True); leaf.write_bytes(signing_pair()[0])
    alias=next(a for a in policy.aliases if a.container==container)
    info=app/alias.info_path; info.parent.mkdir(parents=True,exist_ok=True); info.write_bytes(origin.read_bytes())
    for path,value in alias.links:
        link=app/path; link.parent.mkdir(parents=True,exist_ok=True)
        link.symlink_to('3.12' if bad_geometry=='current-version' and path.endswith('/Current') else value)
    if bad_geometry=='missing-current': (app/(container+'/Versions/Current')).unlink()
    if bad_geometry=='info-changed': info.write_bytes(plistlib.dumps({'CFBundleIdentifier':'evil','CFBundleExecutable':'Other'}))
    if bad_geometry=='executable-link': leaf.unlink(); leaf.symlink_to('unknown')
    if bad_geometry=='case-alias': leaf.rename(leaf.with_name(leaf.name.lower()))
    return s,e,ref,data,container,executable,identifier


@pytest.mark.parametrize('kind',['framework','framework-unversioned','app'])
def test_source_alias_policy_completes_fake_controlled_build_and_sign(run,monkeypatch,kind):
    s,e,ref,data,container,executable,identifier=alias_bound_case(run,monkeypatch,kind)
    ref=s.bind_public_inputs(e,ref,e.app); e=s.advance(e,ref)
    ref=s.sanitize_stage(e,ref,phase='raw',check_only=False); e=s.advance(e,ref)
    identifiers={'.':'com.example.app','Contents/MacOS/Fulcra Collect':'com.example.app',container:identifier,executable:identifier}
    observed=[]
    def fake(argv,**kwargs):
        if argv[0]!=s.CODESIGN:
            (e.app/'Contents/MacOS/Stub').rename(e.app/'Contents/MacOS/Fulcra Collect')
            return s.CommandResult(0,b'',True,True)
        if '--sign' in argv:
            observed.append(argv)
            return s.CommandResult(0,b'',True,True)
        if '--verify' in argv: return s.CommandResult(0,b'',True,True)
        relative='.' if argv[-1]==str(e.app) else str(Path(argv[-1]).relative_to(e.app))
        if '--entitlements' in argv:
            return s.CommandResult(0,plistlib.dumps({'com.apple.security.cs.allow-unsigned-executable-memory':True,
                'com.apple.security.cs.disable-library-validation':True}),True,True)
        text='Identifier='+identifiers[relative]+'\nSignature=adhoc\nTeamIdentifier=not set\nCodeDirectory v=20400 size=1 flags=0x2(adhoc) hashes=1+0 location=embedded\n'
        return s.CommandResult(0,text.encode(),True,True)
    monkeypatch.setattr(s,'_run_process',fake)
    ref=s.controlled_build(e,ref,tuple(data['build_argv'])); e=s.advance(e,ref)
    ref=s.sanitize_stage(e,ref,phase='postbuild',check_only=False); e=s.advance(e,ref)
    ref=s.sanitize_stage(e,ref,phase='postbuild',check_only=True); e=s.advance(e,ref)
    ref=s.controlled_sign(e,ref,tuple(tuple(a) for a in data['sign_argv']),e.policy); e=s.advance(e,ref)
    ref=s.sanitize_stage(e,ref,phase='signed',check_only=True); e=s.advance(e,ref)
    assert e.head.stage=='SIGNED_CHECKED'
    leaf=next(a for a in observed if a[1]==str(e.app/executable))
    parent=next(a for a in observed if a[1]==str(e.app/container))
    assert leaf[6]==parent[6]==identifier
    assert observed.index(leaf)<observed.index(parent)


@pytest.mark.parametrize('bad',['current-version','info-changed','executable-link','missing-current','case-alias'])
def test_unbound_current_alias_geometry_stops_before_any_native_operation(run,monkeypatch,bad):
    s,e,ref,*_=alias_bound_case(run,monkeypatch,bad_geometry=bad)
    def forbidden(*args,**kwargs): pytest.fail('unbound geometry must fail before native operation')
    monkeypatch.setattr(s,'_run_process',forbidden)
    with pytest.raises(s.Unknown): s.bind_public_inputs(e,ref,e.app)
    assert e.head.stage=='PREPARED'


@pytest.mark.parametrize('field',['identifier','entitlements','flags','team'])
def test_every_final_alias_policy_field_is_enforced(run,monkeypatch,field):
    s,e,_,recipe,*_=run
    data,container,executable,identifier,origin=alias_recipe(run,monkeypatch)
    policy=s.derive_signature_policy(recipe,expected_recipe_sha256=sha(recipe.read_bytes()))
    replacements={'identifier':'Python','entitlements':plistlib.dumps({'different':True}),'flags':0,'team':'OTHER'}
    targets=tuple(replace(t,**{field:replacements[field]}) if t.path==executable else t for t in policy.sign_targets)
    with pytest.raises(s.Unknown): s._assert_alias_policies(policy,targets)


@pytest.mark.parametrize('generation',[0,64])
def test_complete_context_exec_and_framed_parent_pipe_are_bounded(run,generation):
    import subprocess
    s=run[0]; expected=complete_large_expected(run)
    head=s.Head()
    for number in range(1,generation+1): head=s.Head('SIGNED_CHECKED',number,'a'*64,head)
    expected=replace(expected,head=head)
    context=s.context_json(expected)
    assert len(context.encode())<=32768
    metadata=json.loads(context)['metadata']
    assert metadata['bytes']>256*1024  # Complete metadata exceeds old reader cap.
    assert len(s.context_json(expected).encode())==len(context.encode())
    readfd,writefd=os.pipe()
    env=dict(os.environ,COLLECT_PROVENANCE_CONTEXT=context)
    code='import sys; sys.path.insert(0,sys.argv[1]); import bundle_provenance_stages as s; e=s.expected_from_environment(); s.emit_completion(e,int(sys.argv[2])); print(len(e.source_files),len(e.policy.targets),len(e.policy.sign_targets))'
    process=subprocess.Popen([sys.executable,'-I','-c',code,str(SCRIPTS),str(writefd)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,pass_fds=(writefd,))
    os.close(writefd)
    try: received=s.read_completion(readfd,timeout=5)
    finally: os.close(readfd)
    stdout,stderr=process.communicate(timeout=5)
    assert process.returncode==0,stderr
    assert stdout==b'500 160 160\n'
    assert s.context_from_json(received)==expected
    print('TRANSPORT_METRICS '+json.dumps({'generation':generation,'source_files':len(expected.source_files),'build_targets':len(expected.policy.targets),'sign_targets':len(expected.policy.sign_targets),'context_bytes':len(context.encode()),'metadata_bytes':metadata['bytes'],'local_arg_max':os.sysconf('SC_ARG_MAX'),'child_returncode':process.returncode}))


@pytest.mark.parametrize('bad',['metadata-bytes','metadata-head','metadata-mode','nonce','invocation-digest','generation-overflow'])
def test_context_metadata_cannot_supply_current_authority(run,bad):
    s,e,*_=run
    raw=s.context_json(e); envelope=json.loads(raw)
    path=Path(envelope['metadata']['path'])
    if bad=='metadata-bytes': path.write_bytes(path.read_bytes()+b' ')
    elif bad=='metadata-head':
        value=json.loads(path.read_text()); value['head']={'stage':'SIGNED','generation':99}
        data=s.encode(value); path.write_bytes(data)
        envelope['metadata'].update(bytes=len(data),sha256=sha(data))
    elif bad=='metadata-mode': path.chmod(0o644)
    elif bad=='nonce': envelope['nonce']='f'*64
    elif bad=='invocation-digest': envelope['invocation_sha256']='f'*64
    else: envelope['head']['generation']=65
    with pytest.raises(s.Unknown): s.context_from_json(json.dumps(envelope))


@pytest.mark.parametrize('frame',[b'9\n{}\n',b'2\n{}\ntrailing',b'32769\n{}\n',b'bad\n{}\n'])
def test_completion_reader_rejects_incomplete_or_ambiguous_frames(run,frame):
    s=run[0]; readfd,writefd=os.pipe()
    os.write(writefd,frame); os.close(writefd)
    try:
        with pytest.raises(s.Unknown): s.read_completion(readfd,timeout=.1)
    finally: os.close(readfd)


def test_completion_writer_requires_concurrent_drain_and_preserves_head_on_timeout(run):
    s,e,*_=run
    readfd,writefd=os.pipe(); os.set_blocking(writefd,False)
    try:
        while True:
            try: os.write(writefd,b'x'*4096)
            except BlockingIOError: break
        with pytest.raises(s.Unknown): s.emit_completion(e,writefd,timeout=.01)
        assert e.head==s.Head()
    finally: os.close(readfd); os.close(writefd)


@pytest.mark.parametrize('bad',['regular-file','read-end','not-a-fd'])
def test_invalid_parent_completion_transport_cannot_publish_a_receipt(run,monkeypatch,bad):
    s,e,*_=run
    readfd,writefd=os.pipe()
    owned=e.run_dir/'not-a-pipe'
    filefd=os.open(owned,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        value=str(filefd if bad=='regular-file' else readfd) if bad!='not-a-fd' else 'bad'
        monkeypatch.setenv('COLLECT_PROVENANCE_COMPLETION_FD',value)
        with pytest.raises(s.Unknown): s.prepare_public_inputs(e)
        assert list(e.run_dir.glob('*.json'))==[]
    finally: os.close(readfd); os.close(writefd); os.close(filefd)


@pytest.mark.parametrize('reason',['argv','environment-entry','future-head-growth'])
def test_transport_capacity_failure_precedes_receipt_publication(run,monkeypatch,reason):
    s,e,*_=run
    if reason=='argv': monkeypatch.setattr(sys,'argv',['helper','x'*(16*1024)])
    elif reason=='environment-entry': monkeypatch.setenv('SYNTHETIC_OVERSIZE_ENTRY','x'*(128*1024))
    else:
        assert len(s._context_envelope(e,s._context_metadata(e),e.head))<1024
        monkeypatch.setattr(s,'MAX_CONTEXT_BYTES',1024)
    with pytest.raises(s.Unknown): s.prepare_public_inputs(e)
    assert list(e.run_dir.glob('*.json'))==[]


def test_canonical_helpers_transport_compact_current_head_through_raw_cleanup(run,synthetic_system_tools):
    import subprocess
    s,e,_,_,app,destination,payload=run
    context=s.context_json(e)
    files,_=synthetic_system_tools
    # The tiny child uses the same synthetic catalog and identity boundary as
    # the parent fixture. Production entry points expose no test flags.
    code="""import sys,json,runpy
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import bundle_provenance_stages as stages
files=json.loads(sys.argv[2])
identity=stages._tool_identity
stages._tool_identity=lambda path:identity(files.get(str(path),path))
stages.core._CATALOG_PATH=Path(sys.argv[3])
script=sys.argv[4]; args=sys.argv[5:]
if script.endswith('bundle_provenance_stages.py'):
    raise SystemExit(stages.main(args))
sys.argv=[script,*args]
runpy.run_path(script,run_name='__main__')
"""
    def helper(argv,current):
        result=subprocess.run([sys.executable,'-I','-c',code,str(SCRIPTS),json.dumps({k:str(v) for k,v in files.items()}),str(s.core._CATALOG_PATH),*map(str,argv)],env=dict(os.environ,
            COLLECT_PROVENANCE_CONTEXT=current,PYTHONDONTWRITEBYTECODE='1'),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5)
        assert result.returncode==0,result.stderr
        assert len(result.stdout)<=32769
        return result.stdout.decode().strip()
    context=helper([SCRIPTS/'bundle_provenance_stages.py','prepare-public-inputs'],context)
    expected=s.context_from_json(context); assert expected.head.stage=='PREPARED'
    target=app/destination; target.parent.mkdir(parents=True); target.write_bytes(payload)
    optional=app/'Contents/Resources/app_packages/bin/tool'; optional.parent.mkdir(); optional.write_text('#!'+str(Path.home())+'/private-interpreter\n')
    context=helper([SCRIPTS/'bundle_provenance_stages.py','bind-public-inputs'],context)
    expected=s.context_from_json(context); assert expected.head.stage=='BOUND'
    context=helper([SCRIPTS/'sanitize_bundle.py',app,'--phase','raw','--provenance-receipt',s.current_ref(expected).path],context)
    expected=s.context_from_json(context)
    assert expected.head.stage=='RAW' and not optional.exists()
    assert s.load_receipt(expected,s.current_ref(expected)).stage=='RAW'


def test_controlled_process_argv_capacity_fails_before_spawning(run,monkeypatch):
    s=run[0]
    def forbidden(*args,**kwargs): pytest.fail('oversize argv must fail before process spawn')
    monkeypatch.setattr(s.subprocess,'Popen',forbidden)
    with pytest.raises(s.Unknown):
        s._run_process(('/inert/tool','x'*(16*1024)),timeout=1,cap=1024,cwd=str(run[1].invocation.workspace),env={})


def test_bad_recipe_claims_before_full_decode_and_cannot_retry(run,monkeypatch):
    s,e,_,recipe,*_=run
    e=s.advance(e,s.prepare_public_inputs(e));raw=s.context_json(e);original=recipe.read_bytes()
    recipe.write_bytes(b'corrupt recipe')
    assert hasattr(s,'_frame_compiler_request'), 'ownership-only compiler sender is missing'
    assert s._frame_compiler_request(raw)==raw
    with pytest.raises(s.Unknown):s.context_from_json(raw)
    with pytest.raises(s.Unknown):s.controlled_install_and_bind(raw)
    claim=e.run_dir/'002-BOUND.compiler.attempt'
    assert claim.is_file() and claim.stat().st_mode&0o777==0o600 and claim.stat().st_size<=8192
    observed=claim.read_bytes(); recipe.write_bytes(original)
    with pytest.raises(s.Unknown):s.controlled_install_and_bind(raw)
    assert claim.read_bytes()==observed and e.head.stage=='PREPARED'


@pytest.mark.parametrize('bad',['metadata','lock','directory','envelope'])
def test_bad_ownership_cannot_claim(run,bad):
    s,e,*_=run;e=s.advance(e,s.prepare_public_inputs(e));raw=s.context_json(e)
    assert hasattr(s,'controlled_install_and_bind'), 'controlled compiler entry is missing'
    if bad=='metadata':(e.run_dir/'context-metadata.json').write_bytes(b'{}')
    elif bad=='lock':next(e.run_dir.parent.glob('.collect-app-*.lock')).write_bytes(b'{}')
    elif bad=='directory':e.run_dir.chmod(0o755)
    else:raw=raw[:-2]
    with pytest.raises(s.Unknown):s.controlled_install_and_bind(raw)
    assert not (e.run_dir/'002-BOUND.compiler.attempt').exists()


def test_cloud_first_bound_requires_live_one_use_admission(run,monkeypatch):
    s,e,*_=run;e=s.advance(e,s.prepare_public_inputs(e))
    assert hasattr(s,'_CompilerAdmission'), 'live compiler admission is missing'
    monkeypatch.setattr(s,'_selected_compiler',lambda e:s.compiler.CLOUD)
    ref=s.ReceiptRef(e.run_dir/'002-BOUND.json','f'*64,2,'BOUND',e.head)
    monkeypatch.setattr(s,'load_receipt',lambda *a,**k:pytest.fail('candidate validation before witness'))
    with pytest.raises(s.Unknown):s.advance(e,ref)
    for witness in (True,object(),{'state':'READY'}):
        with pytest.raises(s.Unknown):s.advance(e,ref,_compiler_admission=witness)
    with pytest.raises(s.Unknown):s._CompilerAdmission()


def test_late_failure_keeps_outer_head_and_attempt(run,monkeypatch):
    s,e,*_=run;e=s.advance(e,s.prepare_public_inputs(e));raw=s.context_json(e)
    assert hasattr(s,'_finish_compiler_bootstrap'), 'compiler bootstrap finish missing'
    def fail(bootstrap,attempt):raise s.Unknown('late full decode failure')
    monkeypatch.setattr(s,'_finish_compiler_bootstrap',fail)
    with pytest.raises(s.Unknown):s.controlled_install_and_bind(raw)
    assert e.head.stage=='PREPARED' and (e.run_dir/'002-BOUND.compiler.attempt').exists()
    with pytest.raises(s.Unknown):s.controlled_install_and_bind(raw)


@pytest.fixture
def cloud_run(run,tmp_path,monkeypatch,request):
    """Synthetic OS filesystem and process boundary; real stages/codecs/ledger."""
    s,e,_,recipe,app,destination,payload=run;q=s.compiler
    root=Path(e.invocation.workspace);scripts=root/'packages/menubar/scripts';scripts.mkdir(parents=True)
    files=[]
    for name in ('cli_launcher.c','install_bundle_launchers.py','bundle_provenance_stages.py','bundle_compiler_qualifiers.py','build_macos_app.sh'):
        target=scripts/name;target.write_bytes((SCRIPTS/name).read_bytes());files.append(target)
    workflow=root/'.github/workflows/macos.yml';workflow.parent.mkdir(parents=True);workflow.write_text('fixed fixture workflow\n');files.append(workflow)
    config=root/'config.toml';config.write_text('fixture=1\n');files.append(config)
    output=b'\x00'*34096
    paths=[p for p,_,_ in q.CLOUD.tools];mapped={}
    for index,path in enumerate(paths):
        fake=tmp_path/f'os-tool-{index}';fake.write_bytes(f'tool-{index}'.encode());fake.chmod(0o755);mapped[path]=fake
    wrappers=tuple((p,mapped[p].stat().st_size,sha(mapped[p].read_bytes())) for p in paths[:4])
    effective=tuple((p,mapped[p].stat().st_size,sha(mapped[p].read_bytes())) for p in paths[4:])
    monkeypatch.setattr(q,'WRAPPERS',wrappers);monkeypatch.setattr(q,'EFFECTIVE',effective)
    monkeypatch.setattr(q,'CLOUD',replace(q.CLOUD,tools=wrappers+effective,tool_sha256=wrappers[0][2],output_sha256=sha(output)))
    original_stat=os.stat
    def fake_stat(path,*args,**kwargs):return original_stat(mapped.get(str(path),path),*args,**kwargs)
    monkeypatch.setattr(os,'stat',fake_stat)
    production=s._tool_identity
    def identity(path,*,_compiler_budget=None):
        target=mapped.get(str(path),Path(path))
        if _compiler_budget is None:return production(target)
        size=target.stat().st_size;_compiler_budget.begin('ordinary',str(path),size)
        raw=target.read_bytes();_compiler_budget.charge(len(raw));_compiler_budget.end(True)
        return sha(raw)
    monkeypatch.setattr(s,'_tool_identity',identity)
    sdk=tmp_path/'sdk';sdk.mkdir()
    def resolution(path,*,directory=False):
        target=sdk if path==q.SDK else mapped[path]
        return {'requested':path,'resolved':str(target),'links':[],'identity':q.identity(target.stat())}
    monkeypatch.setattr(q,'_safe_os_path',resolution)
    monkeypatch.setattr(s,'_os_identity',lambda:q.OS)
    monkeypatch.setenv('TMPDIR',str(tmp_path))
    env={k:os.environ[k] for k in ('HOME','TMPDIR','LANG','LC_ALL') if k in os.environ}|{'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','PYTHONDONTWRITEBYTECODE':'1'}
    python=root/'python';python.write_bytes(b'inert python fixture');python.chmod(0o755)
    data=json.loads(recipe.read_text());data.update(config=str(config),config_sha256=sha(config.read_bytes()),
        compiler_baseline={'schema':2,'profile':'cloud-xcode16.4-arm64-v1','source':str(scripts/'cli_launcher.c')},
        compiler_producer={'python':{'path':str(python),'sha256':sha(python.read_bytes())},'workflow':{'path':str(workflow),'sha256':sha(workflow.read_bytes())},'environment':env})
    recipe.write_text(json.dumps(data))
    policy=replace(e.policy,targets=e.policy.targets+tuple(s.TargetPolicy(p,'fulcra-collect',None,0x20002,'not set') for p in s.COMPILER_PATHS))
    linker=None;debug_files=[]
    if getattr(request,'param',None) in {'linker','debug0','debug1'}:
        from test_bundle_native_proof import canonical_linker_alias,thin
        n=s.native_proof;origin=canonical_linker_alias();r=n.requirements_from_expected_aliases((origin,))[0]
        linker_data=b'inert exact linker data'
        info=plistlib.dumps({'CFBundleIdentifier':'org.python.python','CFBundleExecutable':'Python'})
        alias=s.ContainerAlias(origin.container,origin.executable,origin.info_path,sha(info),origin.links)
        key=(alias.container,alias.executable,alias.info_path,alias.info_sha256,alias.links)
        r=replace(r,bytes=len(linker_data),sha256=sha(linker_data))
        monkeypatch.setattr(n,'_LINKER_DATA_POLICIES',((key,(r,)),))
        policy=replace(policy,targets=policy.targets+tuple(s.TargetPolicy(p,'org.python.python',None,2,'not set') for p in (alias.container,alias.executable)),aliases=(alias,))
        linker=(r,alias,info,linker_data,thin())
        if request.param.startswith('debug'):
            from test_bundle_native_proof import canonical_collect_alias
            outer=s.ContainerAlias(**vars(canonical_collect_alias()))
            outerkey=(outer.container,outer.executable,outer.info_path,outer.info_sha256,outer.links)
            origins=[]
            for index,origin in enumerate(n._DEBUG_DATA_ORIGINS):
                raw=b'\xcf\xfa\xed\xfe'+str(index).encode()+b' fixed debug payload'
                record=replace(origin.requirement,bytes=len(raw),sha256=sha(raw))
                origins.append(replace(origin,requirement=record));debug_files.append((record.path,raw))
            monkeypatch.setattr(n,'_DEBUG_DATA_SELECTOR',(key,outerkey))
            monkeypatch.setattr(n,'_DEBUG_DATA_ORIGINS',tuple(origins))
            policy=replace(policy,targets=policy.targets+(replace(policy.targets[0],path=outer.executable),),aliases=(alias,outer))
            debug_files.extend((('Contents/MacOS/Stub',thin()),('Contents/Info.plist',plistlib.dumps({}))))
    tools=tuple(s.ToolIdentity(p,h) for p,_,h in wrappers)+(s.ToolIdentity(str(python),sha(python.read_bytes())),e.invocation.tools[0])
    e=replace(e,source_files=tuple(files),policy=policy,invocation=replace(e.invocation,source_sha256=s._source_digest(tuple(files)),recipe_sha256=sha(recipe.read_bytes()),policy_sha256=s.policy_digest(policy),tools=tools,os_identity=q.OS))
    e=s.advance(e,s.prepare_public_inputs(e))
    target=app/destination;target.parent.mkdir(parents=True);target.write_bytes(payload)
    (app/'Contents/MacOS').mkdir()
    if linker is not None:
        r,alias,info,linker_data,executable=linker
        for name,data in ((r.path,linker_data),(alias.info_path,info),(alias.executable,executable)):
            file=app/name;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(data);file.chmod(0o644)
        for name,target in alias.links:(app/name).symlink_to(target)
    for name,data in debug_files:
        file=app/name;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(data);file.chmod(0o644)
    calls=[]
    def process(argv,**kw):
        calls.append((argv,kw))
        assert kw['env']==env and kw['timeout']<=30 and kw['cap']==1048576
        if argv[0]==str(python):
            assert argv==(str(python),'-I','-B',str(scripts/'install_bundle_launchers.py'),str(app))
            assert kw['cwd']==str(root/'packages/menubar') and kw['_compiler_limit'] is True
            for path in s.COMPILER_PATHS:leaf=app/path;leaf.write_bytes(output);leaf.chmod(0o755)
            return s.CommandResult(0,b'',True,True)
        if argv[0]=='/usr/bin/xcrun':
            path=q.SDK if argv[1]=='--show-sdk-path' else effective[0 if argv[-1]=='clang' else 1][0]
            return s.CommandResult(0,(path+'\n').encode(),True,True)
        if argv[0]=='/usr/bin/clang':return s.CommandResult(0,b'fixture clang version\n',True,True)
        assert argv[0]==s.CODESIGN
        if '--verify' in argv:return s.CommandResult(0,b'',True,True)
        if '--entitlements' in argv:return s.CommandResult(0,b'',True,True,s._entitlement_diagnostic(argv[-1]))
        return s.CommandResult(0,b'Identifier=fulcra-collect\nSignature=adhoc\nTeamIdentifier=not set\nCodeDirectory v=20400 size=1 flags=0x20002(adhoc,linker-signed) hashes=1+0 location=embedded\n',True,True)
    monkeypatch.setattr(s,'_run_process',process)
    return s,e,calls,process


def test_controlled_installer_preserves_exact_three_leaf_delta(cloud_run):
    s,e,calls,_=cloud_run;old=e.head
    result=s.controlled_install_and_bind(s.context_json(e))
    assert e.head==old and result.head.stage=='BOUND' and result.head.generation==2
    assert len(calls)==17
    assert s.context_from_json(s.context_json(result))==result
    assert s.load_receipt(result,s.current_ref(result)).stage=='BOUND'
    proof=json.loads((e.run_dir/'002-BOUND.compiler.json').read_text())
    assert len(proof['native']['commands'])==9
    assert len(proof['tools']['reserved'])==12
    assert proof['producer']['attribution']=='compiler-success-via-pinned-installer-check-true'
    with pytest.raises(s.Unknown):s.advance(e,s.current_ref(result))
    with pytest.raises(s.Unknown):s.controlled_install_and_bind(s.context_json(e))


@pytest.mark.parametrize('bad',['unrelated-output','late-read','workflow','selection','native-nonzero','pipe-held','output-drift'])
def test_controlled_compiler_failure_never_adopts_partial_head(cloud_run,monkeypatch,bad):
    s,e,calls,process=cloud_run
    original=s._tool_identity
    ordinary=0
    def read_tool(path,*,_compiler_budget=None):
        nonlocal ordinary
        if _compiler_budget is not None:
            ordinary+=1
            if bad=='late-read' and ordinary==6*len(e.invocation.tools):raise s.Unknown('tiny final read failed')
        return original(path,_compiler_budget=_compiler_budget)
    monkeypatch.setattr(s,'_tool_identity',read_tool)
    def broken(argv,**kw):
        result=process(argv,**kw)
        if '-I' in argv:
            if bad=='unrelated-output':(e.app/'unexpected').write_bytes(b'wrong')
            if bad=='workflow':(Path(e.invocation.workspace)/'.github/workflows/macos.yml').write_bytes(b'drift')
        if len(calls)==17 and bad=='selection':return replace(result,output=b'/wrong\n')
        if argv[0]==s.CODESIGN:
            if bad=='native-nonzero':return replace(result,returncode=1)
            if bad=='pipe-held':return replace(result,eof=False)
            if bad=='output-drift':(e.app/s.COMPILER_PATHS[0]).write_bytes(b'changed')
        return result
    monkeypatch.setattr(s,'_run_process',broken)
    raw=s.context_json(e)
    with pytest.raises(s.Unknown):s.controlled_install_and_bind(raw)
    assert e.head.stage=='PREPARED' and (e.run_dir/'002-BOUND.compiler.attempt').exists()
    with pytest.raises(s.Unknown):s.controlled_install_and_bind(raw)


@pytest.mark.parametrize('entry',['start','context','current','native'])
def test_cloud_profile_mapping_rejects_actual_but_crossed_tools(cloud_run,monkeypatch,tmp_path,entry):
    s,e,_,_=cloud_run
    crossed=replace(e,invocation=replace(e.invocation,tools=tuple(replace(t,sha256='c'*64) if t.path=='/usr/bin/clang' else t for t in e.invocation.tools)))
    def no_reads(*args,**kwargs):pytest.fail('cross-binding must fail before actual tool reads')
    monkeypatch.setattr(s,'_tool_identity',no_reads)
    with pytest.raises(s.Unknown):
        if entry=='start':
            s.start_invocation(tmp_path/'other-run',workspace=Path(e.invocation.workspace),app=e.app.parent/'new-app',source_files=e.source_files,lock=e.lock,recipe=e.recipe,archives=dict(e.archives),catalog_sha256=e.invocation.catalog_sha256,architecture='arm64',tools=crossed.invocation.tools,policy=e.policy,markers=e.markers)
        elif entry=='context':s.context_json(crossed)
        elif entry=='current':s._current(crossed)
        else:s.verify_signature_policy(crossed,s.inventory(e.app),e.policy,_only=s.COMPILER_PATHS)


@pytest.mark.parametrize('kind',['copy','deepcopy','pickle','reentrant','wrong-ref','final-ledger'])
def test_live_compiler_admission_is_uncloneable_and_poisoned(cloud_run,monkeypatch,kind):
    import copy,pickle
    s,e,_,_=cloud_run;original=s.advance;seen=[]
    def advancing(old,ref,*,_compiler_admission=None):
        if _compiler_admission is None:return original(old,ref)
        w=_compiler_admission;seen.append(w)
        if kind in ('copy','deepcopy','pickle'):
            with pytest.raises(s.Unknown):{'copy':copy.copy,'deepcopy':copy.deepcopy,'pickle':pickle.dumps}[kind](w)
            return original(old,ref,_compiler_admission=w)
        if kind=='reentrant':s._COMPILER_HANDLES[w]['state']='VALIDATING'
        if kind=='wrong-ref':ref=replace(ref,digest='a'*64)
        if kind=='final-ledger':s._COMPILER_HANDLES[w]['budget'].index+=1
        with pytest.raises(s.Unknown):original(old,ref,_compiler_admission=w)
        assert w not in s._COMPILER_HANDLES
        with pytest.raises(s.Unknown):original(old,ref,_compiler_admission=w)
        raise s.Unknown('witness poisoned')
    monkeypatch.setattr(s,'advance',advancing)
    if kind in ('copy','deepcopy','pickle'):assert s.controlled_install_and_bind(s.context_json(e)).head.stage=='BOUND'
    else:
        with pytest.raises(s.Unknown):s.controlled_install_and_bind(s.context_json(e))
    assert seen and seen[0] not in s._COMPILER_HANDLES
    assert e.head.stage=='PREPARED'


@pytest.mark.parametrize('phase',['full-finish','inventory'])
def test_non_tool_source_and_inventory_expiry_precedes_producer(cloud_run,monkeypatch,phase):
    s,e,calls,_=cloud_run;raw=s.context_json(e);clock=[0.0]
    monkeypatch.setattr(s.time,'monotonic',lambda:clock[0])
    name='_finish_compiler_bootstrap' if phase=='full-finish' else 'inventory'
    original=getattr(s,name)
    def slow(*args,**kwargs):
        value=original(*args,**kwargs);clock[0]=301.0;return value
    monkeypatch.setattr(s,name,slow)
    with pytest.raises(s.Unknown):s.controlled_install_and_bind(raw)
    assert not calls and e.head.stage=='PREPARED'
    assert (e.run_dir/'002-BOUND.compiler.attempt').exists()
    assert not (e.run_dir/'002-BOUND.compiler.json').exists()


@pytest.mark.parametrize('phase',['publication','return'])
def test_publication_or_admission_expiry_cannot_return_expected(cloud_run,monkeypatch,phase):
    s,e,_,_=cloud_run;raw=s.context_json(e);clock=[0.0]
    monkeypatch.setattr(s.time,'monotonic',lambda:clock[0])
    if phase=='publication':
        original=s._exclusive
        def delayed(path,raw):
            if path.name=='002-BOUND.compiler.json':clock[0]=301.0
            return original(path,raw)
        monkeypatch.setattr(s,'_exclusive',delayed)
    else:
        original=s._controlled_install_and_bind_claimed
        def delayed(*args):
            result=original(*args);clock[0]=301.0;return result
        monkeypatch.setattr(s,'_controlled_install_and_bind_claimed',delayed)
    with pytest.raises(s.Unknown):s.controlled_install_and_bind(raw)
    assert e.head.stage=='PREPARED' and not s._COMPILER_HANDLES
    assert (e.run_dir/'002-BOUND.compiler.attempt').exists()
    if phase=='publication':assert not (e.run_dir/'002-BOUND.compiler.json').exists()


def test_presented_admission_revoked_on_early_selector_exception(cloud_run,monkeypatch):
    s,e,_,_=cloud_run;original=s.advance;observed=[]
    def advancing(old,ref,*,_compiler_admission=None):
        if _compiler_admission is None:return original(old,ref)
        witness=_compiler_admission;data=s._COMPILER_HANDLES[witness];observed.append(witness)
        def fail(_):raise s.Unknown('recipe/source read failed before admission')
        monkeypatch.setattr(s,'_selected_compiler',fail)
        with pytest.raises(s.Unknown):original(old,ref,_compiler_admission=witness)
        assert witness not in s._COMPILER_HANDLES
        assert data['state']=='POISONED' and set(data)=={'state','pid'}
        raise s.Unknown('poisoned')
    monkeypatch.setattr(s,'advance',advancing)
    try:
        with pytest.raises(s.Unknown):s.controlled_install_and_bind(s.context_json(e))
        assert e.head.stage=='PREPARED' and observed
    finally:
        for witness in observed:s._COMPILER_HANDLES.pop(witness,None)


@pytest.fixture
def linker_run(run,monkeypatch,request):
    from test_bundle_native_proof import canonical_linker_alias,thin,module
    n=module();assert hasattr(n,'requirements_from_expected_aliases'),'mandatory linker record missing'
    s,e,_,_,app,destination,payload=run
    original=canonical_linker_alias();record=n.requirements_from_expected_aliases((original,))[0]
    data=b'\xca\xfe\xba\xbe inert linker bytes /public/marker'
    info=plistlib.dumps({'CFBundleIdentifier':'org.python.python','CFBundleExecutable':'Python'})
    alias=s.ContainerAlias(original.container,original.executable,original.info_path,sha(info),original.links)
    key=(alias.container,alias.executable,alias.info_path,alias.info_sha256,alias.links)
    record=replace(record,bytes=len(data),sha256=sha(data))
    monkeypatch.setattr(n,'_LINKER_DATA_POLICIES',((key,(record,)),))
    targets=e.policy.targets+tuple(s.TargetPolicy(p,'org.python.python',None,2,'not set') for p in (alias.container,alias.executable))
    policy=replace(e.policy,targets=targets,aliases=(alias,))
    extra_files=[];selected_record=record;selected_data=data
    if str(getattr(request,'param','')).startswith('debug'):
        from test_bundle_native_proof import canonical_collect_alias
        from types import SimpleNamespace
        outer=s.ContainerAlias(**vars(canonical_collect_alias()))
        outerkey=(outer.container,outer.executable,outer.info_path,outer.info_sha256,outer.links)
        debug=[]
        for name in ('_machsignals','_objc'):
            member=f'objc/{name}.cpython-313-darwin.so.dSYM/Contents/Resources/DWARF/{name}.cpython-313-darwin.so'
            raw=b'\xcf\xfa\xed\xfe'+name.encode()+b' exact debug payload /public/marker'
            req=n.LinkerDataRequirement('Contents/Resources/app_packages/'+member,len(raw),0o644,sha(raw),'fixture.whl','a'*64,member)
            debug.append(SimpleNamespace(requirement=req));extra_files.append((req.path,raw))
        monkeypatch.setattr(n,'_DEBUG_DATA_SELECTOR',(key,outerkey),raising=False)
        monkeypatch.setattr(n,'_DEBUG_DATA_ORIGINS',tuple(debug),raising=False)
        policy=replace(policy,targets=policy.targets+(replace(policy.targets[0],path=outer.executable),),aliases=(alias,outer))
        extra_files.extend((('Contents/MacOS/Stub',thin()),('Contents/Info.plist',plistlib.dumps({}))))
        index=int(request.param[-1]);selected_record=debug[index].requirement;selected_data=extra_files[index][1]
    e=replace(e,policy=policy,invocation=replace(e.invocation,policy_sha256=s.policy_digest(policy)))
    e=s.advance(e,s.prepare_public_inputs(e))
    for path,raw in ((destination,payload),(alias.executable,thin()),(alias.info_path,info),(record.path,data),*extra_files):
        p=app/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw);p.chmod(0o644)
    for path,target in alias.links:(app/path).symlink_to(target)
    return s,e,selected_record,selected_data


@pytest.mark.parametrize('entry',['bind','verify'])
@pytest.mark.parametrize('bad',['missing','link','directory','special','mode','size','hash','magic'])
def test_required_linker_data_rejects_missing_nonfile_mode_size_hash_and_changed_magic_at_bind_and_verify(linker_run,monkeypatch,entry,bad):
    s,e,r,data=linker_run;p=e.app/r.path
    if bad in {'missing','link','directory','special'}:
        p.unlink()
        if bad=='link':p.symlink_to('other')
        elif bad=='directory':p.mkdir()
        elif bad=='special':os.mkfifo(p)
    elif bad=='mode':p.chmod(0o755)
    elif bad=='size':p.write_bytes(data+b'x')
    elif bad=='hash':p.write_bytes(data[:-1]+b'X')
    else:p.write_bytes(b'xxxx'+data[4:])
    def forbidden(*a,**kw):pytest.fail('required data must fail before native commands')
    monkeypatch.setattr(s,'_run_process',forbidden)
    with pytest.raises(s.Unknown):
        if entry=='bind':s.bind_public_inputs(e,s.current_ref(e),e.app)
        else:s.verify_signature_policy(e,s.inventory(e.app),e.policy)
    assert e.head.stage=='PREPARED'


def test_linker_classification_preserves_native_verification_inventory_and_privacy_rejection(linker_run,monkeypatch):
    s,e,r,data=linker_run
    ref=s.bind_public_inputs(e,s.current_ref(e),e.app);e=s.advance(e,ref)
    entries=s.inventory(e.app);assert any(x.path==r.path for x in entries)
    calls=[];targets={t.path:t for t in e.policy.targets}
    def observe(argv,**kwargs):
        calls.append(argv)
        rel='.' if argv[-1]==str(e.app) else str(Path(argv[-1]).relative_to(e.app))
        target=targets[rel]
        if '--verify' in argv:return s.CommandResult(0,b'',True,True)
        if '--entitlements' in argv:return s.CommandResult(0,target.entitlements or b'',True,True,s._entitlement_diagnostic(argv[-1]) if target.entitlements is None else b'')
        return s.CommandResult(0,f'Identifier={target.identifier}\nSignature=adhoc\nTeamIdentifier=not set\nCodeDirectory v=20400 size=1 flags=0x{target.flags:x}(adhoc) hashes=1+0 location=embedded\n'.encode(),True,True)
    monkeypatch.setattr(s,'_run_process',observe)
    assert s.verify_signature_policy(e,entries,e.policy).complete
    assert len(calls)==10 and all(argv[-1]!=str(e.app/r.path) for argv in calls)
    assert any(argv[-1]==str(e.app/'Contents/Frameworks/Python.framework') for argv in calls)
    assert s.inventory(e.app)==entries
    scan=s.core.scan_bundle(e.app,('/public/marker',))
    assert scan.status=='COMPLETE' and scan.privacy_rejections>=1 and scan.permitted_hits==0
    # A manually declared native row cannot add signature authority for the data.
    extra=replace(e.policy,targets=e.policy.targets+(s.TargetPolicy(r.path,'data',None,2,'not set'),))
    crossed=replace(e,policy=extra,invocation=replace(e.invocation,policy_sha256=s.policy_digest(extra)))
    calls.clear()
    with pytest.raises(s.Unknown):s.verify_signature_policy(crossed,entries,extra)
    assert calls==[]
    for name,raw in (('other.o',data),('fat',b'\xca\xfe\xba\xbeunknown'),('native',b'\xcf\xfa\xed\xfeordinary')):
        path=e.app/name;path.write_bytes(raw)
        assert name in s._policy_targets(e,s.inventory(e.app))
        path.unlink()


@pytest.mark.parametrize('cloud_run',['linker','debug0','debug1'],indirect=True)
@pytest.mark.parametrize('phase',['C0','C1','admission','success'])
def test_cloud_required_linker_data_stays_owned_through_production(cloud_run,monkeypatch,phase,request):
    s,e,calls,process=cloud_run
    record=s.native_proof.requirements_from_expected_aliases(e.policy.aliases)[{'linker':0,'debug0':1,'debug1':2}[request.node.callspec.params['cloud_run']]]
    target=e.app/record.path
    if phase=='C0':target.unlink()
    elif phase=='C1':
        def changed(argv,**kw):
            result=process(argv,**kw)
            if '-I' in argv:target.write_bytes(b'changed')
            return result
        monkeypatch.setattr(s,'_run_process',changed)
    elif phase=='admission':
        original=s.advance
        def changed(old,ref,*,_compiler_admission=None):
            target.unlink()
            return original(old,ref,_compiler_admission=_compiler_admission)
        monkeypatch.setattr(s,'advance',changed)
    if phase=='success':
        result=s.controlled_install_and_bind(s.context_json(e))
        assert result.head.stage=='BOUND' and len(calls)==17
        assert any(entry.path==record.path for entry in s.load_receipt(result,s.current_ref(result)).entries)
    else:
        with pytest.raises(s.Unknown):s.controlled_install_and_bind(s.context_json(e))
    assert e.head.stage=='PREPARED' and (e.run_dir/'002-BOUND.compiler.attempt').exists()
    assert not s._COMPILER_HANDLES
    if phase=='C0':assert calls==[]
    if phase=='C1':assert len(calls)==5


def test_expired_ownership_bootstrap_cannot_claim(run,monkeypatch):
    s,e,*_=run;e=s.advance(e,s.prepare_public_inputs(e));raw=s.context_json(e)
    clock=[0.0];monkeypatch.setattr(s.time,'monotonic',lambda:clock[0])
    original=s._ownership_compiler_context
    def delayed(raw):
        value=original(raw);clock[0]=301.;return value
    monkeypatch.setattr(s,'_ownership_compiler_context',delayed)
    with pytest.raises(s.Unknown):s.controlled_install_and_bind(raw)
    assert not (e.run_dir/'002-BOUND.compiler.attempt').exists()
    assert not s._COMPILER_HANDLES


@pytest.mark.parametrize('linker_run',['debug0','debug1'],indirect=True)
@pytest.mark.parametrize('entry',['bind','verify'])
@pytest.mark.parametrize('bad',['missing','link','directory','special','mode','size','hash','magic','moved','parent-link'])
def test_required_debug_current_data_cannot_disappear(linker_run,monkeypatch,entry,bad):
    s,e,r,data=linker_run;p=e.app/r.path
    if entry=='verify':(e.app/'Contents/MacOS/Stub').rename(e.app/'Contents/MacOS/Fulcra Collect')
    if bad in {'missing','link','directory','special'}:
        p.unlink()
        if bad=='link':p.symlink_to('other')
        elif bad=='directory':p.mkdir()
        elif bad=='special':os.mkfifo(p)
    elif bad=='mode':p.chmod(0o755)
    elif bad=='size':p.write_bytes(data+b'x')
    elif bad=='hash':p.write_bytes(data[:-1]+b'X')
    elif bad=='magic':p.write_bytes(b'xxxx'+data[4:])
    elif bad=='moved':p.rename(p.with_name('moved'))
    else:
        parent=p.parent;replacement=parent.with_name('replaced');parent.rename(replacement);parent.symlink_to(replacement.name)
    def forbidden(*a,**kw):pytest.fail('missing or changed required debug data reached native command')
    monkeypatch.setattr(s,'_run_process',forbidden)
    with pytest.raises(s.Unknown):
        if entry=='bind':s.bind_public_inputs(e,s.current_ref(e),e.app)
        else:s.verify_signature_policy(e,s.inventory(e.app),e.policy)
    assert e.head.stage=='PREPARED'


@pytest.mark.parametrize('linker_run',['debug0','debug1'],indirect=True)
def test_required_debug_data_exact_binding_and_duplicate_inventory(linker_run):
    s,e,r,data=linker_run
    ref=s.bind_public_inputs(e,s.current_ref(e),e.app);owned=s.advance(e,ref)
    entries=s.load_receipt(owned,ref).entries
    record=next(row for row in entries if row.path==r.path)
    assert record.sha256==sha(data) and record.mode==0o644
    assert s.native_proof.classify_signing_input(e.policy.aliases,r.path,data,0o644)=='exact-linker-data'
    assert s.native_proof.classify_signing_input(e.policy.aliases,r.path+'.extra',data,0o644)=='ordinary'
    with pytest.raises(s.native_proof.LinkerDataUnknown):s.native_proof.require_linker_data_inventory(e.policy.aliases,entries+(record,))


@pytest.mark.parametrize('linker_run',['debug0','debug1'],indirect=True)
def test_debug_data_keeps_full_native_and_privacy_verification(linker_run,monkeypatch):
    s,e,r,data=linker_run
    (e.app/'Contents/MacOS/Stub').rename(e.app/'Contents/MacOS/Fulcra Collect')
    entries=s.inventory(e.app);calls=[];targets={t.path:t for t in e.policy.targets}
    def observe(argv,**kwargs):
        calls.append(argv)
        rel='.' if argv[-1]==str(e.app) else str(Path(argv[-1]).relative_to(e.app))
        target=targets[rel]
        if '--verify' in argv:return s.CommandResult(0,b'',True,True)
        if '--entitlements' in argv:return s.CommandResult(0,target.entitlements or b'',True,True,s._entitlement_diagnostic(argv[-1]) if target.entitlements is None else b'')
        return s.CommandResult(0,f'Identifier={target.identifier}\nSignature=adhoc\nTeamIdentifier=not set\nCodeDirectory v=20400 size=1 flags=0x{target.flags:x}(adhoc) hashes=1+0 location=embedded\n'.encode(),True,True)
    monkeypatch.setattr(s,'_run_process',observe)
    assert s.verify_signature_policy(e,entries,e.policy).complete
    assert len(calls)==13 and '--deep' in calls[-1]
    required=s.native_proof.requirements_from_expected_aliases(e.policy.aliases)
    assert len(required)==3 and all(argv[-1]!=str(e.app/r.path) for argv in calls for r in required)
    assert s.inventory(e.app)==entries
    scan=s.core.scan_bundle(e.app,('/public/marker',))
    assert scan.status=='COMPLETE' and scan.privacy_rejections>=3 and scan.permitted_hits==0
