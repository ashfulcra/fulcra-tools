"""Tests for the interactive `fulcra-media setup` wizard."""
from click.testing import CliRunner

from fulcra_media.cli import cli


def test_setup_shows_help():
    res = CliRunner().invoke(cli, ["setup", "--help"])
    assert res.exit_code == 0
    assert "Interactive picker" in res.output


def test_setup_pick_only_invalid_categories_exits_with_no_picks():
    """All-invalid category numbers → 'No categories picked. Exit.'"""
    # 99,100 are both > the category count, so picks ends up empty
    res = CliRunner().invoke(cli, ["setup"], input="99,100\n")
    assert res.exit_code == 0
    assert "No categories picked" in res.output


def test_setup_pick_music_select_lastfm_shows_wizard_pointer():
    """Pick 'music' (1), then '1' (top-ranked = Last.fm), then skip rest."""
    res = CliRunner().invoke(cli, ["setup"], input="1\n1\n")
    assert res.exit_code == 0, res.output
    out = res.output
    assert "Last.fm" in out
    assert "wizard lastfm" in out
    assert "import lastfm" in out


def test_setup_pick_video_select_trakt():
    """Pick video (2), then '1' (Trakt)."""
    res = CliRunner().invoke(cli, ["setup"], input="2\n1\n")
    assert res.exit_code == 0, res.output
    assert "Trakt" in res.output
    assert "import trakt" in res.output


def test_setup_skip_in_category_shows_completion_message():
    """Pick music (1) then 'skip' → no choice but wizard ends cleanly."""
    res = CliRunner().invoke(cli, ["setup"], input="1\nskip\n")
    assert res.exit_code == 0
    assert "Done." in res.output


def test_setup_picks_planned_service_explains_pathway():
    """Pick a category whose top option is planned-not-shipped (self-hosted Plex)."""
    res = CliRunner().invoke(cli, ["setup"], input="5\n1\n")
    assert res.exit_code == 0
    assert "isn't implemented yet" in res.output


def test_setup_invalid_category_number_ignored():
    """Junk number → ignored; default categories run if any others picked."""
    res = CliRunner().invoke(cli, ["setup"], input="99,1\n1\n")
    assert res.exit_code == 0
    # category 1 (music) processed; 99 ignored
    assert "Last.fm" in res.output


def test_setup_invalid_pick_within_category_skips():
    """Pick music, then 'abc' (unparseable) → skip rest gracefully."""
    res = CliRunner().invoke(cli, ["setup"], input="1\nabc\n")
    assert res.exit_code == 0
    assert "couldn't parse" in res.output
