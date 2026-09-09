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


def test_reminders_bridge_dispatch_without_gui(monkeypatch):
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(sys, 'argv', ['Fulcra Collect', '--reminders-bridge'])
    monkeypatch.setitem(sys.modules, 'fulcra_menubar.app', None)
    monkeypatch.setitem(sys.modules, 'fulcra_apple_reminders._bridge',
                        SimpleNamespace(main=lambda: calls.append('bridge') or 0))
    assert entry.main() == 0
    assert calls == ['bridge']
