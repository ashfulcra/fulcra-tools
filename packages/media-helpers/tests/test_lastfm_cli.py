"""Wizard/Click coverage; actual poll delivery lives in test_poll_routes_v1."""
from click.testing import CliRunner
from fulcra_media.cli import cli


def test_lastfm_wizard_prints_setup_steps():
    result = CliRunner().invoke(cli, ["wizard", "lastfm"])
    assert result.exit_code == 0
    assert "lastfm" in result.output.lower()
