"""Tokenization tests for the CLI-command / backend env overrides.

``cli_base_cmd`` (FULCRA_CLI_COMMAND) and ``_backend_cmd`` (FULCRA_COORD_BACKEND)
turn a single configured string into an argv list. They use ``shlex.split`` —
NOT bare ``str.split`` — so a CLI path that legitimately contains spaces
(``/Applications/My Tools/fulcra-api``) or quoted arguments tokenizes the way a
shell would, instead of being shredded mid-path into a broken argv.
"""

from __future__ import annotations

from fulcra_coord_files import store


def test_cli_base_cmd_preserves_quoted_path_with_spaces(monkeypatch):
    """A quoted CLI path containing a space stays ONE argv token."""
    monkeypatch.setenv("FULCRA_CLI_COMMAND", '"/Applications/My Tools/fulcra-api"')
    assert store.cli_base_cmd() == ["/Applications/My Tools/fulcra-api"]


def test_cli_base_cmd_tokenizes_command_with_args(monkeypatch):
    """A command with trailing args tokenizes into base + args, and quoted
    args with embedded spaces stay intact."""
    monkeypatch.setenv(
        "FULCRA_CLI_COMMAND", 'uv tool run fulcra-api --profile "work laptop"'
    )
    assert store.cli_base_cmd() == [
        "uv",
        "tool",
        "run",
        "fulcra-api",
        "--profile",
        "work laptop",
    ]


def test_cli_base_cmd_plain_command_unchanged(monkeypatch):
    """The common no-spaces case tokenizes identically to the old str.split."""
    monkeypatch.setenv("FULCRA_CLI_COMMAND", "fulcra-api")
    assert store.cli_base_cmd() == ["fulcra-api"]


def test_backend_cmd_preserves_quoted_path_with_spaces(monkeypatch):
    """FULCRA_COORD_BACKEND (the test fake-backend override) tokenizes with the
    same shell-aware rules and appends no ``file`` subcommand — it speaks the
    file protocol directly."""
    monkeypatch.setenv(
        "FULCRA_COORD_BACKEND", '"/opt/fake backend/emu.py" --mode file'
    )
    assert store._backend_cmd() == ["/opt/fake backend/emu.py", "--mode", "file"]


def test_backend_cmd_falls_through_to_cli_base_plus_file(monkeypatch):
    """With no FULCRA_COORD_BACKEND set, _backend_cmd resolves the real CLI base
    (honouring FULCRA_CLI_COMMAND's shlex tokenization) and appends ``file``."""
    monkeypatch.delenv("FULCRA_COORD_BACKEND", raising=False)
    monkeypatch.setenv("FULCRA_CLI_COMMAND", '"/Applications/My Tools/fulcra-api"')
    assert store._backend_cmd() == ["/Applications/My Tools/fulcra-api", "file"]
