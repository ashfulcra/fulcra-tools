from click.testing import CliRunner
from fulcra_media.wizards.spotify import walkthrough


def test_walkthrough_mentions_extended():
    result = CliRunner().invoke(walkthrough, [])
    assert result.exit_code == 0
    assert "Extended" in result.output or "extended" in result.output
    assert "spotify.com/account/privacy" in result.output


def test_walkthrough_mentions_import_command():
    result = CliRunner().invoke(walkthrough, [])
    assert "fulcra-media import spotify-extended" in result.output


def test_walkthrough_warns_about_50_item_cap():
    result = CliRunner().invoke(walkthrough, [])
    assert "50" in result.output
