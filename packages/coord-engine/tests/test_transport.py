"""Transport failure-mode contract: a hung or missing ``fulcra-api`` binary must
degrade to ``TransportError`` / documented soft returns, never escape raw.

These use a REAL subprocess (no fakes): a ``sh -c 'sleep 5'`` shim against a tiny
timeout to force ``subprocess.TimeoutExpired`` inside ``_run``, and a nonexistent
binary to force ``FileNotFoundError``. The guarantee under test is the one the
briefing/needs-me folds rely on: nothing but ``TransportError`` (or the method's
soft return) ever escapes a transport method.
"""

import os
import subprocess
import time
import urllib.error

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


# --- hard per-op bound: a descendant tree can't defeat the timeout ---------
#
# Every fold budget in the engine (briefing/needs-me/overlay/…) assumes each
# transport op is bounded. Bare ``subprocess.run(timeout=)`` breaks that promise
# against a child that spawned helpers: on ``TimeoutExpired`` it kills only the
# DIRECT child (and on POSIX ``wait()``s on it alone), so a grandchild that
# inherited the stdout/stderr pipes is left running — a leaked process tree that
# keeps holding the fds, and on non-POSIX the post-kill drain can block on it
# indefinitely. The hardened path runs the child in its OWN session and, on
# timeout, SIGKILLs the whole group, then drains under a short grace, abandoning
# the pipes rather than blocking if even that won't complete. Invariant: a
# transport op RETURNS OR RAISES within ``timeout`` + a small constant, no
# matter what the child tree does.

# direct child holds the pipes past the timeout (`exec sleep`), and a
# backgrounded grandchild (same script) touches SENTINEL ~1.5s later — long
# after the 0.2s op timeout. If the whole group was killed the sentinel never
# appears; if only the direct child died, the grandchild survives and writes it.
def _grandchild_shim(sentinel: str) -> list[str]:
    script = f'sh -c "sleep 1.5; : > {sentinel}" & exec sleep 30'
    return ["sh", "-c", script]


def test_timeout_kills_grandchild_group_not_just_direct_child(tmp_path):
    sentinel = tmp_path / "grandchild-alive"
    t = tr.FulcraFileTransport(command=_grandchild_shim(str(sentinel)), timeout=0.2)
    t0 = time.monotonic()
    assert t.read("/x.md") is None  # soft-returns on timeout
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"op overran its bound: {elapsed:.2f}s"
    time.sleep(2.5)  # outlast the grandchild's 1.5s delay
    assert not sentinel.exists(), "grandchild survived the op — process tree leaked"


def test_read_hard_bounded_within_timeout_plus_grace_with_pipe_holding_descendant():
    # a grandchild holding the stdout/stderr pipe must not stretch the call:
    # RETURN within timeout + grace regardless.
    t = tr.FulcraFileTransport(
        command=["sh", "-c", "sleep 30 & exec sleep 30"], timeout=0.3
    )
    t0 = time.monotonic()
    assert t.read("/x.md") is None
    assert time.monotonic() - t0 < 5.0


def test_list_dir_bounded_and_raises_transport_error_with_descendant():
    t = tr.FulcraFileTransport(
        command=["sh", "-c", "sleep 30 & exec sleep 30"], timeout=0.3
    )
    t0 = time.monotonic()
    with pytest.raises(tr.TransportError) as ei:
        t.list_dir("/prefix")
    assert time.monotonic() - t0 < 5.0
    assert not isinstance(ei.value, subprocess.TimeoutExpired)
    assert "timeout" in str(ei.value)


# --- COORD_TRANSPORT_TIMEOUT: configurable default, bad-env hardened --------

def test_env_sets_default_timeout(monkeypatch):
    monkeypatch.setenv("COORD_TRANSPORT_TIMEOUT", "8")
    assert tr.FulcraFileTransport(command=["fulcra-api"]).timeout == 8.0


def test_constructor_timeout_overrides_env(monkeypatch):
    monkeypatch.setenv("COORD_TRANSPORT_TIMEOUT", "8")
    assert tr.FulcraFileTransport(command=["fulcra-api"], timeout=3.0).timeout == 3.0


def test_default_timeout_when_env_absent(monkeypatch):
    monkeypatch.delenv("COORD_TRANSPORT_TIMEOUT", raising=False)
    assert (
        tr.FulcraFileTransport(command=["fulcra-api"]).timeout
        == tr.DEFAULT_TRANSPORT_TIMEOUT
    )


@pytest.mark.parametrize("bad", ["", "  ", "abc", "0", "-5", "nan", "inf", "-inf"])
def test_bad_env_timeout_falls_back_to_default(monkeypatch, bad):
    monkeypatch.setenv("COORD_TRANSPORT_TIMEOUT", bad)
    assert (
        tr.FulcraFileTransport(command=["fulcra-api"]).timeout
        == tr.DEFAULT_TRANSPORT_TIMEOUT
    )


# --- recent_changes: the ack fold's evidence source. UNKNOWN is None, always ---
#
# The contract the ack fold rests on: this method NEVER raises and NEVER invents
# an empty list. Every failure (no token, HTTP 500 on an over-wide window, a
# timeout, a body it can't parse) returns None = UNKNOWN, which the caller must
# read as "fall back to the full fold" — never as "nothing changed".

class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _with_urlopen(monkeypatch, handler):
    seen = {}

    def urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.headers.get("Authorization")
        seen["timeout"] = timeout
        return handler()

    monkeypatch.setattr(tr.urllib.request, "urlopen", urlopen)
    return seen


def _authed(monkeypatch) -> tr.FulcraFileTransport:
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "tok-123")
    return tr.FulcraFileTransport(command=list(MISSING), timeout=7.0)


def test_recent_changes_returns_the_files_list(monkeypatch):
    seen = _with_urlopen(monkeypatch, lambda: _FakeResponse(
        b'{"files": [{"full_name": "/team/r/_coord/acks/a/amy.md", "size": 12}]}'))
    monkeypatch.delenv("FULCRA_API_BASE", raising=False)
    out = _authed(monkeypatch).recent_changes("2026-07-01T00:00:00Z", "2026-07-01T06:00:00Z")
    assert out == [{"full_name": "/team/r/_coord/acks/a/amy.md", "size": 12}]
    assert seen["url"] == ("https://api.fulcradynamics.com/input/v1/file/recent_changes"
                           "?start_time=2026-07-01T00%3A00%3A00Z"
                           "&end_time=2026-07-01T06%3A00%3A00Z")
    assert seen["auth"] == "Bearer tok-123"
    assert seen["timeout"] == 7.0   # bounded like every other op


def test_recent_changes_honors_api_base_override(monkeypatch):
    seen = _with_urlopen(monkeypatch, lambda: _FakeResponse(b'{"files": []}'))
    monkeypatch.setenv("FULCRA_API_BASE", "https://api.example.test/")
    assert _authed(monkeypatch).recent_changes("a", "b") == []
    assert seen["url"].startswith("https://api.example.test/input/v1/file/recent_changes?")


def test_recent_changes_returns_none_on_http_error(monkeypatch):
    """The endpoint fails LOUD (500 on an over-wide window) rather than
    truncating — that must surface as UNKNOWN, not as an empty change set."""
    def boom():
        raise urllib.error.HTTPError("u", 500, "server error", {}, None)
    _with_urlopen(monkeypatch, boom)
    assert _authed(monkeypatch).recent_changes("a", "b") is None


def test_recent_changes_returns_none_on_unparseable_body(monkeypatch):
    for body in (b"not json", b'{"files": "nope"}', b'[]', b'{}'):
        _with_urlopen(monkeypatch, lambda body=body: _FakeResponse(body))
        assert _authed(monkeypatch).recent_changes("a", "b") is None, body


def test_recent_changes_returns_none_without_a_token(monkeypatch):
    # no FULCRA_ACCESS_TOKEN and no runnable CLI -> no token -> UNKNOWN, and no
    # request is attempted.
    monkeypatch.delenv("FULCRA_ACCESS_TOKEN", raising=False)
    called = _with_urlopen(monkeypatch, lambda: _FakeResponse(b'{"files": []}'))
    assert tr.FulcraFileTransport(command=list(MISSING), timeout=5.0).recent_changes(
        "a", "b") is None
    assert "url" not in called


def test_recent_changes_returns_none_on_token_timeout(monkeypatch):
    monkeypatch.delenv("FULCRA_ACCESS_TOKEN", raising=False)
    assert _slow().recent_changes("a", "b") is None
