import pathlib
import coord_fold
CEILING = 400
PKG_DIR = pathlib.Path(coord_fold.__file__).parent
SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"   # relative to THIS file: the scripts tree is not in the wheel


def _governed():
    """Every maintained Python module: the installed package AND the scripts tree (coord-fold-ship-24c1e518, both
    reviewers P1: a ceiling that walks only coord_fold/** leaves a one-big-file escape hatch beside it)."""
    files = [p for p in PKG_DIR.rglob("*.py") if "__pycache__" not in p.parts]
    if SCRIPTS_DIR.is_dir():
        files += [p for p in SCRIPTS_DIR.rglob("*.py") if "__pycache__" not in p.parts]
    return files


def test_every_module_is_under_the_ceiling_recursively():
    over = {}
    for p in _governed():
        n = sum(1 for _ in p.open())
        if n > CEILING:
            over[p.name] = n
    assert not over, over


def test_the_ceiling_is_the_documented_number():
    assert f"{CEILING} lines" in (pathlib.Path(__file__).resolve().parents[1] / "README.md").read_text()   # relative to THIS file: under --no-editable the imported package lives in site-packages, beside no README


def test_the_scripts_tree_is_governed_by_the_same_ceiling():
    """Regression for the escape hatch: ship_check.py (and every sibling under scripts/) is inside the walk."""
    names = {p.name for p in _governed()}
    assert {"ship_check.py", "ship_check_private.py", "ship_check_attest.py", "materialize_plan.py"} <= names, names
    assert SCRIPTS_DIR.is_dir()
