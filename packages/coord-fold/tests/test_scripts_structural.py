"""The scripts tree is governed by the same boundary truths as the package (coord-fold-ship-24c1e518, both reviewers
P1): named ownership per module, a declared sibling-load DAG, and the shared 400-line ceiling (test_file_size_ceiling).
Scripts never import each other through sys.path: ship_check loads its siblings by explicit file location only."""
from __future__ import annotations

import ast
import pathlib

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
OWNERSHIP: dict[str, dict[str, str]] = {
    "ship_check.py": {"REQUIRED": "value", "APPROVED_ENGINE_PINS": "value", "TRUSTED": "value", "engine_env": "callable",
                      "engine_executable": "callable", "resolve_trust_roots": "callable", "store_read": "callable", "sh": "callable",
                      "attested_status": "callable", "pinned_tree": "callable", "executing_engine_commit": "callable",
                      "fleet_pin": "callable", "winning_name_ok": "callable", "main": "callable"},
    "ship_check_private.py": {"LS_LED_HEADER": "value", "LS_LED_ENTRY": "value", "parse_ls_led": "callable", "gate_tmp_root": "callable",
                              "acl_entries": "callable", "strip_acls": "callable", "private_dir": "callable", "read_owned_file": "callable"},
    "ship_check_attest.py": {"ATTEST": "value"},
    "materialize_plan.py": {"bare_invocations": "callable", "refuse_bare_runbook_invocations": "callable", "materialize": "callable", "main": "callable"},
}
SIBLING_EDGES: dict[str, set[str]] = {                 # who may load whom, by explicit file location (`_sibling("name")`)
    "ship_check.py": {"ship_check_private", "ship_check_attest"},
    "ship_check_private.py": set(), "ship_check_attest.py": set(), "materialize_plan.py": set(),
}


def _modules():
    return sorted(p for p in SCRIPTS_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _top_defs(tree):
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[node.name] = "callable"
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                names = [t] if isinstance(t, ast.Name) else (list(t.elts) if isinstance(t, ast.Tuple) else [])
                out.update({n.id: "value" for n in names if isinstance(n, ast.Name)})
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out[node.target.id] = "value"
    return out


def _sibling_loads(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_sibling":
            assert node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str), "a sibling is named by a literal"
            out.add(node.args[0].value)
    return out


def test_every_script_is_named_and_every_named_script_exists():
    assert {p.name for p in _modules()} == set(OWNERSHIP), {p.name for p in _modules()} ^ set(OWNERSHIP)


def test_every_script_owns_what_the_map_says_with_the_declared_shape():
    for name, owned in OWNERSHIP.items():
        defs = _top_defs(ast.parse((SCRIPTS_DIR / name).read_text(), filename=name))
        missing = {k: v for k, v in owned.items() if defs.get(k) != v}
        assert not missing, f"{name}: {missing} (has {defs})"


def test_scripts_load_siblings_only_along_declared_edges_and_never_via_sys_path():
    for name in OWNERSHIP:
        tree = ast.parse((SCRIPTS_DIR / name).read_text(), filename=name)
        assert _sibling_loads(tree) == SIBLING_EDGES[name], name
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                assert not any(m.startswith("ship_check") or m.startswith("materialize_plan") for m in mods), f"{name} imports a sibling through sys.path"
