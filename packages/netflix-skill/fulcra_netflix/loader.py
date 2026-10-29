"""Load the one canonical Netflix script for tests and the installed console.

Pure check-only needs only the script; native upload uses the installed adapter
and matching sibling runtime. Console and direct script execution share main.

In a dev/editable checkout the script lives in the source tree next to this
package. In a non-editable install (CI runs pytest with --no-editable) only
the force-included copy under fulcra_netflix/_bundled/ exists — same
convention as collect's _resources.py.
"""
import sys
from importlib import util
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent
_SCRIPT_REL = Path("skills") / "fulcra-netflix" / "scripts" / "netflix_import.py"
_SOURCE_TREE = _PKG_DIR.parent / _SCRIPT_REL
_BUNDLED = _PKG_DIR / "_bundled" / _SCRIPT_REL

SCRIPT_PATH = _SOURCE_TREE if _SOURCE_TREE.exists() else _BUNDLED


def load():
    spec = util.spec_from_file_location("netflix_import", SCRIPT_PATH)
    mod = util.module_from_spec(spec)
    # Register in sys.modules before exec: the dataclasses module resolves
    # field-type annotations via sys.modules[cls.__module__], so a module
    # loaded via exec_module() without this registration raises
    # AttributeError ('NoneType' has no '__dict__') the moment the script
    # defines any @dataclass.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    """Installed fulcra-netflix console, delegating to the exact bundled source."""
    return load().main()
