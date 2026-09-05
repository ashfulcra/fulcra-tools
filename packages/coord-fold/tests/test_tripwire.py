"""FAST FEEDBACK ONLY. THIS IS NOT THE GUARANTEE (G30).

It scans identifiers so a reviewer sees a plain `os.listdir` or `subprocess.Popen` in seconds. It
approximates behaviour and can be walked past by any spelling the AST does not resolve — seven
rounds (r2–r7) proved that. G29's harness is the guarantee; cite that, never this.
"""
import ast
import pathlib

import coord_fold

PKG_DIR = pathlib.Path(coord_fold.__file__).parent
SUSPECT_IDENTIFIERS = {"listdir", "scandir", "walk", "fwalk", "glob", "iglob", "rglob", "iterdir", "list_dir",
                       "system", "popen", "fork", "Popen", "check_output", "check_call", "posix_spawn"}
SUSPECT_MODULES = {"os", "glob", "ctypes", "pty", "multiprocessing", "asyncio", "shutil"}
LAUNCH_ALLOWED_IN = {"transport.py"}


def test_tripwire_identifiers_and_modules():
    hits = []
    for p in PKG_DIR.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in SUSPECT_IDENTIFIERS:
                hits.append(f"{p.name}: .{node.attr}")
            if isinstance(node, ast.Name) and node.id in SUSPECT_IDENTIFIERS:
                hits.append(f"{p.name}: {node.id}")
            if isinstance(node, ast.Import) and any(a.name.split(".")[0] in SUSPECT_MODULES for a in node.names):
                hits.append(f"{p.name}: import {[a.name for a in node.names]}")
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in SUSPECT_MODULES:
                hits.append(f"{p.name}: from {node.module}")
            if isinstance(node, (ast.Import, ast.ImportFrom)) and p.name not in LAUNCH_ALLOWED_IN:
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                if any(m.split(".")[0] == "subprocess" for m in mods):
                    hits.append(f"{p.name}: subprocess outside transport")
    assert not hits, hits
