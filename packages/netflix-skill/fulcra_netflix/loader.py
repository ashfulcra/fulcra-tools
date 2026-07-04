"""Load the vendored PEP 723 script as an importable module for tests.

The shippable artifact is skills/fulcra-netflix/scripts/netflix_import.py —
a single self-contained file end users run with `uv run`. Tests must cover
THAT file (not a copy), so we import it by path. The PEP 723 header is
comments, so a normal importlib load works.
"""
from importlib import util
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills" / "fulcra-netflix" / "scripts" / "netflix_import.py"
)


def load():
    spec = util.spec_from_file_location("netflix_import", SCRIPT_PATH)
    mod = util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
