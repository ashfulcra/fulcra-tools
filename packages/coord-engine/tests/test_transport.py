"""Transport failure-mode contract: a hung or missing ``fulcra-api`` binary must
degrade to ``TransportError`` / documented soft returns, never escape raw.

These use a REAL subprocess (no fakes): a ``sh -c 'sleep 5'`` shim against a tiny
timeout to force ``subprocess.TimeoutExpired`` inside ``_run``, and a nonexistent
binary to force ``FileNotFoundError``. The guarantee under test is the one the
briefing/needs-me folds rely on: nothing but ``TransportError`` (or the method's
soft return) ever escapes a transport method.
"""

import subprocess

import pytest

from coord_engine import transport as tr


# A command that ignores whatever ``_run`` appends (``file <op> <path>...``) and
# just sleeps well past the timeout. Positional args after -c's SCRIPT become
# $0, $1, ... which the script never reads, so the extra argv is inert.
SLOW = ["sh", "-c", "sleep 5", "shim"]
MISSING = ["/nonexistent/definitely-not-a-real-binary"]


def _slow() -> tr.FulcraFileTransport:
    return tr.FulcraFileTransport(command=list(SLOW), timeout=0.1)


def _missing() -> tr.FulcraFileTransport:
    return tr.FulcraFileTransport(command=list(MISSING), timeout=5.0)


# --- timeout: each method honors its own contract, never leaks TimeoutExpired ---

def test_read_returns_none_on_timeout():
    assert _slow().read("/x.md") is None


def test_list_dir_raises_transport_error_not_timeout_on_timeout():
    t = _slow()
    with pytest.raises(tr.TransportError) as ei:
        t.list_dir("/prefix")
    # the critical regression guard: it must be TransportError, NOT the raw
    # subprocess.TimeoutExpired that used to escape and crash the folds.
    assert not isinstance(ei.value, subprocess.TimeoutExpired)
    assert "timeout" in str(ei.value)


def test_write_returns_false_on_timeout():
    assert _slow().write("/x.md", "content") is False


def test_stat_returns_none_on_timeout():
    assert _slow().stat("/x.md") is None


def test_delete_returns_false_on_timeout():
    assert _slow().delete("/x.md") is False


def test_updates_returns_none_on_timeout():
    # updates() already swallows everything; confirm a timeout is included.
    assert _slow().updates("60 seconds") is None


# --- missing binary: same conversion (FileNotFoundError -> TransportError/soft) ---

def test_read_returns_none_on_missing_binary():
    assert _missing().read("/x.md") is None


def test_list_dir_raises_transport_error_on_missing_binary():
    t = _missing()
    with pytest.raises(tr.TransportError) as ei:
        t.list_dir("/prefix")
    assert not isinstance(ei.value, OSError)  # converted, not a raw FileNotFoundError


def test_write_returns_false_on_missing_binary():
    assert _missing().write("/x.md", "content") is False


def test_stat_returns_none_on_missing_binary():
    assert _missing().stat("/x.md") is None


def test_delete_returns_false_on_missing_binary():
    assert _missing().delete("/x.md") is False


def test_updates_returns_none_on_missing_binary():
    assert _missing().updates("60 seconds") is None


# --- briefing-level: a fold over a timing-out transport degrades, no traceback ---

def test_degraded_fold_over_timing_out_transport_yields_result_not_traceback():
    """Simulate what briefing/needs-me do: sweep transport ops behind a
    ``except TransportError`` guard. A timing-out transport must produce a
    degraded tally, never a raw traceback out of the guard."""
    t = _slow()
    errors = 0
    entries = None
    try:
        entries = t.list_dir("/team")
    except tr.TransportError:
        errors += 1
    # soft-return ops just report their degraded value
    result = {
        "entries": entries,
        "sample": t.read("/team/x.md"),
        "meta": t.stat("/team/x.md"),
        "errors": errors,
    }
    assert result == {"entries": None, "sample": None, "meta": None, "errors": 1}
