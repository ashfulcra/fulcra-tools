"""Boundary truths (G5–G7 + ownership-defined-where-planned + DAG + tree). Cheap, true, and NOT the
guarantee — G29's harness is. These say what the package IS; the harness says what a fold DID."""
from __future__ import annotations

import ast
import pathlib
import tomllib

import coord_fold
from coord_fold import transport as tr

PKG_DIR = pathlib.Path(coord_fold.__file__).parent
ENUM_NAMES = ("list_dir", "glob", "listdir", "scandir", "walk", "rglob", "iterdir")
WRITE_NAMES = {"write_event", "save_doc", "_record", "_upload"}
READ_NAMES = {"read_classified", "read_events", "_stat", "_download", "_records"}
OWNERSHIP: dict[str, dict[str, str]] = {
    "events.py": {"PAYLOAD_VERSION": "value", "KINDS": "value", "PRIORITIES": "value", "build_payload": "callable", "parse_event": "callable"},
    "transport.py": {"ReadState": "value", "PointerTransport": "callable", "TransportUnavailable": "callable", "CliPointerReader": "callable", "CliPointerWriter": "callable"},
    "channel.py": {"CONFIG_PATH": "value", "ChannelUnresolved": "callable", "config_path": "callable", "resolve": "callable"},
    "checkpoint.py": {"SCHEMA_VERSION": "value", "path": "callable", "empty": "callable", "apply": "callable", "load": "callable", "save": "callable"},
    "fold.py": {"OVERLAP_SECONDS": "value", "FoldOutcome": "callable", "FoldRefused": "callable", "FoldContended": "callable", "run": "callable"},
    "cli.py": {"main": "callable", "build_parser": "callable"},
    "__init__.py": {"__version__": "value"},
}
ALLOWED_EDGES: dict[str, set[str]] = {
    "cli.py": {"fold", "channel", "events", "checkpoint", "transport"},
    "fold.py": {"channel", "checkpoint", "events", "transport"},
    "channel.py": {"transport"}, "checkpoint.py": {"transport"},
    "events.py": set(), "transport.py": set(), "__init__.py": set(),
}


def _modules():
    return sorted(p for p in PKG_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _tree(name):
    return ast.parse((PKG_DIR / name).read_text(), filename=name)


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


def _package_imports(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            out.update([node.module.split(".")[0]] if node.module else [a.name for a in node.names])
    return out


def test_no_enumeration_method_on_reader_writer_or_fakes():
    from coord_fold_fakes import FakeReader, FakeStore, FakeWriter
    st = FakeStore({}, [])
    for obj in (tr.CliPointerReader(cli=["true"]), tr.CliPointerWriter(cli=["true"]), FakeReader(st), FakeWriter(st)):
        for n in ENUM_NAMES:
            assert not hasattr(obj, n), f"{type(obj).__name__} has {n}"


def test_import_graph_never_reaches_coord_engine():
    for p in _modules():
        for node in ast.walk(ast.parse(p.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            for n in names:
                assert n.split(".")[0] != "coord_engine", f"{p.name} imports {n}"


def test_pyproject_does_not_depend_on_coord_engine_and_ships_only_the_package():
    data = tomllib.loads((PKG_DIR.parent / "pyproject.toml").read_text())
    assert not any(d.startswith("coord-engine") for d in data["project"].get("dependencies", []))
    wheel = data["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel.get("packages") == ["coord_fold"] and not ({"include", "artifacts", "force-include", "only-include"} & set(wheel))


def test_the_protocol_has_exactly_two_methods():
    assert {n for n in dir(tr.PointerTransport) if not n.startswith("_")} == {"read_classified", "read_events"}


def test_reader_and_writer_are_unrelated_classes_with_disjoint_surfaces():
    assert tr.CliPointerReader.__mro__ == (tr.CliPointerReader, object)
    assert tr.CliPointerWriter.__mro__ == (tr.CliPointerWriter, object)
    for n in WRITE_NAMES:
        assert not hasattr(tr.CliPointerReader, n), f"reader has {n}"
    for n in READ_NAMES:
        assert not hasattr(tr.CliPointerWriter, n), f"writer has {n}"
    assert {n for n in vars(tr.CliPointerReader) if not n.startswith("_")} == {"read_classified", "read_events"}
    assert {n for n in vars(tr.CliPointerWriter) if not n.startswith("_")} == {"write_event", "save_doc"}


def test_every_manifest_symbol_is_defined_in_its_module_with_the_right_kind():
    for mod, symbols in OWNERSHIP.items():
        defs = _top_defs(_tree(mod))
        for name, kind in symbols.items():
            assert name in defs, f"{mod} does not define {name!r}"
            if kind == "callable":
                assert defs[name] == "callable", f"{mod}: {name!r} is a bare assignment"


def test_package_tree_recursively_equals_the_manifest():
    found = sorted(p.relative_to(PKG_DIR).as_posix() for p in PKG_DIR.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    assert found == sorted(OWNERSHIP), {"unplanned": sorted(set(found) - set(OWNERSHIP)), "missing": sorted(set(OWNERSHIP) - set(found))}


def test_every_intra_package_import_is_an_allowed_edge_and_no_owner_imports_cli():
    for mod, allowed in ALLOWED_EDGES.items():
        bad = _package_imports(_tree(mod)) - allowed
        assert not bad, f"{mod} imports {sorted(bad)}"
        if mod != "cli.py":
            assert "cli" not in _package_imports(_tree(mod)), f"{mod} imports cli"
