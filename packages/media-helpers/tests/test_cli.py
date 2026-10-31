from click.testing import CliRunner

from fulcra_media.cli import cli


def test_cli_no_args_shows_help():
    result = CliRunner().invoke(cli, [])
    assert result.exit_code == 0
    assert "bootstrap" in result.output
    assert "wizard" in result.output
    assert "import" in result.output
    assert "status" in result.output


def test_status_prints_cached_authority(tmp_path, monkeypatch):
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(tmp_path / 'profile'))
    monkeypatch.setattr('fulcra_media.cli.STATE_PATH', tmp_path / 'legacy')
    result = CliRunner().invoke(cli, ['status'])
    assert result.exit_code == 0
    assert 'cached only' in result.output
    assert not (tmp_path / 'profile').exists()


def test_wizard_netflix_invokes_walkthrough():
    result = CliRunner().invoke(cli, ["wizard", "netflix"], input="1\n")
    assert result.exit_code == 0
    assert "Download all" in result.output


def test_wizard_apple_tv_invokes_walkthrough():
    result = CliRunner().invoke(cli, ["wizard", "apple-tv"])
    assert result.exit_code == 0, result.output
    assert "Apple TV app setup" in result.output
    assert "Fulcra Collect dashboard" in result.output


def test_import_netflix_requires_bootstrap_before_source(tmp_path, monkeypatch):
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(tmp_path / 'profile'))
    monkeypatch.setattr('fulcra_media.cli.STATE_PATH', tmp_path / 'legacy')
    monkeypatch.setattr('fulcra_media.library.resolve', lambda *_: (_ for _ in ()).throw(AssertionError('source touched')))
    result = CliRunner().invoke(cli, ['import', 'netflix', 'absent', '--json'])
    assert result.exit_code == 2
    assert 'bootstrap' in result.output


# Trakt legacy-client pipeline test replaced by test_trakt_cli_v1.py actual
# configured runtime/session/writer/outbox delivery and payload coverage.


# File/system route integration now lives in test_file_routes_v1.py, using
# configured v1 authority, real parsers and the actual record writer.
