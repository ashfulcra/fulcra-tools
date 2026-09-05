import pathlib
import coord_fold
CEILING = 400
PKG_DIR = pathlib.Path(coord_fold.__file__).parent


def test_every_module_is_under_the_ceiling_recursively():
    over = {}
    for p in PKG_DIR.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        n = sum(1 for _ in p.open())
        if n > CEILING:
            over[p.name] = n
    assert not over, over


def test_the_ceiling_is_the_documented_number():
    assert f"{CEILING} lines" in (PKG_DIR.parent / "README.md").read_text()
