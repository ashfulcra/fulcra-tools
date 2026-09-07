"""A downloaded app must expose its own CLI without importing the GUI."""
import sys
import pytest
from fulcra_menubar import __main__ as entry


def test_collect_cli_help_without_gui(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['Fulcra Collect', '--collect', '--help'])
    monkeypatch.setitem(sys.modules, 'fulcra_menubar.app', None)
    with pytest.raises(SystemExit) as exit:
        entry.main()
    assert exit.value.code == 0
    assert 'Background hub' in capsys.readouterr().out


def test_fulcra_cli_help_without_gui(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['Fulcra Collect', '--fulcra', '--help'])
    monkeypatch.setitem(sys.modules, 'fulcra_menubar.app', None)
    with pytest.raises(SystemExit) as exit:
        entry.main()
    assert exit.value.code == 0
    assert 'auth' in capsys.readouterr().out
