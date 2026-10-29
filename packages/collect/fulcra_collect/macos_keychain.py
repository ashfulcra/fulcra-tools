"""Lazy macOS Keychain reads with per-request control of authentication UI.

The parent process's interaction setting and item ACLs are never changed.
A disposable same-executable child disables legacy interaction for its own
lifetime; explicit access runs in the parent. Owned CF objects are released.
"""
from __future__ import annotations

import ctypes as C
import json
import os
import signal
import selectors
import time
import subprocess
import sys
from pathlib import Path
from contextlib import contextmanager
from functools import lru_cache
from ctypes.util import find_library

from keyring.errors import KeyringError


class KeychainAccessRequired(KeyringError):
    """The saved item's availability is UNKNOWN until access is granted."""


class _Security:
    def __init__(self):
        self.sec = C.CDLL(find_library('Security'))
        self.cf = C.CDLL(find_library('CoreFoundation'))
        self.update = self._bind(self.sec, 'SecItemUpdate', C.c_int32, [C.c_void_p, C.c_void_p])
        self.add = self._bind(self.sec, 'SecItemAdd', C.c_int32, [C.c_void_p, C.c_void_p])
        self.delete = self._bind(self.sec, 'SecItemDelete', C.c_int32, [C.c_void_p])
        self.data = self._bind(self.cf, 'CFDataCreate', C.c_void_p,
                               [C.c_void_p, C.c_char_p, C.c_long])
        self.copy = self._bind(self.sec, 'SecItemCopyMatching', C.c_int32,
                               [C.c_void_p, C.POINTER(C.c_void_p)])
        self.string = self._bind(self.cf, 'CFStringCreateWithBytes', C.c_void_p,
                                 [C.c_void_p, C.c_char_p, C.c_long, C.c_uint32, C.c_bool])
        self.dictionary = self._bind(self.cf, 'CFDictionaryCreate', C.c_void_p,
                                     [C.c_void_p, C.POINTER(C.c_void_p), C.POINTER(C.c_void_p),
                                      C.c_long, C.c_void_p, C.c_void_p])
        self.release = self._bind(self.cf, 'CFRelease', None, [C.c_void_p])
        self.data_length = self._bind(self.cf, 'CFDataGetLength', C.c_long, [C.c_void_p])
        self.data_bytes = self._bind(self.cf, 'CFDataGetBytePtr', C.c_void_p, [C.c_void_p])

    def disable_interaction(self):
        # Legacy file-based Keychain ACLs ignore modern per-query UI flags.
        # Called ONLY in a disposable child, never in the daemon/UI process.
        disable = self._bind(self.sec, 'SecKeychainSetUserInteractionAllowed', C.c_int32, [C.c_bool])
        if disable(False) != 0:
            raise KeyringError('Keychain interaction suppression failed')

    @staticmethod
    def _bind(lib, name, result, args):
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = result, args
        return fn

    def constant(self, name):
        lib = self.cf if name.startswith('kCF') else self.sec
        return C.c_void_p.in_dll(lib, name)

    @contextmanager
    def query(self, fields):
        owned = []
        query = None
        try:
            keys, values = [], []
            for name, value in fields.items():
                keys.append(self.constant(name))
                if isinstance(value, str):
                    encoded = value.encode('utf-8')
                    value = self.string(None, encoded, len(encoded), 0x08000100, False)
                    if not value:
                        raise KeyringError('Keychain query allocation failed')
                    owned.append(value)
                elif isinstance(value, bytes):
                    value = self.data(None, value, len(value))
                    if not value:
                        raise KeyringError('Keychain data allocation failed')
                    owned.append(value)
                values.append(value)
            count = len(keys)
            query = self.dictionary(
                None, (C.c_void_p * count)(*keys), (C.c_void_p * count)(*values), count,
                C.byref(C.c_byte.in_dll(self.cf, 'kCFTypeDictionaryKeyCallBacks')),
                C.byref(C.c_byte.in_dll(self.cf, 'kCFTypeDictionaryValueCallBacks')),
            )
            if not query:
                raise KeyringError('Keychain query allocation failed')
            yield query
        finally:
            if query:
                self.release(query)
            for value in owned:
                self.release(value)


@lru_cache(maxsize=1)
def _api():
    return _Security()


def _read_password(service: str, account: str, *, allow_ui: bool = False) -> str | None:
    api = _api()
    fields = {
        'kSecClass': api.constant('kSecClassGenericPassword'),
        'kSecMatchLimit': api.constant('kSecMatchLimitOne'),
        'kSecAttrService': service,
        'kSecAttrAccount': account,
        'kSecReturnData': api.constant('kCFBooleanTrue'),
        # Fail, never Skip: an inaccessible item must not look absent.
        'kSecUseAuthenticationUI': api.constant(
            'kSecUseAuthenticationUIAllow' if allow_ui else 'kSecUseAuthenticationUIFail'),
    }
    data = C.c_void_p()
    try:
        with api.query(fields) as query:
            status = api.copy(query, C.byref(data))
        if status == -25300:  # errSecItemNotFound: the only clean absence
            return None
        if status in {-25308, -25293, -128}:  # UI needed, auth failed, denied
            raise KeychainAccessRequired(
                'Keychain access is required. Open Configure and choose Allow Keychain access.')
        if status != 0:
            raise KeyringError(f'Keychain read unavailable (status {status})')
        if not data.value:
            raise KeyringError('Keychain returned no credential data')
        return C.string_at(api.data_bytes(data), api.data_length(data)).decode('utf-8')
    finally:
        if data.value:
            api.release(data)


def _mutate_password(request):
    api = _api()
    fields = {'kSecClass': api.constant('kSecClassGenericPassword'),
              'kSecAttrService': request['service'], 'kSecAttrAccount': request['account'],
              'kSecUseAuthenticationUI': api.constant('kSecUseAuthenticationUIFail')}
    with api.query(fields) as query:
        if request['op'] == 'delete':
            status = api.delete(query)
            if status == -25300:
                status = 0  # confirmed absent, idempotent deletion
        else:
            with api.query({'kSecValueData': request['value'].encode('utf-8')}) as changes:
                status = api.update(query, changes)
            if status == -25300:
                with api.query({**fields, 'kSecValueData': request['value'].encode('utf-8')}) as item:
                    status = api.add(item, None)
    if status in {-25308, -25293, -128}:
        raise KeychainAccessRequired('Open Configure and choose Allow Keychain access.')
    if status != 0:
        raise KeyringError('Keychain write unavailable')


def _valid_mutation(value):
    if not isinstance(value, dict) or value.get('op') not in ('set', 'delete'):
        return False
    keys = {'op', 'service', 'account'} | ({'value'} if value['op'] == 'set' else set())
    return (set(value) == keys and value.get('service') == 'fulcra-collect:trakt'
            and _valid_request({k: value.get(k) for k in ('service', 'account')})
            and (value['op'] == 'delete' or isinstance(value.get('value'), str)
                 and len(value['value'].encode('utf-8')) <= 65536))


_MAX_PACKET_BYTES = 256 * 1024
_CHILD_TIMEOUT_S = 2.0


def isolated_command() -> list[str]:
    app_bin = Path(sys.executable).parent
    if app_bin.name == 'MacOS' and app_bin.parent.name == 'Contents':
        # Same executable/signing identity as the process requesting access.
        return [sys.executable, '--keychain-read']
    return [sys.executable, '-m', 'fulcra_collect', '_keychain-read']


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('duplicate key')
    return result


def _valid_request(value) -> bool:
    return (isinstance(value, dict) and set(value) == {'service', 'account'}
            and all(isinstance(value[k], str) and value[k]
                    and len(value[k].encode('utf-8')) <= 512
                    and not any(ord(c) < 32 or ord(c) == 127 for c in value[k])
                    for k in ('service', 'account'))
            and value['service'].startswith('fulcra-collect:'))


def get_password(service: str, account: str, *, allow_ui: bool = False) -> str | None:
    if allow_ui:
        return _read_password(service, account, allow_ui=True)
    request = {'service': service, 'account': account}
    if not _valid_request(request):
        raise KeyringError('Keychain request is unavailable')
    return _isolated_request(request, writing=False)


def mutate_trakt_password(account, value):
    request = {'op': 'delete' if value is None else 'set',
               'service': 'fulcra-collect:trakt', 'account': account}
    if value is not None:
        request['value'] = value
    if not _valid_mutation(request):
        raise KeyringError('Keychain request is unavailable')
    return _isolated_request(request, writing=True)


def _isolated_request(request, *, writing):
    proc = None
    deadline = time.monotonic() + _CHILD_TIMEOUT_S
    try:
        proc = subprocess.Popen(isolated_command(), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
        packet = json.dumps(request, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        stdout = _communicate(proc, packet, deadline)
        response = json.loads(stdout, object_pairs_hook=_unique_object)
        if response == {'status': 'access_required'}:
            raise KeychainAccessRequired('Open Configure and choose Allow Keychain access.')
        if writing:
            if proc.returncode != 0 or response != {'status': 'ok'}:
                raise KeyringError('Keychain write acknowledgement is UNKNOWN')
            return None
        if (proc.returncode != 0 or not isinstance(response, dict)
                or set(response) != {'status', 'value'} or response['status'] != 'ok'
                or not (response['value'] is None or isinstance(response['value'], str))):
            raise KeyringError('Keychain response is unavailable')
        return response['value']
    except subprocess.TimeoutExpired:
        # Never propagate TimeoutExpired.output: it could contain a secret.
        raise KeychainAccessRequired('Keychain read did not complete; availability is unknown.') from None
    except KeyringError:
        raise
    except Exception:
        raise KeyringError('Keychain read is unavailable') from None
    finally:
        if proc is not None:
            # A direct child may exit while a descendant still holds pipes.
            # This session belongs to this request; kill it even after exit.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                pass  # Availability is already UNKNOWN; no data is returned.
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None and not stream.closed:
                    stream.close()


def _communicate(proc, packet: bytes, deadline: float) -> bytes:
    """One absolute deadline covers stdin, process exit and both pipe EOFs."""
    stdout = bytearray()
    stderr_size = 0
    sent = 0
    with selectors.DefaultSelector() as selector:
        for stream, events, name in ((proc.stdin, selectors.EVENT_WRITE, 'stdin'),
                                     (proc.stdout, selectors.EVENT_READ, 'stdout'),
                                     (proc.stderr, selectors.EVENT_READ, 'stderr')):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, events, name)
        while selector.get_map() or proc.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired('isolated Keychain read', _CHILD_TIMEOUT_S)
            if not selector.get_map():
                proc.wait(timeout=remaining)
                break
            for key, _ in selector.select(remaining):
                stream = key.fileobj
                if key.data == 'stdin':
                    sent += os.write(stream.fileno(), packet[sent:])
                    if sent == len(packet):
                        selector.unregister(stream)
                        stream.close()
                else:
                    chunk = os.read(stream.fileno(), 65536)
                    if not chunk:
                        selector.unregister(stream)
                        stream.close()
                    elif key.data == 'stdout':
                        if len(stdout) + len(chunk) > _MAX_PACKET_BYTES:
                            raise KeyringError('Keychain response is unavailable')
                        stdout.extend(chunk)
                    else:
                        stderr_size += len(chunk)
                        if stderr_size > 8192:
                            raise KeyringError('Keychain response is unavailable')
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired('isolated Keychain read', _CHILD_TIMEOUT_S)
    return bytes(stdout)


def child_main() -> int:
    """One private-pipe read; the process-wide legacy flag is child-local."""
    response = {'status': 'unavailable'}
    rc = 1
    try:
        raw = sys.stdin.read(_MAX_PACKET_BYTES + 1)
        if len(raw.encode('utf-8')) > _MAX_PACKET_BYTES:
            raise ValueError('request too large')
        request = json.loads(raw, object_pairs_hook=_unique_object)
        writing = _valid_mutation(request)
        if not writing and not _valid_request(request):
            raise ValueError('invalid request')
        _api().disable_interaction()
        if writing:
            _mutate_password(request)
            response = {'status': 'ok'}
        else:
            value = _read_password(request['service'], request['account'])
            if value is not None and len(value.encode('utf-8')) > 64 * 1024:
                raise ValueError('credential too large')
            response = {'status': 'ok', 'value': value}
        rc = 0
    except KeychainAccessRequired:
        response = {'status': 'access_required'}
    except Exception:
        pass  # Static closed response only; never print a native exception.
    packet = json.dumps(response, ensure_ascii=False, separators=(',', ':'))
    if len(packet.encode('utf-8')) > _MAX_PACKET_BYTES:
        packet, rc = '{"status":"unavailable"}', 1
    sys.stdout.write(packet)
    sys.stdout.flush()
    return rc
