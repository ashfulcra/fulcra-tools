from click.testing import CliRunner
from fulcra_media.wizards.apple_podcasts import walkthrough


def test_walkthrough_mentions_db_path():
    result = CliRunner().invoke(walkthrough, [])
    assert result.exit_code == 0
    assert "MTLibrary.sqlite" in result.output
    assert "243LU875E5.groups.com.apple.podcasts" in result.output


def test_walkthrough_mentions_completed_filter():
    result = CliRunner().invoke(walkthrough, [])
    assert "completed" in result.output.lower()
    assert "ZPLAYSTATE" in result.output


def test_walkthrough_mentions_fragility():
    result = CliRunner().invoke(walkthrough, [])
    assert "fragility" in result.output.lower() or "auto-delete" in result.output.lower()
