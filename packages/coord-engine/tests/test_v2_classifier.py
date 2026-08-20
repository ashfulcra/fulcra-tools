"""Red-first contract tests for v2 identity and canonical-read classification."""

from __future__ import annotations

import json

from coord_engine import classifier
from coord_engine import config
from coord_engine import transport


def test_resolve_identity_catches_the_precedence_mutation_that_replaces_env_with_host():
    """A live self-contest occurred when a host fallback replaced an exported id."""
    assert classifier.resolve_identity(
        explicit="session-a",
        environ={"FULCRA_COORD_AGENT": "session-b"},
        persisted="persisted-session",
        hostname=lambda: "same-host",
    ) == "session-a"
    assert classifier.resolve_identity(
        environ={"FULCRA_COORD_AGENT": "session-b"},
        persisted="persisted-session",
        hostname=lambda: "same-host",
    ) == "session-b"
    assert classifier.resolve_identity(
        environ={}, persisted="persisted-session", hostname=lambda: "same-host"
    ) == "persisted-session"
    assert classifier.resolve_identity(
        environ={}, persisted=None, hostname=lambda: "same-host"
    ) == "coord-reconcile:same-host"


def test_persisted_identity_catches_cwd_identity_being_ignored(tmp_path, monkeypatch):
    """The persisted layer must remain between environment and host fallbacks."""
    monkeypatch.setenv("COORD_ENGINE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    path = config.identity_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"identity": "persisted-session"}), encoding="utf-8")
    assert config.persisted_identity() == "persisted-session"


def test_cli_identity_resolver_catches_persisted_identity_losing_to_host(
        tmp_path, monkeypatch):
    """The CLI host fallback must route through the shared resolver."""
    from coord_engine import cli

    monkeypatch.setenv("COORD_ENGINE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    path = config.identity_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"identity": "persisted-session"}), encoding="utf-8")
    monkeypatch.setattr(cli.socket, "gethostname", lambda: "same-host")
    assert cli._host() == "persisted-session"


class _Transport:
    def __init__(self, listing, reads=()):
        self.listing = listing
        self.reads = iter(reads)

    def list_dir(self, _prefix):
        if isinstance(self.listing, BaseException):
            raise self.listing
        return self.listing

    def read_classified(self, _path):
        return next(self.reads)


def test_canonical_read_catches_positive_empty_being_reported_unknown():
    result = classifier.canonical_read(_Transport([]), "team/a/items/")
    assert result.state is classifier.CanonicalState.EMPTY
    assert result.documents == ()


def test_canonical_read_catches_lifecycle_tombstone_being_reported_empty():
    result = classifier.canonical_read(
        _Transport([{"name": "old.md", "state": "deleted"}]), "team/a/items/"
    )
    assert result.state is classifier.CanonicalState.TOMBSTONED


def test_canonical_read_catches_unreadable_directory_being_reported_empty():
    result = classifier.canonical_read(_Transport(RuntimeError("network down")), "team/a/items/")
    assert result.state is classifier.CanonicalState.UNKNOWN


def test_canonical_read_catches_unreadable_listed_document_being_reported_absent():
    result = classifier.canonical_read(
        _Transport([{"name": "live.md"}], [(None, "error")]), "team/a/items/"
    )
    assert result.state is classifier.CanonicalState.UNKNOWN


def test_canonical_read_catches_a_listed_then_absent_document_being_reported_empty():
    """Conflicting listing/read evidence is ambiguous, never a clean absence."""
    result = classifier.canonical_read(
        _Transport([{"name": "racing.md"}], [(None, "absent")]), "team/a/items/"
    )
    assert result.state is classifier.CanonicalState.UNKNOWN


def test_canonical_read_catches_unsupported_entry_shape_being_reported_empty():
    result = classifier.canonical_read(_Transport([{"size": "12B"}]), "team/a/items/")
    assert result.state is classifier.CanonicalState.UNSUPPORTED


def test_transport_canonical_read_catches_a_second_none_interpretation_seam(monkeypatch):
    """Transport must expose the shared classifier instead of another read rule."""
    real = transport.FulcraFileTransport(command=["unused"])
    monkeypatch.setattr(real, "list_dir", lambda _prefix: [])
    result = real.canonical_read("team/a/items/")
    assert result.state is classifier.CanonicalState.EMPTY
