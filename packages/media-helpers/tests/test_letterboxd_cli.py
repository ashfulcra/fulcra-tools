"""Wizard/Click coverage; actual poll delivery lives in test_poll_routes_v1."""
from click.testing import CliRunner
from fulcra_media.cli import cli


def test_letterboxd_cli_missing_username_errors_via_click():
    """Click's own required-option machinery rejects the call before we run."""
    res = CliRunner().invoke(cli, ["import", "letterboxd", "--json"])
    # Click exits 2 on usage error with --json absent in its stderr path; the
    # important thing is we don't crash and the user is told what's missing.
    assert res.exit_code != 0
    assert "username" in res.output.lower() or "username" in (res.stderr or "").lower()


def test_letterboxd_wizard_prints_setup_steps():
    res = CliRunner().invoke(cli, ["wizard", "letterboxd"])
    assert res.exit_code == 0
    assert "letterboxd" in res.output.lower()
    assert "/rss/" in res.output
    assert "rewatch" in res.output.lower()
