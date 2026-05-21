from click.testing import CliRunner
from fulcra_media.wizards.apple_takeout import walkthrough


def test_walkthrough_mentions_privacy_apple():
    result = CliRunner().invoke(walkthrough, [])
    assert result.exit_code == 0
    assert "privacy.apple.com" in result.output


def test_walkthrough_mentions_playback_activity_csv():
    result = CliRunner().invoke(walkthrough, [])
    assert "Playback Activity.csv" in result.output


def test_walkthrough_mentions_import_command():
    result = CliRunner().invoke(walkthrough, [])
    assert "fulcra-media import apple-takeout" in result.output
