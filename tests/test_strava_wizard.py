"""Tests for the Strava setup wizard."""
from click.testing import CliRunner

from fulcra_media.cli import cli


def test_strava_wizard_prints_setup_steps():
    res = CliRunner().invoke(cli, ["wizard", "strava"])
    assert res.exit_code == 0
    out = res.output
    # Reference the canonical Strava setup URL
    assert "strava.com/settings/api" in out
    # The authorize-url path (manual code exchange)
    assert "strava.com/oauth/authorize" in out
    # The exchange endpoint
    assert "strava.com/oauth/token" in out
    # Required scopes
    assert "activity:read" in out
    # Creds file location
    assert "~/.config/fulcra-media/strava.json" in out
    # Mentions refresh_token
    assert "refresh_token" in out
    # Mentions running the import command
    assert "fulcra-media import strava" in out


def test_strava_wizard_mentions_webhook_as_advanced():
    res = CliRunner().invoke(cli, ["wizard", "strava"])
    assert res.exit_code == 0
    # Webhook subscription noted but explicitly out-of-scope for this CLI
    assert "webhook" in res.output.lower()
