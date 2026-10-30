"""The embedded CLI must find trusted roots without disabling TLS checks."""
import os
import ssl
import sys
from types import SimpleNamespace

import certifi
import pytest

from fulcra_menubar import __main__ as entry


def _dispatch(monkeypatch, tmp_path, *, bundled=True):
    executable = tmp_path / 'Collect.app/Contents/MacOS/Fulcra Collect' if bundled else tmp_path / 'bin/python'
    monkeypatch.setattr(sys, 'executable', str(executable))
    monkeypatch.setattr(sys, 'argv', ['Fulcra Collect', '--fulcra', 'auth', 'login', '--get-auth-url'])
    calls = []
    monkeypatch.setitem(sys.modules, 'fulcra_api.cli', SimpleNamespace(cli=lambda **kw: calls.append(dict(os.environ))))
    monkeypatch.setitem(sys.modules, 'fulcra_menubar.app', None)
    return calls


def test_packaged_cli_selects_its_included_roots_before_auth_dispatch(monkeypatch, tmp_path):
    monkeypatch.delenv('SSL_CERT_FILE', raising=False)
    monkeypatch.delenv('SSL_CERT_DIR', raising=False)
    calls = _dispatch(monkeypatch, tmp_path)
    assert entry.main() == 0
    assert calls[0]['SSL_CERT_FILE'] == certifi.where()
    context = ssl.create_default_context()
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert context.get_ca_certs()


@pytest.mark.parametrize('key', ['SSL_CERT_FILE', 'SSL_CERT_DIR'])
def test_packaged_cli_preserves_an_operator_supplied_trust_store(monkeypatch, tmp_path, key):
    monkeypatch.delenv('SSL_CERT_FILE', raising=False)
    monkeypatch.delenv('SSL_CERT_DIR', raising=False)
    custom = str(tmp_path / 'enterprise-trust')
    monkeypatch.setenv(key, custom)
    calls = _dispatch(monkeypatch, tmp_path)
    assert entry.main() == 0
    assert {k: calls[0][k] for k in ('SSL_CERT_FILE', 'SSL_CERT_DIR') if k in calls[0]} == {key: custom}


def test_source_cli_keeps_its_existing_python_trust_configuration(monkeypatch, tmp_path):
    monkeypatch.delenv('SSL_CERT_FILE', raising=False)
    monkeypatch.delenv('SSL_CERT_DIR', raising=False)
    calls = _dispatch(monkeypatch, tmp_path, bundled=False)
    assert entry.main() == 0
    assert 'SSL_CERT_FILE' not in calls[0] and 'SSL_CERT_DIR' not in calls[0]


def test_missing_bundled_roots_refuse_before_any_auth_dispatch(monkeypatch, tmp_path):
    monkeypatch.delenv('SSL_CERT_FILE', raising=False)
    monkeypatch.delenv('SSL_CERT_DIR', raising=False)
    monkeypatch.setattr(certifi, 'where', lambda: str(tmp_path / 'absent.pem'))
    calls = _dispatch(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match='certificate roots'):
        entry.main()
    assert calls == []


def test_keychain_child_dispatch_precedes_gui_and_tls(monkeypatch):
    from fulcra_collect import macos_keychain
    monkeypatch.setattr(sys,'argv',['Fulcra Collect','--keychain-read'])
    monkeypatch.setattr(macos_keychain,'child_main',lambda:17)
    monkeypatch.setattr(entry,'_configure_bundle_tls',lambda:pytest.fail('no HTTP in credential reader'))
    monkeypatch.setitem(sys.modules,'fulcra_menubar.app',None)
    assert entry.main()==17
