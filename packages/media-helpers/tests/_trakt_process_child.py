"""Picklable spawn entries; only stdlib loads before child containment."""
import os
from pathlib import Path
import stat
import sys


def private_paths(base, *paths):
    """Validate supplied temp ownership/containment before any mkdir or open."""
    base = Path(base).resolve(strict=True)
    info = base.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077):
        raise ValueError('process test requires an owned private temporary directory')
    resolved = tuple(Path(path).resolve() for path in paths)
    if any(path == base or not path.is_relative_to(base) for path in resolved):
        raise ValueError('process test path escapes its private temporary directory')
    return resolved


def _prepare(base, path, root, profile, *extra):
    path, root, profile, *extra = private_paths(base, path, root, profile, *extra)
    # Preserve interpreter/dependencies via spawn's sys.path/sys.prefix, not
    # inherited credential variables. No application module has loaded yet.
    os.environ.clear()
    os.environ.update({'HOME': str(Path(base) / 'private-child-home'),
                       'FULCRA_COLLECT_HOME': str(profile),
                       'FULCRA_ACCESS_TOKEN': 'synthetic-child-token',
                       'PATH': os.defpath})
    import ctypes
    import socket
    import subprocess

    def forbidden(*args, **kwargs):
        raise AssertionError('process test attempted external auth/native/network/child access')

    subprocess.Popen = forbidden
    os.system = os.popen = forbidden
    socket.socket.connect = socket.socket.connect_ex = forbidden
    socket.create_connection = socket.getaddrinfo = forbidden
    ctypes.CDLL = ctypes.PyDLL = forbidden
    ctypes.cdll.LoadLibrary = ctypes.pydll.LoadLibrary = forbidden
    for name in ('execl', 'execle', 'execlp', 'execlpe', 'execv', 'execve', 'execvp', 'execvpe',
                 'spawnl', 'spawnle', 'spawnlp', 'spawnlpe', 'spawnv', 'spawnve', 'spawnvp', 'spawnvpe'):
        if hasattr(os, name):
            setattr(os, name, forbidden)

    # A file-only owner has no reason to import any credential/native/provider
    # adapter. Fail before their module bodies, rather than trusting fake state
    # inherited from pytest (spawn has none).
    blocked = ('fulcra_collect.credentials', 'fulcra_collect.macos_keychain',
               'fulcra_collect.oauth', 'fulcra_collect.routes', 'fulcra_common',
               'fulcra_api', 'keyring', 'httpx', 'requests', 'Security', 'Foundation')

    def prohibited(name):
        return any(name == prefix or name.startswith(prefix + '.') for prefix in blocked)

    application = ('fulcra_collect', 'fulcra_media', 'fulcra_common', 'fulcra_api')
    if any(prohibited(name) or any(name == prefix or name.startswith(prefix + '.')
                                  for prefix in application) for name in sys.modules):
        raise AssertionError('application module loaded before child containment')

    class NoExternalImports:
        def find_spec(self, fullname, path=None, target=None):
            if prohibited(fullname):
                forbidden()
            return None

    sys.meta_path.insert(0, NoExternalImports())
    from fulcra_collect import config, config_leases, trakt_source as source
    source.pwd.getpwuid = forbidden  # actual OS-user home lookup is never selected
    source._os_user_root = lambda: root
    source.KeychainStore = forbidden
    original_dir = config.config_dir

    def private_config_dir():
        if config.profile_path() != profile:
            raise AssertionError('process test selected an unapproved profile')
        return original_dir()

    config.config_dir = config_leases.config_dir = private_config_dir
    assert source._os_user_root() == root
    assert set(os.environ) == {'HOME', 'FULCRA_COLLECT_HOME', 'FULCRA_ACCESS_TOKEN', 'PATH'}
    return source, path, root, extra


def file_child(base, path, root, profile, ready, start, results, grants):
    source, path, root, (grants,) = _prepare(base, path, root, profile, grants)
    binding = source.Owner(source.FileStore(path), _root=root).capture(lambda **_: None, lambda: None)
    ready.put('ready')
    assert start.wait(5)

    def grant(_):
        fd = os.open(grants, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, b'one-grant\n')
        finally:
            os.close(fd)
        return {'access_token': 'next-access', 'refresh_token': 'next-refresh',
                'created_at': 1, 'expires_in': 100}

    try:
        binding.refresh(grant)
        results.put('committed')
    except source.SourceHold:
        results.put('held')


def receipt_child(base, path, root, profile, crash):
    source, path, root, _ = _prepare(base, path, root, profile)
    store = source.FileStore(path)
    owner = source.Owner(store, _root=root)
    binding = owner.capture(lambda **_: None, lambda: None)
    if crash == 'receipt-ack-lost':
        save = store.save_receipt

        def receipt(attempt, value):
            save(attempt, value)
            raise OSError('synthetic lost receipt acknowledgement')

        store.save_receipt = receipt
    else:
        assert crash == 'active-published-journal-lost'
        publish = store.publish

        def published(values):
            publish(values)
            raise OSError('synthetic exit after active publication')

        store.publish = published
    try:
        binding.refresh(lambda _: {'access_token': 'after-crash-a', 'refresh_token': 'after-crash-r',
                                   'created_at': 2, 'expires_in': 100})
    except OSError:
        return
    raise AssertionError('missing synthetic crash')
