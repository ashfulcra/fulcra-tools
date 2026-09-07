"""Task 16 ship check. Exit 0 only with BOTH required responsibility-distribution approvals on the EXACT
commit — the engine's folded result AND, per reviewer, the exact WINNING shard the fold kept (never a
refold of filenames here), each quoting that commit's tree hash for packages/coord-fold.
Fails closed on any absence, including an engine that does not expose `winning`.
TRUST MODEL (r34, stated after codex-reviewer rounds 25-28): the gate defends against the MUTABLE TOOL ENVIRONMENT
(everything an engine install controls: its files, its interpreter, its bytecode, its metadata), against PATH and
environment-variable resolution, and against world-writable or cross-user temporary locations. It does NOT defend
against a concurrent process running as the SAME USER on the gate host: such a process can replace the gate's own
bytes, its interpreter, or its git, so no pathname handoff between the gate and its trusted executables can be
bound against it and none is claimed to be. Within that model every remaining handoff is owned: the temp root is a
gate-created 0700 directory under the user's state dir (an inherited TMPDIR is ignored), the engine child receives
its CLI as an absolute path in FULCRA_CLI_COMMAND (no link, no PATH lookup), and every downloaded body is checked
for owner, mode and non-link status immediately before it is read.
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
APPROVED_ENGINE_PINS: frozenset = frozenset({
    "1994a3ead63189afc4b1e18d159c6a31bb7a5c1c",   # 2026-09-07: the fleet pin moved here (PR #747 merged; store adopt-latest.sh uploaded 16:42:34Z,
                                                  # byte-verified). Register `engine-ship-gate-1994a3ea` read APPROVED by codex-coder and
                                                  # codex-reviewer on this exact head (17:23Z). Carries, on top of e9bbe55b: the forge build
                                                  # budget default 60s -> 300s (PR #744, pin 3901251e, register engine-ship-gate-3901251e also
                                                  # APPROVED by both) and reconcile writing the fence-stamped summaries.json when only the
                                                  # generation's inventory sections refuse publication (PR #746). Nothing in the switch or the
                                                  # fold-serving path changed. The set names exactly the adopted fleet pin; the previous pin
                                                  # leaves when the fleet leaves it.
})


IMPORT_AFFECTING = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONSAFEPATH", "VIRTUAL_ENV", "CONDA_PREFIX")


def engine_env(fulcra_api=None):
    """The environment a child is invoked with: NOTHING that can change which coord_engine imports; PATH is one
    EMPTY private directory (no lookup can succeed); and the engine receives its CLI as an ABSOLUTE PATH in
    FULCRA_CLI_COMMAND, which its transport shlex-splits — no link, no PATH, no pathname the child resolves for
    itself (r34, replacing r29's private-bin symlink: codex-reviewer round 28). The inherited overrides
    (FULCRA_CLI_COMMAND, FULCRA_API_BASE, COORD_TRANSPORT_HTTP) are scrubbed first. (coord-boss 8268376f: a pinned
    launcher answered with the working tree's capabilities because subprocess.run inherited PYTHONPATH.)"""
    import shlex
    env = {k: v for k, v in os.environ.items() if k not in IMPORT_AFFECTING and k not in SCRUBBED_OVERRIDES and not k.startswith("PYTHON")}
    env["PYTHONNOUSERSITE"] = "1"
    env["PATH"] = private_dir("coord-fold-empty-path-")
    if fulcra_api:
        env["FULCRA_CLI_COMMAND"] = shlex.quote(os.path.realpath(fulcra_api))
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


def store_read(remote):
    """Read one store file through the trusted fulcra-api into a PRIVATE temp file and return its text.
    r30 (found by the first real measurement of fleet_pin, 2026-09-05): the real CLI validates LOCAL_FILE as a
    readable path and REFUSES /dev/stdout whenever stdout is a pipe. r34: the file lives under the gate's own temp
    root and is checked for owner, mode and non-link status immediately before the read. -> (rc, text, err)."""
    d = private_dir("coord-fold-store-")
    f = os.path.join(d, "body")
    try:
        rc, _, err = sh("fulcra-api", "file", "download", remote, f)
        if rc:
            return rc, "", err
        try:
            return 0, read_owned_file(f), ""
        except (OSError, PermissionError) as exc:
            return 3, "", f"downloaded body refused: {exc}"
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


def _sibling(name):
    """A module that ships BESIDE this script, loaded by explicit file location (never sys.path, never a package
    import that PATH or cwd could redirect). The same clean-tree check that binds this script to the exact head
    binds its siblings: `git status --porcelain -- packages/coord-fold` must be empty at that commit."""
    import importlib.util
    here = os.path.dirname(os.path.realpath(__file__))
    spec = importlib.util.spec_from_file_location(name, os.path.join(here, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_private = _sibling("ship_check_private")
gate_tmp_root, acl_entries, strip_acls, private_dir, read_owned_file, parse_ls_led = (
    _private.gate_tmp_root, _private.acl_entries, _private.strip_acls, _private.private_dir, _private.read_owned_file, _private.parse_ls_led)
LS_LED_HEADER, LS_LED_ENTRY = _private.LS_LED_HEADER, _private.LS_LED_ENTRY
ATTEST = _sibling("ship_check_attest").ATTEST


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
    try:
        gate_tmp_root()                                                 # r34: our own 0700 temp root; TMPDIR is never consulted
    except (RuntimeError, PermissionError) as exc:
        print(f"ship_check: {exc} — refusing"); return 1                # r39: a root that cannot be made/proven private is a refusal, not a crash
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
