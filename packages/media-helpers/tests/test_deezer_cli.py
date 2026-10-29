"""Wizard/Click coverage; actual poll delivery lives in test_poll_routes_v1."""
from click.testing import CliRunner
from fulcra_media.cli import cli


def test_deezer_wizard_prints_setup_steps():
    res = CliRunner().invoke(cli, ["wizard", "deezer"])
    assert res.exit_code == 0
    assert "deezer" in res.output.lower()
    # Must mention the manual mint URL and the creds path
    assert "developers.deezer.com" in res.output
    assert "~/.config/fulcra-media/deezer.json" in res.output
    assert "access_token" in res.output
