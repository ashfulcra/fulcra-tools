"""fulcra-dayone CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from fulcra_common.client import BaseFulcraClient

import fulcra_dayone.cli as cli_mod
from fulcra_dayone.cli import cli

_SAMPLE = {
    "entries": [
        {
            "uuid": "AAA111",
            "creationDate": "2024-01-15T09:30:00Z",
            "text": "First",
            "tags": ["work"],
            "starred": True,
        },
        {
            "uuid": "BBB222",
            "creationDate": "2024-02-20T14:00:00Z",
            "text": "Second",
            "starred": False,
        },
    ]
}


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-token")


def _export(tmp_path: Path) -> Path:
    folder = tmp_path / "export"
    folder.mkdir()
    (folder / "Personal.json").write_text(json.dumps(_SAMPLE), encoding="utf-8")
    return folder


def test_no_filters_without_all_is_an_error(tmp_path: Path):
    res = CliRunner().invoke(cli, ["import", str(_export(tmp_path))])
    assert res.exit_code != 0
    assert "--all" in res.output


def test_db_path_without_local_db_is_an_error(tmp_path: Path):
    res = CliRunner().invoke(
        cli,
        ["import", str(_export(tmp_path)), "--all", "--db-path", "/tmp/x.sqlite"],
    )
    assert res.exit_code != 0
    assert "--db-path" in res.output


def test_dry_run_reports_counts_without_network(tmp_path: Path):
    res = CliRunner().invoke(
        cli, ["import", str(_export(tmp_path)), "--all", "--dry-run"]
    )
    assert res.exit_code == 0, res.output
    assert "Would import 2 entries" in res.output


def test_dry_run_with_starred_filter(tmp_path: Path):
    res = CliRunner().invoke(
        cli,
        ["import", str(_export(tmp_path)), "--starred", "--dry-run"],
    )
    assert res.exit_code == 0, res.output
    assert "Would import 1 entry" in res.output


def test_live_resumes_before_source_even_when_empty(tmp_path, monkeypatch):
    from contextlib import contextmanager

    events = []

    class Client:
        @contextmanager
        def open(self, settings):
            assert settings["source"] == str((tmp_path / "missing").resolve())

            class Writer:
                def resume(self):
                    events.append("resume")

                def run_import(self, entries):
                    pytest.fail("empty writer invoked")

            yield Writer()

    monkeypatch.setattr(cli_mod, "DayOneFulcraClient", Client)
    monkeypatch.setattr(cli_mod, "read", lambda *a, **kw: events.append("read") or [])
    res = CliRunner().invoke(cli, ["import", str(tmp_path / "missing"), "--all"])
    assert res.exit_code == 0, res.output
    assert events == ["resume", "read"]


def test_dry_run_does_not_construct_client_or_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli_mod, "DayOneFulcraClient", lambda: pytest.fail("client in dry run")
    )
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path / "unused"))
    res = CliRunner().invoke(
        cli, ["import", str(_export(tmp_path)), "--all", "--dry-run"]
    )
    assert res.exit_code == 0, res.output
    assert not (tmp_path / "unused").exists()


def test_destination_rejects_legacy_annotation_uuid(tmp_path):
    res = CliRunner().invoke(
        cli,
        [
            "import",
            str(tmp_path),
            "--all",
            "--destination",
            "11111111-2222-4333-8444-555555555555",
        ],
    )
    assert res.exit_code != 0
    assert "Event/UUID" in res.output


@pytest.mark.parametrize("empty", [False, True])
def test_dry_run_never_enters_auth_profile_or_db(tmp_path, monkeypatch, empty):
    from fulcra_collect import config, db
    from fulcra_common.client import BaseFulcraClient

    def forbidden(*a, **kw):
        pytest.fail("dry run entered external state")

    monkeypatch.setattr(config, "profile_path", forbidden)
    monkeypatch.setattr(db, "open", forbidden)
    monkeypatch.setattr(BaseFulcraClient, "get_token", forbidden)
    monkeypatch.setattr(cli_mod, "DayOneFulcraClient", forbidden)
    args = ["import", str(_export(tmp_path)), "--all", "--dry-run"]
    if empty:
        args += ["--tag", "absent"]
    res = CliRunner().invoke(cli, args)
    assert res.exit_code == 0, res.output


def test_auth_failure_public_error_is_static_before_source(tmp_path, monkeypatch):
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path / "profile"))
    monkeypatch.setattr(
        BaseFulcraClient,
        "get_token",
        lambda _: (_ for _ in ()).throw(RuntimeError("SECRET AUTH STDERR")),
    )
    monkeypatch.setattr(
        cli_mod, "read", lambda *a, **kw: pytest.fail("source before auth")
    )
    res = CliRunner().invoke(cli, ["import", str(tmp_path / "source"), "--all"])
    assert res.exit_code != 0
    assert (
        str(res.exception)
        == "Standalone authentication unavailable; retain pending work"
    )
    assert "SECRET" not in res.output
    assert res.exception.__suppress_context__


def test_default_discovery_failure_happens_after_recovery(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from fulcra_dayone.readers import local_db

    events = []

    class Client:
        @contextmanager
        def open(self, settings):
            assert settings["local_db"] is True and settings["db_path"] is None

            class Writer:
                def resume(self):
                    events.append("resume")

            yield Writer()

    def missing():
        events.append("discover")
        raise FileNotFoundError("synthetic missing source")

    monkeypatch.setattr(cli_mod, "DayOneFulcraClient", Client)
    monkeypatch.setattr(local_db, "find_database", missing)
    res = CliRunner().invoke(cli, ["import", "--local-db", "--all"])
    assert res.exit_code != 0
    assert events == ["resume", "discover"]
