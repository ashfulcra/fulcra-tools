"""_parse_stat honesty + the download-failure observable (tombstone groundwork).

THE LIVE BUG (2026-06-11, ~12 repair markers blocked forever): the Fulcra
Files platform DELETE is a SOFT delete — a deleted file keeps version history
that ``fulcra file stat`` still reports, while ``download`` fails
deterministically with a not-found-class error. Every absence check built on
"stat is None => maybe absent" therefore read a tombstone as "exists but
unreadable" forever. Two store-level honesty properties underpin the fix:

  * ``_parse_stat`` must return None for output that carries NONE of the
    expected stat fields. It used to return a truthy ``{"raw": text}`` for ANY
    non-empty stdout, so a message-shaped output (a "not found"-style line
    printed with rc 0, a usage hint) read as "the file exists". No caller in
    either package consumes the raw-only fallback dict (grep: the only "raw"
    consumer is ``stat_changed``'s last-resort comparison, which is only
    reachable when BOTH sides are raw-only dicts — i.e. only via the very
    fallback being removed), so dropping it tightens honesty with no loss.

  * ``download`` must expose WHY it failed (``last_download_error``, the exact
    counterpart of ``last_upload_error``) so the absence layer can distinguish
    a tombstone's deterministic not-found from transient transport weather.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from fulcra_coord_files import store

FAKE = (
    Path(__file__).resolve().parents[2]
    / "fulcra-coord"
    / "tests"
    / "fake_fulcra_backend.py"
)

LIVE_TEXT_STAT = """/coordination/tasks/TASK-20260531-example-abc12345.json (65 bytes)
Uploaded: 2026-05-31T17:50:10.725882Z
Version: ae726cf0-1351-4491-93a7-996e632ee8e8
Previous Versions: 1
- 48ef4d2c-b7e4-4bb7-96cb-49b63ad84e3f 2026-05-31T17:50:07.753359Z (65 bytes)
"""


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    """No real backoff sleeps; pin the retry knob to its default."""
    monkeypatch.setattr(store, "_RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.delenv("FULCRA_COORD_TRANSIENT_RETRIES", raising=False)


# ---------------------------------------------------------------------------
# _parse_stat: stat-shaped output keeps parsing exactly as before
# ---------------------------------------------------------------------------

def test_parse_stat_live_text_shape_still_parses():
    parsed = store._parse_stat(LIVE_TEXT_STAT)
    assert parsed is not None
    assert parsed["size"] == 65
    assert parsed["version_id"] == "ae726cf0-1351-4491-93a7-996e632ee8e8"
    assert parsed["uploaded_at"] == "2026-05-31T17:50:10.725882Z"
    assert parsed["previous_versions"] == 1


def test_parse_stat_json_dict_still_parses():
    # The fake backend's JSON stat shape (path/size/version).
    parsed = store._parse_stat('{"path": "/x.json", "size": 3, "version": "abc"}')
    assert parsed == {"path": "/x.json", "size": 3, "version": "abc"}


# ---------------------------------------------------------------------------
# _parse_stat: non-stat-shaped output must be None, never a truthy raw dict
# ---------------------------------------------------------------------------

def test_parse_stat_empty_output_is_none():
    assert store._parse_stat("") is None
    assert store._parse_stat("   \n  ") is None


def test_parse_stat_message_only_output_is_none():
    # A "not found"-style message printed on stdout with rc 0 must NOT read
    # as "the file exists" — this is the dishonesty that made tombstoned
    # paths permanently look present.
    assert store._parse_stat("File not found: /coordination/tasks/x.json") is None
    assert store._parse_stat("Error: HTTP Error 404: Not Found") is None


def test_parse_stat_junk_output_is_none():
    assert store._parse_stat("complete garbage with no fields") is None


def test_parse_stat_non_dict_json_is_none():
    # Bare JSON scalars are not stat output.
    assert store._parse_stat("404") is None
    assert store._parse_stat('"ok"') is None
    assert store._parse_stat("null") is None


# ---------------------------------------------------------------------------
# The not-found classifier (the tombstone download signature)
# ---------------------------------------------------------------------------

def test_not_found_classifier_positive():
    assert store._is_not_found_failure("Error: HTTP Error 404: Not Found")
    assert store._is_not_found_failure("error: file not found")
    assert store._is_not_found_failure("path does not exist (deleted)")


def test_not_found_classifier_negative():
    # Transient weather is NOT a tombstone signal...
    assert not store._is_not_found_failure("HTTP Error 504: Gateway Timeout")
    assert not store._is_not_found_failure("Connection reset by peer")
    # ...and neither is an UNKNOWN failure (empty stderr / bare exit code):
    # confirming absence is a destructive decision, so only a POSITIVE
    # not-found-class error counts.
    assert not store._is_not_found_failure("")
    assert not store._is_not_found_failure(None)
    assert not store._is_not_found_failure("exit 1")


# ---------------------------------------------------------------------------
# last_download_error: the download counterpart of last_upload_error
# ---------------------------------------------------------------------------

def _always_404_backend(tmp_path: Path) -> list[str]:
    os.environ["FULCRA_FAKE_ROOT"] = str(tmp_path)
    script = tmp_path / "backend_404.py"
    script.write_text(
        """
import sys
sys.stderr.write("Error: HTTP Error 404: Not Found\\n")
sys.exit(1)
"""
    )
    return [sys.executable, str(script)]


def test_download_failure_records_stderr_tail(tmp_path):
    backend = _always_404_backend(tmp_path)
    store.last_download_error = None
    assert store.download("/coordination/tasks/gone.json", backend=backend) is None
    assert store.last_download_error is not None
    assert "404" in store.last_download_error


def test_download_success_does_not_clear_the_observable(tmp_path):
    # Same documented semantics as last_upload_error: "the most recent
    # FAILURE's reason", deliberately never cleared on success.
    backend = _always_404_backend(tmp_path)
    store.last_download_error = None
    assert store.download("/x.json", backend=backend) is None
    recorded = store.last_download_error
    assert recorded and "404" in recorded
    os.environ["FULCRA_FAKE_ROOT"] = str(tmp_path)
    real = [sys.executable, str(FAKE)]
    (tmp_path / "ok.json").write_text("{}")
    assert store.download("/ok.json", backend=real) == "{}"
    assert store.last_download_error == recorded


def test_download_empty_stderr_failure_records_exit_code(tmp_path):
    # rc!=0 with silent stderr (e.g. the test suite's `false` safety-net
    # backend) must still record SOMETHING diagnosable — and that something
    # must NOT classify as not-found (see classifier tests above).
    os.environ["FULCRA_FAKE_ROOT"] = str(tmp_path)
    script = tmp_path / "backend_silent.py"
    script.write_text("import sys; sys.exit(1)\n")
    store.last_download_error = None
    assert store.download("/x.json", backend=[sys.executable, str(script)]) is None
    assert store.last_download_error == "exit 1"
    assert not store._is_not_found_failure(store.last_download_error)
