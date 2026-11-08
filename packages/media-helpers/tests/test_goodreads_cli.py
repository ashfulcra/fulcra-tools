"""Wizard/Click coverage; actual poll delivery lives in test_poll_routes_v1."""
from click.testing import CliRunner
from fulcra_media.cli import cli


def test_goodreads_cli_missing_user_id_errors_via_click():
    """Click's required-option machinery rejects the call before we run."""
    res = CliRunner().invoke(cli, ["import", "goodreads", "--json"])
    assert res.exit_code != 0
    assert "user-id" in res.output.lower() or "user-id" in (res.stderr or "").lower()


def test_goodreads_wizard_prints_setup_steps():
    res = CliRunner().invoke(cli, ["wizard", "goodreads"])
    assert res.exit_code == 0
    out = res.output.lower()
    assert "goodreads" in out
    assert "user_id" in out or "user id" in out
    assert "list_rss" in out
    assert "read" in out
