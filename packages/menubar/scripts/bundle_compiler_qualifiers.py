"""Closed compiler profiles and a physical-read/command ledger.

This module observes trusted OS inputs. It never owns a stage/head or admits a
receipt. The stage module owns operation capabilities and publication.
"""
from dataclasses import dataclass, asdict
import hashlib
import os
from pathlib import Path
import stat
import time
from bundle_operation_deadline import current_operation_deadline

import bundle_provenance_stages as s

SOURCE_SHA='9df6e0d1e35d56e0daaacca800f3cb6ed449eb3d1942f3bca2c92c681909d0e5'
INSTALLER_SHA='867421abe8b0fc87a0863d10421df62c5b2164d15d750c4b0d7c6151a262269c'
LOCAL_TOOL='b8763cf250e607a778bb4603cecb5b90338814d0a3dfcba0d57b1de242f610e9'
LOCAL_OUTPUT='f69e1f665d9436f551bdac836caf530a93ae319626e887751ef221f33b8bac1a'
WRAPPERS=(('/usr/bin/clang',118864,'8291fbbbaa75761bb649970a16af2ef84b4afd5da0bc133f4d8915f0f2f63e98'),
 ('/usr/bin/git',118864,'8291fbbbaa75761bb649970a16af2ef84b4afd5da0bc133f4d8915f0f2f63e98'),
 ('/usr/bin/xcrun',117392,'274eb256dc4af66bef7737a28b330713105eb67f362338135e932ae3ded9ad76'),
 ('/usr/bin/codesign',378144,'1cdff754c0405ff031983ade8791ad0835777ae5787957d8d63090da0e159f88'))
DEVELOPER='/Applications/Xcode_16.4.app/Contents/Developer'
EFFECTIVE=((DEVELOPER+'/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang',264942528,'4c458256bcdf913774de1bb4a37768244d3b41f32ad55afb2dfec3432f46f4fe'),
 (DEVELOPER+'/Toolchains/XcodeDefault.xctoolchain/usr/bin/ld',4344144,'648a8aac7f6f829a42305d24dddd7a3cfa48ee0bd9d4e7ec1ded5c51edea372b'))
SDK=DEVELOPER+'/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk'
OS=('Darwin','24.6.0','Darwin Kernel Version 24.6.0: Sun Oct 11 23:44:11 PDT 2026; root:xnu-11417.140.69.713.47~2/RELEASE_ARM64_VMAPPLE','arm64')
FILE_LIMIT=256*1024*1024
TOTAL_LIMIT=570425344
READ_SECONDS=60
OUTPUT_LIMIT=2097152
ENV_FORBIDDEN={'LD_PRELOAD','CCC_OVERRIDE_OPTIONS','COMPILER_PATH','GCC_EXEC_PREFIX','DEVELOPER_DIR','SDKROOT','TOOLCHAINS','MACOSX_DEPLOYMENT_TARGET','CPATH','C_INCLUDE_PATH','CPLUS_INCLUDE_PATH','OBJC_INCLUDE_PATH','LIBRARY_PATH','ZERO_AR_DATE'}


@dataclass(frozen=True)
class CompilerQualifier:
    profile: str
    source_sha256: str
    tool_sha256: str
    output_sha256: str
    output_size: int | None
    installer_sha256: str | None
    os_identity: tuple
    tools: tuple
    sdk: str | None

    @property
    def sha256(self): return s.digest(s.encode(asdict(self)))


LOCAL=CompilerQualifier('local-v1',SOURCE_SHA,LOCAL_TOOL,LOCAL_OUTPUT,None,None,(),(),None)
CLOUD=CompilerQualifier('cloud-xcode16.4-arm64-v1',SOURCE_SHA,WRAPPERS[0][2],
 '41cec1c57154dbab34c1a8bc7c9813cca21591efe71141f54fe58c21c347f3c8',34096,INSTALLER_SHA,OS,WRAPPERS+EFFECTIVE,SDK)


def select_compiler_qualifier(recipe,workspace):
    baseline=recipe.get('compiler_baseline')
    s.require(isinstance(baseline,dict))
    source=Path(baseline.get('source',''))
    s.require(source.is_absolute() and '..' not in source.parts)
    if set(baseline)=={'source','source_sha256','tool_sha256','output_sha256'}:
        selected=LOCAL
        s.require(baseline['source_sha256']==SOURCE_SHA and baseline['tool_sha256']==LOCAL_TOOL and baseline['output_sha256']==LOCAL_OUTPUT)
    else:
        s.require(set(baseline)=={'schema','profile','source'} and type(baseline['schema']) is int and baseline['schema']==2 and baseline['profile']==CLOUD.profile)
        s.require(source==Path(workspace)/'packages/menubar/scripts/cli_launcher.c')
        selected=CLOUD
    s.require(s.digest(s.read(source))==SOURCE_SHA)
    return selected


def assert_profile_tools(selected,tools,recipe):
    s.require(selected in (LOCAL,CLOUD) and 0<len(tools)<=16 and len({t.path for t in tools})==len(tools))
    mapping={t.path:t.sha256 for t in tools}
    s.require(mapping.get('/usr/bin/clang')==selected.tool_sha256)
    if selected==CLOUD:
        producer=recipe.get('compiler_producer')
        s.require(isinstance(producer,dict) and set(producer)=={'python','workflow','environment'})
        for name in ('python','workflow'):
            item=producer[name]
            s.require(isinstance(item,dict) and set(item)=={'path','sha256'} and isinstance(item['path'],str) and Path(item['path']).is_absolute() and '..' not in Path(item['path']).parts and s.core._hex(item['sha256']))
        s.require(all(mapping.get(path)==sha for path,_,sha in WRAPPERS))
        s.require(mapping.get(producer['python']['path'])==producer['python']['sha256'])


def identity(info):
    return dict(dev=info.st_dev,ino=info.st_ino,mode=info.st_mode,uid=info.st_uid,gid=info.st_gid,
        nlink=info.st_nlink,size=info.st_size,mtime_ns=info.st_mtime_ns,ctime_ns=info.st_ctime_ns)


def _tool_mode(info,*,typed):
    s.require(stat.S_ISREG(info.st_mode) and info.st_mode&0o111 and not info.st_mode&0o022)
    s.require(info.st_uid in (0,os.getuid()) and (info.st_uid==0 or info.st_nlink==1))
    s.require(info.st_size<=(FILE_LIMIT if typed else 128*1024*1024))


def _safe_os_path(path,*,directory=False):
    """Resolve only the fixed OS roots; retain and recheck every symlink hop."""
    requested=Path(path)
    s.require(requested.is_absolute() and '..' not in requested.parts and len(requested.parts)<=64 and len(os.fsencode(path))<=4096)
    s.require(str(requested).startswith(('/usr/bin/','/Applications/Xcode_16.4.app/')))
    current=requested; links=[]; seen=set()
    for _ in range(9):
        parts=current.parts; found=False
        for index in range(1,len(parts)):
            s._operation_check()
            piece=Path(*parts[:index+1]); info=piece.lstat()
            s.require(info.st_uid in (0,os.getuid()) and (stat.S_ISLNK(info.st_mode) or not info.st_mode&0o022))
            if stat.S_ISLNK(info.st_mode):
                s.require(len(links)<8 and str(piece) not in seen); seen.add(str(piece))
                target=os.readlink(piece)
                s.require(identity(info)==identity(piece.lstat()))
                links.append({'path':str(piece),'target':target,'identity':identity(info)})
                replacement=Path(os.path.normpath(str(piece.parent/target))) if not Path(target).is_absolute() else Path(target)
                current=replacement.joinpath(*parts[index+1:])
                s.require(str(current).startswith(('/usr/bin/','/Applications/Xcode_16.4.app/')) and len(current.parts)<=64 and len(os.fsencode(current))<=4096)
                found=True;break
            if index<len(parts)-1: s.require(stat.S_ISDIR(info.st_mode))
        if not found: break
    else: s.require(False)
    if directory:s.require(stat.S_ISDIR(current.lstat().st_mode))
    s._operation_check()
    return {'requested':str(requested),'resolved':str(current),'links':links,'identity':identity(current.lstat())}


@dataclass(frozen=True)
class ReadSlot:
    kind: str
    path: str
    size: int


class CompilerReadBudget:
    def __init__(self,slots):
        self.slots=tuple(slots);self.reservation=sum(x.size for x in slots)
        s.require(self.reservation<=TOTAL_LIMIT)
        self.index=0;self.bytes=0;self.seconds=0.;self.active=None;self.failed=False;self.records=[]
        operation=current_operation_deadline()
        self.deadline=operation.expires if operation is not None else time.monotonic()+300
        self.commands=[];self.retained=0

    def begin(self,kind,path,size):
        s.require(not self.failed and self.active is None and self.index<len(self.slots))
        slot=self.slots[self.index]
        s.require(slot==ReadSlot(kind,str(path),size) and self.seconds<READ_SECONDS and time.monotonic()<self.deadline)
        self.active=(time.monotonic(),self.bytes,slot);self.index+=1
        return min(self.deadline,self.active[0]+READ_SECONDS-self.seconds)

    def charge(self,count):
        self.bytes+=count
        s.require(self.active is not None and self.bytes<=self.reservation and self.bytes<=TOTAL_LIMIT)
        s.require(time.monotonic()<self.deadline and time.monotonic()-self.active[0]+self.seconds<READ_SECONDS)

    def end(self,success):
        s.require(self.active is not None)
        started,before,slot=self.active
        self.seconds+=time.monotonic()-started;self.active=None
        complete=success and self.bytes-before==slot.size and self.seconds<READ_SECONDS and time.monotonic()<self.deadline
        self.records.append({'kind':slot.kind,'path':slot.path,'size':slot.size,'bytes':self.bytes-before,'complete':complete})
        if not complete:self.failed=True
        s.require(complete)

    def complete(self):
        s.require(not self.failed and self.active is None and self.index==len(self.slots) and self.bytes==self.reservation and self.seconds<READ_SECONDS and time.monotonic()<self.deadline)
        s.require(self.records==[asdict(slot)|{'bytes':slot.size,'complete':True} for slot in self.slots])

    def command(self,argv,*,cwd,env,producer=False):
        s.require(len(self.commands)<17 and time.monotonic()<self.deadline)
        remaining=self.deadline-time.monotonic()-1.0
        s.require(remaining>0)
        kwargs={'timeout':min(30,remaining),'cap':1024*1024,'cwd':cwd,'env':env}
        if producer: kwargs['_compiler_limit']=True
        result=s._run_process(tuple(argv),**kwargs)
        self.retained+=len(result.output)+len(result.stderr)
        s.require(self.retained<=8*1024*1024)
        record={'argv':list(argv),'returncode':result.returncode,'stdout_bytes':len(result.output),'stdout_sha256':s.digest(result.output),
            'stderr_bytes':len(result.stderr),'stderr_sha256':s.digest(result.stderr),'eof':result.eof,'reaped':result.reaped}
        self.commands.append(record);s._successful(result)
        s.require(time.monotonic()<self.deadline)
        return result


def plan_compiler_reads(expected,qualifier):
    s.require(qualifier==CLOUD)
    ordinary=[]
    for tool in expected.invocation.tools:
        s._operation_check()
        info=os.stat(tool.path,follow_symlinks=False)
        _tool_mode(info,typed=False)
        ordinary.append(ReadSlot('ordinary',tool.path,info.st_size))
    observed=[ReadSlot('os',p,size) for p,size,_ in qualifier.tools]
    # Initial receipt(2), before(1), native(1), after(1), final current(1), admission(2).
    return CompilerReadBudget(ordinary*2+observed+ordinary+observed+ordinary*3)


def read_os_tool(path,budget):
    records={p:(size,sha) for p,size,sha in CLOUD.tools}
    s.require(path in records)
    size,want=records[path]; resolution=_safe_os_path(path)
    resolved=Path(resolution['resolved'])
    info=resolved.lstat();_tool_mode(info,typed=True);s.require(info.st_size==size)
    deadline=budget.begin('os',path,size);good=False
    fd=None;parent=None
    try:
        parent=s.core._open_directory(resolved.parent)
        fd=os.open(resolved.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        s.require(identity(info)==identity(os.fstat(fd)))
        sha=hashlib.sha256();count=0
        while True:
            s.require(time.monotonic()<deadline)
            chunk=os.read(fd,min(1024*1024,size-count+1));budget.charge(len(chunk))
            if not chunk:break
            count+=len(chunk);s.require(count<=size);sha.update(chunk)
        s.require(count==size and sha.hexdigest()==want and identity(info)==identity(os.fstat(fd)))
        s.require(resolution==_safe_os_path(path))
        good=True
        return resolution|{'sha256':sha.hexdigest(),'size':count}
    finally:
        if fd is not None:os.close(fd)
        if parent is not None:os.close(parent)
        budget.end(good)


def ordinary_tool_identity(path,budget):
    """Same no-follow generic bound, now charged by physical chunks, not estimates."""
    info=os.stat(path,follow_symlinks=False);size=info.st_size
    s.require(stat.S_ISREG(info.st_mode) and info.st_mode&0o111 and size<=128*1024*1024)
    deadline=budget.begin('ordinary',str(path),size);good=False;fd=None;parent=None
    try:
        parent=s.core._open_directory(Path(path).parent)
        fd=os.open(Path(path).name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        s.require(identity(info)==identity(os.fstat(fd)))
        sha=hashlib.sha256();count=0
        while True:
            s.require(time.monotonic()<deadline)
            chunk=os.read(fd,min(1024*1024,size-count+1));budget.charge(len(chunk))
            if not chunk:break
            count+=len(chunk);s.require(count<=size);sha.update(chunk)
        s.require(count==size and identity(info)==identity(os.fstat(fd)) and identity(info)==identity(os.stat(path,follow_symlinks=False)))
        good=True;return sha.hexdigest()
    finally:
        if fd is not None:os.close(fd)
        if parent is not None:os.close(parent)
        budget.end(good)


def environment(recipe):
    producer=recipe['compiler_producer'];declared=producer['environment']
    s.require(not any(k.startswith('DYLD_') or k in ENV_FORBIDDEN for k in os.environ))
    s.require('HOME' in os.environ and 'TMPDIR' in os.environ)
    actual={k:os.environ[k] for k in ('HOME','TMPDIR','LANG','LC_ALL') if k in os.environ}
    actual.update(PATH='/usr/bin:/bin:/usr/sbin:/sbin',PYTHONDONTWRITEBYTECODE='1')
    s.require(declared==actual)
    return actual


def environment_record(env):
    return {'sha256':s.digest(s.encode(env)),'keys':sorted(env),'values':{k:{'bytes':len(v.encode()),'sha256':s.digest(v.encode())} for k,v in env.items()}}


def inputs(expected,qualifier):
    recipe=s._json(s.read(expected.recipe));root=Path(expected.invocation.workspace)
    pinned={str(root/'packages/menubar/scripts/cli_launcher.c'):SOURCE_SHA,
        str(root/'packages/menubar/scripts/install_bundle_launchers.py'):INSTALLER_SHA,
        str(root/'.github/workflows/macos.yml'):recipe['compiler_producer']['workflow']['sha256'],
        recipe['config']:recipe['config_sha256']}
    s.require(recipe['compiler_producer']['workflow']['path']==str(root/'.github/workflows/macos.yml'))
    required=set(pinned)|{str(root/'packages/menubar/scripts'/name) for name in ('bundle_provenance_stages.py','bundle_compiler_qualifiers.py','build_macos_app.sh')}
    s.require(required<={str(p) for p in expected.source_files})
    result={}
    for path,want in pinned.items():
        first=identity(os.stat(path,follow_symlinks=False));raw=s.read(path)
        s.require(s.digest(raw)==want and first==identity(os.stat(path,follow_symlinks=False)))
        result[path]={'identity':first,'sha256':want}
    return result


def _selections(expected,budget,env):
    root=str(Path(expected.invocation.workspace)/'packages/menubar');result={}
    for args,want in ((('--find','clang'),EFFECTIVE[0][0]),(('--find','ld'),EFFECTIVE[1][0]),(('--show-sdk-path',),SDK)):
        observed=budget.command(('/usr/bin/xcrun',*args),cwd=root,env=env)
        s.require(observed.output==(want+'\n').encode() and observed.stderr==b'')
        result[want]=_safe_os_path(want,directory=want==SDK)
    return result


@dataclass(frozen=True)
class CompilerBefore:
    qualifier: CompilerQualifier
    inputs: dict
    environment: dict
    tools: dict
    selections: dict


@dataclass(frozen=True)
class CompilerAfter:
    tools: dict
    selections: dict
    inputs: dict
    environment: dict


def observe_compiler_before(expected,qualifier,budget):
    s.require(qualifier==CLOUD and s._os_identity()==OS)
    recipe=s._json(s.read(expected.recipe));env=environment(recipe)
    before_inputs=inputs(expected,qualifier)
    budget.command(('/usr/bin/clang','--version'),cwd=str(Path(expected.invocation.workspace)/'packages/menubar'),env=env)
    selections=_selections(expected,budget,env)
    tools={p:read_os_tool(p,budget) for p,_,_ in qualifier.tools}
    s.require(selections=={p:_safe_os_path(p,directory=p==SDK) for p in selections})
    return CompilerBefore(qualifier,before_inputs,environment_record(env),tools,selections)


def observe_compiler_after(expected,before,budget):
    env=environment(s._json(s.read(expected.recipe)))
    tools={p:read_os_tool(p,budget) for p,_,_ in before.qualifier.tools}
    selections=_selections(expected,budget,env);after_inputs=inputs(expected,before.qualifier)
    after_env=environment_record(env)
    s.require(tools==before.tools and selections==before.selections and after_inputs==before.inputs and after_env==before.environment)
    return CompilerAfter(tools,selections,after_inputs,after_env)
