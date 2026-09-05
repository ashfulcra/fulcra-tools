"""Task 16 ship check. Exit 0 only with BOTH required responsibility-distribution approvals on the EXACT
commit — the engine's folded result AND, per reviewer, the exact WINNING shard the fold kept (never a
refold of filenames here), each quoting that commit's tree hash for packages/coord-fold.
Fails closed on any absence, including an engine that does not expose `winning`.
Usage: python scripts/ship_check.py <team> <40-hex head> --git <abs path> --fulcra-api <abs path>
(both trust roots are REQUIRED and stated as absolute paths — never discovered through PATH; r29/r31)"""
import json
import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import sys

REQUIRED = ("codex-reviewer", "codex-coder")
# Engine heads whose register `review-winning-envelope-e9c0089b` read APPROVED AND whose pin PR shipped.
# EMPTY until a deliberate plan revision adds one; an empty set means ship_check refuses, correctly.
APPROVED_ENGINE_PINS: frozenset = frozenset()


IMPORT_AFFECTING = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONSAFEPATH", "VIRTUAL_ENV", "CONDA_PREFIX")


def engine_env(fulcra_api=None):
    """The environment a child is invoked with: NOTHING that can change which coord_engine imports, and (r29)
    a PATH that is one private directory holding a single link to the STATED fulcra-api — so the engine's own
    transport, which shells out by name, can reach nothing else — with the engine's command/store overrides
    (FULCRA_CLI_COMMAND, FULCRA_API_BASE, COORD_TRANSPORT_HTTP) scrubbed. (coord-boss 8268376f: a pinned launcher
    answered with the working tree's capabilities because subprocess.run inherited PYTHONPATH.)"""
    env = {k: v for k, v in os.environ.items() if k not in IMPORT_AFFECTING and k not in SCRUBBED_OVERRIDES and not k.startswith("PYTHON")}
    env["PYTHONNOUSERSITE"] = "1"
    env["PATH"] = private_bin({"fulcra-api": fulcra_api} if fulcra_api else {})
    return env


def engine_executable():
    """Resolved ONCE, by main, to an absolute path. Nothing else may call `which`: the identity read and
    every invocation receive the SAME path (both reviewers, round 16: two resolutions let approved
    launcher A authorise unapproved launcher B after a PATH swap)."""
    exe = shutil.which("coord-engine")
    return os.path.realpath(exe) if exe else None


TRUST_ROOT_NAMES = ("git", "fulcra-api")
TRUSTED: dict = {}          # name -> realpath, filled ONCE by resolve_trust_roots(); sh() executes from here and nowhere else
SCRUBBED_OVERRIDES = ("FULCRA_CLI_COMMAND", "FULCRA_API_BASE", "COORD_TRANSPORT_HTTP")   # the engine's own command/store overrides


def tool_env_root(exe):
    """The mutable tool environment: the directory two levels above the launcher (<env>/bin/coord-engine)."""
    return str(os.path.realpath(str(pathlib.Path(exe).parent.parent)))


def resolve_trust_roots(stated, env_root):
    """r29 (codex-coder, round 26): the trusted executables are STATED by the operator as absolute paths — never
    discovered through PATH, which is also how the mutable launcher is found (a planted bin/git could bind tampered
    bytes to attacker hashes; a planted bin/fulcra-api could return an approved pin and approving verdicts). Each is
    resolved by realpath exactly once, refused if it or its target lies under the tool environment, and the resolved
    path is what every later call executes. -> (table, None) or (None, why)."""
    root = str(env_root).rstrip(os.sep) + os.sep
    out = {}
    for name in TRUST_ROOT_NAMES:
        p = stated.get(name)
        if not p or not os.path.isabs(p):
            return None, f"trust root {name!r} must be stated as an absolute path (--{name}); it is never discovered through PATH"
        real = os.path.realpath(p)
        if p.startswith(root) or real.startswith(root):
            return None, f"trust root {name!r} resolves under the tool environment {env_root} — refusing"
        if not (os.path.isfile(real) and os.access(real, os.X_OK)):
            return None, f"trust root {name!r} at {real} is not an executable file"
        out[name] = real
    return out, None


def private_bin(links):
    """A fresh 0700 directory holding ONLY the given links; the child's PATH is exactly this directory."""
    import tempfile
    d = tempfile.mkdtemp(prefix="coord-fold-bin-")
    os.chmod(d, 0o700)
    for name, target in links.items():
        os.symlink(target, os.path.join(d, name))
    return d


def store_read(remote):
    """Read one store file through the trusted fulcra-api into a PRIVATE temp file and return its text.
    r30 (found by the first real measurement of fleet_pin, 2026-09-05): the real CLI validates LOCAL_FILE as a
    readable path and REFUSES /dev/stdout whenever stdout is a pipe — so every earlier revision's `download ...
    /dev/stdout` form never worked outside the tests, whose fake shell returned bodies on stdout and hid it.
    -> (rc, text, err)."""
    import tempfile
    d = tempfile.mkdtemp(prefix="coord-fold-store-")
    os.chmod(d, 0o700)
    f = os.path.join(d, "body")
    try:
        rc, _, err = sh("fulcra-api", "file", "download", remote, f)
        if rc:
            return rc, "", err
        with open(f, encoding="utf-8") as fh:
            return 0, fh.read(), ""
    finally:
        shutil.rmtree(d, ignore_errors=True)


def sh(*argv):
    """Runs a TRUSTED executable by its once-resolved absolute path. A bare name never reaches the OS (r29)."""
    name, rest = argv[0], list(argv[1:])
    exe = TRUSTED.get(name)
    if not exe:
        raise RuntimeError(f"sh({name!r}) before trust roots were resolved — a bare name never executes")
    p = subprocess.run([exe, *rest], capture_output=True, text=True, env=engine_env(TRUSTED.get("fulcra-api")))
    return p.returncode, p.stdout.strip(), p.stderr.strip()


ATTEST = r"""
import sys, json, io, contextlib, os, glob, hashlib, importlib.abc, importlib.machinery
def refuse(why):
    print(json.dumps({"refused": why})); sys.exit(2)
def verify_tree(site, expected):
    # r26 (codex-reviewer, round 23): read every file ONCE, hash the bytes we KEEP, and execute those bytes.
    # Hashing a pathname and letting the importer reopen it later is a TOCTOU window; this closes it.
    pkg = os.path.join(site, "coord_engine")
    present = {os.path.relpath(os.path.join(dp, f), pkg) for dp, _, fs in os.walk(pkg) for f in fs if "__pycache__" not in dp}
    sourceless = sorted(f for f in present if f.endswith((".pyc", ".pyo", ".pyd", ".so")))
    if sourceless:
        refuse(f"compiled/sourceless files under coord_engine/ could answer: {sourceless[:3]}")
    missing = sorted(set(expected) - present)
    if missing:
        refuse(f"files in the pinned commit's tree are missing from the installed package: {missing[:3]}")
    extra = sorted(present - set(expected))
    if extra:
        refuse(f"files under coord_engine/ that the pinned commit's tree does not contain: {extra[:3]}")
    blobs = {}
    for rel, want in sorted(expected.items()):
        data = open(os.path.join(pkg, rel), "rb").read()
        got = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
        if got != want:
            refuse(f"installed file does not match the pinned commit's blob: {rel}")
        blobs[rel] = data
    return pkg, blobs
class VerifiedResources:
    # importlib.resources reader over the VERIFIED BYTES (r27): package data such as default_models.json is
    # served from memory too, never re-read from disk. resource_path is refused: there is no trusted path.
    def __init__(self, blobs, prefix):
        self.blobs, self.prefix = blobs, prefix
    def open_resource(self, name):
        rel = self.prefix + name
        if rel not in self.blobs:
            raise FileNotFoundError(rel)
        return io.BytesIO(self.blobs[rel])
    def resource_path(self, name):
        raise FileNotFoundError(name)
    def is_resource(self, name):
        return (self.prefix + name) in self.blobs
    def contents(self):
        return [r[len(self.prefix):] for r in self.blobs if r.startswith(self.prefix) and "/" not in r[len(self.prefix):]]
class VerifiedImporter(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    # Serves coord_engine, every submodule AND every package resource from the VERIFIED BYTES. The filesystem
    # is never reopened for package code or data. A coord_engine name outside the verified tree is an
    # ImportError, never a fallback to the path importer.
    def __init__(self, root, blobs):
        self.root, self.blobs, self.loaded = root, blobs, {}
    def get_resource_reader(self, fullname):
        parts = fullname.split(".")[1:]
        return VerifiedResources(self.blobs, "/".join(parts) + "/" if parts else "")
    def find_spec(self, fullname, path=None, target=None):
        parts = fullname.split(".")
        if parts[0] != "coord_engine":
            return None
        sub = "/".join(parts[1:])
        for rel, is_pkg in (((sub + "/" if sub else "") + "__init__.py", True), (sub + ".py", False)):
            if rel in self.blobs:
                origin = os.path.join(self.root, rel)
                spec = importlib.machinery.ModuleSpec(fullname, self, origin=origin, is_package=is_pkg)
                spec.has_location = True
                if is_pkg:
                    spec.submodule_search_locations = [os.path.dirname(origin)]
                return spec
        raise ImportError(f"{fullname} is not in the verified tree")
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        rel = os.path.relpath(module.__spec__.origin, self.root)
        self.loaded[module.__name__] = rel
        exec(compile(self.blobs[rel], module.__spec__.origin, "exec", dont_inherit=True), module.__dict__)
def install_verified_importer(pkg, blobs):
    for n in list(sys.modules):
        if n == "coord_engine" or n.startswith("coord_engine."):
            refuse(f"coord_engine was already imported before the verified importer was installed: {n}")
    imp = VerifiedImporter(pkg, blobs)
    sys.meta_path.insert(0, imp)
    return imp
def run_status(team, slug):
    import coord_engine
    from coord_engine import cli
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["review", "status", team, slug, "--json"])
    lines = [l for l in buf.getvalue().splitlines() if l.startswith("{")]
    return rc, (json.loads(lines[-1]) if lines else None), os.path.realpath(coord_engine.__file__)
def rogue_modules(imp, site):
    # r27 (codex-coder, round 24): EVERY loaded module is checked, not only coord_engine names. A module whose
    # file lives under the tool environment and was not served by the verified importer executed unverified code.
    root = os.path.realpath(site) + os.sep
    out = []
    for n, m in list(sys.modules.items()):
        served = getattr(m, "__loader__", None) is imp
        if n == "coord_engine" or n.startswith("coord_engine."):
            if not served:
                out.append(n)
            continue
        f = getattr(m, "__file__", None)
        if f and os.path.realpath(f).startswith(root):
            out.append(n)
    return sorted(out)
def paths_under(site):
    root = os.path.realpath(site) + os.sep
    return [p for p in sys.path if os.path.realpath(p or os.getcwd()).startswith(root) or os.path.realpath(p or os.getcwd()) == root[:-1]]
def canonical_tree_digest(tree):
    return hashlib.sha256(json.dumps(tree, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
def main():
    site, team, slug = sys.argv[1:4]
    dis = sorted(glob.glob(os.path.join(site, "coord_engine-*.dist-info")))
    if len(dis) != 1:
        refuse(f"{len(dis)} coord_engine dist-infos under site-packages; exactly one is required")
    # r28 (codex-reviewer, round 25): the expected tree arrives on STDIN — the parent-child pipe — never via a
    # pathname the child would have to trust. The child echoes a canonical digest of exactly what it received and
    # the parent compares it to its own; a same-count substitution has nowhere to happen.
    try:
        expected = json.loads(sys.stdin.read())
    except ValueError:
        refuse("the expected tree on stdin is not JSON")
    if not isinstance(expected, dict) or not expected or not all(isinstance(k, str) and isinstance(v, str) and len(v) == 40 for k, v in expected.items()):
        refuse("the expected tree on stdin is not a non-empty {relpath: blob-sha1} object")
    tree_digest = canonical_tree_digest(expected)
    pkg, blobs = verify_tree(site, expected)
    if sys.pycache_prefix is None or os.listdir(sys.pycache_prefix):
        refuse("bytecode is not redirected to a fresh empty pycache_prefix; stale __pycache__ could answer")
    try:
        du = json.load(open(os.path.join(dis[0], "direct_url.json")))
    except (OSError, ValueError):
        du = {}
    if paths_under(site):
        refuse(f"the tool environment is on sys.path before attestation: {paths_under(site)[:2]}")
    imp = install_verified_importer(pkg, blobs)
    # r27 (codex-coder, round 24): the tool environment's site-packages is NEVER placed on sys.path. r26 inserted it
    # "for metadata lookups" and thereby let a forged top-level argparse.py in that directory answer for the whole
    # attestation. direct_url.json is read by path above; nothing else from that directory is needed.
    rc, status, file = run_status(team, slug)
    rogue = rogue_modules(imp, site)
    if rogue:
        refuse(f"modules were loaded from the tool environment outside the verified importer: {rogue[:3]}")
    if paths_under(site):
        refuse(f"the tool environment appeared on sys.path during attestation: {paths_under(site)[:2]}")
    print(json.dumps({"file": file, "reported_commit": du.get("vcs_info", {}).get("commit_id"),
                      "tree_verified": len(blobs), "dist_info": os.path.basename(dis[0]),
                      "loader": "verified-bytes", "memory_loaded": len(imp.loaded), "tree_digest": tree_digest,
                      "rc": rc, "status": status}))
    sys.exit(rc)                      # the outer process carries the inner verdict's rc too; both are checked
if __name__ == "__main__":
    main()
"""


def gate_python():
    """THE TRUSTED RUNTIME for the attestation: the interpreter running this gate (a trust root the host
    already relies on, alongside git). NEVER the tool environment's bin/python — codex-reviewer, round 22:
    that file is part of the mutable environment and a wrapper there can forge the whole payload."""
    return sys.executable


def dist_site_packages(exe):
    """The site-packages that holds the coord_engine dist-info beside `exe` — the ONLY path the attestation may import from."""
    root = pathlib.Path(exe).parent.parent
    for di in sorted(root.glob("lib/python*/site-packages/coord_engine-*.dist-info")):
        return os.path.realpath(di.parent)
    return None


def pinned_tree(pin):
    """{relpath: git blob sha1} for coord_engine/** at the PINNED COMMIT, read from the clone ship_check runs in.
    The commit id fixes this tree; nothing inside a tool environment can be regenerated to satisfy it.
    None if the commit is not in the clone (fail closed: fetch it, do not guess)."""
    rc, _, _ = sh("git", "cat-file", "-e", f"{pin}^{{commit}}")
    if rc:
        return None
    rc, out, _ = sh("git", "ls-tree", "-r", "--format=%(objectname) %(path)", f"{pin}:packages/coord-engine/coord_engine")
    if rc or not out:
        return None
    return {path: obj for obj, path in (line.split(" ", 1) for line in out.splitlines() if " " in line)}


def attested_status(exe, team, slug, pin):
    """The status, from a process that PROVES what answered it (codex-coder, round 17): the launcher
    env's interpreter under -I -S (no env, no user site, no .pth, no sitecustomize), NO tool-environment
    path on sys.path at all — the package and its resources are reachable only through VerifiedImporter
    (r28: an earlier version of this docstring instructed the opposite) — the executing bytes verified against the PINNED
    COMMIT's tree (codex-reviewer, round 21), and the fold computed in that same process.
    -> (ok, detail, status_dict_or_None). The verifier is the GATE's interpreter (r25), so the tool
    environment cannot substitute the process that reports on it; and the bytes that EXECUTE are the bytes
    that were VERIFIED (r26): read once, hashed, served by an in-memory importer — the filesystem is never
    reopened for package code, so a replacement after verification cannot answer. The tool environment's
    site-packages is never on sys.path (r27), package resources are served from the verified bytes, and any
    module loaded from that directory outside the verified importer is a refusal."""
    py, site = gate_python(), dist_site_packages(exe)
    if not py or not site:
        return False, f"no site-packages beside {exe} (or no gate interpreter)", None
    tree = pinned_tree(pin)
    if not tree:
        return False, f"the pinned commit {pin} (or its coord_engine tree) is not in this clone — fetch it; not guessing", None
    import tempfile
    fresh_pycache = tempfile.mkdtemp(prefix="coord-fold-attest-pyc-")     # empty: no stale bytecode can be consulted (PEP 552)
    canonical = json.dumps(tree, sort_keys=True, separators=(",", ":"))
    tree_digest = hashlib.sha256(canonical.encode()).hexdigest()
    # r28: the tree goes down the pipe (stdin), never through a file the child would have to trust.
    p = subprocess.run([py, "-I", "-S", "-B", "-X", f"pycache_prefix={fresh_pycache}", "-c", ATTEST, site, team, slug], input=canonical, capture_output=True, text=True, env=engine_env(TRUSTED.get("fulcra-api")))
    try:
        a = json.loads([l for l in p.stdout.splitlines() if l.startswith("{")][-1])
    except (ValueError, IndexError):
        return False, f"attestation did not answer (rc {p.returncode}): {p.stderr.strip()[-200:]}", None
    if not isinstance(a, dict):
        return False, "attestation payload is not an object", None
    if a.get("refused"):
        return False, f"the attestation refused before importing: {a['refused']}", None
    if a.get("tree_verified") != len(tree):
        return False, f"the attestation verified {a.get('tree_verified')!r} files against the pinned tree of {len(tree)}", None
    if a.get("tree_digest") != tree_digest:
        return False, "the attestation verified against a tree whose canonical digest is not the pinned tree's (r28) — a substituted expected-tree, not a count mismatch", None
    if not str(a.get("file", "")).startswith(site + os.sep):
        return False, f"the module that answered lives at {a.get('file')!r}, not under {site} — a startup hook or shadow tree answered", None
    if a.get("loader") != "verified-bytes" or not isinstance(a.get("memory_loaded"), int) or a.get("memory_loaded") < 1:
        return False, "the answering process did not execute the verified bytes through the verified importer (r26)", None
    # BOTH exit codes, before any status is trusted (both reviewers, round 18): a status that
    # prints an APPROVED-shaped tally while returning rc 3 is UNKNOWN, not approval.
    if p.returncode != 0 or a.get("rc") != 0:
        return False, f"the attested review status returned rc {a.get('rc')!r} (process rc {p.returncode}) — UNKNOWN is not approval", None
    status = a.get("status")
    if not isinstance(status, dict) or not isinstance(status.get("state"), str) or not isinstance(status.get("approvals"), list) or not isinstance(status.get("head"), str):
        return False, "the attested status is not a review tally of the expected shape", None
    return True, pin, status                                   # the binding is the tree, not a reported commit


def executing_engine_commit(exe):
    """The build commit of `exe` — the same absolute path that will answer `review status` — from the
    direct_url.json beside its installed dist-info, the identity adopt-latest.sh trusts. None if unprovable."""
    if not exe:
        return None
    root = pathlib.Path(exe).parent.parent                              # <tool-env>/bin/coord-engine -> <tool-env>
    for du in sorted(root.glob("lib/python*/site-packages/coord_engine-*.dist-info/direct_url.json")):
        try:
            commit = json.loads(du.read_text()).get("vcs_info", {}).get("commit_id")
        except (OSError, ValueError):
            return None
        return commit if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit) else None
    return None


def fleet_pin(team: str):
    """The engine pin the fleet runs — from adopt-latest.sh, never from a slug name."""
    rc, body, _ = store_read(f"team/{team}/_coord/bus-v3/adopt-latest.sh")
    m = re.search(r'^PIN="([0-9a-f]{40})"', body, re.M) if rc == 0 else None
    return m.group(1) if m else None


def winning_name_ok(name: str, head: str, reviewer: str) -> bool:
    """Both authoritative forms: the exact-head plain shard, or an append-only envelope."""
    return name == f"{head}--{reviewer}.md" or name.startswith(f"{head}--{reviewer}--")


def main(team: str, head: str, git: str = None, fulcra_api: str = None) -> int:
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        print("ship_check: head must be a 40-hex commit"); return 1
    exe = engine_executable()                                           # THE one resolution of the launcher
    if not exe:
        print("ship_check: coord-engine not found on PATH — refusing"); return 1
    table, why = resolve_trust_roots({"git": git, "fulcra-api": fulcra_api}, tool_env_root(exe))   # r29: stated, once, outside the env
    if table is None:
        print(f"ship_check: {why}"); return 1
    TRUSTED.clear(); TRUSTED.update(table)
    pin = fleet_pin(team)
    if pin is None or pin not in APPROVED_ENGINE_PINS:
        print(f"ship_check: fleet engine pin {pin!r} is not an APPROVED+PINNED corrected engine (approved set: {sorted(APPROVED_ENGINE_PINS)}) — refusing; the fold's ordering contract is not proven on this engine"); return 1
    local = executing_engine_commit(exe)
    if local != pin:
        print(f"ship_check: the coord-engine at {exe} is build {local!r}, not the approved pin {pin} — refusing; a lagging host must not trust its own unapproved fold"); return 1
    slug = f"coord-fold-ship-{head}"
    ok, detail, fold = attested_status(exe, team, slug, pin)            # the answering process attests itself against the PINNED tree
    if not ok:
        print(f"ship_check: {detail} — refusing"); return 1
    if detail != pin:
        print(f"ship_check: the process that answered reports build {detail!r}, not the approved pin {pin} — refusing"); return 1
    rc, at, _ = sh("git", "rev-parse", "HEAD")
    if rc or at != head:
        print(f"ship_check: working tree is at {at!r}, not {head}"); return 1
    rc, dirty, _ = sh("git", "status", "--porcelain", "--", "packages/coord-fold")
    if rc or dirty:
        print("ship_check: packages/coord-fold has uncommitted changes — the on-disk tree is not the commit"); return 1
    rc, tree, _ = sh("git", "rev-parse", f"{head}:packages/coord-fold")
    if rc or not re.fullmatch(r"[0-9a-f]{40}", tree):
        print("ship_check: no packages/coord-fold tree at that commit"); return 1
    if not fold:
        print(f"ship_check: no folded review result for {slug}"); return 1
    winning = fold.get("winning")
    if not isinstance(winning, dict):
        print("ship_check: UNKNOWN — this engine does not expose `winning` (needs review-winning-envelope); refusing rather than refolding filenames"); return 1
    ok = True
    if fold.get("state") != "APPROVED" or fold.get("head") != head or set(REQUIRED) - set(fold.get("approvals", [])):
        print(f"ship_check: folded result is {fold.get('state')} on head {fold.get('head')} with approvals {fold.get('approvals')}"); ok = False
    for reviewer in REQUIRED:
        win = winning.get(reviewer) or {}
        name = win.get("name")
        if not name or not winning_name_ok(name, head, reviewer):
            print(f"ship_check: no winning shard from {reviewer} for {head} (fold says {win})"); ok = False; continue
        if win.get("verdict") != "approve":
            print(f"ship_check: {reviewer}'s winning shard {name} is {win.get('verdict')}, not approve"); ok = False
        rc, body, err = store_read(f"team/{team}/review/{slug}/verdicts/{name}")
        if rc:
            print(f"ship_check: cannot read {name} ({err[:80]})"); ok = False; continue
        verdict = re.search(r"^verdict:\s*(\S+)", body, re.M)
        quoted = re.search(r"^\s*tree:\s*([0-9a-f]{40})", body, re.M)
        if not verdict or verdict.group(1) != "approve":
            print(f"ship_check: {name} says {verdict.group(1) if verdict else 'nothing'}, not approve"); ok = False
        if not quoted or quoted.group(1) != tree:
            print(f"ship_check: {reviewer} quotes tree {quoted.group(1) if quoted else 'none'}, the commit's is {tree}"); ok = False
    print("ship_check: OK — folded APPROVED and both winning shards approve this exact head and tree" if ok else "ship_check: REFUSED")
    return 0 if ok else 1


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="coord-fold ship gate")
    ap.add_argument("team"); ap.add_argument("head")
    ap.add_argument("--git", required=True, help="absolute path of the trusted git (never discovered through PATH)")
    ap.add_argument("--fulcra-api", required=True, dest="fulcra_api", help="absolute path of the trusted fulcra-api")
    a = ap.parse_args()
    raise SystemExit(main(a.team, a.head, git=a.git, fulcra_api=a.fulcra_api))
