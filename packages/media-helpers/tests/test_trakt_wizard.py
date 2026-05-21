from click.testing import CliRunner

from fulcra_media.wizards.trakt import walkthrough


def test_walkthrough_mentions_streaming_scrobbler():
    result = CliRunner().invoke(walkthrough, [])
    assert result.exit_code == 0
    assert "Streaming Scrobbler" in result.output
    assert "trakt.tv/oauth/applications/new" in result.output


def test_walkthrough_mentions_cluster_detection():
    result = CliRunner().invoke(walkthrough, [])
    assert "timestamp_confidence" in result.output
    assert "cluster" in result.output.lower()


def test_walkthrough_mentions_import_command():
    result = CliRunner().invoke(walkthrough, [])
    assert "fulcra-media import trakt" in result.output
