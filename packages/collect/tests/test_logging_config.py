"""Tests for daemon logging configuration.

The daemon used to start with no root-logger handler and uvicorn pinned
to ``log_level="warning"``, so its INFO startup line ("web UI: ...") and
all HTTP access logs were silently dropped — ``daemon.out.log`` stayed
empty even while the server happily answered requests, which made a live
401 ("auth required") impossible to diagnose from the logs.

``_configure_logging`` is the fix: it installs a single stderr handler on
the root logger at INFO (so launchd captures startup + warnings + errors)
and is idempotent so repeated daemon starts in one process don't stack
duplicate handlers.
"""
from __future__ import annotations

import logging
import os

import pytest

from fulcra_collect.cli import _configure_logging


@pytest.fixture(autouse=True)
def _restore_root_logging():
    """Snapshot and restore global root-logger state so configuring it
    here can't leak into (or be leaked into by) other tests."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_env = os.environ.get("FULCRA_COLLECT_LOG_LEVEL")
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        if saved_env is None:
            os.environ.pop("FULCRA_COLLECT_LOG_LEVEL", None)
        else:
            os.environ["FULCRA_COLLECT_LOG_LEVEL"] = saved_env


def _fulcra_handlers(root: logging.Logger) -> list[logging.Handler]:
    return [h for h in root.handlers if getattr(h, "_fulcra_collect", False)]


def test_configure_logging_installs_info_handler():
    root = logging.getLogger()
    root.handlers[:] = []  # start clean

    _configure_logging()

    assert root.level == logging.INFO
    handlers = _fulcra_handlers(root)
    assert len(handlers) == 1
    assert handlers[0].level == logging.INFO


def test_configure_logging_is_idempotent():
    root = logging.getLogger()
    root.handlers[:] = []

    _configure_logging()
    _configure_logging()
    _configure_logging()

    assert len(_fulcra_handlers(root)) == 1


def test_configure_logging_emits_info_records_to_stderr(capsys):
    # launchd captures the daemon's stderr to daemon.err.log, so the
    # meaningful guarantee is that an INFO record actually reaches stderr
    # (the bug was that it didn't). Assert against the real stream rather
    # than caplog, whose handler this test's root-handler reset removes.
    root = logging.getLogger()
    root.handlers[:] = []
    _configure_logging()

    logging.getLogger("fulcra_collect").info("web UI: http://127.0.0.1:9292")

    assert "web UI:" in capsys.readouterr().err


def test_configure_logging_env_override():
    os.environ["FULCRA_COLLECT_LOG_LEVEL"] = "DEBUG"
    root = logging.getLogger()
    root.handlers[:] = []

    _configure_logging()

    assert root.level == logging.DEBUG
    assert _fulcra_handlers(root)[0].level == logging.DEBUG


def test_configure_logging_tolerates_bad_level():
    # A malformed value must not raise — the root logger falls back to INFO.
    os.environ["FULCRA_COLLECT_LOG_LEVEL"] = "not-a-level"
    root = logging.getLogger()
    root.handlers[:] = []

    _configure_logging()  # must not raise

    assert root.level == logging.INFO


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, "info"),
        ("DEBUG", "debug"),
        ("warning", "warning"),
        ("TRACE", "trace"),
        ("not-a-level", "info"),  # bad value -> safe fallback, never KeyError
        ("20", "info"),           # numeric string is not a uvicorn level name
    ],
)
def test_uvicorn_log_level_is_always_valid(value, expected):
    # uvicorn.Config(log_level=...) raises KeyError on anything outside its
    # fixed set, which would crash web-server startup. _uvicorn_log_level
    # must only ever return a name uvicorn accepts.
    from fulcra_collect.web import _UVICORN_LOG_LEVELS, _uvicorn_log_level

    if value is None:
        os.environ.pop("FULCRA_COLLECT_LOG_LEVEL", None)
    else:
        os.environ["FULCRA_COLLECT_LOG_LEVEL"] = value

    result = _uvicorn_log_level()
    assert result == expected
    assert result in _UVICORN_LOG_LEVELS
