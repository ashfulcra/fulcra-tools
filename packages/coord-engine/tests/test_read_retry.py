"""One retry for a transient single-doc read failure.

Background (2026-08-05): codex-coder's tick reported "records config could not
be read; state=UNKNOWN, error_code=config-read-failed, cursor untouched". One
minute later the same document read fine from another host — the reader had
collided with the VPS heartbeat's hourly rewrite. The fail-closed handling was
CORRECT and nothing was lost, but every wake report rendered a blip as a
catastrophe.

The tests that matter here are the ones asserting what did NOT change: a
double failure is still UNKNOWN, absence is still absence, and the retry adds
no read at all to the paths that were already answering.
"""
from __future__ import annotations

import pytest

from coord_engine import bus_tags, checkpoint_channel, read_retry, records


class Reader:
    """A classified reader with a scripted sequence of outcomes."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[str] = []

    def __call__(self, path):
        self.calls.append(path)
        if self.outcomes:
            return self.outcomes.pop(0)
        return None, "error"


@pytest.fixture
def no_sleep():
    """Assert the backoff is requested without ever paying it."""
    slept: list[float] = []
    return slept.append, slept


# --- the env switch --------------------------------------------------------

def test_default_delay_when_unset(monkeypatch):
    monkeypatch.delenv(read_retry.ENV_RETRY_MS, raising=False)
    assert read_retry.retry_delay_ms() == read_retry.DEFAULT_RETRY_MS


def test_zero_disables(monkeypatch):
    monkeypatch.setenv(read_retry.ENV_RETRY_MS, "0")
    assert read_retry.retry_delay_ms() == 0


def test_garbage_falls_back_to_the_default_rather_than_raising(monkeypatch):
    # This sits on the read path of every command; a typo in an env var must
    # not be able to take the engine down.
    monkeypatch.setenv(read_retry.ENV_RETRY_MS, "two seconds")
    assert read_retry.retry_delay_ms() == read_retry.DEFAULT_RETRY_MS


def test_explicit_value_is_honored(monkeypatch):
    monkeypatch.setenv(read_retry.ENV_RETRY_MS, "50")
    assert read_retry.retry_delay_ms() == 50


# --- the rescue ------------------------------------------------------------

def test_first_read_fails_retry_succeeds_serves_normally(no_sleep, monkeypatch):
    monkeypatch.delenv(read_retry.ENV_RETRY_MS, raising=False)
    sleep, slept = no_sleep
    reader = Reader((None, "error"), ("{}", "ok"))
    raw, status = read_retry.read_classified_retrying(
        reader, "p", sleep=sleep, log=_SilentLog())
    assert (raw, status) == ("{}", "ok")
    assert len(reader.calls) == 2
    assert slept == [read_retry.DEFAULT_RETRY_MS / 1000.0]


def test_a_rescue_leaves_a_breadcrumb(no_sleep, monkeypatch):
    monkeypatch.delenv(read_retry.ENV_RETRY_MS, raising=False)
    sleep, _ = no_sleep
    log = _SilentLog()
    read_retry.read_classified_retrying(
        Reader((None, "error"), ("{}", "ok")), "team/f/doc.json",
        sleep=sleep, log=log)
    assert len(log.infos) == 1
    msg, ctx = log.infos[0]
    assert "rescued by retry" in msg
    assert ctx["path"] == "team/f/doc.json"


def test_the_breadcrumb_is_info_not_a_warning(no_sleep, monkeypatch):
    """Ash's complaint was red text. A rescued blip is not an alarm."""
    monkeypatch.delenv(read_retry.ENV_RETRY_MS, raising=False)
    sleep, _ = no_sleep
    log = _SilentLog()
    read_retry.read_classified_retrying(
        Reader((None, "error"), ("{}", "ok")), "p", sleep=sleep, log=log)
    assert log.warns == [] and log.errors == []


# --- what did NOT change ---------------------------------------------------

def test_both_reads_failing_is_unknown_exactly_as_before(no_sleep, monkeypatch):
    monkeypatch.delenv(read_retry.ENV_RETRY_MS, raising=False)
    sleep, _ = no_sleep
    log = _SilentLog()
    reader = Reader((None, "error"), (None, "error"))
    assert read_retry.read_classified_retrying(
        reader, "p", sleep=sleep, log=log) == (None, "error")
    assert len(reader.calls) == 2
    assert log.infos == []  # nothing was rescued, so nothing to say


def test_retry_disabled_behaves_exactly_as_today(no_sleep, monkeypatch):
    monkeypatch.setenv(read_retry.ENV_RETRY_MS, "0")
    sleep, slept = no_sleep
    reader = Reader((None, "error"))
    assert read_retry.read_classified_retrying(
        reader, "p", sleep=sleep) == (None, "error")
    assert len(reader.calls) == 1
    assert slept == []


def test_a_successful_read_is_not_retried(no_sleep):
    sleep, slept = no_sleep
    reader = Reader(("{}", "ok"))
    read_retry.read_classified_retrying(reader, "p", sleep=sleep)
    assert len(reader.calls) == 1 and slept == []


@pytest.mark.parametrize("status", ["absent", "invalid"])
def test_answers_are_not_retried(status, no_sleep):
    """absent and invalid are ANSWERS — the store spoke. Asking again would
    neither change them nor mean anything if it did."""
    sleep, slept = no_sleep
    reader = Reader((None, status))
    assert read_retry.read_classified_retrying(
        reader, "p", sleep=sleep) == (None, status)
    assert len(reader.calls) == 1 and slept == []


def test_a_raising_reader_still_raises(no_sleep):
    """Callers own the except-to-UNKNOWN decision; swallowing it here would
    move that choice away from the code that documents it."""
    sleep, _ = no_sleep

    def boom(path):
        raise RuntimeError("store on fire")

    with pytest.raises(RuntimeError):
        read_retry.read_classified_retrying(boom, "p", sleep=sleep)


# --- the plain-read shape --------------------------------------------------

def test_plain_read_shape_retries_when_classification_is_available(
        no_sleep, monkeypatch):
    monkeypatch.delenv(read_retry.ENV_RETRY_MS, raising=False)
    sleep, _ = no_sleep

    class T:
        def __init__(self):
            self.reader = Reader((None, "error"), ("body", "ok"))

        def read_classified(self, path):
            return self.reader(path)

    t = T()
    assert read_retry.read_retrying(t, "p", sleep=sleep,
                                    log=_SilentLog()) == "body"


def test_unclassified_transport_keeps_its_old_behavior_with_no_retry(no_sleep):
    """Without classification, absent and unreadable are the same None — and
    retrying every absent document would tax the normal path to no purpose."""
    sleep, slept = no_sleep
    calls = []

    class OldTransport:
        def read(self, path):
            calls.append(path)
            return None

    assert read_retry.read_retrying(OldTransport(), "p", sleep=sleep) is None
    assert len(calls) == 1 and slept == []


# --- the wired call sites --------------------------------------------------

class FlakyTransport:
    """Fails the first classified read of each path, then serves."""

    def __init__(self, body):
        self.body = body
        self.failed: set[str] = set()
        self.calls: list[str] = []

    def read_classified(self, path):
        self.calls.append(path)
        if path not in self.failed:
            self.failed.add(path)
            return None, "error"
        return self.body, "ok"


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    """Turn the retry back ON (fast) — conftest disables it suite-wide.

    The assertion is the point: this file is the ONLY coverage of the retry, so
    an ordering change that let conftest win would turn every call-site test
    below into a vacuous pass. Prove the override took effect instead of
    trusting fixture ordering.
    """
    monkeypatch.setenv(read_retry.ENV_RETRY_MS, "1")
    assert read_retry.retry_delay_ms() == 1


def test_records_config_survives_one_transient_failure():
    body = '{"data_type": "MomentAnnotation/x", "api_version": "v1alpha1"}'
    cfg, status = records.load_config_classified(FlakyTransport(body), "fulcra")
    assert status == "ok" and cfg["data_type"] == "MomentAnnotation/x"


def test_checkpoints_config_survives_one_transient_failure():
    checkpoint_channel.cache_clear()
    body = ('{"schema": "coord.checkpoints-channel.v1", '
            '"data_type": "MomentAnnotation/y", "api_version": "v1alpha1"}')
    cfg, status = checkpoint_channel.load_config(FlakyTransport(body), "fulcra")
    assert status == "ok" and cfg["data_type"] == "MomentAnnotation/y"


def test_tags_registry_survives_one_transient_failure():
    bus_tags.cache_clear()
    body = ('{"schema": "coord.bus-tags.v2", '
            '"base": "cb951ecb-f21c-4aee-826e-2cb0b12517d6", "agents": {}}')
    reg, status = bus_tags.load_registry(FlakyTransport(body), "fulcra")
    assert status == "ok" and reg["base"].startswith("cb951ecb")


def test_a_persistently_dark_store_is_still_UNKNOWN_at_every_call_site():
    class Dark:
        def read_classified(self, path):
            return None, "error"

    checkpoint_channel.cache_clear()
    bus_tags.cache_clear()
    assert records.load_config_classified(Dark(), "fulcra") == (None, "error")
    assert checkpoint_channel.load_config(Dark(), "fulcra") == (None, "error")
    assert bus_tags.load_registry(Dark(), "fulcra") == (None, "error")


class _SilentLog:
    def __init__(self):
        self.infos: list[tuple] = []
        self.warns: list[tuple] = []
        self.errors: list[tuple] = []

    def info(self, msg, **ctx):
        self.infos.append((msg, ctx))

    def warn(self, msg, **ctx):
        self.warns.append((msg, ctx))

    def error(self, msg, **ctx):
        self.errors.append((msg, ctx))
