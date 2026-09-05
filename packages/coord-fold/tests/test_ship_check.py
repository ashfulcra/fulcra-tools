"""End-to-end over REAL append-only envelope names. The script consumes `winning` from the typed
surface and never refolds: a same-second earlier APPROVE with a lexically larger digest cannot beat
the later CHANGES the fold kept (both reviewers, round 14)."""
import importlib.util
import json
import pathlib

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "ship_check.py"
spec = importlib.util.spec_from_file_location("ship_check", SCRIPT)
ship_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ship_check)

HEAD = "e67ac6474ebb38a93bc747afd422dfd6935998bc"
OTHER = "b6d867d96abf91ef2d82f066bf2e4977429cbe54"
TREE = "1111111111111111111111111111111111111111"
ENV = {r: f"{HEAD}--{r}--2026-09-05T01:00:24Z-626c9635.md" for r in ("codex-reviewer", "codex-coder")}
APPROVE = f"verdict: approve\ntree: {TREE}"
PIN = "f" * 40


def _approve_pin(monkeypatch, local=PIN):
    monkeypatch.setattr(ship_check, "APPROVED_ENGINE_PINS", frozenset({PIN}))
    monkeypatch.setattr(ship_check, "engine_executable", lambda: "/tool/bin/coord-engine")
    monkeypatch.setattr(ship_check, "executing_engine_commit", lambda exe: local)


def world(*, at=HEAD, dirty="", state="APPROVED", approvals=("codex-reviewer", "codex-coder"), fold_head=HEAD,
          winning=None, bodies=None, pin=PIN):
    if winning is None:
        winning = {r: {"name": n, "verdict": "approve", "sort_key": "2026-09-05T01:00:24.000000Z"} for r, n in ENV.items()}
    if bodies is None:
        bodies = {w["name"]: APPROVE for w in winning.values() if w.get("name")} if isinstance(winning, dict) else {}
    def fake_sh(*argv):
        a = list(argv)
        if a[:2] == ["git", "rev-parse"] and a[2] == "HEAD":
            return 0, at, ""
        if a[:2] == ["git", "status"]:
            return 0, dirty, ""
        if a[:2] == ["git", "rev-parse"]:
            return 0, TREE, ""
        if a[:3] == ["fulcra-api", "file", "download"] and a[3].endswith("adopt-latest.sh"):
            pass
        if a[:1] == ["fulcra-api"] and a[1:3] == ["file", "download"] and not a[3].endswith("adopt-latest.sh"):
            pass
        if a[:2] == ["/tool/bin/coord-engine", "review"] or a[:2] == ["coord-engine", "review"]:
            fold = {"state": state, "head": fold_head, "approvals": list(approvals)}
            if winning != "absent":
                fold["winning"] = winning
            return 0, json.dumps(fold), ""
        if a[:3] == ["fulcra-api", "file", "download"]:
            assert len(a) == 5 and a[4] != "/dev/stdout", a                                       # r30: the real CLI refuses /dev/stdout under a pipe
            if a[3].endswith("adopt-latest.sh"):
                body = f'#!/bin/sh\nPIN="{pin}"   # coord-engine\n' if pin else None
            else:
                n = a[3].rsplit("/", 1)[-1]; body = bodies.get(n)
            if body is None:
                return 1, "", "Error: File not found"
            pathlib.Path(a[4]).write_text(body); return 0, "", ""                                   # the body lands in LOCAL_FILE, as the real CLI does
        raise AssertionError(a)
    return fake_sh


def _attest_from(fake, attested_commit=PIN):
    def attested(exe, team, slug, pin):
        rc, out, _ = fake("coord-engine", "review", "status", team, slug, "--json")
        return True, attested_commit, json.loads(out)
    return attested


def _blob(path):
    import hashlib
    data = pathlib.Path(path).read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _tree_of(site):
    pkg = site / "coord_engine"
    return {str(f.relative_to(pkg)): _blob(f) for f in pkg.rglob("*") if f.is_file() and "__pycache__" not in f.parts}


def run(monkeypatch, local=PIN, attested_commit=PIN, **kw):
    _approve_pin(monkeypatch, local=local)
    fake = world(**kw)
    monkeypatch.setattr(ship_check, "sh", fake)
    monkeypatch.setattr(ship_check, "attested_status", _attest_from(fake, attested_commit))
    import sys
    return ship_check.main("fulcra", HEAD, git=sys.executable, fulcra_api=sys.executable)     # stated roots; sh is faked above


def test_the_answering_process_reporting_another_build_refuses(monkeypatch, capsys):
    assert run(monkeypatch, attested_commit="8d0ed90e000185ca9fc71bc3a95983869d120bbf") == 1
    assert "process that answered reports build" in capsys.readouterr().out


def test_a_pth_or_sitecustomize_shadow_wins_under_site_and_loses_under_the_attestation(tmp_path, monkeypatch):
    """codex-coder round 17: env scrubbing does not stop startup hooks INSIDE the launcher environment.
    Build one with an approved dist-info AND a .pth AND a sitecustomize that prepend a shadow tree."""
    import subprocess, sys
    launcher = _tool_env(tmp_path, APPROVED_CLI)
    site = _site_of(launcher)
    (site / "coord_engine" / "__init__.py").write_text("WHO = 'APPROVED'\n")
    (site / "coord_engine-2.0.6.dist-info" / "RECORD").write_text("\n".join(_record_line(site, r) for r in ("coord_engine/__init__.py", "coord_engine/cli.py")) + "\n")
    shadow = tmp_path / "shadow"; (shadow / "coord_engine").mkdir(parents=True)
    (shadow / "coord_engine" / "__init__.py").write_text("WHO = 'SHADOW'\n")
    (site / "zzz_shadow.pth").write_text(f"import sys; sys.path.insert(0, {str(shadow)!r})\n")
    (site / "sitecustomize.py").write_text(f"import sys; sys.path.insert(0, {str(shadow)!r})\n")
    # the hole: a normal, site-enabled start of the SAME interpreter with this site-packages
    hole = subprocess.run([sys.executable, "-c", f"import site; site.addsitedir({str(site)!r}); import coord_engine; print(coord_engine.WHO)"],
                          capture_output=True, text=True, env=ship_check.engine_env()).stdout.strip()
    assert hole == "SHADOW"
    # the fix: the attestation under -I -S, site-packages NEVER on sys.path, the package reachable only through VerifiedImporter, bytes bound to the pinned tree
    _pin_tree(monkeypatch, launcher)
    ok, commit, status = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert ok and commit == PIN and status["state"] == "APPROVED"


def _record_line(site, rel):
    import base64, hashlib
    data = (site / rel).read_bytes()
    return f"{rel},sha256={base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()},{len(data)}"


def _tool_env(tmp_path, cli_body):
    """A minimal uv-style tool environment: bin/python -> this interpreter, one fake coord_engine, an approved
    dist-info WITH a RECORD binding the package bytes (r22)."""
    import sys
    env = tmp_path / "coord-engine"; (env / "bin").mkdir(parents=True); (env / "bin" / "python").symlink_to(sys.executable)
    launcher = env / "bin" / "coord-engine"; launcher.write_text("#!/bin/sh\n"); launcher.chmod(0o755)
    site = env / "lib" / "python3.13" / "site-packages"; (site / "coord_engine").mkdir(parents=True)
    (site / "coord_engine" / "__init__.py").write_text("")
    (site / "coord_engine" / "cli.py").write_text(cli_body)
    di = site / "coord_engine-2.0.6.dist-info"; di.mkdir()
    (di / "METADATA").write_text("Metadata-Version: 2.1\nName: coord-engine\nVersion: 2.0.6\n")
    (di / "direct_url.json").write_text(json.dumps({"vcs_info": {"commit_id": PIN}}))
    (di / "RECORD").write_text("\n".join(_record_line(site, r) for r in ("coord_engine/__init__.py", "coord_engine/cli.py")) + "\n")
    return launcher


APPROVED_CLI = "import json\ndef main(argv):\n    print(json.dumps({'state': 'APPROVED', 'head': 'x', 'approvals': ['codex-reviewer', 'codex-coder'], 'winning': {}}))\n    return 0\n"


def _site_of(launcher):
    return launcher.parent.parent / "lib" / "python3.13" / "site-packages"


def _pin_tree(monkeypatch, launcher):
    """The pinned commit's tree is what the distribution installed — modelled from the env BEFORE any tampering."""
    tree = _tree_of(_site_of(launcher))
    monkeypatch.setattr(ship_check, "pinned_tree", lambda pin: dict(tree))
    return tree


def test_replacing_cli_and_regenerating_its_record_row_is_refused_by_the_pinned_tree(tmp_path, monkeypatch):
    """codex-reviewer round 21: RECORD and direct_url are mutable; regenerate the row and r23 passed. The pinned
    commit's tree cannot be regenerated from inside the environment."""
    launcher = _tool_env(tmp_path, "def main(argv):\n    print('{}')\n    return 3\n")
    _pin_tree(monkeypatch, launcher)
    site = _site_of(launcher)
    (site / "coord_engine" / "cli.py").write_text(APPROVED_CLI)                                     # replaced...
    (site / "coord_engine-2.0.6.dist-info" / "RECORD").write_text("\n".join(_record_line(site, r) for r in ("coord_engine/__init__.py", "coord_engine/cli.py")) + "\n")   # ...and RECORD regenerated to match
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "does not match the pinned commit's blob" in detail


def test_an_extra_or_missing_file_versus_the_pinned_tree_is_refused(tmp_path, monkeypatch):
    launcher = _tool_env(tmp_path, APPROVED_CLI)
    _pin_tree(monkeypatch, launcher)
    site = _site_of(launcher)
    (site / "coord_engine" / "extra.py").write_text("")
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "does not contain" in detail
    (site / "coord_engine" / "extra.py").unlink(); (site / "coord_engine" / "cli.py").unlink()
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "missing from the installed package" in detail


def test_duplicate_dist_info_is_refused(tmp_path, monkeypatch):
    launcher = _tool_env(tmp_path, APPROVED_CLI)
    _pin_tree(monkeypatch, launcher)
    (_site_of(launcher) / "coord_engine-2.0.5.dist-info").mkdir()
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "exactly one is required" in detail


FORGING_WRAPPER = """#!/bin/sh
# a substituted tool-env interpreter: ignores every flag, reads the expected-tree path from argv, forges the payload
TREE=$(for a in "$@"; do :; done; echo "$a")
N=$(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))))" "$TREE" 2>/dev/null || echo 2)
SITE=$(dirname "$0")/../lib/python3.13/site-packages
printf '{"file": "%s/coord_engine/__init__.py", "reported_commit": "forged", "tree_verified": %s, "dist_info": "x", "rc": 0, "status": {"state": "APPROVED", "head": "x", "approvals": ["codex-reviewer", "codex-coder"], "winning": {}}}\n' "$(cd "$SITE" && pwd -P)" "$N"
exit 0
"""


def test_a_forging_tool_env_interpreter_is_never_run_by_the_gate(tmp_path, monkeypatch):
    """codex-reviewer round 22: the verifier was <tool-env>/bin/python — part of the mutable environment.
    Install a forging wrapper there: run directly it prints a perfect payload (the hole, asserted); the gate
    runs its OWN interpreter instead, so an intact package still attests and a tampered one still refuses."""
    import subprocess
    launcher = _tool_env(tmp_path, "def main(argv):\n    print('{}')\n    return 3\n")          # installed: refuses (rc 3)
    _pin_tree(monkeypatch, launcher)
    wrapper = launcher.parent / "python"; wrapper.unlink(); wrapper.write_text(FORGING_WRAPPER); wrapper.chmod(0o755)
    forged = subprocess.run([str(wrapper), "-I", "-S", "-c", "x", str(_site_of(launcher)), "fulcra", "slug", "/dev/null"], capture_output=True, text=True).stdout
    assert '"state": "APPROVED"' in forged and '"rc": 0' in forged                                   # the wrapper forges when RUN
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "rc 3" in detail                                                              # the gate ran its own interpreter: the real source answered rc 3
    (_site_of(launcher) / "coord_engine" / "cli.py").write_text(APPROVED_CLI)                       # tamper the bytes too
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "does not match the pinned commit's blob" in detail                          # and the tree binding still refuses


def test_the_gate_runtime_is_the_process_interpreter():
    import sys
    assert ship_check.gate_python() == sys.executable


RC3_CLI = "def main(argv):\n    print('{}')\n    return 3\n"
TOCTOU_DRIVER = r"""
import sys, json
site, team, slug, tree_file, attest_file, replace_path, forged_file, mode = sys.argv[1:9]
ns = {"__name__": "attest_lib"}; exec(open(attest_file).read(), ns)      # the attestation as a library; main() does not run
pkg, blobs = ns["verify_tree"](site, json.load(open(tree_file)))         # phase 1: verified
if replace_path != "-":
    open(replace_path, "w").write(open(forged_file).read())              # THE REPLACEMENT: after verification, before import (synchronized, not raced)
if mode == "verified":
    ns["install_verified_importer"](pkg, blobs)                          # r26/r27: site-packages is NOT put on sys.path
else:
    sys.path.insert(0, site)                                             # r25/r26 behaviour: the path importer resolves against the tool environment
rc, status, file = ns["run_status"](team, slug)
print(json.dumps({"rc": rc, "state": status and status.get("state"), "file": file, "out": status and status.get("out")}))
"""


def _driver_run(tmp_path, launcher, tree, replace_path, forged_text, mode, restore=None):
    import subprocess, sys
    site = _site_of(launcher)
    tree_file = tmp_path / "tree.json"; tree_file.write_text(json.dumps(tree))
    attest_file = tmp_path / "attest.py"; attest_file.write_text(ship_check.ATTEST)
    forged = tmp_path / "forged.txt"; forged.write_text(forged_text)
    driver = tmp_path / "driver.py"; driver.write_text(TOCTOU_DRIVER)
    for path, text in (restore or {}).items():
        path.write_text(text)                                                          # restore the verified bytes before each run
    p = subprocess.run([sys.executable, "-I", "-S", "-B", str(driver), str(site), "fulcra", "slug", str(tree_file),
                        str(attest_file), str(replace_path) if replace_path else "-", str(forged), mode],
                       capture_output=True, text=True, env=ship_check.engine_env())
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.splitlines()[-1])


def test_a_replacement_between_verification_and_import_executes_only_the_verified_bytes(tmp_path, monkeypatch):
    """codex-reviewer round 23 (P0, TOCTOU): r25 hashed pathnames and let SourceFileLoader reopen them.
    Synchronized, not raced: verify, then replace cli.py with an APPROVED forgery, then import. The path
    importer executes the forgery (positive control: the hole is real); the verified importer executes the
    bytes that were hashed, so the rc-3 source answers and the forgery on disk is never read."""
    import os
    launcher = _tool_env(tmp_path, RC3_CLI)
    tree = _pin_tree(monkeypatch, launcher)
    site = _site_of(launcher); cli_path = site / "coord_engine" / "cli.py"
    hole = _driver_run(tmp_path, launcher, tree, cli_path, APPROVED_CLI, "path", restore={cli_path: RC3_CLI})
    assert hole["rc"] == 0 and hole["state"] == "APPROVED"                              # the r25 importer executed the replacement
    closed = _driver_run(tmp_path, launcher, tree, cli_path, APPROVED_CLI, "verified", restore={cli_path: RC3_CLI})
    assert closed["rc"] == 3 and closed["state"] != "APPROVED"                         # r26: the verified bytes answered
    assert closed["file"].startswith(str(site.resolve()) + os.sep)                     # reported path is the site path (reporting only)


FORGED_ARGPARSE = """# a forged top-level module in the tool environment's site-packages: answers for the whole attestation
import sys, json
print(json.dumps({"file": sys.argv[1] + "/coord_engine/__init__.py", "reported_commit": "forged", "tree_verified": 2, "dist_info": "x",
                  "loader": "verified-bytes", "memory_loaded": 1, "rc": 0,
                  "status": {"state": "APPROVED", "head": "x", "approvals": ["codex-reviewer", "codex-coder"], "winning": {}}}))
sys.exit(0)
"""
ARGPARSE_CLI = "import argparse\ndef main(argv):\n    print('{}')\n    return 3\n"


def test_a_forged_top_level_module_in_the_tool_environment_is_never_executed(tmp_path, monkeypatch):
    """codex-coder round 24 (P0): r26 put site-packages on sys.path 'for metadata lookups'; a planted argparse.py
    there ran on the pinned cli's first stdlib import and printed a perfect verified-bytes APPROVED payload.
    Positive control under the r26 path insertion: the forgery answers and exits 0. Fixed attestation: the
    directory is never on sys.path, so the stdlib argparse loads and the verified rc-3 source answers."""
    launcher = _tool_env(tmp_path, ARGPARSE_CLI)
    tree = _pin_tree(monkeypatch, launcher)
    site = _site_of(launcher); (site / "argparse.py").write_text(FORGED_ARGPARSE)     # outside coord_engine/: the tree binding cannot see it
    hole = _driver_run(tmp_path, launcher, tree, None, "", "path")
    assert hole.get("loader") == "verified-bytes" and hole["status"]["state"] == "APPROVED" and hole["rc"] == 0   # the forgery answered
    closed = _driver_run(tmp_path, launcher, tree, None, "", "verified")
    assert closed["rc"] == 3 and closed["state"] != "APPROVED" and "loader" not in closed                          # stdlib argparse; verified cli answered


RESOURCE_CLI = ("from importlib.resources import files\n"
                "def main(argv):\n    import json\n    print(json.dumps({'out': files('coord_engine').joinpath('default_models.json').read_text()}))\n    return 3\n")


def test_package_resources_are_served_from_the_verified_bytes(tmp_path, monkeypatch):
    """Package DATA has the same TOCTOU as package code: the engine reads default_models.json through
    importlib.resources. Replace it on disk after verification: the path importer's reader returns the
    replacement (positive control); the verified importer's resource reader returns the hashed bytes."""
    launcher = _tool_env(tmp_path, RESOURCE_CLI)
    site = _site_of(launcher); res = site / "coord_engine" / "default_models.json"; res.write_text('{"verified": true}')
    tree = _pin_tree(monkeypatch, launcher)                                            # tree includes the resource
    hole = _driver_run(tmp_path, launcher, tree, res, '{"replaced": true}', "path", restore={res: '{"verified": true}'})
    assert json.loads(hole["out"]) == {"replaced": True}
    closed = _driver_run(tmp_path, launcher, tree, res, '{"replaced": true}', "verified", restore={res: '{"verified": true}'})
    assert json.loads(closed["out"]) == {"verified": True} and closed["rc"] == 3


def test_no_instruction_anywhere_puts_the_tool_environment_on_sys_path():
    """codex-coder round 25 (P0 plan contradiction): prose, docstring and a test comment still instructed the builder
    to insert the verified site-packages on sys.path, recreating the round-24 bypass with every gate green.
    Static: ATTEST mutates sys.path nowhere; ship_check's source and this file's source carry no such instruction;
    the attestation docstring names the only reachability path."""
    import ast, pathlib
    tree = ast.parse(ship_check.ATTEST)
    def is_sys_path(node):
        return isinstance(node, ast.Attribute) and node.attr == "path" and isinstance(node.value, ast.Name) and node.value.id == "sys"
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and is_sys_path(node.func.value):
            raise AssertionError(f"ATTEST mutates sys.path: sys.path.{node.func.attr}(...) at line {node.lineno}")
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            assert not any(is_sys_path(t) or (isinstance(t, ast.Subscript) and is_sys_path(t.value)) for t in targets), f"ATTEST assigns sys.path at line {node.lineno}"
    stale = ("exactly one path " + "on sys.path", "exactly the verified " + "site-packages", "inserts exactly " + "the verified")
    for src_path in (pathlib.Path(ship_check.__file__), pathlib.Path(__file__)):
        text = src_path.read_text()
        for phrase in stale:
            assert phrase not in text, f"{src_path.name} still carries the stale instruction {phrase!r}"
    assert "VerifiedImporter" in ship_check.attested_status.__doc__ and "sys.path.insert" not in pathlib.Path(ship_check.__file__).read_text()


def test_the_expected_tree_travels_on_stdin_and_its_digest_is_compared_exactly(monkeypatch, tmp_path):
    """codex-reviewer round 25 (P0): the tree was a mutable temp file the child trusted by name and the parent checked
    by COUNT. Now: the parent sends the canonical tree on stdin (no filename in argv) and refuses unless the child's
    echoed canonical digest equals its own."""
    import hashlib, subprocess, types
    launcher = _tool_env(tmp_path, APPROVED_CLI); tree = _pin_tree(monkeypatch, launcher); site = _site_of(launcher)
    calls = []
    real_run = subprocess.run
    def spy(cmd, **kw):
        calls.append((cmd, kw)); return real_run(cmd, **kw)
    monkeypatch.setattr(ship_check.subprocess, "run", spy)
    ok, commit, status = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert ok and status["state"] == "APPROVED"
    cmd, kw = calls[-1]
    assert cmd[cmd.index("-c") + 2:] == [str(site), "fulcra", f"coord-fold-ship-{HEAD}"]                # no tree filename in argv
    canonical = json.dumps(tree, sort_keys=True, separators=(",", ":"))
    assert kw.get("input") == canonical                                                                 # the tree went down the pipe
    # a child that echoes the right COUNT but a different digest (a substituted tree of equal size) is refused
    forged = json.dumps({"file": str(site / "coord_engine" / "__init__.py"), "reported_commit": PIN, "tree_verified": len(tree), "dist_info": "x",
                         "loader": "verified-bytes", "memory_loaded": 1, "tree_digest": hashlib.sha256(b"other").hexdigest(), "rc": 0,
                         "status": {"state": "APPROVED", "head": HEAD, "approvals": ["codex-reviewer", "codex-coder"]}})
    monkeypatch.setattr(ship_check.subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(returncode=0, stdout=forged + "\n", stderr=""))
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "canonical digest" in detail


def _shadow_path(tmp_path, names):
    d = tmp_path / "shadow-bin"; d.mkdir(exist_ok=True)
    for n in names:
        f = d / n; f.write_text("#!/bin/sh\necho FORGED-" + n + "\nexit 0\n"); f.chmod(0o755)
    return d


def test_trust_roots_are_stated_not_discovered_and_a_path_shadow_never_executes(tmp_path, monkeypatch):
    """codex-coder round 26 (P0): ship_check ran bare git and bare fulcra-api through PATH — the same PATH that finds
    the mutable launcher. Positive control: a shadow dir first on PATH hands out forgeries for both. Fixed: nothing
    stated -> refused (PATH never consulted); stated absolute paths -> resolved once; every call executes that path."""
    import os, shutil, subprocess
    shadow = _shadow_path(tmp_path, ("git", "fulcra-api"))
    monkeypatch.setenv("PATH", f"{shadow}{os.pathsep}{os.environ.get('PATH', '')}")
    for n in ("git", "fulcra-api"):
        assert shutil.which(n) == str(shadow / n)
        assert subprocess.run([shutil.which(n), "rev-parse"], capture_output=True, text=True).stdout.strip() == f"FORGED-{n}"
    table, why = ship_check.resolve_trust_roots({}, "/tool")
    assert table is None and "absolute path" in why
    trusted = tmp_path / "trusted"; trusted.mkdir()
    for n in ("git", "fulcra-api"):
        f = trusted / n; f.write_text("#!/bin/sh\necho TRUSTED-" + n + "\n"); f.chmod(0o755)
    table, why = ship_check.resolve_trust_roots({"git": str(trusted / "git"), "fulcra-api": str(trusted / "fulcra-api")}, "/tool")
    assert why is None and table == {n: os.path.realpath(trusted / n) for n in ("git", "fulcra-api")}
    monkeypatch.setattr(ship_check, "TRUSTED", dict(table))
    calls = []; real = subprocess.run
    monkeypatch.setattr(ship_check.subprocess, "run", lambda cmd, **kw: (calls.append(list(cmd)), real(cmd, **kw))[1])
    assert ship_check.sh("git", "rev-parse", "HEAD")[1] == "TRUSTED-git"
    assert ship_check.sh("fulcra-api", "file", "download", "x", "/dev/stdout")[1] == "TRUSTED-fulcra-api"
    OS_ROOTS = ("/bin/chmod", "/bin/ls")                                                       # r38: the ACL helpers call the OS's own chmod/ls by absolute path (OS trust roots, like the interpreter)
    assert [c[0] for c in calls if c[0] not in OS_ROOTS] == [table["git"], table["fulcra-api"]]  # every OTHER call is an absolute trusted path
    assert all(c[0].startswith("/") for c in calls)                                              # and nothing is ever resolved through PATH
    monkeypatch.setattr(ship_check, "TRUSTED", {})
    import pytest
    with pytest.raises(RuntimeError, match="bare name never executes"):
        ship_check.sh("git", "rev-parse", "HEAD")


def test_a_trust_root_under_the_tool_environment_is_refused(tmp_path):
    env = tmp_path / "coord-engine"; (env / "bin").mkdir(parents=True)
    g = env / "bin" / "git"; g.write_text("#!/bin/sh\n"); g.chmod(0o755)
    outside = tmp_path / "outside"; outside.mkdir(); link = outside / "git"; link.symlink_to(g)   # OUTSIDE, pointing INSIDE
    fa = tmp_path / "fulcra-api"; fa.write_text("#!/bin/sh\n"); fa.chmod(0o755)
    for stated in (str(g), str(link)):
        table, why = ship_check.resolve_trust_roots({"git": stated, "fulcra-api": str(fa)}, str(env))
        assert table is None and "under the tool environment" in why
    table, why = ship_check.resolve_trust_roots({"git": "git", "fulcra-api": str(fa)}, str(env))
    assert table is None and "absolute path" in why                                             # a bare name is not a statement
    table, why = ship_check.resolve_trust_roots({"git": str(tmp_path / "missing"), "fulcra-api": str(fa)}, str(env))
    assert table is None and "not an executable file" in why


def test_a_swap_after_resolution_does_not_change_what_executes(tmp_path, monkeypatch):
    import os
    a = tmp_path / "A"; a.write_text("#!/bin/sh\necho A\n"); a.chmod(0o755)
    b = tmp_path / "B"; b.write_text("#!/bin/sh\necho B\n"); b.chmod(0o755)
    link = tmp_path / "git"; link.symlink_to(a)
    fa = tmp_path / "fulcra-api"; fa.write_text("#!/bin/sh\n"); fa.chmod(0o755)
    table, why = ship_check.resolve_trust_roots({"git": str(link), "fulcra-api": str(fa)}, "/tool")
    assert why is None and table["git"] == os.path.realpath(a)
    monkeypatch.setattr(ship_check, "TRUSTED", dict(table))
    link.unlink(); link.symlink_to(b)                                                            # THE SWAP, after resolution
    monkeypatch.setenv("PATH", str(tmp_path))                                                   # and PATH now hands out B too
    assert ship_check.sh("git", "anything")[1] == "A"                                            # the once-resolved realpath executes


def test_the_attestation_child_can_reach_only_the_stated_fulcra_api(tmp_path, monkeypatch):
    import os, shutil
    shadow = _shadow_path(tmp_path, ("fulcra-api",))
    monkeypatch.setenv("PATH", f"{shadow}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FULCRA_CLI_COMMAND", "evil"); monkeypatch.setenv("FULCRA_API_BASE", "https://evil.invalid"); monkeypatch.setenv("COORD_TRANSPORT_HTTP", "0")
    trusted = tmp_path / "trusted-fulcra-api"; trusted.write_text("#!/bin/sh\n"); trusted.chmod(0o755)
    assert shutil.which("fulcra-api") == str(shadow / "fulcra-api")                              # positive control: inherited PATH hands out the forgery
    env = ship_check.engine_env(str(trusted))
    entries = env["PATH"].split(os.pathsep)
    assert len(entries) == 1 and os.listdir(entries[0]) == []                                   # r34: NO link at all; PATH resolves nothing
    assert shutil.which("fulcra-api", path=env["PATH"]) is None and shutil.which("sh", path=env["PATH"]) is None
    import shlex
    assert shlex.split(env["FULCRA_CLI_COMMAND"]) == [os.path.realpath(trusted)]                # the engine is HANDED the absolute path (its transport shlex-splits)
    for k in ("FULCRA_API_BASE", "COORD_TRANSPORT_HTTP"):
        assert k not in env
    assert "FULCRA_CLI_COMMAND" not in ship_check.engine_env() and os.listdir(ship_check.engine_env()["PATH"]) == []   # nothing stated -> nothing reachable; inherited 'evil' scrubbed


def test_main_refuses_without_stated_trust_roots_before_touching_the_store(monkeypatch, capsys):
    _approve_pin(monkeypatch)
    touched = []
    monkeypatch.setattr(ship_check, "sh", lambda *a: (touched.append(a), (1, "", ""))[1])
    assert ship_check.main("fulcra", HEAD) == 1
    assert "never discovered through PATH" in capsys.readouterr().out and touched == []


REAL_CLI_REFUSAL = "Error: Invalid value for '[LOCAL_FILE]': Path '/dev/stdout' is not readable."
FAKE_FULCRA_API = """#!/bin/sh
# behaves like the real fulcra-api file download (measured 2026-09-05): refuses /dev/stdout, writes LOCAL_FILE
if [ "$1" != "file" ] || [ "$2" != "download" ]; then echo "unexpected: $*" >&2; exit 64; fi
if [ "$4" = "/dev/stdout" ]; then echo "REAL_CLI_REFUSAL" >&2; exit 2; fi
case "$3" in
  */adopt-latest.sh) printf '#!/bin/sh\nPIN="PIN_VALUE"   # coord-engine\n' > "$4" ;;
  *) printf 'verdict: approve\ntree: TREE_VALUE\n' > "$4" ;;
esac
"""


def test_store_reads_go_through_a_private_file_because_the_real_cli_refuses_dev_stdout(tmp_path, monkeypatch):
    """r30. Positive control: a fulcra-api that behaves like the real one refuses `download ... /dev/stdout` (rc 2) —
    which is what every revision before r30 did, unmeasured. The fix reads through a private temp file."""
    import os
    fa = tmp_path / "fulcra-api"; fa.write_text(FAKE_FULCRA_API.replace("REAL_CLI_REFUSAL", REAL_CLI_REFUSAL).replace("PIN_VALUE", PIN).replace("TREE_VALUE", TREE)); fa.chmod(0o755)
    g = tmp_path / "git"; g.write_text("#!/bin/sh\n"); g.chmod(0o755)
    table, why = ship_check.resolve_trust_roots({"git": str(g), "fulcra-api": str(fa)}, "/tool"); assert why is None
    monkeypatch.setattr(ship_check, "TRUSTED", dict(table))
    rc, out, err = ship_check.sh("fulcra-api", "file", "download", "team/fulcra/_coord/bus-v3/adopt-latest.sh", "/dev/stdout")
    assert rc == 2 and "not readable" in err and out == ""                                        # the hole: nothing ever came back
    assert ship_check.fleet_pin("fulcra") == PIN                                                   # the fix: read through a file
    rc, body, _ = ship_check.store_read("team/fulcra/review/x/verdicts/y.md")
    assert rc == 0 and f"tree: {TREE}" in body
    assert not [d for d in os.listdir(tempfile_dir()) if d.startswith("coord-fold-store-")]        # nothing left behind


def tempfile_dir():
    import tempfile
    return tempfile.gettempdir()


def test_the_real_cli_accepts_the_runbook_invocation_and_refuses_the_bare_form(tmp_path):
    """codex-coder round 27: Task 14/16 runbooks still invoked `ship_check.py fulcra HEAD` while argparse required
    --git/--fulcra-api, so the mandatory gate always exited at argument parsing. End-to-end through the REAL CLI, so a
    main()-level unit call cannot mask drift: the bare form exits 2 at parsing (positive control); the runbook form
    parses, resolves the stated roots, reads the fleet pin, and reaches the pin check (rc 1, 'not an APPROVED+PINNED'
    under the shipped empty approved set)."""
    import os, subprocess, sys
    fa = tmp_path / "fulcra-api"; fa.write_text(FAKE_FULCRA_API.replace("REAL_CLI_REFUSAL", REAL_CLI_REFUSAL).replace("PIN_VALUE", PIN).replace("TREE_VALUE", TREE)); fa.chmod(0o755)
    g = tmp_path / "git"; g.write_text("#!/bin/sh\necho " + HEAD + "\n"); g.chmod(0o755)
    launcher_dir = tmp_path / "env" / "bin"; launcher_dir.mkdir(parents=True); (launcher_dir / "coord-engine").write_text("#!/bin/sh\n"); (launcher_dir / "coord-engine").chmod(0o755)
    env = {**os.environ, "PATH": str(launcher_dir)}                                                # the launcher is discovered (env root = tmp/env); the roots are stated OUTSIDE it
    bare = subprocess.run([sys.executable, str(SCRIPT), "fulcra", HEAD], capture_output=True, text=True, env=env)
    assert bare.returncode == 2 and "--git" in bare.stderr and "required" in bare.stderr             # the old runbook form: dead at parsing
    run = subprocess.run([sys.executable, str(SCRIPT), "fulcra", HEAD, "--git", str(g), "--fulcra-api", str(fa)], capture_output=True, text=True, env=env)
    assert run.returncode == 1 and "not an APPROVED+PINNED" in run.stdout, (run.stdout, run.stderr)  # parsed, roots resolved, pin read, refused on the empty approved set


def test_a_same_count_tree_substitution_cannot_bind_tampered_bytes(tmp_path, monkeypatch):
    """Synchronized, not raced. Positive control (the r27 hole as a library flow): tamper cli.py, build the attacker's
    tree of the SAME SIZE from the tampered site, substitute it for the real tree file after the parent wrote it and
    before the child read it — verify_tree accepts and the count matches. Fixed gate: the real tree goes down the
    pipe, so the tampered bytes fail the blob check; the substituted file, wherever it is planted, is never read."""
    launcher = _tool_env(tmp_path, RC3_CLI); real_tree = _pin_tree(monkeypatch, launcher); site = _site_of(launcher)
    (site / "coord_engine" / "cli.py").write_text(APPROVED_CLI)                                        # tampered bytes
    attacker_tree = _tree_of(site)
    assert len(attacker_tree) == len(real_tree) and attacker_tree != real_tree
    tree_file = tmp_path / "attest.tree.json"; tree_file.write_text(json.dumps(real_tree))            # the parent's write
    tree_file.write_text(json.dumps(attacker_tree))                                                    # THE SUBSTITUTION, before the child's read
    ns = {"__name__": "attest_lib"}; exec(ship_check.ATTEST, ns)
    _, blobs = ns["verify_tree"](str(site), json.load(open(tree_file)))                                # r27 child: trusts the pathname
    assert len(blobs) == len(real_tree)                                                                # r27 parent: count matches -> accepted (the hole)
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "does not match the pinned commit's blob" in detail                            # r28: the real tree came down the pipe


def test_the_attestation_refuses_when_the_tool_environment_is_on_sys_path(tmp_path, monkeypatch):
    launcher = _tool_env(tmp_path, RC3_CLI); _pin_tree(monkeypatch, launcher); site = _site_of(launcher)
    import subprocess, sys
    probe = tmp_path / "probe.py"; probe.write_text(
        "import sys\nsite = sys.argv[1]\nns = {'__name__': 'attest_lib'}; exec(open(sys.argv[2]).read(), ns)\n"
        "sys.path.insert(0, site)\nprint('under', ns['paths_under'](site) == [site])\n")
    attest_file = tmp_path / "attest.py"; attest_file.write_text(ship_check.ATTEST)
    p = subprocess.run([sys.executable, "-I", "-S", "-B", str(probe), str(site), str(attest_file)], capture_output=True, text=True)
    assert "under True" in p.stdout, p.stderr


def test_the_attestation_refuses_a_coord_engine_module_loaded_outside_the_verified_importer(tmp_path, monkeypatch):
    """The positive post-check: if any coord_engine* module came from another loader, the process refuses."""
    import subprocess, sys
    launcher = _tool_env(tmp_path, RC3_CLI); _pin_tree(monkeypatch, launcher); site = _site_of(launcher)
    probe = tmp_path / "probe.py"; probe.write_text(
        "import sys, json\nsite = sys.argv[1]\nns = {'__name__': 'attest_lib'}; exec(open(sys.argv[2]).read(), ns)\n"
        "sys.path.insert(0, site); import coord_engine\n"                                   # loaded by the path importer FIRST
        "try:\n    ns['install_verified_importer'](site + '/coord_engine', {})\nexcept SystemExit as e:\n    print('exit', e.code)\n")
    attest_file = tmp_path / "attest.py"; attest_file.write_text(ship_check.ATTEST)
    p = subprocess.run([sys.executable, "-I", "-S", "-B", str(probe), str(site), str(attest_file)], capture_output=True, text=True)
    assert "already imported before the verified importer" in p.stdout and "exit 2" in p.stdout


def test_a_pin_not_in_the_clone_refuses(tmp_path, monkeypatch):
    launcher = _tool_env(tmp_path, APPROVED_CLI)
    monkeypatch.setattr(ship_check, "pinned_tree", lambda pin: None)
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "not in this clone" in detail


def test_pinned_tree_reads_the_commit_from_a_real_git_clone_and_the_intact_package_attests(tmp_path, monkeypatch):
    """The positive path through real git: commit the package, read its tree, verify the installed copy."""
    import subprocess, os
    launcher = _tool_env(tmp_path, APPROVED_CLI)
    site = _site_of(launcher)
    repo = tmp_path / "repo"; (repo / "packages" / "coord-engine").mkdir(parents=True)
    import shutil as _sh
    _sh.copytree(site / "coord_engine", repo / "packages" / "coord-engine" / "coord_engine")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.invalid"}
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "pin"]):
        subprocess.run(cmd, cwd=repo, check=True, env=env, capture_output=True)
    pin = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    monkeypatch.chdir(repo)
    import shutil, sys
    monkeypatch.setattr(ship_check, "TRUSTED", {"git": os.path.realpath(shutil.which("git")), "fulcra-api": sys.executable})   # r29: stated by the test, as the operator would
    tree = ship_check.pinned_tree(pin)
    assert tree == _tree_of(site)
    ok, commit, status = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", pin)
    assert ok and commit == pin and status["state"] == "APPROVED"


def test_a_replaced_cli_with_any_record_content_is_refused_by_the_pinned_tree(tmp_path, monkeypatch):
    """Round 20's hashless-row case and round 21's regenerated-row case are the same case under r24: RECORD is
    not consulted; the pinned tree is."""
    launcher = _tool_env(tmp_path, "def main(argv):\n    print('{}')\n    return 3\n")
    _pin_tree(monkeypatch, launcher)
    site = _site_of(launcher)
    (site / "coord_engine" / "cli.py").write_text(APPROVED_CLI)
    (site / "coord_engine-2.0.6.dist-info" / "RECORD").write_text(_record_line(site, "coord_engine/__init__.py") + "\ncoord_engine/cli.py,,\n")
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "does not match the pinned commit's blob" in detail


def test_stale_unchecked_hash_bytecode_answers_under_a_normal_import_and_never_under_the_attestation(tmp_path, monkeypatch):
    """codex-coder, round 20: __pycache__ was excluded from the check, and an unchecked-hash .pyc is executed
    without consulting the source. The verified source returns rc 3; a stale pyc compiled from APPROVED_CLI sits
    beside it. Normal import: the pyc answers APPROVED. Attestation (-B, fresh pycache_prefix): the source answers."""
    import importlib.util, py_compile, subprocess, sys
    launcher = _tool_env(tmp_path, "def main(argv):\n    print('{\"state\": \"APPROVED\", \"head\": \"x\", \"approvals\": [], \"winning\": {}}')\n    return 3\n")   # what RECORD verifies
    site = _site_of(launcher)
    stale_src = tmp_path / "stale_cli.py"; stale_src.write_text(APPROVED_CLI)
    pyc = pathlib.Path(importlib.util.cache_from_source(str(site / "coord_engine" / "cli.py")))
    pyc.parent.mkdir(parents=True, exist_ok=True)
    py_compile.compile(str(stale_src), cfile=str(pyc), dfile=str(site / "coord_engine" / "cli.py"), invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
    hole = subprocess.run([sys.executable, "-S", "-c", f"import site; site.addsitedir({str(site)!r}); from coord_engine import cli; print(cli.main(['x','x','x','x']))"],
                          capture_output=True, text=True, env=ship_check.engine_env()).stdout.strip().splitlines()
    assert hole and hole[-1] == "0", hole                              # the stale bytecode answered rc 0 (the hole, asserted)
    _pin_tree(monkeypatch, launcher)
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "rc 3" in detail                                 # the verified SOURCE answered (rc 3) — bytecode never consulted


def test_a_sourceless_pyc_under_the_package_is_refused(tmp_path, monkeypatch):
    launcher = _tool_env(tmp_path, APPROVED_CLI)
    _pin_tree(monkeypatch, launcher)
    (_site_of(launcher) / "coord_engine" / "helper.pyc").write_bytes(b"\x00")
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and ("could answer" in detail or "does not contain" in detail)


def test_a_recorded_intact_package_attests_and_answers(tmp_path, monkeypatch):
    launcher = _tool_env(tmp_path, APPROVED_CLI)
    _pin_tree(monkeypatch, launcher)
    ok, commit, status = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert ok and commit == PIN and status["state"] == "APPROVED"


def test_an_approved_shaped_status_that_returns_rc_3_is_refused(tmp_path, monkeypatch):
    """Both reviewers, round 18: the inner verdict's rc was recorded and never checked."""
    launcher = _tool_env(tmp_path, "import json\ndef main(argv):\n    print(json.dumps({'state': 'APPROVED', 'head': 'x', 'approvals': ['codex-reviewer', 'codex-coder'], 'winning': {}}))\n    return 3\n")
    _pin_tree(monkeypatch, launcher)
    ok, detail, status = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "rc 3" in detail and status is None


def test_a_status_of_the_wrong_shape_is_refused(tmp_path, monkeypatch):
    launcher = _tool_env(tmp_path, "def main(argv):\n    print('[1, 2]')\n    return 0\n")
    _pin_tree(monkeypatch, launcher)
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "expected shape" in detail


def test_the_attestation_refuses_a_module_answering_from_outside_the_verified_site(monkeypatch, tmp_path):
    import subprocess, sys
    env = tmp_path / "coord-engine"; (env / "bin").mkdir(parents=True); (env / "bin" / "python").symlink_to(sys.executable)
    launcher = env / "bin" / "coord-engine"; launcher.write_text("#!/bin/sh\n"); launcher.chmod(0o755)
    site = env / "lib" / "python3.13" / "site-packages"; (site / "coord_engine-2.0.6.dist-info").mkdir(parents=True)
    (site / "coord_engine-2.0.6.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\nName: coord-engine\nVersion: 2.0.6\n")
    (site / "coord_engine-2.0.6.dist-info" / "direct_url.json").write_text(json.dumps({"vcs_info": {"commit_id": PIN}}))
    (site / "coord_engine-2.0.6.dist-info" / "RECORD").write_text("coord_engine-2.0.6.dist-info/METADATA,,\n")
    elsewhere = tmp_path / "elsewhere"; (elsewhere / "coord_engine").mkdir(parents=True)
    (elsewhere / "coord_engine" / "__init__.py").write_text("")
    (elsewhere / "coord_engine" / "cli.py").write_text("def main(argv):\n    print('{}')\n    return 0\n")
    # no coord_engine package under site; a matching tree exists ELSEWHERE. r28 (codex-coder round 25): the r27 form of
    # this test monkeypatched a sys.path.insert that no longer existed and passed vacuously. Now: the pinned tree is the
    # elsewhere tree; nothing under site can satisfy it, and nothing elsewhere can answer (no sys.path, no importer entry).
    monkeypatch.setattr(ship_check, "pinned_tree", lambda pin: _tree_of(elsewhere))
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "missing from the installed package" in detail


def test_the_executable_is_resolved_exactly_once_and_that_path_is_what_executes(monkeypatch):
    """Both reviewers, round 16 (TOCTOU): `which` answers approved A first and stale B afterwards.
    The gate must resolve once and execute A; B must never be consumed."""
    answers = iter(["/tool-A/bin/coord-engine", "/tool-B/bin/coord-engine"])
    resolutions = []
    def which(name):
        r = next(answers); resolutions.append(r); return r
    monkeypatch.setattr(ship_check.shutil, "which", which)
    monkeypatch.setattr(ship_check.os.path, "realpath", lambda p: p)
    monkeypatch.setattr(ship_check, "APPROVED_ENGINE_PINS", frozenset({PIN}))
    identity_reads, executed = [], []
    def commit(exe):
        identity_reads.append(exe); return PIN
    monkeypatch.setattr(ship_check, "executing_engine_commit", commit)
    fake = world()
    monkeypatch.setattr(ship_check, "sh", fake)
    def attested(exe, team, slug, pin):
        executed.append(exe); rc, out, _ = fake("coord-engine", "review", "status", team, slug, "--json"); return True, PIN, json.loads(out)
    monkeypatch.setattr(ship_check, "attested_status", attested)
    import sys
    assert ship_check.main("fulcra", HEAD, git=sys.executable, fulcra_api=sys.executable) == 0
    assert resolutions == ["/tool-A/bin/coord-engine"]                      # exactly one resolution
    assert identity_reads == ["/tool-A/bin/coord-engine"] and executed == ["/tool-A/bin/coord-engine"]   # A verified, A executed, B never touched


def test_remote_pin_approved_but_the_executing_engine_is_another_build_refuses(monkeypatch, capsys):
    """codex-reviewer round 14: a lagging host names the approved pin while PATH still runs an older engine."""
    assert run(monkeypatch, local="8d0ed90e000185ca9fc71bc3a95983869d120bbf") == 1
    assert "not the approved pin" in capsys.readouterr().out


def test_an_unprovable_executing_engine_refuses(monkeypatch):
    assert run(monkeypatch, local=None) == 1


def test_engine_env_strips_every_import_affecting_variable(monkeypatch):
    for k in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONWARNINGS", "VIRTUAL_ENV", "CONDA_PREFIX"):
        monkeypatch.setenv(k, "x")
    env = ship_check.engine_env()
    assert not any(k.startswith("PYTHON") for k in env if k != "PYTHONNOUSERSITE")
    assert env["PYTHONNOUSERSITE"] == "1" and "VIRTUAL_ENV" not in env and "CONDA_PREFIX" not in env


def test_a_shadow_coord_engine_on_pythonpath_is_not_imported_under_the_scrubbed_env(tmp_path, monkeypatch):
    """coord-boss's reproduction, as a test: same launcher, same interpreter — PYTHONPATH decides which
    coord_engine answers. (-S on the child: a uv workspace venv has the REAL coord_engine installed and it would
    answer first; PYTHONPATH is still honoured under -S, so the shadow still wins when inherited.) Original text:
    coord_engine answers. Under ship_check's env the site-packages build (the one the identity check proved) wins."""
    import subprocess, sys
    site = tmp_path / "env" / "lib" / "python3.13" / "site-packages"; (site / "coord_engine").mkdir(parents=True)
    (site / "coord_engine" / "__init__.py").write_text("WHO = 'PINNED'\n")
    shadow = tmp_path / "shadow"; (shadow / "coord_engine").mkdir(parents=True)
    (shadow / "coord_engine" / "__init__.py").write_text("WHO = 'SHADOW'\n")
    launcher = tmp_path / "env" / "bin" / "coord-engine"; launcher.parent.mkdir(parents=True)
    launcher.write_text(f"#!{sys.executable} -S\nimport sys, site\nsite.addsitedir({str(site)!r})\nimport coord_engine\nprint(coord_engine.WHO)\n"); launcher.chmod(0o755)
    monkeypatch.setenv("PYTHONPATH", str(shadow))
    inherited = subprocess.run([str(launcher)], capture_output=True, text=True).stdout.strip()
    assert inherited == "SHADOW"                                                    # the hole, reproduced
    fixed = subprocess.run([str(launcher)], capture_output=True, text=True, env=ship_check.engine_env()).stdout.strip()
    assert fixed == "PINNED"                                                        # the env fix (necessary, not sufficient: see the .pth test)


def test_executing_engine_commit_reads_direct_url_beside_the_dist_info(tmp_path, monkeypatch):
    env = tmp_path / "coord-engine"; (env / "bin").mkdir(parents=True)
    exe = env / "bin" / "coord-engine"; exe.write_text("#!/bin/sh\n"); exe.chmod(0o755)
    di = env / "lib" / "python3.13" / "site-packages" / "coord_engine-2.0.6.dist-info"; di.mkdir(parents=True)
    (di / "direct_url.json").write_text(json.dumps({"url": "https://github.com/ashfulcra/fulcra-tools", "vcs_info": {"vcs": "git", "commit_id": PIN}, "subdirectory": "packages/coord-engine"}))
    assert ship_check.executing_engine_commit(str(exe)) == PIN
    (di / "direct_url.json").unlink()
    assert ship_check.executing_engine_commit(str(exe)) is None


def test_the_shipped_approved_set_is_exactly_the_adopted_fleet_pin_and_any_other_pin_refuses(monkeypatch, capsys):
    """r38: the fleet pin moved to e06e69e5 (PR #698 + store upload, 2026-09-05), the build that carries the approved
    supersession contract (#695). The approved set names exactly it; a fleet pin outside it still refuses."""
    assert ship_check.APPROVED_ENGINE_PINS == frozenset({"e06e69e5d44d92b2b52a09020f53f2bd1ccdc1d5"})
    monkeypatch.setattr(ship_check, "sh", world(pin="0" * 40))
    monkeypatch.setattr(ship_check, "engine_executable", lambda: "/tool/bin/coord-engine")
    import sys
    assert ship_check.main("fulcra", HEAD, git=sys.executable, fulcra_api=sys.executable) == 1 and "not an APPROVED+PINNED" in capsys.readouterr().out
def test_a_pin_outside_the_approved_set_refuses(monkeypatch):
    assert run(monkeypatch, pin="e" * 40) == 1


def test_a_missing_adopt_latest_refuses(monkeypatch):
    assert run(monkeypatch, pin=None) == 1


def test_the_plain_exact_head_form_is_an_accepted_winning_name(monkeypatch):
    plain = {r: f"{HEAD}--{r}.md" for r in ENV}
    winning = {r: {"name": n, "verdict": "approve", "sort_key": "x"} for r, n in plain.items()}
    assert run(monkeypatch, winning=winning, bodies={n: APPROVE for n in plain.values()}) == 0


def test_both_exact_head_and_tree_approvals_pass(monkeypatch, capsys):
    assert run(monkeypatch) == 0 and "OK" in capsys.readouterr().out


def test_same_second_earlier_approve_with_larger_digest_does_not_beat_the_winning_changes(monkeypatch, capsys):
    """The round-14 hole. The fold (engine) kept the later CHANGES (digest 058ddb93) and says so in
    `winning`; the earlier APPROVE (feb86aee) exists too. The script must read winning, never max(name)."""
    later_changes = f"{HEAD}--codex-coder--2026-09-05T01:32:10Z-058ddb93.md"
    earlier_approve = f"{HEAD}--codex-coder--2026-09-05T01:32:10Z-feb86aee.md"
    winning = {"codex-reviewer": {"name": ENV["codex-reviewer"], "verdict": "approve", "sort_key": "2026-09-05T01:00:24.000000Z"},
               "codex-coder": {"name": later_changes, "verdict": "changes", "sort_key": "2026-09-05T01:32:10.900000Z"}}
    bodies = {ENV["codex-reviewer"]: APPROVE, later_changes: f"verdict: changes\ntree: {TREE}", earlier_approve: APPROVE}
    assert run(monkeypatch, winning=winning, bodies=bodies, state="CHANGES", approvals=("codex-reviewer",)) == 1
    assert "058ddb93" in capsys.readouterr().out


def test_engine_without_winning_is_unknown_and_refuses(monkeypatch, capsys):
    assert run(monkeypatch, winning="absent") == 1 and "does not expose `winning`" in capsys.readouterr().out


def test_winning_shard_on_another_head_refuses(monkeypatch):
    winning = {r: {"name": n.replace(HEAD, OTHER), "verdict": "approve", "sort_key": "x"} for r, n in ENV.items()}
    assert run(monkeypatch, winning=winning, bodies={w["name"]: APPROVE for w in winning.values()}, fold_head=OTHER) == 1


def test_changes_verdict_refuses(monkeypatch):
    winning = {"codex-reviewer": {"name": ENV["codex-reviewer"], "verdict": "approve", "sort_key": "x"},
               "codex-coder": {"name": ENV["codex-coder"], "verdict": "changes", "sort_key": "x"}}
    bodies = {ENV["codex-reviewer"]: APPROVE, ENV["codex-coder"]: f"verdict: changes\ntree: {TREE}"}
    assert run(monkeypatch, winning=winning, bodies=bodies, state="CHANGES", approvals=("codex-reviewer",)) == 1


def test_missing_reviewer_refuses(monkeypatch):
    winning = {"codex-reviewer": {"name": ENV["codex-reviewer"], "verdict": "approve", "sort_key": "x"}}
    assert run(monkeypatch, winning=winning, approvals=("codex-reviewer",), state="PENDING") == 1


def test_dirty_package_refuses(monkeypatch):
    assert run(monkeypatch, dirty=" M packages/coord-fold/coord_fold/fold.py") == 1


def test_tree_mismatch_refuses(monkeypatch):
    bodies = {n: "verdict: approve\ntree: 2222222222222222222222222222222222222222" for n in ENV.values()}
    assert run(monkeypatch, bodies=bodies) == 1


def test_winning_says_approve_but_the_shard_body_does_not_refuses(monkeypatch):
    bodies = {ENV["codex-reviewer"]: APPROVE, ENV["codex-coder"]: f"verdict: changes\ntree: {TREE}"}
    assert run(monkeypatch, bodies=bodies) == 1


def test_no_repo_prose_invokes_ship_check_without_stated_trust_roots():
    """codex-coder rounds 27/28: the prose contract drifted from argparse twice (Task 14, then Task 16). Every
    invocation written in repo prose — AGENTS.md, the package README, the script's own Usage — must carry the
    stated trust roots on the same line. (The plan document itself is checked by Task 0's materialize_plan.)"""
    import importlib.util
    mp_spec = importlib.util.spec_from_file_location("materialize_plan", SCRIPT.parent / "materialize_plan.py"); mp = importlib.util.module_from_spec(mp_spec); mp_spec.loader.exec_module(mp)
    bare = mp.bare_invocations                                                                    # ONE parser, shared with the plan gate (r34: the r33 regex checked only --git)
    here = pathlib.Path(__file__).resolve()
    files = [f for f in (here.parents[3] / "AGENTS.md", here.parents[1] / "README.md", SCRIPT) if f.exists()]   # a materialized plan tree has no repo AGENTS.md
    assert len(files) >= 2, files
    for f in files:
        for i, ln in enumerate(f.read_text().splitlines(), 1):
            assert not bare(ln), f"{f.name}:{i}: {bare(ln)}"
    assert bare("`scripts/ship_check.py fulcra <HEAD>` and fails closed")                          # the sentence that drifted
    assert bare("scripts/ship_check.py fulcra <HEAD> --git /usr/bin/git") == ["missing --fulcra-api: scripts/ship_check.py fulcra <HEAD> --git /usr/bin/git"]   # codex-coder round 29 (r39: the match carries the path)
    assert bare("scripts/ship_check.py fulcra <HEAD> --fulcra-api /x")[0].startswith("missing --git")
    assert bare("scripts/ship_check.py fulcra <HEAD> --git --fulcra-api /x")[0].startswith("--git has no value")                      # codex-coder round 30
    assert not bare("`scripts/ship_check.py fulcra <HEAD> --git /x --fulcra-api /y`")


def test_the_gate_temp_root_is_its_own_0700_directory_and_TMPDIR_is_ignored(tmp_path, monkeypatch):
    """codex-coder round 29: mkdtemp under an uncontrolled TMPDIR is a pathname handoff the gate did not own."""
    import os, stat, tempfile
    home = tmp_path / "home"; home.mkdir(); monkeypatch.setenv("HOME", str(home))
    world = tmp_path / "world"; world.mkdir(); world.chmod(0o777); monkeypatch.setenv("TMPDIR", str(world)); tempfile.tempdir = None
    root = ship_check.gate_tmp_root()
    assert root == str(home / ".local" / "state" / "coord-fold" / "tmp") and stat.S_IMODE(os.stat(root).st_mode) == 0o700
    d = ship_check.private_dir("x-"); assert d.startswith(root + os.sep) and not d.startswith(str(world))
    os.chmod(root, 0o755)
    import pytest
    with pytest.raises(RuntimeError, match="not a private directory"):
        ship_check.gate_tmp_root()                                                                  # a root that lost its privacy is refused, never reused
    os.chmod(root, 0o700); tempfile.tempdir = None


def test_store_read_refuses_a_body_whose_handoff_state_changed_before_the_read(tmp_path, monkeypatch):
    """Synchronized, not raced (codex-reviewer round 28): the fake CLI writes the body and then — before the gate reads —
    (a) makes the private dir world-readable, (b) replaces the body with a symlink, (c) keeps it intact. Only (c) is read."""
    import os, tempfile
    home = tmp_path / "home"; home.mkdir(); monkeypatch.setenv("HOME", str(home)); tempfile.tempdir = None
    g = tmp_path / "git"; g.write_text("#!/bin/sh\n"); g.chmod(0o755)
    secret = tmp_path / "secret.txt"; secret.write_text("PIN=\"deadbeef\"\n")
    def fake(mode):
        fa = tmp_path / f"fulcra-api-{mode}"
        # absolute tool paths: the gate hands the CLI an EMPTY PATH, so a bare `chmod` would silently not run (measured)
        body = {"chmod": 'printf "ok" > "$4"; /bin/chmod 755 "$(/usr/bin/dirname "$4")"', "link": f'printf "ok" > "$4"; /bin/rm "$4"; /bin/ln -s {secret} "$4"',
                "bodymode": 'printf "ok" > "$4"; /bin/chmod 666 "$4"',                                        # codex-reviewer round 31: the BODY left world-writable
                "intact": 'printf "PIN=x" > "$4"'}[mode]
        fa.write_text("#!/bin/sh\n" + body + "\nexit 0\n"); fa.chmod(0o755); return fa
    for mode, expect in (("chmod", "no longer private"), ("link", "not a regular file"), ("bodymode", "is writable by others (mode 0o666)"), ("intact", None)):
        table, why = ship_check.resolve_trust_roots({"git": str(g), "fulcra-api": str(fake(mode))}, "/tool"); assert why is None
        monkeypatch.setattr(ship_check, "TRUSTED", dict(table))
        rc, body, err = ship_check.store_read("team/fulcra/x")
        if expect:
            assert rc == 3 and body == "" and expect in err, (mode, rc, err)
        else:
            assert rc == 0 and body == "PIN=x", (rc, body, err)
    tempfile.tempdir = None


def test_the_bare_invocation_guard_parses_the_command_shape():
    """codex-coder rounds 29-30: presence of an option NAME is not a usable command. Each required root must carry
    exactly one non-option value; a shell comment ends the command."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("materialize_plan", SCRIPT.parent / "materialize_plan.py"); mp = importlib.util.module_from_spec(spec); spec.loader.exec_module(mp)
    f = mp.refuse_bare_runbook_invocations
    ok = "scripts/ship_check.py fulcra <HEAD> --git /usr/bin/git --fulcra-api /x\n"
    assert f(ok) == [] and f("scripts/ship_check.py fulcra <HEAD> --fulcra-api /x --git /usr/bin/git\n") == []          # order-independent
    assert f("scripts/ship_check.py fulcra <HEAD> --git=/usr/bin/git --fulcra-api=/x\n") == []                             # the = form
    assert f("scripts/ship_check.py fulcra <HEAD> --git <abs> --fulcra-api <abs path>\n") == []                            # documentation placeholders (allowlisted)
    assert f("scripts/ship_check.py fulcra " + "e" * 40 + " --git /usr/bin/git --fulcra-api /x\n") == []                    # a real 40-hex head
    bad = {
        "run `scripts/ship_check.py fulcra <HEAD>`\n": "missing --git; missing --fulcra-api",
        "scripts/ship_check.py fulcra <HEAD> --git /usr/bin/git\n": "missing --fulcra-api",
        "scripts/ship_check.py fulcra <HEAD> --fulcra-api /x\n": "missing --git",
        "scripts/ship_check.py fulcra <HEAD> --git --fulcra-api /x\n": "--git has no value",                                  # codex-coder (1)
        "scripts/ship_check.py fulcra <HEAD> --git /usr/bin/git --fulcra-api\n": "--fulcra-api has no value",                # codex-coder (2)
        "scripts/ship_check.py fulcra <HEAD> # --git /x --fulcra-api /y\n": "missing --git; missing --fulcra-api",           # codex-coder (3): a comment ends the command
        "scripts/ship_check.py fulcra <HEAD> --git /a --git /b --fulcra-api /x\n": "--git given 2 times",
        "scripts/ship_check.py fulcra <HEAD> --git= --fulcra-api /x\n": "--git has no value",
        "scripts/ship_check.py fulcra <HEAD> --git git --fulcra-api fulcra-api\n": "--git value 'git' is not an absolute path",      # codex-coder round 31: relative roots
        "scripts/ship_check.py fulcra <HEAD> --git /usr/bin/git --fulcra-api /x --bogus\n": "unexpected token '--bogus'",            # unknown option
        "scripts/ship_check.py fulcra <HEAD> --git /usr/bin/git --fulcra-api /x extra\n": "unexpected token 'extra'",                # trailing positional
        "scripts/ship_check.py fulcra deadbee --git /usr/bin/git --fulcra-api /x\n": "is not 40 lowercase hex",                   # codex-coder round 32: 7-hex head
        "scripts/ship_check.py fulcra deadbe --git /usr/bin/git --fulcra-api /x\n": "is not 40 lowercase hex",                    # codex-coder round 33: 6 hex
        "scripts/ship_check.py fulcra DEADBEEF --git /usr/bin/git --fulcra-api /x\n": "is not 40 lowercase hex",                  # uppercase
        "scripts/ship_check.py fulcra not-a-head --git /usr/bin/git --fulcra-api /x\n": "is not 40 lowercase hex",                # not hex
        "scripts/ship_check.py fulcra " + "g" * 40 + " --git /usr/bin/git --fulcra-api /x\n": "is not 40 lowercase hex",           # 40 non-hex
        "scripts/ship_check.py fulcra " + "e" * 39 + " --git /usr/bin/git --fulcra-api /x\n": "is not 40 lowercase hex",           # 39 hex
        "run scripts/ship_check.py\n": "missing team; missing head",                                                             # codex-coder round 34: no positionals
        "run scripts/ship_check.py fulcra\n": "missing head",                                                                     # one positional
        "python scripts/ship_check.py fulcra <HEAD>\n": "missing --git",                                                          # positionals only, no roots
        "scripts/ship_check.py fulcra <HEAD> --git <abs --bogus> --fulcra-api /x\n": "unexpected token",                             # codex-coder round 32: option hidden in <...>
    }
    for text, why in bad.items():
        got = f(text); assert got and why in got[0], (text, got)


def test_acl_entries_are_stripped_from_gate_directories_and_refused_on_bodies(tmp_path, monkeypatch):
    """codex-reviewer round 33: on macOS an ACL survives chmod and is invisible to stat. An INHERITED everyone-write ACL
    on the parent of the gate's temp root must not reach the root or any private dir (stripped, proven); an ACL added
    to a body or its dir before the read is refused."""
    import os, subprocess, sys, tempfile
    if sys.platform != "darwin":
        import pytest; pytest.skip("macOS ACL semantics")
    home = tmp_path / "home"; home.mkdir(); monkeypatch.setenv("HOME", str(home)); tempfile.tempdir = None
    subprocess.run(["/bin/chmod", "+a", "everyone allow write,delete,add_file,add_subdirectory,file_inherit,directory_inherit", str(home)], check=True)
    root = ship_check.gate_tmp_root(); assert ship_check.acl_entries(root) == [], ship_check.acl_entries(root)          # inherited entry stripped from the root
    d = ship_check.private_dir("acl-"); assert ship_check.acl_entries(d) == []                                        # and from every private dir
    body = os.path.join(d, "body"); open(body, "w").write("x"); os.chmod(body, 0o600)
    assert ship_check.read_owned_file(body) == "x"
    subprocess.run(["/bin/chmod", "+a", "everyone allow write", body], check=True)                                     # the body's mode still reads 0600
    assert oct(os.stat(body).st_mode & 0o777) == "0o600"
    import pytest
    with pytest.raises(PermissionError, match="carries ACL entries"):
        ship_check.read_owned_file(body)
    subprocess.run(["/bin/chmod", "-N", body], check=True); subprocess.run(["/bin/chmod", "+a", "everyone allow delete", d], check=True)
    with pytest.raises(PermissionError, match="carries ACL entries"):
        ship_check.read_owned_file(body)
    tempfile.tempdir = None


def test_a_bare_path_reference_in_prose_is_not_an_invocation():
    """r39: `see scripts/ship_check.py` is a reference; `run scripts/ship_check.py` is an invocation missing both positionals."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("materialize_plan", SCRIPT.parent / "materialize_plan.py"); mp = importlib.util.module_from_spec(spec); spec.loader.exec_module(mp)
    f = mp.refuse_bare_runbook_invocations
    assert f("see `scripts/ship_check.py` for the gate\n") == []
    assert f("(`scripts/ship_check.py`: the engine's folded result)\n") == []
    assert f("run `scripts/ship_check.py`\n")[0].startswith("line 1: missing team; missing head")
    # r40 (codex-coder round 35): an executable form in command position expresses execution even with no positionals
    assert f("./scripts/ship_check.py\n")[0].startswith("line 1: missing team; missing head")
    assert f("/opt/gate/scripts/ship_check.py\n")[0].startswith("line 1: missing team; missing head")
    assert f("../scripts/ship_check.py fulcra\n")[0].startswith("line 1: missing head")
    assert f("the file `scripts/ship_check.py` holds the gate\n") == []                                # still a reference (repo prose backticks paths)


def test_a_failed_acl_inspection_or_removal_refuses_instead_of_reading_as_no_acl(tmp_path, monkeypatch):
    """codex-coder + codex-reviewer round 34 (P0): inability to inspect ACLs was accepted as 'no ACL'. Force the
    inspector (ls -led / listxattr) to fail: acl_entries raises, read_owned_file refuses, strip_acls refuses."""
    import os, subprocess, sys, types
    d = tmp_path / "d"; d.mkdir(); os.chmod(d, 0o700); body = d / "body"; body.write_text("x"); body.chmod(0o600)
    if sys.platform == "darwin":
        real = subprocess.run
        def failing(cmd, **kw):
            if cmd[:1] == ["/bin/ls"]:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="inspection denied")
            if cmd[:1] == ["/bin/chmod"]:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="removal denied")
            return real(cmd, **kw)
        monkeypatch.setattr(ship_check.subprocess, "run", failing)
    else:
        def boom(path, *a, **k):
            raise OSError("inspection denied")
        monkeypatch.setattr(ship_check.os, "listxattr", boom)
    import pytest
    with pytest.raises(PermissionError, match="ACL inspection .* failed"):
        ship_check.acl_entries(str(d))
    with pytest.raises(PermissionError, match="ACL inspection .* failed"):
        ship_check.read_owned_file(str(body))                                        # never reads on a failed inspection
    with pytest.raises((RuntimeError, PermissionError), match="failed"):
        ship_check.strip_acls(str(d))                                                # never "stripped" on a failed removal
