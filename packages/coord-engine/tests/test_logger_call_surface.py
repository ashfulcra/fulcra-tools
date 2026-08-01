"""Gate: no module may call a Logger method that does not exist.

Live incident 2026-07-31. The File Store returned HTTP 500 on two listings
mid-`listen`. The engine's degradation handling did the right thing and tried
to log a warning — and `cli.py` called ``_log.warning(...)`` against a
:class:`coord_engine.log.Logger` whose method is ``warn``. The handler raised
``AttributeError: 'Logger' object has no attribute 'warning'`` and killed the
command.

The shape of the bug is what matters, not the three call sites it happened to
occupy. A logging call on an error path is only ever exercised when something
else has already gone wrong, so a typo there survives every green test run and
then detonates precisely when the system is degraded — turning a handled
degradation into an unhandled crash. Unit tests cannot be relied on to cover
every error branch; a static gate over the whole package can.

This gate walks every module's AST and asserts that each attribute call on a
module-level ``Logger`` binding names a real method on the class. Adding a
method to ``Logger`` widens the allowed set automatically; misspelling one at
any call site fails here instead of in production.
"""

from __future__ import annotations

import ast
import pathlib

from coord_engine import log as log_mod

PACKAGE = pathlib.Path(log_mod.__file__).parent

#: Constructors that yield a coord ``Logger``. A name is only checked when the
#: module actually binds it from one of these — matching on the name alone
#: (``logger``, ``_log``) produces false positives, because modules that use
#: the STDLIB logger legitimately call methods coord's Logger lacks
#: (``atc_dash`` calls ``logger.exception``, which stdlib provides). Caught by
#: this gate's first run against the real package.
LOGGER_FACTORIES = {"get_logger", "Logger"}


def _logger_methods() -> set[str]:
    return {
        name for name in dir(log_mod.Logger)
        if not name.startswith("_") and callable(getattr(log_mod.Logger, name))
    }


def _coord_logger_names(tree: ast.AST) -> set[str]:
    """Names in this module bound to a coord Logger, by assignment."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        called = (func.id if isinstance(func, ast.Name)
                  else func.attr if isinstance(func, ast.Attribute) else None)
        if called not in LOGGER_FACTORIES:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _logger_calls(tree: ast.AST, names: set[str]):
    """Yield (attribute, lineno) for every `<coord-logger>.<attr>(...)` call."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        value = func.value
        if isinstance(value, ast.Name) and value.id in names:
            yield func.attr, func.lineno


def test_every_logger_call_names_a_real_method():
    allowed = _logger_methods()
    assert {"debug", "info", "warn", "error"} <= allowed, (
        "Logger lost a level method; this gate's premise is broken"
    )

    checked_modules = 0
    offenders: list[str] = []
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = _coord_logger_names(tree)
        if not names:
            continue
        checked_modules += 1
        for attr, lineno in _logger_calls(tree, names):
            if attr not in allowed:
                offenders.append(f"{path.name}:{lineno} -> .{attr}()")

    assert checked_modules, "gate found no coord Logger bindings — it is inert"
    assert not offenders, (
        "Logger method(s) that do not exist — these raise AttributeError on the "
        "very error paths they were added to report:\n  "
        + "\n  ".join(offenders)
        + f"\nLogger provides: {', '.join(sorted(allowed))}"
    )


def test_gate_detects_a_bad_call():
    """The gate must fail on the real regression, or it proves nothing."""
    src = "_log = get_logger('x')\n_log.warning('x', a=1)\n_log.warn('ok')\n"
    tree = ast.parse(src)
    names = _coord_logger_names(tree)
    assert names == {"_log"}
    found = dict(_logger_calls(tree, names))
    assert "warning" in found and "warn" in found
    assert "warning" not in _logger_methods()


def test_gate_ignores_the_stdlib_logger():
    """A stdlib logger legitimately has methods coord's Logger lacks."""
    src = "logger = logging.getLogger('x')\nlogger.exception('boom')\n"
    tree = ast.parse(src)
    assert _coord_logger_names(tree) == set()
    assert list(_logger_calls(tree, _coord_logger_names(tree))) == []


def test_logger_warn_emits_at_warn_level():
    """Pin the method the call sites must use (and its wire level)."""
    import io
    import json

    stream = io.StringIO()
    logger = log_mod.Logger("gate", level="debug", stream=stream)
    logger.warn("store listing unreadable", path="team/x/review/")
    record = json.loads(stream.getvalue().strip())
    assert record["level"] == "warn"
    assert record["msg"] == "store listing unreadable"
    assert record["path"] == "team/x/review/"
