"""Shared test fixtures."""

import pytest

from coord_engine import bus_tags, checkpoint_channel
from coord_engine.cli import INHERITED_ENV


@pytest.fixture(autouse=True)
def _clean_bus_tag_cache():
    """The bus tag registry and the checkpoint-channel config are both memoized
    per PROCESS (each changes only when a human provisions), which in a
    single-process suite would leak one module's team config into the next.
    Clear both around every test."""
    bus_tags.cache_clear()
    checkpoint_channel.cache_clear()
    yield
    bus_tags.cache_clear()
    checkpoint_channel.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """Every test gets a throwaway COORD_ENGINE_STATE_DIR so the suite never
    writes nonce state into the real ~/.local/state/coord-engine (a stray
    artifact there can trigger spurious double-acting warnings in real use)."""
    monkeypatch.setenv("COORD_ENGINE_STATE_DIR", str(tmp_path / "state"))


@pytest.fixture(autouse=True)
def _no_host_wake_provisioning(monkeypatch):
    """The suite must never inherit a developer's host wake provisioning: with
    COORD_WAKE_ADAPTER_DIR set, the default host-adapter invoker would run that
    host's real adapter script (and post a real notification) from a test. Tests
    that exercise the seam set it explicitly to a throwaway stub dir."""
    monkeypatch.delenv("COORD_WAKE_ADAPTER_DIR", raising=False)
    monkeypatch.delenv("COORD_WAKE_ADAPTER_TIMEOUT", raising=False)


@pytest.fixture(autouse=True)
def _no_inherited_coord_environment(monkeypatch):
    """The suite must never inherit the environment of whoever is running it.

    Every agent's standing wake prompt opens with
    ``export FULCRA_COORD_AGENT=<identity>``, and 25 tests across
    ``records_transactional``, ``queue_contract_engine_adapter``,
    ``records_write`` and ``dispatch_companion`` read it as a fallback sender.
    So the suite reported 25 failures on a GREEN tree for anyone following the
    documented procedure, and passed for anyone who happened not to — the answer
    depended on who ran it.

    That is worse than the failures themselves. A suite that is red for
    environmental reasons is indistinguishable at a glance from one that is red
    for real ones, and the next real regression arrives inside that noise. It
    also defeats the controls you would normally reach for: stashing the diff,
    merging main and probing origin/main in a clean worktree all agreed the tree
    was broken, because every one of them inherited the same shell.

    The channel variable ``COORD_RECORDS_TYPE`` is the same shape one family
    over, and sharper: all 8 tests it broke have a PREMISE that the records
    config is absent or unreadable, so an exported channel supplies the very
    thing they test the absence of. They did not merely fail — they stopped
    being the tests they are named after, which is the version of this bug that
    survives a green suite.

    Tests that exercise identity or channel resolution set these explicitly in
    their own body; ``monkeypatch`` is function-scoped and applies after this
    fixture, so they still see exactly the environment they intend.
    """
    for name in INHERITED_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_transient_read_retry(monkeypatch):
    """Disable the single transient-read retry by default across the suite.

    The retry (``coord_engine.read_retry``) sleeps ~2s before its one retry, and
    it fires on exactly the ``error`` path that dozens of fail-closed tests
    exercise deliberately — roughly 20s of pure sleep across a 70s suite, on the
    tests that are SUPPOSED to be simulating a dark store.

    Turning it off here also keeps those tests measuring what they were written
    to measure: that an unreadable document classifies UNKNOWN rather than
    absent. That claim is about classification, not about blip absorption, and
    it should not silently start depending on how many times the transport is
    asked.

    The retry itself is covered explicitly — including at all four wired call
    sites — in ``test_read_retry.py``, which sets this variable itself. Tests
    that want the retry set the env var; nothing here can hide a regression in
    it.
    """
    monkeypatch.setenv("COORD_READ_RETRY_MS", "0")
