"""Narrow invocation-owned BUILD/sign and existing sanitizer boundaries.

Receipts transport evidence; independently owned Expected/head values authorize
consumption. Missing/lost authority raises Unknown. No release policy is provided.
"""
from dataclasses import asdict, dataclass, replace
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import selectors
import signal
import stat
import subprocess
import time
from bundle_operation_deadline import (compiler_operation, current_operation_deadline,
    OperationDeadlineExceeded)

import bundle_provenance as core
import bundle_native_proof as native_proof
import sys
if __name__ == "__main__": sys.modules["bundle_provenance_stages"] = sys.modules[__name__]
import bundle_compiler_qualifiers as compiler

MAX_RECEIPT = 256 * 1024
MAX_INVENTORY = 8 * 1024 * 1024
MAX_CONTEXT_METADATA = 8 * 1024 * 1024
MAX_CONTEXT_BYTES = 32 * 1024
MAX_TRANSPORT_ARGV = 16 * 1024
COMPLETION_SECONDS = 5.0
MAX_COMPLETION_FRAME = MAX_CONTEXT_BYTES + 16
CODESIGN = '/usr/bin/codesign'
EMPTY_DIGEST = '0' * 64
STAGES = {'PREPARED': {'EMPTY'}, 'BOUND': {'PREPARED'}, 'RAW': {'BOUND'},
          'BUILD_SIGNED': {'RAW'}, 'POSTBUILD': {'BUILD_SIGNED'},
          'POSTBUILD_CHECKED': {'POSTBUILD'},
          'RUNTIME': {'POSTBUILD_CHECKED', 'SIGNED'},
          'CLEANED': {'RUNTIME'}, 'SIGNED': {'POSTBUILD_CHECKED','CLEANED','SIGNED_CHECKED'},
          'SIGNED_CHECKED': {'SIGNED'}}


class Unknown(RuntimeError):
    """Incomplete/unsupported evidence; caller must retain its current head."""


def require(value):
    if not value:
        raise Unknown('UNKNOWN: provenance scope, authority, input, policy, or transition')


def _operation_check():
    operation=current_operation_deadline()
    if operation is not None:
        try: operation.check()
        except OperationDeadlineExceeded as exc: raise Unknown(str(exc)) from exc


def _operation_clamp(deadline):
    _operation_check()
    operation=current_operation_deadline()
    return min(operation.expires,deadline) if operation is not None else deadline


def digest(raw):
    _operation_check()
    value=hashlib.sha256(raw).hexdigest()
    _operation_check()
    return value


def encode(value):
    _operation_check()
    raw=json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    _operation_check()
    return raw


def read(path, limit=MAX_RECEIPT):
    try:
        return core._read_path(Path(path), limit, _operation_clamp(time.monotonic()+60))
    except (OSError, core._Unknown) as exc:
        raise Unknown('UNKNOWN: bounded stable no-follow read') from exc


def _tool_identity(path, *, _compiler_budget=None):
    """Stable no-follow executable identity at the system-tool boundary.

    Production always reads the declared absolute path. Pure tests replace only
    this boundary with synthetic files, leaving argv and policy enforcement real.
    """
    require(Path(path).is_absolute())
    if _compiler_budget is not None: return compiler.ordinary_tool_identity(path,_compiler_budget)
    raw=read(path,128*1024*1024)
    info=os.stat(path,follow_symlinks=False)
    require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) & 0o111)
    return digest(raw)


@dataclass(frozen=True)
class ToolIdentity:
    path: str
    sha256: str


@dataclass(frozen=True)
class TargetPolicy:
    path: str
    identifier: str
    entitlements: bytes | None  # None means positively observed absence, not unreadability.
    flags: int
    team: str


@dataclass(frozen=True)
class ContainerAlias:
    container: str
    executable: str
    info_path: str
    info_sha256: str
    links: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SignaturePolicy:
    targets: tuple[TargetPolicy, ...]
    identity: str = 'adhoc'
    sign_targets: tuple[TargetPolicy, ...] = ()
    aliases: tuple[ContainerAlias, ...] = ()


def policy_data(policy):
    return {'identity': policy.identity, 'targets': [dict(path=t.path, identifier=t.identifier,
        entitlements=t.entitlements.hex() if t.entitlements is not None else None,
        flags=t.flags, team=t.team) for t in policy.targets],
        'sign_targets': [dict(path=t.path,identifier=t.identifier,
        entitlements=t.entitlements.hex() if t.entitlements is not None else None,
        flags=t.flags,team=t.team) for t in policy.sign_targets],
        'aliases':[asdict(a) for a in policy.aliases]}


def policy_digest(policy):
    return digest(encode(policy_data(policy)))


@dataclass(frozen=True)
class Invocation:
    nonce: str
    source_sha256: str
    lock_sha256: str
    catalog_sha256: str
    recipe_sha256: str
    tools: tuple[ToolIdentity, ...]
    workspace: str
    app: str
    architecture: str
    policy_sha256: str
    markers_sha256: str
    os_identity: tuple[str, ...]


@dataclass(frozen=True)
class Head:
    stage: str = 'EMPTY'
    generation: int = 0
    digest: str = EMPTY_DIGEST
    predecessor: 'Head | None' = None


@dataclass(frozen=True)
class ReceiptRef:
    path: Path
    digest: str
    generation: int
    stage: str
    predecessor: Head


@dataclass(frozen=True)
class Expected:
    invocation: Invocation
    head: Head
    run_dir: Path
    source_files: tuple[Path, ...]
    lock: Path
    recipe: Path
    archives: tuple[tuple[str, Path], ...]
    policy: SignaturePolicy
    markers: tuple[str, ...]

    @property
    def app(self):
        return Path(self.invocation.app)


@dataclass(frozen=True)
class ValidatedReceipt:
    stage: str
    generation: int
    entries: tuple[core.InventoryEntry, ...]
    signature_state: str
    signature_operation: str
    public_inputs: core.VerifiedPublicInputs
    controlled_fields: tuple = ()


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: bytes
    eof: bool
    reaped: bool
    stderr: bytes = b''


@dataclass(frozen=True)
class SignatureObservation:
    complete: bool
    inventory_sha256: str
    commands_sha256: str


@dataclass(frozen=True)
class BoundaryRef:
    expected: Expected
    predecessor: ReceiptRef
    operation: str
    entries: tuple[core.InventoryEntry, ...]
    seal: object = None


_BOUNDARY_SEAL = object()


def inventory(app):
    _operation_check()
    entries=[]
    result=core.scan_bundle(app, (), _inventory=entries)
    _operation_check()
    require(result.status == 'COMPLETE')
    return tuple(entries)


def inventory_digest(entries):
    return digest(encode([asdict(e) for e in entries]))


def _source_digest(files):
    require(files and len(files) <= 20000)
    deadline=_operation_clamp(time.monotonic()+60); total=0; values=[]
    for path in files:
        require(time.monotonic()<deadline)
        try: raw=core._read_path(path,core.MAX_FILE_BYTES,deadline)
        except (OSError,core._Unknown) as exc: raise Unknown('UNKNOWN: current source read') from exc
        total+=len(raw); require(total<=core.MAX_TOTAL_BYTES)
        values.append((str(path),digest(raw)))
    return digest(encode(values))


def start_invocation(run_dir, *, workspace, app, source_files, lock, recipe,
                     archives, catalog_sha256, architecture, tools, policy, markers):
    """Trusted entry only, before CREATE. Return I plus EMPTY head, never a receipt.

    The caller must retain this value outside candidate receipt data. The scope
    lock is deliberately not auto-stolen after a crash: recovery starts fresh.
    """
    workspace=Path(workspace).absolute(); app=Path(app).absolute()
    require(app.is_relative_to(workspace) and app != workspace and not app.exists())
    require(architecture == 'arm64' and core._hex(catalog_sha256))
    require(type(markers) is tuple and all(isinstance(m,str) and m for m in markers))
    markers=tuple(dict.fromkeys((*markers,*required_markers())))
    source_files=tuple(Path(p).absolute() for p in source_files)
    require(all(p.is_relative_to(workspace) for p in source_files))
    require(len(set(source_files)) == len(source_files))
    require(tools and len(tools)<=16 and len({t.path for t in tools}) == len(tools))
    _profile_mapping(_json(read(recipe)),workspace,tools,policy)
    for tool in tools:
        require(_tool_identity(tool.path) == tool.sha256)
    nonce=secrets.token_hex(32)
    invocation=Invocation(nonce,_source_digest(source_files),digest(read(lock,16*1024*1024)),
        catalog_sha256,digest(read(recipe)),tuple(tools),str(workspace),str(app),architecture,
        policy_digest(policy),digest(encode(markers)),_os_identity())
    run_dir=Path(run_dir).absolute()
    require(not run_dir.is_relative_to(workspace))
    try:
        run_dir.mkdir(mode=0o700)
        fd=core._open_directory(run_dir)
        os.close(fd)
        lockpath=run_dir.parent / ('.collect-app-'+digest(os.fsencode(app))+'.lock')
        _exclusive(lockpath,encode({'nonce':nonce,'app':str(app)}))
    except (OSError,core._Unknown) as exc:
        raise Unknown('UNKNOWN: exclusive private invocation/scope lock unavailable') from exc
    expected=Expected(invocation,Head(),run_dir,source_files,Path(lock).absolute(),
                    Path(recipe).absolute(),tuple(sorted((n,Path(p).absolute()) for n,p in archives.items())),policy,markers)
    _context_preflight(expected)
    return expected


def required_markers():
    return tuple(dict.fromkeys(v for v in (str(Path.home()),os.environ.get('COLLECT_QA_ROOT'),os.environ.get('RUNNER_TEMP')) if v))


def _os_identity():
    value=os.uname()
    return (value.sysname,value.release,value.version,value.machine)


def _current_unchecked(e, *, _compiler_budget=None):
    require(isinstance(e,Expected) and isinstance(e.invocation,Invocation))
    i=e.invocation
    require(i.os_identity==_os_identity())
    require(core._hex(i.nonce) and i.workspace == str(Path(i.workspace).absolute()))
    require(e.app.is_relative_to(Path(i.workspace)) and e.app != Path(i.workspace))
    require(_source_digest(e.source_files) == i.source_sha256)
    require(digest(read(e.lock,16*1024*1024)) == i.lock_sha256)
    require(digest(read(core._CATALOG_PATH)) == i.catalog_sha256)
    require(digest(read(e.recipe)) == i.recipe_sha256 and policy_digest(e.policy) == i.policy_sha256)
    recipe=_json(read(e.recipe))
    _profile_mapping(recipe,Path(i.workspace),i.tools,e.policy)
    if 'policy_schema' in recipe:
        require(derive_signature_policy(e.recipe,expected_recipe_sha256=i.recipe_sha256)==e.policy)
    require(digest(encode(e.markers)) == i.markers_sha256 and set(required_markers())<=set(e.markers))
    require(stat.S_IMODE(e.run_dir.stat(follow_symlinks=False).st_mode) == 0o700)
    fd=core._open_directory(e.run_dir); os.close(fd)
    lockpath=e.run_dir.parent / ('.collect-app-'+digest(os.fsencode(e.app))+'.lock')
    require(read(lockpath) == encode({'nonce':i.nonce,'app':i.app}))
    for tool in i.tools:
        require((_tool_identity(tool.path) if _compiler_budget is None else _tool_identity(tool.path,_compiler_budget=_compiler_budget)) == tool.sha256)
    public=core.load_public_inputs(dict(e.archives),lock=e.lock,architecture=i.architecture,expected_catalog_sha256=i.catalog_sha256)
    require(public.complete)
    return public


def _current(e, *, _compiler_budget=None):
    try:
        return _current_unchecked(e,_compiler_budget=_compiler_budget)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, core._Unknown) as exc:
        raise Unknown('UNKNOWN: current invocation facts incomplete') from exc


def _exclusive(path, raw):
    """Atomic publication using same-directory temp + exclusive hard link."""
    _operation_check()
    require(len(raw) <= MAX_INVENTORY)
    try: fd=core._open_directory(path.parent)
    except (OSError,core._Unknown) as exc: raise Unknown('UNKNOWN: private publication scope') from exc
    name='.pending-'+secrets.token_hex(16)
    opened=None
    try:
        opened=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
        view=memoryview(raw)
        while view:
            _operation_check()
            n=os.write(opened,view); _operation_check(); require(n>0); view=view[n:]
        os.fsync(opened); _operation_check(); os.close(opened); opened=None
        _operation_check()
        os.link(name,path.name,src_dir_fd=fd,dst_dir_fd=fd,follow_symlinks=False)
        os.fsync(fd); _operation_check()
    except OSError as exc:
        raise Unknown('UNKNOWN: receipt publication failed or generation already exists') from exc
    finally:
        if opened is not None: os.close(opened)
        try: os.unlink(name,dir_fd=fd)
        except FileNotFoundError: pass
        os.close(fd)


def _head(value):
    require(isinstance(value,dict) and set(value)=={'stage','generation','digest','predecessor'})
    require(type(value['generation']) is int and 0 <= value['generation'] <= 64)
    require(core._hex(value['digest']))
    return Head(value['stage'],value['generation'],value['digest'],_head(value['predecessor']) if value['predecessor'] else None)


def _json(raw):
    try:
        _operation_check()
        value=json.loads(raw,object_pairs_hook=core._unique_json)
        _operation_check()
        return value
    except (ValueError,TypeError,core._Unknown) as exc: raise Unknown('UNKNOWN: incomplete or ambiguous JSON') from exc


def _emit(e,stage,entries,*,signature_state='UNSIGNED',signature_operation='none',input_entries=(),controls=(),command=None):
    require(e.head.stage in STAGES[stage] and e.head.generation < 64)
    generation=e.head.generation+1
    _context_preflight(replace(e,head=Head(stage,generation,EMPTY_DIGEST,e.head)))
    path=e.run_dir / f'{generation:03d}-{stage}.json'
    invraw=encode([asdict(entry) for entry in entries])
    require(len(invraw) <= MAX_INVENTORY)
    invpath=path.with_suffix('.inventory.json')
    doc={'schema':1,'invocation':asdict(e.invocation),'stage':stage,'generation':generation,
         'predecessor':asdict(e.head),'input_inventory_sha256':inventory_digest(input_entries),
         'output_inventory_sha256':digest(invraw),'inventory':invpath.name,
         'signature_state':signature_state,'signature_operation':signature_operation,'controlled_fields':controls,
         'command':command}
    raw=encode(doc); require(len(raw)<=MAX_RECEIPT)
    _exclusive(invpath,invraw); _exclusive(path,raw)
    return ReceiptRef(path,digest(raw),generation,stage,e.head)


def advance(e,ref,*,_compiler_admission=None):
    """First cloud BOUND needs a live operation witness before candidate H exists."""
    witness=_compiler_admission
    # A presented live capability enters its revocation guard before any selector
    # or source read can fail; a saved ref alone still cannot create a Head.
    if witness is not None:
        return _admit_compiler(e,ref,witness)
    if ref.stage=='BOUND' and _selected_compiler(e)==compiler.CLOUD:
        return _admit_compiler(e,ref,None)
    require(ref.predecessor == e.head and e.head.stage in STAGES.get(ref.stage,set()))
    require(ref.generation == e.head.generation+1 and ref.path == e.run_dir/f'{ref.generation:03d}-{ref.stage}.json')
    candidate=replace(e,head=Head(ref.stage,ref.generation,ref.digest,e.head))
    load_receipt(candidate,ref)
    return candidate


def load_receipt(e,candidate):
    return _load_receipt(e,candidate)


def _load_receipt(e,candidate,*,_compiler_budget=None,_admission=None):
    """Expected comes from current orchestration, never parsed from candidate."""
    try:
        public=_current(e,_compiler_budget=_compiler_budget)
        require(isinstance(candidate,ReceiptRef) and isinstance(e.head,Head))
        require((candidate.stage,candidate.generation,candidate.digest,candidate.predecessor)==
            (e.head.stage,e.head.generation,e.head.digest,e.head.predecessor))
        require(candidate.path == e.run_dir/f'{e.head.generation:03d}-{e.head.stage}.json')
        require(stat.S_IMODE(candidate.path.stat(follow_symlinks=False).st_mode)==0o600)
        raw=read(candidate.path); require(digest(raw)==e.head.digest)
        doc=_json(raw)
        require(set(doc)=={'schema','invocation','stage','generation','predecessor','input_inventory_sha256',
            'output_inventory_sha256','inventory','signature_state','signature_operation','controlled_fields','command'})
        require(doc['schema']==1 and doc['invocation']==json.loads(encode(asdict(e.invocation))))
        require(doc['stage']==e.head.stage and doc['generation']==e.head.generation)
        predecessor=_head(doc['predecessor']); require(predecessor==e.head.predecessor)
        require(predecessor.stage in STAGES.get(e.head.stage,set()) and predecessor.generation+1==e.head.generation)
        require(doc['inventory']==candidate.path.with_suffix('.inventory.json').name)
        require(stat.S_IMODE((e.run_dir/doc['inventory']).stat(follow_symlinks=False).st_mode)==0o600)
        invraw=read(e.run_dir/doc['inventory'],MAX_INVENTORY)
        require(digest(invraw)==doc['output_inventory_sha256'])
        values=_json(invraw); require(isinstance(values,list) and len(values)<=core.MAX_APP_ENTRIES)
        entries=tuple(core.InventoryEntry(**v) for v in values)
        require(tuple(sorted(entries,key=lambda x:x.path))==entries and len({x.path for x in entries})==len(entries))
        require(doc['signature_state'] in {'UNSIGNED','VERIFIED','DIRTY'})
        require(doc['signature_operation'] in {'none','build','sign'})
        require(doc['signature_state']!='VERIFIED' or doc['signature_operation']!='none')
        controls=tuple((p,dict(v)) for p,v in doc['controlled_fields'])
        if e.head.stage=='PREPARED': require(not entries and not controls)
        else:
            require(inventory(e.app)==entries)
            _prepared_app_policy(e,_json(read(e.recipe)),entries)  # exact signer output first, before broader proof
            phase='raw' if e.head.stage in {'BOUND','RAW'} else 'postbuild'
            if doc['signature_state']=='VERIFIED': verify_signature_policy(e,entries,e.policy,operation=doc['signature_operation'])
            _scan(e,public,entries,phase,controls,markers=() if e.head.stage=='BOUND' else None)
        if e.head.generation>=2 and _selected_compiler(e)==compiler.CLOUD:
            _load_compiler_proof(e,doc,_admission=_admission)
        _current(e,_compiler_budget=_compiler_budget)
        if e.head.stage!='PREPARED': require(inventory(e.app)==entries)
        return ValidatedReceipt(e.head.stage,e.head.generation,entries,doc['signature_state'],doc['signature_operation'],public,controls)
    except (OSError,ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError,core._Unknown) as exc:
        raise Unknown('UNKNOWN: receipt or current authority incomplete') from exc


def _scan(e,public,entries,phase,controls=(),markers=None):
    permit=core._stage_permission(e.app,phase,entries,controls)
    result=core.scan_bundle(e.app,e.markers if markers is None else markers,public_inputs=public,phase=phase,_stage=permit)
    require(result.status=='COMPLETE' and result.privacy_rejections==0)
    return result


def prepare_public_inputs(e):
    _current(e); require(e.head==Head() and not e.app.exists())
    return _emit(e,'PREPARED',())


def _prepared_app_policy(e,recipe,entries):
    _require_linker_data(e,entries)
    _validate_alias_geometry(e,entries)
    if 'policy_schema' not in recipe: return
    import tomllib
    config=tomllib.loads(read(recipe['config']).decode('utf-8'))['tool']['briefcase']
    name,app=next(iter(config['app'].items()))
    info=plistlib.loads(read(e.app/'Contents/Info.plist'))
    require(info['CFBundleIdentifier']==config['bundle']+'.'+name)
    require(info['CFBundleExecutable']==app['formal_name']=='Fulcra Collect')
    item=recipe.get('briefcase_entitlements')
    require(isinstance(item,dict) and set(item)=={'path','sha256'})
    raw=read(item['path']); require(digest(raw)==item['sha256'])
    outer=[target for target in e.policy.targets if target.path=='.']
    require(len(outer)==1 and outer[0].entitlements is not None)
    require(_plist_equal(_entitlement_map(raw),_entitlement_map(outer[0].entitlements)))


def bind_public_inputs(e,predecessor,app):
    require(_selected_compiler(e)!=compiler.CLOUD)
    prior=load_receipt(e,predecessor); require(prior.stage=='PREPARED' and Path(app).absolute()==e.app)
    entries=inventory(e.app)
    _scan(e,prior.public_inputs,entries,'raw',markers=())
    recipe=_json(read(e.recipe))
    _prepared_app_policy(e,recipe,entries)
    if any(t.path in COMPILER_PATHS and t.entitlements is None for t in e.policy.targets):
        baseline=_compiler_baseline(recipe)
        for path in COMPILER_PATHS:
            require(any(t.path==path and t.identifier=='fulcra-collect' and t.flags==0x20002 and t.entitlements is None for t in e.policy.targets))
            require(digest(read(e.app/path,core.MAX_FILE_BYTES))==baseline['output_sha256'])
        verify_signature_policy(e,entries,e.policy,_only=COMPILER_PATHS)
    return _emit(e,'BOUND',entries,input_entries=prior.entries)


def _optional(path):
    prefix='Contents/Resources/app_packages/'
    if not path.startswith(prefix): return False
    parts=path[len(prefix):].split('/')
    return ('__pycache__' in parts or path.endswith(('.pyc','.pyo')) or parts[0] in {'bin','PyObjCTest','_pytest','pytest','.pytest_cache'}
        or 'tests' in parts or re.fullmatch(r'pytest(?:_mock)?-[^/]+\.dist-info',parts[0]) is not None
        or (len(parts)==2 and parts[0]=='pyarrow' and parts[1].startswith('_pyarrow_cpp_tests.')))


def _allowed_boundary(before,after,operation):
    old={e.path:e for e in before}; new={e.path:e for e in after}
    for path in set(old)|set(new):
        if old.get(path)==new.get(path): continue
        if operation=='runtime':
            require(path not in old and path.startswith('Contents/Resources/app_packages/'))
            entry=new[path]
            require((entry.kind=='directory' and Path(path).name=='__pycache__') or
                (entry.kind=='file' and path.endswith(('.pyc','.pyo')) and not entry.mode & 0o111))
        else:
            require(path not in new and _optional(path))


def sanitize_stage(e,predecessor,*,phase,check_only):
    _context_preflight(e)
    prior=load_receipt(e,predecessor)
    if phase=='raw': require(prior.stage=='BOUND' and not check_only); stage='RAW'
    elif phase=='postbuild':
        require(prior.stage==('POSTBUILD' if check_only else 'BUILD_SIGNED'))
        stage='POSTBUILD_CHECKED' if check_only else 'POSTBUILD'
    else:
        require(phase=='signed' and check_only and prior.stage=='SIGNED' and prior.signature_state=='VERIFIED')
        verify_signature_policy(e,prior.entries,e.policy,operation='sign'); stage='SIGNED_CHECKED'
    from sanitize_bundle import _cleanup
    _cleanup(e.app,check_only=check_only)
    after=inventory(e.app)
    if check_only: require(after==prior.entries)
    else: _allowed_boundary(prior.entries,after,'cleanup')
    _scan(e,prior.public_inputs,after,phase,prior.controlled_fields)
    _current(e); require(inventory(e.app)==after)
    state='DIRTY' if after!=prior.entries else prior.signature_state
    return _emit(e,stage,after,signature_state=state,signature_operation=prior.signature_operation,input_entries=prior.entries,controls=prior.controlled_fields)


def boundary_before(e,predecessor,operation):
    _context_preflight(e)
    require(operation in {'runtime','cleanup'})
    prior=load_receipt(e,predecessor)
    require(prior.stage in STAGES['RUNTIME' if operation=='runtime' else 'CLEANED'])
    return BoundaryRef(e,predecessor,operation,prior.entries,_BOUNDARY_SEAL)


def boundary_after(e,boundary,actual_result,operation):
    require(isinstance(boundary,BoundaryRef) and boundary.seal is _BOUNDARY_SEAL and boundary.expected==e and boundary.operation==operation)
    _successful(actual_result); public=_current(e)
    after=inventory(e.app); _allowed_boundary(boundary.entries,after,operation)
    # Existing signed payload proof is re-derived, never permission on caches.
    raw=read(boundary.predecessor.path); require(digest(raw)==boundary.predecessor.digest)
    doc=_json(raw); controls=tuple(doc['controlled_fields'])
    _scan(e,public,after,'postbuild',controls)
    stage='RUNTIME' if operation=='runtime' else 'CLEANED'
    return _emit(e,stage,after,signature_state='DIRTY' if after!=boundary.entries else doc['signature_state'],signature_operation=doc['signature_operation'],input_entries=boundary.entries,controls=controls)


def _successful(result):
    require(isinstance(result,CommandResult) and result.returncode==0 and result.eof and result.reaped)


def _compiler_child_limits():
    import resource
    require(os.geteuid()!=0 and hasattr(resource,'RLIMIT_FSIZE'))
    soft,hard=resource.getrlimit(resource.RLIMIT_FSIZE)
    require(all(value==resource.RLIM_INFINITY or value>=2097152 for value in (soft,hard)))
    resource.setrlimit(resource.RLIMIT_FSIZE,(2097152,2097152))
    require(resource.getrlimit(resource.RLIMIT_FSIZE)==(2097152,2097152))
    os.umask(0o22)


def _run_process(argv,*,timeout,cap,cwd,env,_compiler_limit=False):
    """Bound combined output and actual EOF, including pipe-holding children."""
    require(timeout>0 and cap>0 and Path(argv[0]).is_absolute())
    _exec_capacity(argv,env)
    deadline=_operation_clamp(time.monotonic()+timeout)
    operation=current_operation_deadline()
    if operation is not None:
        deadline=min(deadline,operation.expires-1.0)  # bounded kill/reap reserve
        require(deadline>time.monotonic())
    try:
        proc=subprocess.Popen(argv,cwd=cwd,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True,preexec_fn=_compiler_child_limits if _compiler_limit else None)
    except (OSError,subprocess.SubprocessError) as exc:
        raise Unknown('UNKNOWN: pinned tool unavailable or child limit refused') from exc
    output=bytearray(); errors=bytearray(); selector=selectors.DefaultSelector()
    selector.register(proc.stdout,selectors.EVENT_READ,output); selector.register(proc.stderr,selectors.EVENT_READ,errors)
    good=False
    try:
        os.set_blocking(proc.stdout.fileno(),False); os.set_blocking(proc.stderr.fileno(),False)
        while selector.get_map():
            remaining=deadline-time.monotonic(); require(remaining>0)
            for key,_ in selector.select(min(remaining,0.1)):
                data=os.read(key.fd,min(65536,cap-len(output)-len(errors)+1))
                if data: key.data.extend(data); require(len(output)+len(errors)<=cap)
                else: selector.unregister(key.fileobj)
        remaining=deadline-time.monotonic(); require(remaining>0)
        code=proc.wait(timeout=remaining); _operation_check(); good=True
        return CommandResult(code,bytes(output),True,True,bytes(errors))
    except (OSError,subprocess.SubprocessError) as exc:
        raise Unknown('UNKNOWN: command failed or exceeded bound') from exc
    finally:
        try:
            if not good:
                try: os.killpg(proc.pid,signal.SIGKILL)
                except ProcessLookupError: pass
                reap=1.0 if operation is None else max(0.0,min(1.0,operation.expires-time.monotonic()))
                try: proc.wait(timeout=reap)
                except subprocess.TimeoutExpired as exc: raise Unknown('UNKNOWN: killed group leader not reaped') from exc
        finally:
            selector.close(); proc.stdout.close(); proc.stderr.close()


def _targets(entries):
    targets={'.'}
    for entry in entries:
        if entry.kind=='directory' and entry.path.endswith(('.framework','.app')): targets.add(entry.path)
    return targets


def _require_linker_data(e,entries):
    """Expected source policy requires presence and a fresh stable current read."""
    try:
        records=native_proof.require_linker_data_inventory(e.policy.aliases,entries)
        for record in records:
            _operation_check()
            path=e.app/record.path;before=path.stat(follow_symlinks=False)
            require(stat.S_ISREG(before.st_mode) and stat.S_IMODE(before.st_mode)==record.mode and before.st_size==record.bytes)
            raw=read(path,core.MAX_FILE_BYTES)
            require(compiler.identity(before)==compiler.identity(path.stat(follow_symlinks=False)))
            require(native_proof.classify_signing_input(e.policy.aliases,record.path,raw,stat.S_IMODE(before.st_mode))=='exact-linker-data')
        return records
    except (OSError,ValueError,core._Unknown) as exc:
        raise Unknown('UNKNOWN: required source linker data') from exc


def _policy_targets(e,entries):
    _require_linker_data(e,entries)
    targets=_targets(entries)
    for entry in entries:
        if entry.kind=='file':
            raw=read(e.app/entry.path,core.MAX_FILE_BYTES)
            try: classification=native_proof.classify_signing_input(e.policy.aliases,entry.path,raw,entry.mode)
            except native_proof.LinkerDataUnknown as exc: raise Unknown(str(exc)) from exc
            if classification=='exact-linker-data': continue
            require(raw[:4] not in {b'\xfe\xed\xfa\xcf',b'\xfe\xed\xfa\xce',b'\xce\xfa\xed\xfe',b'\xbe\xba\xfe\xca',b'\xca\xfe\xba\xbf',b'\xbf\xba\xfe\xca'})
            if raw[:4] in {b'\xcf\xfa\xed\xfe',b'\xca\xfe\xba\xbe'}: targets.add(entry.path)
    return targets


def _metadata(raw,target):
    try: text=raw.decode('utf-8')
    except UnicodeError as exc: raise Unknown('UNKNOWN: malformed platform metadata') from exc
    def one(pattern):
        values=re.findall(pattern,text,re.MULTILINE)
        require(len(values)==1); return values[0]
    require(one(r'^Identifier=(.*)$')==target.identifier)
    require(one(r'^Signature=(.*)$')=='adhoc')
    require(one(r'^TeamIdentifier=(.*)$')==target.team)
    require(not re.search(r'^Authority=',text,re.MULTILINE))
    require(int(one(r'^CodeDirectory v=[0-9a-fA-F]+ size=[0-9]+ flags=0x([0-9a-fA-F]+)\([^\n]*\) hashes=[0-9]+\+[0-9]+ location=embedded$'),16)==target.flags)


def _entitlement_map(raw):
    import xml.etree.ElementTree as ET
    try:
        require(raw.lstrip().startswith((b'<?xml',b'<plist')))
        root=ET.fromstring(raw); require(root.tag=='plist' and len(root)==1 and root[0].tag=='dict')
        def unique(node):
            if node.tag=='dict':
                children=list(node); require(len(children)%2==0)
                keys=[]
                for index in range(0,len(children),2):
                    require(children[index].tag=='key'); keys.append(children[index].text)
                    unique(children[index+1])
                require(len(keys)==len(set(keys)))
            elif node.tag=='array':
                for child in node: unique(child)
        unique(root[0]); value=plistlib.loads(raw); require(isinstance(value,dict))
        return value
    except (ET.ParseError,ValueError,plistlib.InvalidFileException,OverflowError) as exc:
        raise Unknown('UNKNOWN: malformed or ambiguous entitlement plist') from exc


def _plist_equal(left,right):
    # Python's True == 1 must not equate different entitlement types.
    if type(left) is not type(right): return False
    if isinstance(left,dict): return left.keys()==right.keys() and all(_plist_equal(left[k],right[k]) for k in left)
    if isinstance(left,list): return len(left)==len(right) and all(_plist_equal(a,b) for a,b in zip(left,right))
    return left==right


def _entitlement_diagnostic(path):
    return ('Executable='+path+'\n'+
        "warning: Specifying ':' in the path is deprecated and will not work in a future release\n").encode()


def verify_signature_policy(e,entries,policy,*,operation='build',_only=None,_compiler_budget=None):
    require(policy.identity=='adhoc' and policy_digest(policy)==e.invocation.policy_sha256)
    require(entries==inventory(e.app))
    require(operation in {'build','sign'})
    targets=policy.sign_targets if operation=='sign' and policy.sign_targets else policy.targets
    _assert_alias_policies(policy,targets)
    if _only is None: require({t.path for t in targets}==_policy_targets(e,entries))
    else:
        _require_linker_data(e,entries)
        require(operation=='build' and set(_only)<={t.path for t in targets})
        targets=tuple(t for t in targets if t.path in _only)
    require(targets and len({t.path for t in targets})==len(targets))
    require(any(t.path==CODESIGN for t in e.invocation.tools))
    _profile_mapping(_json(read(e.recipe)),Path(e.invocation.workspace),e.invocation.tools,e.policy)
    for tool in e.invocation.tools:
        require((_tool_identity(tool.path) if _compiler_budget is None else _tool_identity(tool.path,_compiler_budget=_compiler_budget))==tool.sha256)
    command_digests=[]; deadline=_operation_clamp(time.monotonic()+300)
    for target in sorted(targets,key=lambda t:(-len(Path(t.path).parts),t.path)):
        require(target.path=='.' or (not Path(target.path).is_absolute() and '..' not in Path(target.path).parts))
        path=str(e.app if target.path=='.' else e.app/target.path)
        for args in [('--verify','--strict'),('-d','--verbose=4'),('-d','--entitlements',':-')]:
            remaining=deadline-time.monotonic(); require(remaining>0)
            result=(_run_process((CODESIGN,*args,path),timeout=min(30,remaining),cap=1024*1024,cwd=e.invocation.workspace,env=_tool_env()) if _compiler_budget is None else _compiler_budget.command((CODESIGN,*args,path),cwd=str(Path(e.invocation.workspace)/'packages/menubar'),env=compiler.environment(_json(read(e.recipe)))))
            _successful(result); require(len(result.output)+len(result.stderr)<=1024*1024)
            command_digests.append(digest(encode([CODESIGN,*args,path])+result.output+result.stderr))
            if '--verbose=4' in args: _metadata(result.output+result.stderr,target)
            elif '--entitlements' in args:
                if target.entitlements is None:
                    require(result.output==b'' and result.stderr==_entitlement_diagnostic(path))
                else:
                    try:
                        observed=_entitlement_map(result.output); expected=_entitlement_map(target.entitlements)
                    except (ValueError,plistlib.InvalidFileException,OverflowError) as exc:
                        raise Unknown('UNKNOWN: unreadable entitlement observation') from exc
                    require(result.stderr in {b'',_entitlement_diagnostic(path)})
                    require(isinstance(observed,dict) and isinstance(expected,dict) and _plist_equal(observed,expected))
            require(inventory(e.app)==entries)
    if _only is None:
        remaining=deadline-time.monotonic(); require(remaining>0)
        deep=_run_process((CODESIGN,'--verify','--deep','--strict',str(e.app)),timeout=min(30,remaining),cap=1024*1024,cwd=e.invocation.workspace,env=_tool_env())
        _successful(deep); require(len(deep.output)+len(deep.stderr)<=1024*1024)
        command_digests.append(digest(deep.output+deep.stderr))
    require(time.monotonic()<deadline and inventory(e.app)==entries)
    return SignatureObservation(True,inventory_digest(entries),digest(encode(command_digests)))


def _tool_env():
    # No credentials or wheelhouse-selected codesign. HOME remains actual HOME.
    return {k:v for k,v in os.environ.items() if k in {'HOME','TMPDIR','LANG','LC_ALL','SYSTEMROOT'}} | {'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','PYTHONDONTWRITEBYTECODE':'1'}


def _alias_policy_equal(left,right):
    require((left.identifier,left.flags,left.team)==(right.identifier,right.flags,right.team))
    require((left.entitlements is None)==(right.entitlements is None))
    if left.entitlements is not None:
        require(_plist_equal(_entitlement_map(left.entitlements),_entitlement_map(right.entitlements)))


def _assert_alias_policies(policy,targets):
    bypath={t.path:t for t in targets}
    require(len(bypath)==len(targets))
    containers={p for p in bypath if p.endswith(('.framework','.app'))}
    if 'Contents/MacOS/Fulcra Collect' in bypath: containers.add('.')
    require({a.container for a in policy.aliases}==containers)
    require(len({a.container for a in policy.aliases})==len(policy.aliases))
    require(len({a.executable for a in policy.aliases})==len(policy.aliases))
    for alias in policy.aliases:
        require(alias.container in bypath and alias.executable in bypath)
        _alias_policy_equal(bypath[alias.container],bypath[alias.executable])


def _read_alias_link(app,path):
    full=app/path; fd=None
    try:
        fd=core._open_directory(full.parent)
        before=os.stat(full.name,dir_fd=fd,follow_symlinks=False)
        require(stat.S_ISLNK(before.st_mode))
        value=os.readlink(full.name,dir_fd=fd)
        require(core._identity(before)==core._identity(os.stat(full.name,dir_fd=fd,follow_symlinks=False)))
        return value
    except (OSError,core._Unknown) as exc:
        raise Unknown('UNKNOWN: container alias link missing or unreadable') from exc
    finally:
        if fd is not None: os.close(fd)


def _validate_alias_geometry(e,entries):
    _assert_alias_policies(e.policy,e.policy.targets)
    if e.policy.sign_targets: _assert_alias_policies(e.policy,e.policy.sign_targets)
    scoped={entry.path:entry for entry in entries} if e.policy.aliases else {}
    for alias in e.policy.aliases:
        executable=alias.executable
        if alias.container=='.' and e.head.stage in {'PREPARED','BOUND','RAW'}:
            executable='Contents/MacOS/Stub'
        require(alias.container in scoped and scoped[alias.container].kind=='directory')
        require(executable in scoped and scoped[executable].kind=='file')
        require(alias.info_path in scoped and scoped[alias.info_path].kind=='file')
        raw=read(e.app/executable,core.MAX_FILE_BYTES)
        require(raw[:4] in {b'\xcf\xfa\xed\xfe',b'\xca\xfe\xba\xbe'})
        if alias.info_sha256:
            require(digest(read(e.app/alias.info_path))==alias.info_sha256)
        for path,target in alias.links:
            require(path in scoped and scoped[path].kind=='link' and scoped[path].link==target)
            require(_read_alias_link(e.app,path)==target)


def _sign_plan(e,recipe):
    targets=e.policy.sign_targets or e.policy.targets
    _assert_alias_policies(e.policy,targets)
    require(targets and e.policy.identity=='adhoc')
    entitlements=recipe.get('entitlements_inputs',{})
    commands=[]
    for target in sorted(targets,key=lambda t:(-len(Path(t.path).parts),t.path)):
        require(target.flags==2 and target.team=='not set')
        path=str(e.app if target.path=='.' else e.app/target.path)
        command=(CODESIGN,path,'--force','--sign','-','--identifier',target.identifier,
                 '--options','0','--timestamp=none')
        if target.entitlements is not None:
            item=entitlements.get(target.path); require(isinstance(item,dict) and set(item)=={'path','sha256'})
            raw=read(item['path']); require(digest(raw)==item['sha256'])
            require(_plist_equal(_entitlement_map(raw),_entitlement_map(target.entitlements)))
            command+=('--entitlements',item['path'])
        else: require(target.path not in entitlements)
        commands.append(command)
    require(recipe.get('sign_argv')==[list(c) for c in commands])
    return tuple(commands)


def _controlled(e,predecessor,argv,kind):
    _context_preflight(e)
    prior=load_receipt(e,predecessor)
    require(prior.stage in ({'RAW'} if kind=='build' else STAGES['SIGNED']))
    require(isinstance(argv,tuple) and argv)
    recipe=_json(read(e.recipe))
    if kind=='build':
        require(all(isinstance(a,str) for a in argv) and recipe.get('build_argv')==list(argv))
        require(len(argv)==3 and Path(argv[0]).name=='briefcase' and argv[1:]==('build','macOS'))
        require(any(t.path==argv[0] for t in e.invocation.tools))
        plan=(argv,)
    else:
        plan=_sign_plan(e,recipe)
        require(argv==plan)
    require(any(t.path==CODESIGN for t in e.invocation.tools))
    for command in plan: _exec_capacity(command,_tool_env())
    _current(e)
    before=inventory(e.app); require(before==prior.entries)
    _prepared_app_policy(e,recipe,before)
    current_targets=_policy_targets(e,before)
    if kind=='build' and 'Contents/MacOS/Stub' in current_targets:
        current_targets.remove('Contents/MacOS/Stub'); current_targets.add('Contents/MacOS/Fulcra Collect')
    expected_targets=e.policy.sign_targets if kind=='sign' and e.policy.sign_targets else e.policy.targets
    require(current_targets=={t.path for t in expected_targets})
    snapshots={}
    operation_targets=e.policy.sign_targets if kind=='sign' and e.policy.sign_targets else e.policy.targets
    for entry in before:
        if entry.kind=='file' and (entry.path in {t.path for t in operation_targets} or (kind=='build' and entry.path=='Contents/MacOS/Stub' and 'Contents/MacOS/Fulcra Collect' in {t.path for t in operation_targets})):
            snapshots[entry.path]=read(e.app/entry.path,core.MAX_FILE_BYTES)
    require(sum(len(v) for v in snapshots.values())<=core.MAX_TOTAL_BYTES)
    operation_cwd=Path(e.invocation.workspace)/recipe.get('operation_cwd','.')
    require(operation_cwd.is_relative_to(Path(e.invocation.workspace)) and '..' not in operation_cwd.parts)
    command_deadline=time.monotonic()+(900 if kind=='build' else 300)
    result_digests=[]
    for command in plan:
        remaining=command_deadline-time.monotonic(); require(remaining>0)
        result=_run_process(command,timeout=min(900 if kind=='build' else 30,remaining),
            cap=16*1024*1024 if kind=='build' else 1024*1024,cwd=str(operation_cwd),env=_tool_env())
        _successful(result)
        result_digests.append(digest(result.output+result.stderr))
    _current(e); after=inventory(e.app)
    # Derive only supported public pair fields from the owned current output;
    # structural parser still bounds and validates them, never treats as policy.
    controls=list(prior.controlled_fields)
    _controlled_changes(e,prior.public_inputs,before,after,kind,controls,snapshots)
    controls=tuple(sorted(dict(controls).items()))
    _scan(e,prior.public_inputs,after,'postbuild',controls)
    observation=verify_signature_policy(e,after,e.policy,operation=kind)
    require(inventory(e.app)==after)
    return _emit(e,'BUILD_SIGNED' if kind=='build' else 'SIGNED',after,signature_state='VERIFIED',signature_operation=kind,
        input_entries=before,controls=tuple(controls),command={'argv_sha256':digest(encode(argv)),
        'output_sha256':digest(encode(result_digests)),'observation_sha256':observation.commands_sha256})


def _controlled_changes(e,public,before,after,kind,controls,snapshots):
    old={v.path:v for v in before}; new={v.path:v for v in after}
    alias={'Contents/MacOS/Stub':'Contents/MacOS/Fulcra Collect'} if kind=='build' else {}
    expected_paths={alias.get(p,p) for p in old}
    # Signature resources are exact paths declared by pinned policy recipe,
    # never a blanket _CodeSignature exception.
    recipe=_json(read(e.recipe)); resources=recipe.get('signature_resources',[])
    require(isinstance(resources,list) and all(isinstance(p,str) and p.endswith('/_CodeSignature/CodeResources') for p in resources))
    resource_dirs={str(Path(p).parent) for p in resources}
    require(set(new)-expected_paths <= set(resources)|resource_dirs)
    require(expected_paths-set(new) <= set(resources)|resource_dirs)
    for path in resources:
        if path in new: require(new[path].kind=='file' and new[path].mode==0o644)
    for path in resource_dirs:
        if path in new: require(new[path].kind=='directory' and new[path].mode==0o755)
    public_dest={alias.get(m.destination,m.destination):m for m in public.members}
    target_paths={t.path for t in (e.policy.sign_targets if kind=='sign' and e.policy.sign_targets else e.policy.targets) if t.path!='.'}
    for path,entry in old.items():
        dest=alias.get(path,path); current=new.get(dest)
        if current==replace(entry,path=dest): continue
        if dest in resources: continue
        require(current is not None and current.kind==entry.kind=='file' and current.mode==entry.mode)
        require(dest in target_paths and path in snapshots)
        raw=read(e.app/dest,core.MAX_FILE_BYTES)
        fields=_linkedit_fields(raw)
        proof=core.prove_public_pair(snapshots[path],raw,'signed',controlled_fields=fields)
        require(proof.complete)
        if dest in public_dest:
            require(core.prove_public_pair(public_dest[dest].origin,raw,'signed',controlled_fields=fields).complete)
            controls.append((dest,fields))


def _linkedit_fields(raw):
    import struct
    fields={}
    # Exact thin-only observations; unsupported FAT mutation stays UNKNOWN.
    if raw[:4]==b'\xcf\xfa\xed\xfe' and len(raw)>=32:
        count,size=struct.unpack_from('<II',raw,16); pos=32
        require(count<=4096 and pos+size<=len(raw))
        for _ in range(count):
            require(pos+8<=32+size); command,length=struct.unpack_from('<II',raw,pos)
            require(length>=8 and pos+length<=32+size)
            if command==0x19 and length>=72 and raw[pos+8:pos+24].rstrip(b'\0')==b'__LINKEDIT':
                require(not fields)
                fields={'linkedit_vmsize':struct.unpack_from('<Q',raw,pos+32)[0],
                        'linkedit_filesize':struct.unpack_from('<Q',raw,pos+48)[0]}
            pos+=length
        require(pos==32+size)
    return fields


def controlled_build(e,predecessor,pinned_argv):
    return _controlled(e,predecessor,pinned_argv,'build')


def controlled_sign(e,predecessor,pinned_argv,policy):
    require(policy==e.policy)
    return _controlled(e,predecessor,pinned_argv,'sign')


BRIEFCASE_SOURCE_SHA256='b56e724daa5593fd33fc6046e654dabfca7d4446d8c7992449d236bef14a892e'
TEMPLATE_SHA256='5e54ee522354426c020f75f661f406a7f4c6273c9d2667d3e26c452b6e9d579d'
COMPILER_SOURCE_SHA256='9df6e0d1e35d56e0daaacca800f3cb6ed449eb3d1942f3bca2c92c681909d0e5'
COMPILER_TOOL_SHA256='b8763cf250e607a778bb4603cecb5b90338814d0a3dfcba0d57b1de242f610e9'
COMPILER_OUTPUT_SHA256='f69e1f665d9436f551bdac836caf530a93ae319626e887751ef221f33b8bac1a'
COMPILER_PATHS=tuple('Contents/MacOS/'+name for name in ('fulcra-collect','fulcra','fulcra-api'))


def _compiler_baseline(recipe):
    source=Path(recipe.get('compiler_baseline',{}).get('source',''))
    selected=compiler.select_compiler_qualifier(recipe,source.parents[3] if len(source.parents)>3 else Path('.'))
    return {'source':str(source),'source_sha256':selected.source_sha256,
        'tool_sha256':selected.tool_sha256,'output_sha256':selected.output_sha256}


def _derive_container_aliases_unchecked(recipe,rules):
    paths={rule['path'] for rule in rules}
    nested={path for path in paths if path.endswith(('.framework','.app'))}
    inputs=recipe.get('bundle_info_inputs',{})
    require(isinstance(inputs,dict) and set(inputs)==nested)
    aliases=[]
    for container in sorted(nested):
        require(container.startswith('Contents/') and core._safe_path(container))
        pinned=inputs[container]
        require(isinstance(pinned,dict) and set(pinned)=={'path','sha256','executable_path'})
        origin=read(pinned['path']); require(digest(origin)==pinned['sha256'])
        info=plistlib.loads(origin)
        identifier=info['CFBundleIdentifier']; name=info['CFBundleExecutable']
        require(isinstance(identifier,str) and identifier and isinstance(name,str) and name and '/' not in name and '\\' not in name and name not in {'.','..'})
        executable=pinned['executable_path']; require(core._safe_path(executable))
        require(executable in paths and executable.startswith(container+'/'))
        relative=Path(executable).relative_to(container).parts
        links=()
        if container.endswith('.app'):
            require(relative==('Contents','MacOS',name))
            info_path=container+'/Contents/Info.plist'
        elif relative==(name,):
            info_path=container+'/Resources/Info.plist'
        else:
            require(len(relative)==3 and relative[0]=='Versions' and relative[1]!='Current' and relative[2]==name)
            version=relative[1]
            info_path=container+'/Versions/'+version+'/Resources/Info.plist'
            links=((container+'/Versions/Current',version),
                (container+'/'+name,'Versions/Current/'+name),
                (container+'/Resources','Versions/Current/Resources'))
        container_rules=[rule for rule in rules if rule['path']==container]
        require(len(container_rules)==1 and container_rules[0]['identifier']==identifier)
        aliases.append(ContainerAlias(container,executable,info_path,pinned['sha256'],links))
    if '.' in paths and any(rule['path']=='.' and rule['role']=='app' for rule in rules):
        require('Contents/MacOS/Fulcra Collect' in paths)
    if 'Contents/MacOS/Fulcra Collect' in paths:
        require('.' in paths)
        aliases.append(ContainerAlias('.','Contents/MacOS/Fulcra Collect','Contents/Info.plist',''))
    return tuple(sorted(aliases,key=lambda a:a.container))


def _derive_container_aliases(recipe,rules):
    try: return _derive_container_aliases_unchecked(recipe,rules)
    except (OSError,ValueError,KeyError,TypeError,AttributeError,core._Unknown) as exc:
        raise Unknown('UNKNOWN: unbound container/main-executable geometry') from exc


def derive_signature_policy(recipe_path,*,expected_recipe_sha256):
    """Source/template/config determine expected maps before any operation.

    Manifest entries are exact targets, never observed signature metadata.
    Unknown compiler/tool/template forms require review and fail closed.
    """
    import tomllib
    raw=read(recipe_path); require(digest(raw)==expected_recipe_sha256)
    recipe=_json(raw)
    require(recipe.get('policy_schema')==1 and recipe.get('mode')=='adhoc')
    require(digest(read(recipe['briefcase_source'],128*1024*1024))==recipe['briefcase_source_sha256']==BRIEFCASE_SOURCE_SHA256)
    require(digest(read(recipe['template']))==recipe['template_sha256']==TEMPLATE_SHA256)
    config=read(recipe['config']); require(digest(config)==recipe['config_sha256'])
    data=tomllib.loads(config.decode('utf-8'))['tool']['briefcase']
    apps=data['app']; require(len(apps)==1)
    name,app=next(iter(apps.items())); mac=app.get('macOS',{})
    require(mac.get('universal_build') is False)
    entitlements={'com.apple.security.cs.allow-unsigned-executable-memory':True,
                  'com.apple.security.cs.disable-library-validation':True}
    permissions=app.get('permission',{})
    require(isinstance(permissions,dict))
    known={'camera':'com.apple.security.device.camera','microphone':'com.apple.security.device.microphone',
        'background_location':'com.apple.security.personal-information.location',
        'fine_location':'com.apple.security.personal-information.location',
        'coarse_location':'com.apple.security.personal-information.location',
        'photo_library':'com.apple.security.personal-information.photo_library'}
    require(set(permissions)<=set(known))
    for key,value in permissions.items():
        if value: entitlements[known[key]]=True
    overrides=mac.get('entitlement',{}); require(isinstance(overrides,dict)); entitlements.update(overrides)
    bundle_id=data['bundle']+'.'+name
    aliases=_derive_container_aliases(recipe,recipe['targets'])
    if 'sign_targets' in recipe: require(_derive_container_aliases(recipe,recipe['sign_targets'])==aliases)
    alias_by_executable={a.executable:a for a in aliases}
    def derive(rules,operation):
        targets=[]
        for rule in rules:
            require(set(rule)=={'path','identifier','role','flags','team'})
            role=rule['role']; path=rule['path']
            require(role in {'briefcase','compiler','app','adhoc-absent'})
            require(path=='.' or core._safe_path(path))
            require(type(rule['flags']) is int and rule['team']=='not set')
            identifier=rule['identifier']; blob=plistlib.dumps(entitlements)
            if role=='app':
                require(path in {'.','Contents/MacOS/Fulcra Collect'} and identifier==bundle_id and rule['flags']==2)
            elif role=='compiler':
                require(operation=='build' and path in COMPILER_PATHS)
                _compiler_baseline(recipe)
                require(identifier=='fulcra-collect' and rule['flags']==0x20002)
                blob=None
            else:
                require(rule['flags']==2)
                if path in alias_by_executable:
                    container=alias_by_executable[path].container
                    container_rule=next(rule for rule in rules if rule['path']==container)
                    require(identifier==container_rule['identifier'])
                elif path.endswith(('.framework','.app')):
                    require(any(a.container==path for a in aliases))
                else: require(identifier==Path(path).name)
                if role=='adhoc-absent':
                    require(operation=='sign'); blob=None
            require(isinstance(identifier,str) and identifier and '\n' not in identifier)
            targets.append(TargetPolicy(path,identifier,blob,rule['flags'],rule['team']))
        require(targets and len(targets)<=core.MAX_APP_ENTRIES and len({t.path for t in targets})==len(targets))
        return tuple(targets)
    targets=derive(recipe['targets'],'build')
    signed=derive(recipe['sign_targets'],'sign') if 'sign_targets' in recipe else ()
    policy=SignaturePolicy(targets,'adhoc',signed,aliases)
    _assert_alias_policies(policy,targets)
    if signed: _assert_alias_policies(policy,signed)
    return policy


def _context_metadata(e):
    value=asdict(e); del value['head']
    value['policy']=policy_data(e.policy)
    value['run_dir']=str(e.run_dir)
    value['source_files']=[str(p) for p in e.source_files]
    value['lock']=str(e.lock); value['recipe']=str(e.recipe)
    value['archives']=[(n,str(p)) for n,p in e.archives]
    raw=encode(value)
    require(len(raw)<=MAX_CONTEXT_METADATA)
    return raw


def _context_envelope(e,metadata,head):
    return encode({'schema':2,'nonce':e.invocation.nonce,
        'invocation_sha256':digest(encode(asdict(e.invocation))),
        'metadata':{'path':str(e.run_dir/'context-metadata.json'),'bytes':len(metadata),'sha256':digest(metadata)},
        'head':asdict(head)})


def _exec_capacity(argv,environment):
    require(isinstance(environment,dict) and all(isinstance(value,str) for value in argv))
    encoded_env=[os.fsencode(k+'='+v) for k,v in environment.items()]
    encoded_argv=[os.fsencode(value) for value in argv]
    require(all(len(value)+1<=128*1024 for value in (*encoded_env,*encoded_argv)))
    require(sum(len(value)+1 for value in encoded_argv)<=MAX_TRANSPORT_ARGV)
    try: capacity=os.sysconf('SC_ARG_MAX')
    except (OSError,ValueError) as exc: raise Unknown('UNKNOWN: exec transport capacity unavailable') from exc
    require(type(capacity) is int and capacity>0)
    # Reserve a full bounded argv, pointer table and shebang/OS margin.
    needed=sum(len(value)+1 for value in encoded_env)+MAX_TRANSPORT_ARGV+8*(len(encoded_env)+len(encoded_argv)+4)+8192
    require(needed<=capacity)


def _environment_capacity(raw):
    import sys
    require(len(raw)<=MAX_CONTEXT_BYTES)
    environment={k:v for k,v in os.environ.items() if k!='COLLECT_PROVENANCE_CONTEXT'}
    environment['COLLECT_PROVENANCE_CONTEXT']=raw.decode('utf-8')
    _exec_capacity(sys.argv,environment)


def _context_preflight(e):
    require(type(e) is Expected)
    _validate_context_recipe(e)
    inherited=os.environ.get('COLLECT_PROVENANCE_COMPLETION_FD')
    if inherited is not None:
        require(inherited.isascii() and inherited.isdigit())
        fd=int(inherited); require(fd>=3); _require_pipe(fd,writing=True)
    require(type(e.head.generation) is int and 0<=e.head.generation<=64)
    require(e.run_dir.is_absolute() and '..' not in e.run_dir.parts)
    require(len(os.fsencode(e.run_dir/'context-metadata.json'))<=core.MAX_PATH_BYTES)
    metadata=_context_metadata(e)
    require(_expected_from_metadata(_json(metadata),_head(_json(encode(asdict(e.head)))))==e)
    future=e.head
    for generation in range(e.head.generation+1,65):
        future=Head('POSTBUILD_CHECKED',generation,EMPTY_DIGEST,future)
    reserved=_context_envelope(e,metadata,future)
    require(len(reserved)<=MAX_CONTEXT_BYTES)
    _environment_capacity(reserved)
    current=_context_envelope(e,metadata,e.head)
    require(len(current)<=MAX_CONTEXT_BYTES)
    return metadata,current


def context_json(e):
    """Compact independently retained I/H plus an exact immutable metadata ref.

    Metadata carries no head. Its bytes never establish current authority: the
    live trusted entry/completion envelope owns nonce, I digest, metadata digest
    and H independently of any candidate receipt/file.
    """
    metadata,envelope=_context_preflight(e)
    path=e.run_dir/'context-metadata.json'
    try:
        info=path.stat(follow_symlinks=False)
    except FileNotFoundError:
        _exclusive(path,metadata)
    else:
        require(stat.S_IMODE(info.st_mode)==0o600 and read(path,MAX_CONTEXT_METADATA)==metadata)
    require(read(path,MAX_CONTEXT_METADATA)==metadata)
    raw=envelope.decode()
    require(context_from_json(raw)==e)
    return raw


def _structural_from_metadata(value,head):
    require(set(value)=={'invocation','run_dir','source_files','lock','recipe','archives','policy','markers'})
    i=dict(value['invocation']); i['os_identity']=tuple(i['os_identity']); i['tools']=tuple(ToolIdentity(**t) for t in i['tools'])
    policy=value['policy']; require(set(policy)=={'identity','targets','sign_targets','aliases'})
    def targets(values):
        require(isinstance(values,list) and len(values)<=core.MAX_APP_ENTRIES)
        for target in values:
            require(set(target)=={'path','identifier','entitlements','flags','team'})
            require(all(isinstance(target[k],str) for k in ('path','identifier','team')) and type(target['flags']) is int)
            require(target['entitlements'] is None or isinstance(target['entitlements'],str))
        return tuple(TargetPolicy(t['path'],t['identifier'],bytes.fromhex(t['entitlements']) if t['entitlements'] is not None else None,t['flags'],t['team']) for t in values)
    require(isinstance(policy['aliases'],list))
    require(all(set(a)=={'container','executable','info_path','info_sha256','links'} for a in policy['aliases']))
    aliases=tuple(ContainerAlias(a['container'],a['executable'],a['info_path'],a['info_sha256'],tuple(tuple(p) for p in a['links'])) for a in policy['aliases'])
    return Expected(Invocation(**i),head,Path(value['run_dir']),
        tuple(Path(p) for p in value['source_files']),Path(value['lock']),Path(value['recipe']),
        tuple((n,Path(p)) for n,p in value['archives']),SignaturePolicy(targets(policy['targets']),policy['identity'],targets(policy['sign_targets']),aliases),tuple(value['markers']))


def _expected_from_metadata(value,head):
    expected=_structural_from_metadata(value,head)
    _validate_context_recipe(expected)
    return expected


def _profile_mapping(recipe,workspace,tools,policy):
    if 'compiler_baseline' not in recipe:
        require(not any(t.path in COMPILER_PATHS for t in policy.targets))
        return None
    selected=compiler.select_compiler_qualifier(recipe,workspace)
    compiler.assert_profile_tools(selected,tools,recipe)
    return selected


def _validate_context_recipe(e):
    raw=read(e.recipe);require(digest(raw)==e.invocation.recipe_sha256)
    selected=_profile_mapping(_json(raw),Path(e.invocation.workspace),e.invocation.tools,e.policy)
    if selected==compiler.CLOUD:
        require(e.invocation.os_identity==compiler.OS)
        compiler.inputs(e,selected)
        compiler.environment(_json(raw))
    return selected


def _selected_compiler(e):
    return _validate_context_recipe(e)


def context_from_json(raw):
    try:
        require(isinstance(raw,str) and len(raw.encode())<=MAX_CONTEXT_BYTES)
        envelope=_json(raw)
        require(set(envelope)=={'schema','nonce','invocation_sha256','metadata','head'} and envelope['schema']==2)
        ref=envelope['metadata']; require(set(ref)=={'path','bytes','sha256'})
        require(type(ref['bytes']) is int and 0<ref['bytes']<=MAX_CONTEXT_METADATA and core._hex(ref['sha256']))
        path=Path(ref['path']); require(path.is_absolute() and path.name=='context-metadata.json')
        require(stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)==0o600)
        require(stat.S_IMODE(path.parent.stat(follow_symlinks=False).st_mode)==0o700)
        metadata=read(path,MAX_CONTEXT_METADATA)
        require(len(metadata)==ref['bytes'] and digest(metadata)==ref['sha256'])
        expected=_expected_from_metadata(_json(metadata),_head(envelope['head']))
        require(path==expected.run_dir/'context-metadata.json')
        require(expected.invocation.nonce==envelope['nonce'])
        require(digest(encode(asdict(expected.invocation)))==envelope['invocation_sha256'])
        return expected
    except (OSError,ValueError,KeyError,TypeError,AttributeError,RecursionError) as exc:
        raise Unknown('UNKNOWN: compact context metadata missing, changed, or incomplete') from exc


def _require_pipe(fd,*,writing):
    import fcntl
    require(type(fd) is int and fd>=0)
    try:
        require(stat.S_ISFIFO(os.fstat(fd).st_mode))
        access=fcntl.fcntl(fd,fcntl.F_GETFL)&os.O_ACCMODE
        require(access!=(os.O_RDONLY if writing else os.O_WRONLY))
    except OSError as exc:
        raise Unknown('UNKNOWN: inherited completion pipe unavailable') from exc


def _pipe_transfer(fd,*,outgoing=None,timeout=COMPLETION_SECONDS):
    require(type(timeout) in {int,float} and 0<timeout<=3600)
    _require_pipe(fd,writing=outgoing is not None)
    deadline=time.monotonic()+timeout
    selector=selectors.DefaultSelector()
    blocking=os.get_blocking(fd)
    data=bytearray(); position=0
    try:
        os.set_blocking(fd,False)
        selector.register(fd,selectors.EVENT_WRITE if outgoing is not None else selectors.EVENT_READ)
        while True:
            remaining=deadline-time.monotonic(); require(remaining>0)
            if not selector.select(remaining): continue
            try:
                if outgoing is not None:
                    count=os.write(fd,outgoing[position:]); require(count>0)
                    position+=count
                    if position==len(outgoing): return
                else:
                    chunk=os.read(fd,min(65536,MAX_COMPLETION_FRAME-len(data)+1))
                    if not chunk: return bytes(data)
                    data.extend(chunk); require(len(data)<=MAX_COMPLETION_FRAME)
            except BlockingIOError: continue
    except OSError as exc:
        raise Unknown('UNKNOWN: completion pipe failed') from exc
    finally:
        selector.close(); os.set_blocking(fd,blocking)


def emit_completion(e,fd,*,timeout=COMPLETION_SECONDS):
    """One length-framed compact context on a parent-owned, concurrently drained pipe."""
    require(type(fd) is int and fd>=3)
    _require_pipe(fd,writing=True)
    raw=context_json(e).encode()
    frame=str(len(raw)).encode()+b'\n'+raw+b'\n'
    require(len(frame)<=MAX_COMPLETION_FRAME)
    _pipe_transfer(fd,outgoing=frame,timeout=timeout)


def read_completion(fd,*,timeout=COMPLETION_SECONDS):
    """Read one bounded frame through EOF; caller still owns I/H acceptance."""
    frame=_pipe_transfer(fd,timeout=timeout)
    prefix,separator,rest=frame.partition(b'\n')
    require(separator and 0<len(prefix)<=5 and prefix.isdigit())
    count=int(prefix); require(0<count<=MAX_CONTEXT_BYTES and prefix==str(count).encode())
    require(len(rest)==count+1 and rest[-1:]==b'\n')
    raw=rest[:-1].decode('utf-8')
    context_from_json(raw)
    return raw


def _emit_context_stdout(e):
    _require_pipe(1,writing=True)
    raw=context_json(e).encode()+b'\n'
    _pipe_transfer(1,outgoing=raw)


def expected_from_environment():
    try: return context_from_json(os.environ['COLLECT_PROVENANCE_CONTEXT'])
    except (KeyError,ValueError,TypeError,AttributeError) as exc:
        raise Unknown('UNKNOWN: independent COLLECT_PROVENANCE_CONTEXT is missing or malformed') from exc


def current_ref(e):
    require(e.head.predecessor is not None)
    return ReceiptRef(e.run_dir/f'{e.head.generation:03d}-{e.head.stage}.json',
                      e.head.digest,e.head.generation,e.head.stage,e.head.predecessor)


# Private compiler handles deliberately carry no serializable authority.
_COMPILER_HANDLES={}


class _CompilerHandle:
    __slots__=()
    def __new__(cls): raise Unknown('UNKNOWN: private compiler handle')
    def __copy__(self): raise Unknown('UNKNOWN: private compiler handle')
    def __deepcopy__(self,memo): raise Unknown('UNKNOWN: private compiler handle')
    def __reduce__(self): raise Unknown('UNKNOWN: private compiler handle')


class _CompilerBootstrap(_CompilerHandle): __slots__=()
class AttemptRef(_CompilerHandle): __slots__=()
class _CompilerAdmission(_CompilerHandle): __slots__=()


def _new_compiler_handle(cls,**data):
    handle=object.__new__(cls)
    _COMPILER_HANDLES[handle]=dict(data,pid=os.getpid())
    return handle


def _handle(handle,cls,state):
    require(type(handle) is cls and handle in _COMPILER_HANDLES)
    data=_COMPILER_HANDLES[handle]
    require(data['pid']==os.getpid() and data['state']==state)
    return data


def _private_file(path,limit):
    info=path.stat(follow_symlinks=False)
    require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode)==0o600 and info.st_uid==os.getuid() and info.st_nlink==1)
    raw=read(path,limit)
    require(compiler.identity(info)==compiler.identity(path.stat(follow_symlinks=False)))
    return raw,compiler.identity(info)


def _ownership_compiler_context(raw):
    require(isinstance(raw,str) and len(raw.encode())<=MAX_CONTEXT_BYTES)
    envelope=_json(raw)
    require(set(envelope)=={'schema','nonce','invocation_sha256','metadata','head'} and type(envelope['schema']) is int and envelope['schema']==2)
    require(core._hex(envelope['nonce']) and core._hex(envelope['invocation_sha256']))
    ref=envelope['metadata'];require(set(ref)=={'path','bytes','sha256'})
    require(type(ref['bytes']) is int and 0<ref['bytes']<=MAX_CONTEXT_METADATA and core._hex(ref['sha256']))
    path=Path(ref['path']);require(path.is_absolute() and path.name=='context-metadata.json' and '..' not in path.parts)
    directory=path.parent;info=directory.stat(follow_symlinks=False)
    require(stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode)==0o700 and info.st_uid==os.getuid())
    fd=core._open_directory(directory);os.close(fd)
    metadata,identity=_private_file(path,MAX_CONTEXT_METADATA)
    require(len(metadata)==ref['bytes'] and digest(metadata)==ref['sha256'])
    value=_json(metadata);head=_head(envelope['head'])
    # Structural parser only: never read a recipe or perform a tool/source sweep.
    structural=_structural_from_metadata(value,head)
    i=structural.invocation
    require(core._hex(i.nonce) and i.nonce==envelope['nonce'] and digest(encode(asdict(i)))==envelope['invocation_sha256'])
    require(all(core._hex(getattr(i,key)) for key in ('source_sha256','lock_sha256','catalog_sha256','recipe_sha256','policy_sha256','markers_sha256')))
    require(type(i.tools) is tuple and 0<len(i.tools)<=16 and len({t.path for t in i.tools})==len(i.tools))
    require(all(isinstance(t.path,str) and Path(t.path).is_absolute() and core._hex(t.sha256) for t in i.tools))
    require(i.architecture=='arm64' and len(i.os_identity)==4 and all(isinstance(v,str) for v in i.os_identity))
    workspace=Path(i.workspace);app=Path(i.app)
    require(workspace.is_absolute() and app.is_absolute() and '..' not in workspace.parts and '..' not in app.parts)
    require(app.is_relative_to(workspace) and app!=workspace and not directory.is_relative_to(workspace))
    require(str(directory)==value['run_dir'] and head.stage=='PREPARED' and head.generation==1 and head.predecessor==Head())
    lockpath=directory.parent/('.collect-app-'+digest(os.fsencode(app))+'.lock')
    lockraw,lockidentity=_private_file(lockpath,MAX_RECEIPT)
    require(lockraw==encode({'nonce':i.nonce,'app':i.app}))
    return {'raw':raw,'metadata':metadata,'metadata_identity':identity,'directory_identity':compiler.identity(info),
        'lock_identity':lockidentity,'value':value,'head':head,'invocation':i,'run_dir':directory}


def _frame_compiler_request(owned_envelope: str) -> str:
    try:
        _ownership_compiler_context(owned_envelope)
        _environment_capacity(owned_envelope.encode())
        return owned_envelope
    except (OSError,ValueError,TypeError,KeyError,AttributeError,core._Unknown) as exc:
        raise Unknown('UNKNOWN: compiler request ownership') from exc


def _bootstrap_compiler_request(trusted_raw: str) -> _CompilerBootstrap:
    try:return _new_compiler_handle(_CompilerBootstrap,state='OWNED',owned=_ownership_compiler_context(trusted_raw))
    except (OSError,ValueError,TypeError,KeyError,AttributeError,core._Unknown) as exc:
        raise Unknown('UNKNOWN: compiler request ownership') from exc


def _claim_compiler_attempt(bootstrap) -> AttemptRef:
    state=_handle(bootstrap,_CompilerBootstrap,'OWNED');state['state']='CLAIMING'
    owned=state['owned'];i=owned['invocation']
    path=owned['run_dir']/'002-BOUND.compiler.attempt'
    raw=encode({'schema':1,'kind':'compiler-attempt','invocation_sha256':digest(encode(asdict(i))),
        'prepared_head':asdict(owned['head']),'qualifier_sha256':compiler.CLOUD.sha256,'app':i.app,'bound_stage':'BOUND','bound_generation':2})
    require(len(raw)<=8192)
    try:
        # Exclusive publication refuses ANY pre-existing object, including corrupt claims.
        _exclusive(path,raw)
        actual,identity=_private_file(path,8192);require(actual==raw)
        state['state']='CLAIMED'
        return _new_compiler_handle(AttemptRef,state='CLAIMED',bootstrap=bootstrap,path=path,raw=raw,identity=identity)
    except BaseException:
        state['state']='POISONED';raise


def _check_attempt(attempt,state='ACTIVE'):
    data=_handle(attempt,AttemptRef,state)
    actual,identity=_private_file(data['path'],8192)
    require(actual==data['raw'] and identity==data['identity'])
    return data


def _finish_compiler_bootstrap(bootstrap,attempt) -> Expected:
    state=_handle(bootstrap,_CompilerBootstrap,'CLAIMED');claim=_check_attempt(attempt,'CLAIMED')
    require(claim['bootstrap'] is bootstrap);state['state']='FINISHING'
    try:
        old=state['owned'];fresh=_ownership_compiler_context(old['raw'])
        # Directory timestamps legitimately changed when the claim was published.
        require({k:v for k,v in fresh.items() if k!='directory_identity'}=={k:v for k,v in old.items() if k!='directory_identity'})
        require(all(fresh['directory_identity'][k]==old['directory_identity'][k] for k in ('dev','ino','mode','uid','gid')))
        expected=context_from_json(old['raw'])
        require(expected.invocation==old['invocation'] and expected.head==old['head'])
        require(_selected_compiler(expected)==compiler.CLOUD)
        _context_preflight(expected)
        claim.update(state='ACTIVE',expected=expected);state['state']='ACTIVE'
        return expected
    except BaseException:
        state['state']='POISONED';claim['state']='POISONED';raise


def controlled_install_and_bind(trusted_raw_context: str) -> Expected:
    bootstrap=None;attempt=None
    try:
        with compiler_operation():
            bootstrap=_bootstrap_compiler_request(trusted_raw_context)
            _operation_check()
            attempt=_claim_compiler_attempt(bootstrap)
            _operation_check()
            expected=_finish_compiler_bootstrap(bootstrap,attempt)
            _operation_check()
            result=_controlled_install_and_bind_claimed(expected,attempt)
            _operation_check()
            return result
    except OperationDeadlineExceeded as exc:
        raise Unknown(str(exc)) from exc
    finally:
        if attempt is not None:_COMPILER_HANDLES.pop(attempt,None)
        if bootstrap is not None:_COMPILER_HANDLES.pop(bootstrap,None)


def _admit_compiler(e,ref,witness):
    genuine=type(witness) is _CompilerAdmission and witness in _COMPILER_HANDLES
    try:
        data=_handle(witness,_CompilerAdmission,'READY')
        data['state']='VALIDATING'
        require(_selected_compiler(e)==compiler.CLOUD and data['expected']==e and data['ref']==ref)
        require(e.head.stage=='PREPARED' and e.head.generation==1 and ref.predecessor==e.head and ref.stage=='BOUND' and ref.generation==2)
        require(ref.path==e.run_dir/'002-BOUND.json')
        _check_attempt(data['attempt'])
        candidate=replace(e,head=Head('BOUND',2,ref.digest,e.head))
        _load_receipt(candidate,ref,_compiler_budget=data['budget'],_admission=witness)
        data['budget'].complete();_check_attempt(data['attempt'])
        require(inventory(e.app)==data['entries'] and _compiler_outputs(e)==data['proof']['outputs'])
        require(compiler.inputs(e,compiler.CLOUD)=={p:v['after'] for p,v in data['proof']['inputs'].items()})
        require(compiler.environment_record(compiler.environment(_json(read(e.recipe))))==data['proof']['environment'])
        data['budget'].complete()
        _operation_check()
        data['state']='CONSUMED'
        return candidate
    finally:
        if genuine:
            revoked=_COMPILER_HANDLES.pop(witness,None)
            if revoked is not None:
                state='CONSUMED' if revoked['state']=='CONSUMED' else 'POISONED'
                pid=revoked['pid'];revoked.clear();revoked.update(state=state,pid=pid)


def _proof_record_keys(value,keys):
    require(isinstance(value,dict) and set(value)==set(keys.split()))


def _validate_compiler_proof(expected,bound_receipt,proof,*,_admission=None) -> None:
    e=expected;i=e.invocation
    _proof_record_keys(proof,'schema kind invocation_sha256 predecessor bound_stage bound_generation qualifier_sha256 recipe_sha256 policy_sha256 app created_inventory_sha256 output_inventory_sha256 inputs environment producer tools outputs native')
    require(type(proof['schema']) is int and proof['schema']==1 and proof['kind']=='compiler-production')
    require(proof['invocation_sha256']==digest(encode(asdict(i))) and proof['qualifier_sha256']==compiler.CLOUD.sha256)
    require(proof['recipe_sha256']==i.recipe_sha256 and proof['policy_sha256']==i.policy_sha256 and proof['app']==i.app)
    require(proof['bound_stage']=='BOUND' and type(proof['bound_generation']) is int and proof['bound_generation']==2)
    predecessor=_head(proof['predecessor']);require(predecessor.stage=='PREPARED' and predecessor.generation==1 and predecessor.predecessor==Head())
    require(proof['predecessor']==bound_receipt['predecessor'] and proof['output_inventory_sha256']==bound_receipt['output_inventory_sha256'])
    require(core._hex(proof['created_inventory_sha256']) and bound_receipt['input_inventory_sha256']==inventory_digest(()))
    require(bound_receipt['stage']=='BOUND' and bound_receipt['generation']==2 and bound_receipt['signature_state']=='UNSIGNED' and bound_receipt['signature_operation']=='none' and not bound_receipt['controlled_fields'])
    recipe=_json(read(e.recipe));root=Path(i.workspace);env=recipe['compiler_producer']['environment']
    require(encode(proof['environment'])==encode(compiler.environment_record(env)))
    producer=proof['producer'];_proof_record_keys(producer,'argv cwd umask file_limit output_reservation leader attribution')
    argv=[recipe['compiler_producer']['python']['path'],'-I','-B',str(root/'packages/menubar/scripts/install_bundle_launchers.py'),i.app]
    require(producer['argv']==argv and producer['cwd']==str(root/'packages/menubar') and producer['umask']==0o22 and producer['file_limit']==2097152 and producer['output_reservation']==6291456)
    require(producer['attribution']=='compiler-success-via-pinned-installer-check-true')
    def command(record):
        _proof_record_keys(record,'argv returncode stdout_bytes stdout_sha256 stderr_bytes stderr_sha256 eof reaped')
        require(type(record['returncode']) is int and record['returncode']==0 and record['eof'] is True and record['reaped'] is True)
        require(all(type(record[k]) is int and 0<=record[k]<=1024*1024 for k in ('stdout_bytes','stderr_bytes')))
        require(record['stdout_bytes']+record['stderr_bytes']<=1024*1024)
        require(core._hex(record['stdout_sha256']) and core._hex(record['stderr_sha256']))
        require(isinstance(record['argv'],list) and all(isinstance(v,str) for v in record['argv']))
    command(producer['leader']);require(producer['leader']['argv']==argv)
    _proof_record_keys(bound_receipt['command'],'argv_sha256 output_sha256 observation_sha256')
    require(bound_receipt['command']['argv_sha256']==digest(encode(argv)))
    require(bound_receipt['command']['output_sha256']==digest(encode({k:producer['leader'][k] for k in ('stdout_bytes','stdout_sha256','stderr_bytes','stderr_sha256')})))
    require(bound_receipt['command']['observation_sha256']==digest(encode(proof)))
    def ident(value):
        _proof_record_keys(value,'dev ino mode uid gid nlink size mtime_ns ctime_ns')
        require(all(type(v) is int for v in value.values()))
    pinned={str(root/'packages/menubar/scripts/cli_launcher.c'):compiler.SOURCE_SHA,str(root/'packages/menubar/scripts/install_bundle_launchers.py'):compiler.INSTALLER_SHA,
        str(root/'.github/workflows/macos.yml'):recipe['compiler_producer']['workflow']['sha256'],recipe['config']:recipe['config_sha256']}
    require(set(proof['inputs'])==set(pinned))
    for path,want in pinned.items():
        pair=proof['inputs'][path];_proof_record_keys(pair,'before after');require(encode(pair['before'])==encode(pair['after']))
        _proof_record_keys(pair['before'],'identity sha256');require(pair['before']['sha256']==want);ident(pair['before']['identity'])
    tools=proof['tools'];_proof_record_keys(tools,'slots limits reservation completed reserved before after selections_before selections_after commands')
    require(tools['limits']=={'file':268435456,'total':570425344,'seconds':60,'operation_seconds':300})
    require(encode(tools['before'])==encode(tools['after']) and encode(tools['selections_before'])==encode(tools['selections_after']))
    require(set(tools['before'])=={p for p,_,_ in compiler.CLOUD.tools})
    for path,size,want in compiler.CLOUD.tools:
        item=tools['before'][path];_proof_record_keys(item,'requested resolved links identity sha256 size')
        require(item['requested']==path and item['sha256']==want and item['size']==size);ident(item['identity'])
        require(item['identity']['size']==size and item['identity']['uid'] in (0,os.getuid()) and item['identity']['mode']&0o111 and not item['identity']['mode']&0o022)
        require(item['identity']['uid']==0 or item['identity']['nlink']==1)
        require(isinstance(item['links'],list) and len(item['links'])<=8)
        for link in item['links']:
            _proof_record_keys(link,'path target identity');ident(link['identity'])
    require(set(tools['selections_before'])=={compiler.EFFECTIVE[0][0],compiler.EFFECTIVE[1][0],compiler.SDK})
    for path,item in tools['selections_before'].items():
        _proof_record_keys(item,'requested resolved links identity');require(item['requested']==path);ident(item['identity'])
        for link in item['links']:_proof_record_keys(link,'path target identity');ident(link['identity'])
    ordinary=tools['slots'][:len(i.tools)]
    require(len(ordinary)==len(i.tools))
    for slot,tool in zip(ordinary,i.tools):
        _proof_record_keys(slot,'kind path size');require(slot['kind']=='ordinary' and slot['path']==tool.path and type(slot['size']) is int and 0<=slot['size']<=128*1024*1024)
    observed=[{'kind':'os','path':p,'size':size} for p,size,_ in compiler.CLOUD.tools]
    require(tools['slots']==ordinary*2+observed+ordinary+observed+ordinary*3)
    require(tools['reservation']==sum(x['size'] for x in tools['slots'])<=570425344)
    require(tools['reserved']==ordinary*2 and len(tools['completed'])==len(tools['slots'])-2*len(ordinary))
    for slot,record in zip(tools['slots'],tools['completed']):
        require(record==slot|{'bytes':slot['size'],'complete':True})
    require(len(tools['commands'])==7)
    for record in tools['commands']:command(record)
    expected_commands=[['/usr/bin/clang','--version']]+[['/usr/bin/xcrun',*args] for args in (('--find','clang'),('--find','ld'),('--show-sdk-path',))]*2
    require([r['argv'] for r in tools['commands']]==expected_commands)
    outputs=proof['outputs'];require(set(outputs)==set(COMPILER_PATHS))
    for path,item in outputs.items():
        _proof_record_keys(item,'path size mode sha256 identity');ident(item['identity'])
        require(item['path']==path and item['size']==34096 and item['mode']==0o755 and item['sha256']==compiler.CLOUD.output_sha256)
        require(item['identity']['size']==34096 and stat.S_ISREG(item['identity']['mode']) and stat.S_IMODE(item['identity']['mode'])==0o755 and item['identity']['uid']==os.getuid() and item['identity']['nlink']==1)
    native=proof['native'];_proof_record_keys(native,'commands policies');require(len(native['commands'])==9)
    native_argv=[]
    for path in sorted(COMPILER_PATHS):
        for args in (('--verify','--strict'),('-d','--verbose=4'),('-d','--entitlements',':-')):native_argv.append([CODESIGN,*args,str(e.app/path)])
    require([r['argv'] for r in native['commands']]==native_argv)
    for record in native['commands']:command(record)
    require(native['policies']==[{'path':p,'identifier':'fulcra-collect','entitlements':None,'flags':0x20002,'team':'not set'} for p in sorted(COMPILER_PATHS)])
    if _admission is not None:
        admission=_handle(_admission,_CompilerAdmission,'VALIDATING')
        require(admission['expected'].invocation==i and admission['expected'].head==predecessor and admission['proof']==proof)
        _check_attempt(admission['attempt'])
    else:
        # Ownership must precede this call; a candidate-created Head cannot enter here through advance.
        cursor=e.head;nodes=[]
        while cursor is not None:
            if cursor.stage=='BOUND' and cursor.generation==2:nodes.append(cursor)
            cursor=cursor.predecessor
        require(len(nodes)==1 and nodes[0].predecessor==predecessor and nodes[0].digest==digest(encode(bound_receipt)))
    if e.head.stage=='BOUND':
        require(inventory_digest(inventory(e.app))==proof['output_inventory_sha256'])
        require(_compiler_outputs(e)==outputs)


def _load_compiler_proof(e,current_doc,*,_admission=None):
    if _admission is not None:
        _handle(_admission,_CompilerAdmission,'VALIDATING');bound=current_doc
    else:
        cursor=e.head
        while cursor is not None and not (cursor.stage=='BOUND' and cursor.generation==2):cursor=cursor.predecessor
        require(cursor is not None)
        raw,_=_private_file(e.run_dir/'002-BOUND.json',MAX_RECEIPT);require(digest(raw)==cursor.digest)
        bound=_json(raw)
        require(set(bound)=={'schema','invocation','stage','generation','predecessor','input_inventory_sha256','output_inventory_sha256','inventory','signature_state','signature_operation','controlled_fields','command'})
        require(bound['schema']==1 and bound['invocation']==_json(encode(asdict(e.invocation))) and bound['inventory']=='002-BOUND.inventory.json')
    raw,_=_private_file(e.run_dir/'002-BOUND.compiler.json',MAX_RECEIPT)
    require(bound['command'] is not None and digest(raw)==bound['command']['observation_sha256'])
    proof=_json(raw);require(encode(proof)==raw)
    _validate_compiler_proof(e,bound,proof,_admission=_admission)


def _compiler_outputs(e):
    result={}
    for path in COMPILER_PATHS:
        file=e.app/path;info=file.stat(follow_symlinks=False)
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode)==0o755 and info.st_uid==os.getuid() and info.st_nlink==1 and info.st_size==34096)
        raw=read(file,2097152);require(digest(raw)==compiler.CLOUD.output_sha256 and compiler.identity(info)==compiler.identity(file.stat(follow_symlinks=False)))
        result[path]={'path':path,'size':len(raw),'mode':0o755,'sha256':digest(raw),'identity':compiler.identity(info)}
    return result


def _controlled_install_and_bind_claimed(e,attempt) -> Expected:
    _operation_check()
    claim=_check_attempt(attempt);require(claim['expected']==e)
    # Consume the body once even if a later validation fails.
    bootstrap=_handle(claim['bootstrap'],_CompilerBootstrap,'ACTIVE');bootstrap['state']='PRODUCING'
    qualifier=_selected_compiler(e);require(qualifier==compiler.CLOUD)
    _context_preflight(e)
    budget=compiler.plan_compiler_reads(e,qualifier)
    prior=_load_receipt(e,current_ref(e),_compiler_budget=budget)
    require(prior.stage=='PREPARED' and not prior.entries)
    recipe=_json(read(e.recipe));created=inventory(e.app)
    _prepared_app_policy(e,recipe,created)
    for path in COMPILER_PATHS:require(not os.path.lexists(e.app/path))
    before=compiler.observe_compiler_before(e,qualifier,budget)
    env=compiler.environment(recipe)
    argv=(recipe['compiler_producer']['python']['path'],'-I','-B',str(Path(e.invocation.workspace)/'packages/menubar/scripts/install_bundle_launchers.py'),str(e.app))
    require(all(not os.path.lexists(e.app/path) for path in COMPILER_PATHS))
    require(inventory(e.app)==created)
    result=budget.command(argv,cwd=str(Path(e.invocation.workspace)/'packages/menubar'),env=env,producer=True)
    producer=budget.commands[-1]
    entries=inventory(e.app);outputs=_compiler_outputs(e)
    require(tuple(x for x in entries if x.path not in COMPILER_PATHS)==created)
    require({x.path for x in entries}-{x.path for x in created}==set(COMPILER_PATHS))
    _scan(e,prior.public_inputs,entries,'raw',markers=());_prepared_app_policy(e,recipe,entries)
    for path in COMPILER_PATHS:
        require(any(t.path==path and t.identifier=='fulcra-collect' and t.flags==0x20002 and t.entitlements is None and t.team=='not set' for t in e.policy.targets))
    native_start=len(budget.commands)
    verify_signature_policy(e,entries,e.policy,_only=COMPILER_PATHS,_compiler_budget=budget)
    native=budget.commands[native_start:]
    after=compiler.observe_compiler_after(e,before,budget)
    _current(e,_compiler_budget=budget)
    require(compiler.inputs(e,qualifier)==after.inputs and compiler.environment_record(compiler.environment(recipe))==after.environment)
    require(inventory(e.app)==entries and _compiler_outputs(e)==outputs)
    require(len(budget.commands)==17 and len(native)==9 and budget.index==len(budget.slots)-2*len(e.invocation.tools) and not budget.failed)
    _check_attempt(attempt)
    proof={'schema':1,'kind':'compiler-production','invocation_sha256':digest(encode(asdict(e.invocation))),
        'predecessor':asdict(e.head),'bound_stage':'BOUND','bound_generation':2,'qualifier_sha256':qualifier.sha256,
        'recipe_sha256':e.invocation.recipe_sha256,'policy_sha256':e.invocation.policy_sha256,'app':str(e.app),
        'created_inventory_sha256':inventory_digest(created),'output_inventory_sha256':inventory_digest(entries),
        'inputs':{p:{'before':v,'after':after.inputs[p]} for p,v in before.inputs.items()},'environment':before.environment,
        'producer':{'argv':list(argv),'cwd':str(Path(e.invocation.workspace)/'packages/menubar'),'umask':0o22,'file_limit':2097152,'output_reservation':6291456,
            'leader':producer,'attribution':'compiler-success-via-pinned-installer-check-true'},
        'tools':{'slots':[asdict(slot) for slot in budget.slots],'limits':{'file':268435456,'total':570425344,'seconds':60,'operation_seconds':300},
            'reservation':budget.reservation,'completed':list(budget.records),'reserved':[asdict(slot) for slot in budget.slots[budget.index:]],
            'before':before.tools,'after':after.tools,'selections_before':before.selections,'selections_after':after.selections,
            'commands':budget.commands[:4]+budget.commands[-3:]},'outputs':outputs,
        'native':{'commands':native,'policies':[{'path':p,'identifier':'fulcra-collect','entitlements':None,'flags':0x20002,'team':'not set'} for p in sorted(COMPILER_PATHS)]}}
    raw=encode(proof);require(len(raw)<=MAX_RECEIPT)
    _exclusive(e.run_dir/'002-BOUND.compiler.json',raw)
    actual,_=_private_file(e.run_dir/'002-BOUND.compiler.json',MAX_RECEIPT);require(actual==raw)
    command={'argv_sha256':digest(encode(list(argv))),'output_sha256':digest(encode({k:producer[k] for k in ('stdout_bytes','stdout_sha256','stderr_bytes','stderr_sha256')})),'observation_sha256':digest(raw)}
    ref=_emit(e,'BOUND',entries,input_entries=prior.entries,command=command)
    receipt,_=_private_file(ref.path,MAX_RECEIPT);require(digest(receipt)==ref.digest)
    _operation_check()
    witness=_new_compiler_handle(_CompilerAdmission,state='READY',expected=e,ref=ref,proof=proof,attempt=attempt,budget=budget,entries=entries)
    return advance(e,ref,_compiler_admission=witness)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    completion=sub.add_parser('emit-completion'); completion.add_argument('--fd',type=int,required=True)
    entry=sub.add_parser('start'); entry.add_argument('--request',type=Path,required=True); entry.add_argument('--run-dir',type=Path,required=True)
    for command in ('prepare-public-inputs','bind-public-inputs','controlled-install-and-bind','controlled-build','controlled-sign'):
        sub.add_parser(command)
    try: args=parser.parse_args(argv)
    except SystemExit as exc: return exc.code
    try:
        if args.command!='emit-completion': _require_pipe(1,writing=True)
        if args.command=='emit-completion':
            emit_completion(expected_from_environment(),args.fd)
            return 0
        if args.command=='start':
            request=_json(read(args.request))
            require(set(request)=={'workspace','app','source_files','lock','recipe','recipe_sha256','archives','catalog_sha256','architecture','tools','markers'})
            policy=derive_signature_policy(request['recipe'],expected_recipe_sha256=request['recipe_sha256'])
            e=start_invocation(args.run_dir,workspace=Path(request['workspace']),app=Path(request['app']),
                source_files=tuple(Path(p) for p in request['source_files']),lock=Path(request['lock']),recipe=Path(request['recipe']),
                archives={n:Path(p) for n,p in request['archives'].items()},catalog_sha256=request['catalog_sha256'],
                architecture=request['architecture'],tools=tuple(ToolIdentity(**t) for t in request['tools']),
                policy=policy,markers=tuple(request['markers']))
        elif args.command=='controlled-install-and-bind':
            e=controlled_install_and_bind(os.environ['COLLECT_PROVENANCE_CONTEXT'])
        else:
            e=expected_from_environment()
            if args.command=='prepare-public-inputs': ref=prepare_public_inputs(e)
            elif args.command=='bind-public-inputs': ref=bind_public_inputs(e,current_ref(e),e.app)
            elif args.command=='controlled-build': ref=controlled_build(e,current_ref(e),tuple(_json(read(e.recipe))['build_argv']))
            else: ref=controlled_sign(e,current_ref(e),tuple(tuple(a) for a in _json(read(e.recipe))['sign_argv']),e.policy)
            e=advance(e,ref)
        _emit_context_stdout(e)
        return 0
    except (Unknown,OSError,ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError,core._Unknown):
        import sys
        print('UNKNOWN: provenance command did not advance current head',file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
