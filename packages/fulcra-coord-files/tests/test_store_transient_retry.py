"""Transient-failure retry tests for the object-store transport.

The live failure mode (2026-06-10 night): the Fulcra Files backend's natural
per-op latency is 1-16s and its gateway times out at ~15s, so intermittent
``HTTP Error 504: Gateway Timeout`` responses are permanent weather. Every read
primitive in ``store`` was SINGLE-ATTEMPT — one transient 5xx read as
"missing/empty" upstream (a landed upload logged "DELIVERY NOT CONFIRMED",
searches reported "No tasks found" for files that exist, presence flapped).

These tests pin the bounded-retry contract:
  * transient stderr (5xx / timeout / connection reset) -> one backoff'd retry
  * success and not-found stay SINGLE-call (existence probes against missing
    files are extremely common and must stay fast; the perf call-count pins in
    fulcra-coord depend on the success path adding zero subprocess spawns)
  * an explicit caller timeout is a hard budget — a retry never makes one call
    cost ~2x the stated budget.

Invocation counts are observed via a side-effect file the flaky fake appends a
line to on every spawn — counting at the subprocess boundary, not via mocks, so
the pin survives any internal refactor of the retry loop.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from fulcra_coord_files import store

# Real stateful fake (shared with the coord suite) — the flaky wrapper execs
# into it once its scripted failures are exhausted, so the success attempt is a
# true wire round-trip.
FAKE = (
    Path(__file__).resolve().parents[2]
    / "fulcra-coord"
    / "tests"
    / "fake_fulcra_backend.py"
)

TRANSIENT_504 = "Error: HTTP Error 504: Gateway Timeout"
NOT_FOUND_404 = "Error: HTTP Error 404: Not Found"


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    """Zero the backoff (no 1s sleeps in the suite) and pin the retry knob to
    its default so an operator's env can't skew the invocation-count pins."""
    monkeypatch.setattr(store, "_RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.delenv("FULCRA_COORD_TRANSIENT_RETRIES", raising=False)


def _backend(tmp_path: Path) -> list[str]:
    """Point the real fake at ``tmp_path`` and return its command."""
    os.environ["FULCRA_FAKE_ROOT"] = str(tmp_path)
    return [sys.executable, str(FAKE)]


def _flaky_backend(
    tmp_path: Path, *, fail_times: int, stderr_text: str
) -> tuple[list[str], Path]:
    """Build a backend that fails its first ``fail_times`` invocations with
    ``stderr_text`` on stderr (rc=1), then execs into the real fake backend.

    Returns ``(backend_cmd, calls_file)``; the calls file gains one line per
    subprocess invocation, which is how tests pin exact attempt counts."""
    os.environ["FULCRA_FAKE_ROOT"] = str(tmp_path)
    calls_file = tmp_path / "calls.log"
    script = tmp_path / "flaky_backend.py"
    script.write_text(
        f"""
import os, sys
calls_file = {str(calls_file)!r}
with open(calls_file, "a") as f:
    f.write("call\\n")
with open(calls_file) as f:
    n = sum(1 for _ in f)
if n <= {fail_times}:
    sys.stderr.write({stderr_text!r})
    sys.exit(1)
os.execv(sys.executable, [sys.executable, {str(FAKE)!r}] + sys.argv[1:])
"""
    )
    return [sys.executable, str(script)], calls_file


def _calls(calls_file: Path) -> int:
    return len(calls_file.read_text().splitlines()) if calls_file.exists() else 0


def _seed(tmp_path: Path, path: str = "/coordination/x.json") -> str:
    """Upload a record via the real fake so reads have something to hit."""
    assert store.upload('{"a": 1}', path, backend=_backend(tmp_path)) is True
    return path


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "stderr",
    [
        "Error: HTTP Error 504: Gateway Timeout",
        "Error: HTTP Error 502: Bad Gateway",
        "Error: HTTP Error 503: Service Unavailable",
        "gateway time-out",
        "Gateway Timeout",
        "read timed out",
        "request timeout exceeded",
        "Connection reset by peer",
        "Connection refused",
        "Temporary failure in name resolution",
        "Service Unavailable",
    ],
)
def test_classifier_transient(stderr):
    assert store._is_transient_failure(stderr) is True


@pytest.mark.parametrize(
    "stderr",
    [
        "",
        "Error: HTTP Error 404: Not Found",
        "Not Found",
        "Error: HTTP Error 403: Forbidden",
        "usage: fulcra-api file [-h] ...",
        "FULCRA_FAKE_ROOT not set",
    ],
)
def test_classifier_not_transient(stderr):
    assert store._is_transient_failure(stderr) is False


# ---------------------------------------------------------------------------
# Transient-then-success: each primitive recovers in exactly 2 attempts
# ---------------------------------------------------------------------------

def test_stat_retries_transient_then_succeeds(tmp_path):
    path = _seed(tmp_path)
    flaky, calls = _flaky_backend(tmp_path, fail_times=1, stderr_text=TRANSIENT_504)
    result = store.stat(path, backend=flaky)
    assert result is not None
    assert result["size"] == len('{"a": 1}')
    assert _calls(calls) == 2


def test_download_retries_transient_then_succeeds(tmp_path):
    path = _seed(tmp_path)
    flaky, calls = _flaky_backend(tmp_path, fail_times=1, stderr_text=TRANSIENT_504)
    assert store.download(path, backend=flaky) == '{"a": 1}'
    assert _calls(calls) == 2


def test_list_files_retries_transient_then_succeeds(tmp_path):
    path = _seed(tmp_path)
    flaky, calls = _flaky_backend(tmp_path, fail_times=1, stderr_text=TRANSIENT_504)
    assert store.list_files("/coordination/", backend=flaky) == [path]
    assert _calls(calls) == 2


def test_upload_retries_transient_then_succeeds(tmp_path):
    flaky, calls = _flaky_backend(tmp_path, fail_times=1, stderr_text=TRANSIENT_504)
    assert store.upload("body", "/coordination/up.json", backend=flaky) is True
    assert _calls(calls) == 2
    # The retried write actually landed.
    assert store.download("/coordination/up.json", backend=_backend(tmp_path)) == "body"


# ---------------------------------------------------------------------------
# Success first time: zero extra spawns (protects the perf call-count pins)
# ---------------------------------------------------------------------------

def test_success_first_time_is_single_invocation(tmp_path):
    path = _seed(tmp_path)
    flaky, calls = _flaky_backend(tmp_path, fail_times=0, stderr_text=TRANSIENT_504)
    assert store.stat(path, backend=flaky) is not None
    assert _calls(calls) == 1
    assert store.download(path, backend=flaky) == '{"a": 1}'
    assert _calls(calls) == 2
    assert store.list_files("/coordination/", backend=flaky) == [path]
    assert _calls(calls) == 3
    assert store.upload("b2", "/coordination/y.json", backend=flaky) is True
    assert _calls(calls) == 4


# ---------------------------------------------------------------------------
# Not-found is NOT transient: existence probes stay single-attempt fast
# ---------------------------------------------------------------------------

def test_not_found_is_not_retried(tmp_path):
    flaky, calls = _flaky_backend(tmp_path, fail_times=99, stderr_text=NOT_FOUND_404)
    assert store.stat("/coordination/missing.json", backend=flaky) is None
    assert _calls(calls) == 1
    assert store.download("/coordination/missing.json", backend=flaky) is None
    assert _calls(calls) == 2
    assert store.list_files("/coordination/missing/", backend=flaky) == []
    assert _calls(calls) == 3
    assert store.upload("b", "/coordination/missing.json", backend=flaky) is False
    assert _calls(calls) == 4


# ---------------------------------------------------------------------------
# Retries exhausted: default budget is 1 retry -> 2 attempts, then failure
# ---------------------------------------------------------------------------

def test_retries_exhausted_returns_failure(tmp_path):
    flaky, calls = _flaky_backend(tmp_path, fail_times=99, stderr_text=TRANSIENT_504)
    assert store.stat("/coordination/x.json", backend=flaky) is None
    assert _calls(calls) == 2
    assert store.download("/coordination/x.json", backend=flaky) is None
    assert _calls(calls) == 4
    assert store.list_files("/coordination/", backend=flaky) == []
    assert _calls(calls) == 6


def test_upload_exhausted_records_last_attempt_stderr(tmp_path):
    store.last_upload_error = None
    flaky, calls = _flaky_backend(tmp_path, fail_times=99, stderr_text=TRANSIENT_504)
    assert store.upload("body", "/coordination/x.json", backend=flaky) is False
    assert _calls(calls) == 2
    assert store.last_upload_error is not None
    assert "504" in store.last_upload_error


def test_retry_knob_env_extends_attempts(tmp_path, monkeypatch):
    monkeypatch.setenv("FULCRA_COORD_TRANSIENT_RETRIES", "3")
    path = _seed(tmp_path)
    flaky, calls = _flaky_backend(tmp_path, fail_times=3, stderr_text=TRANSIENT_504)
    assert store.download(path, backend=flaky) == '{"a": 1}'
    assert _calls(calls) == 4


# ---------------------------------------------------------------------------
# Budget rule: an explicit caller timeout is a hard per-call budget
# ---------------------------------------------------------------------------

class _FakeClock:
    """Deterministic stand-in for the ``time`` module inside ``store``: feeds
    ``monotonic()`` a scripted sequence (last value repeats) and records sleeps
    instead of performing them — no real waits, no flakiness."""

    def __init__(self, values: list[float]):
        self.values = list(values)
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        if len(self.values) > 1:
            return self.values.pop(0)
        return self.values[0]

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def test_explicit_timeout_budget_blocks_retry(tmp_path, monkeypatch):
    """A transient failure that has already burned the caller's budget must NOT
    retry: deadline-budgeted callers size their budgets assuming one call costs
    at most ``timeout``. Elapsed time is injected (start=0.0, then 14.0s) so the
    test never actually waits."""
    clock = _FakeClock([0.0, 14.0])
    monkeypatch.setattr(store, "time", clock)
    flaky, calls = _flaky_backend(tmp_path, fail_times=99, stderr_text=TRANSIENT_504)
    assert store.download("/coordination/x.json", backend=flaky, timeout=15) is None
    assert _calls(calls) == 1
    assert clock.sleeps == []


def test_explicit_timeout_with_headroom_still_retries(tmp_path, monkeypatch):
    """Budget discipline must not disable retry outright: a fast transient
    failure with plenty of budget left retries within the same call."""
    clock = _FakeClock([0.0, 0.5, 1.0])
    monkeypatch.setattr(store, "time", clock)
    path = _seed(tmp_path)
    flaky, calls = _flaky_backend(tmp_path, fail_times=1, stderr_text=TRANSIENT_504)
    assert store.download(path, backend=flaky, timeout=20) == '{"a": 1}'
    assert _calls(calls) == 2
    assert clock.sleeps == [0.0]
