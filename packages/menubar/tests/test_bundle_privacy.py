"""Release bundles must not retain paths from the build machine."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / 'scripts' / 'sanitize_bundle.py'


def _module():
    spec = importlib.util.spec_from_file_location('sanitize_bundle', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _app(tmp_path):
    app = tmp_path / 'Collect.app'
    (app / 'Contents/Resources/app_packages').mkdir(parents=True)
    return app


def test_removes_nonportable_pip_scripts_but_preserves_native_launchers(tmp_path):
    app = _app(tmp_path)
    pip_bin = app / 'Contents/Resources/app_packages/bin'
    pip_bin.mkdir()
    (pip_bin / 'fulcra').write_text('#!/Users/tester/build/python\n')
    native_bin = app / 'Contents/MacOS'
    native_bin.mkdir()
    launcher = native_bin / 'fulcra'
    launcher.write_bytes(b'native launcher')
    _module().sanitize(app, home=Path('/Users/tester'))
    assert not pip_bin.exists()
    assert launcher.read_bytes() == b'native launcher'


def test_rejects_builder_home_in_binary_metadata_without_printing_it(tmp_path):
    app = _app(tmp_path)
    leak = app / 'Contents/Resources/metadata.bin'
    leak.write_bytes(b'\x00' + b'/Users/tester/build/cache' + b'\x00')
    with pytest.raises(RuntimeError) as error:
        _module().sanitize(app, home=Path('/Users/tester'))
    assert '1 bundle entries' in str(error.value)
    assert '/Users/tester' not in str(error.value)


def test_refuses_to_remove_a_symlinked_script_directory(tmp_path):
    app = _app(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'keep').write_text('keep')
    (app / 'Contents/Resources/app_packages/bin').symlink_to(outside)
    with pytest.raises(RuntimeError):
        _module().sanitize(app, home=Path('/Users/tester'))
    assert (outside / 'keep').exists()


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-16-le', 'utf-16-be'])
def test_rejects_bare_home_and_utf16_paths(tmp_path, encoding):
    app = _app(tmp_path)
    (app / 'Contents/Resources/file').write_bytes('/Users/tester'.encode(encoding))
    with pytest.raises(RuntimeError):
        _module().sanitize(app, home=Path('/Users/tester'))


def test_rejects_symlink_target_without_following_it(tmp_path):
    app = _app(tmp_path)
    (app / 'Contents/Resources/link').symlink_to('/Users/tester/private')
    with pytest.raises(RuntimeError):
        _module().sanitize(app, home=Path('/Users/tester'))


def test_check_only_cannot_delete_new_build_scripts(tmp_path):
    app = _app(tmp_path)
    scripts = app / 'Contents/Resources/app_packages/bin'
    scripts.mkdir()
    with pytest.raises(RuntimeError):
        _module().sanitize(app, check_only=True)
    assert scripts.exists()
