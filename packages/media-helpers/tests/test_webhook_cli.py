"""Configured-v1 CLI lifecycle with synthetic transport and port-0 listeners."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import subprocess
import sys
import threading

import pytest
from click.testing import CliRunner

from fulcra_media.cli import cli
from fulcra_media.media_config import configured_media, FILENAME
from webhook_authority import authority as authority


def no_leaks(authority, addresses=()):
    assert not [t for t in threading.enumerate()
                if t.name in ('fulcra-media-owner', 'fulcra-webhook')]
    for address in addresses:
        with socket.socket() as sock:
            assert sock.connect_ex(address) != 0
    with configured_media(authority.root.parent / 'legacy', client=authority.client, lease_timeout_s=0):
        pass


@pytest.mark.parametrize('mode', ['missing', 'corrupt', 'account', 'destination', 'pending'])
def test_startup_holds_never_bind_or_ready(authority, monkeypatch, mode):
    path = authority.root / FILENAME
    if mode == 'missing':
        path.unlink()
    elif mode == 'corrupt':
        path.write_text('{broken')
    elif mode == 'account':
        authority.account = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
    elif mode == 'destination':
        authority.settings['v1_destination_ids']['watched'] = 'Event/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
        path.write_text(json.dumps(authority.settings))
    else:
        from fulcra_collect import db
        conn = db.open(authority.root / 'state.db')
        db.set_record_outbox(conn, 'cli-media', 'v1_media_pending', {'malformed': 'retain'})
    before = path.read_bytes() if path.exists() else None
    monkeypatch.setattr('fulcra_media.webhook_receiver.make_server', lambda **_: pytest.fail('bind'))
    result = CliRunner().invoke(cli, ['webhook', '--port', '0', '--json'])
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)['errors'][0]['stage'] == 'setup'
    assert '"stage": "ready"' not in result.output
    assert (path.read_bytes() if path.exists() else None) == before
    # Both lifetime locks must be released even when retained evidence prevents
    # acquiring a valid configured runtime for a second time.
    import fcntl
    for name in ('.cli-media-settings.lock', '.cli-media.lock'):
        lock_path = authority.root / name
        if lock_path.exists():
            with lock_path.open('r') as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    assert not [t for t in threading.enumerate() if t.name == 'fulcra-media-owner']


def test_non_loopback_without_token_emits_args_error(authority):
    result = CliRunner().invoke(cli, ['webhook', '--host', '0.0.0.0', '--json'])
    assert result.exit_code == 2
    assert json.loads(result.output)['errors'][0]['stage'] == 'args'


@pytest.mark.parametrize('port', [8765, 8766, 0, 12345])
def test_requested_port_forwarded_without_binding_service_ports(authority, monkeypatch, port):
    seen = []
    def occupied(**kwargs):
        seen.append(kwargs['port'])
        raise OSError('synthetic occupied')
    monkeypatch.setattr('fulcra_media.webhook_receiver.make_server', occupied)
    args = [] if port == 8765 else ['--port', str(port)]
    result = CliRunner().invoke(cli, ['webhook', *args, '--json'])
    assert result.exit_code == 2
    assert seen == [port]
    assert json.loads(result.output)['errors'][0]['stage'] == 'bind'
    no_leaks(authority)


def test_actual_occupied_ephemeral_port_releases_owner(authority):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        sock.listen()
        result = CliRunner().invoke(cli, ['webhook', '--port', str(sock.getsockname()[1]), '--json'])
    assert result.exit_code == 2
    assert json.loads(result.output)['errors'][0]['stage'] == 'bind'
    no_leaks(authority)


@pytest.mark.parametrize('fault', ['signal-first', 'signal-second', 'start', 'pre-loop',
                                 'health', 'ready-write', 'ready-flush', 'serve'])
def test_postbind_faults_close_listener_owner_restore_signals(authority, monkeypatch, fault):
    from fulcra_media import cli as cli_module, webhook_receiver as wr
    original_make, original_signal = wr.make_server, signal.signal
    original_start = threading.Thread.start
    original_echo = cli_module.click.echo
    previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    addresses = []
    calls = []
    def make(**kwargs):
        server = original_make(**kwargs)
        addresses.append(server.server_address)
        if fault == 'pre-loop':
            def fail(**kwargs):
                raise SystemExit('synthetic serve-before-entry')
            server.serve_forever = fail
        if fault == 'serve':
            def fail():
                raise SystemExit('synthetic real-loop failure')
            server.service_actions = fail
        return server
    monkeypatch.setattr(wr, 'make_server', make)
    def install(signum, handler):
        calls.append(signum)
        if len(calls) == (1 if fault == 'signal-first' else 2) and fault.startswith('signal-'):
            raise RuntimeError('synthetic signal failure')
        return original_signal(signum, handler)
    monkeypatch.setattr(signal, 'signal', install)
    def start(thread):
        if fault == 'start' and thread.name == 'fulcra-webhook':
            raise RuntimeError('synthetic start failure')
        return original_start(thread)
    monkeypatch.setattr(threading.Thread, 'start', start)
    if fault == 'health':
        monkeypatch.setattr(cli_module, '_wait_for_webhook_health', lambda *_: False)
    def echo(message=None, **kwargs):
        if message and json.loads(message).get('stage') == 'ready':
            if fault == 'ready-write':
                raise OSError('synthetic ready output')
            if fault == 'ready-flush':
                stream = sys.stdout
                original_flush = stream.flush
                def flush():
                    monkeypatch.setattr(stream, 'flush', original_flush)
                    raise OSError('synthetic flush failure')
                monkeypatch.setattr(stream, 'flush', flush)
        return original_echo(message, **kwargs)
    monkeypatch.setattr(cli_module.click, 'echo', echo)
    result = CliRunner().invoke(cli, ['webhook', '--port', '0', '--json'])
    assert result.exit_code == 2, (fault, result.output, result.exception)
    assert '"stage": "shutdown", "ok": true' not in result.output
    for signum, handler in previous.items():
        assert signal.getsignal(signum) is handler
    no_leaks(authority, addresses)


def test_owner_cleanup_failure_never_emits_successful_shutdown(authority, monkeypatch):
    from fulcra_media import media_config
    from fulcra_media import cli as cli_module
    original, echo = media_config.configured_media, cli_module.click.echo
    @contextmanager
    def cleanup(path, **kw):
        with original(path, **kw) as run:
            yield run
        raise RuntimeError('synthetic cleanup failure with secret URL')
    monkeypatch.setattr(media_config, 'configured_media', cleanup)
    def ready(message=None, **kwargs):
        result = echo(message, **kwargs)
        if message and json.loads(message).get('stage') == 'ready':
            os.kill(os.getpid(), signal.SIGTERM)
        return result
    monkeypatch.setattr(cli_module.click, 'echo', ready)
    result = CliRunner().invoke(cli, ['webhook', '--port', '0', '--json'])
    assert result.exit_code == 2
    lines = [json.loads(line) for line in result.output.splitlines()]
    assert lines[0]['stage'] == 'ready'
    assert lines[-1]['ok'] is False
    assert lines[-1]['errors'][0]['stage'] == 'shutdown'
    assert 'secret URL' not in result.output
    assert not [t for t in threading.enumerate() if t.name == 'fulcra-media-owner']


# Child imports this actual synthetic authority helper and installs the external
# network guard explicitly (pytest's parent fixtures do not cross subprocesses).
CHILD = '''
import sys
from pathlib import Path
import pytest
from _pytest_network_guard import install
from webhook_authority import Authority
from fulcra_media import cli as module
patch = pytest.MonkeyPatch()
install(patch)
authority = Authority(Path(sys.argv[1]))
authority.install(patch)
module.STATE_PATH = Path(sys.argv[1]).parent / 'legacy'
module.cli(['webhook', '--port', '0', '--bearer-token', 'synthetic-bearer', '--json'])
'''


def ready_line(proc):
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        assert selector.select(10), 'child never produced lifecycle output'
        return json.loads(proc.stdout.readline())


@pytest.mark.parametrize('signum', [signal.SIGINT, signal.SIGTERM])
def test_actual_configured_subprocess_immediate_signal_after_ready(tmp_path, signum):
    env = os.environ.copy()
    root = Path(__file__).resolve().parents[3]
    env['PYTHONPATH'] = os.pathsep.join((str(Path(__file__).parent), str(root)))
    env['PYTHONUNBUFFERED'] = '1'
    proc = subprocess.Popen([sys.executable, '-c', CHILD, str(tmp_path / 'profile')],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        ready = ready_line(proc)
        assert ready['stage'] == 'ready', ready
        assert ready['port'] > 0 and ready['port'] not in (8765, 8766)
        proc.send_signal(signum)
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0, stderr.decode()
        shutdown = json.loads(stdout.splitlines()[-1])
        assert shutdown['stage'] == 'shutdown' and shutdown['api_version'] == 'v1'
        with socket.socket() as sock:
            assert sock.connect_ex(('127.0.0.1', ready['port'])) != 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_actual_fatal_writer_reaches_http_as_502_and_main_as_failed_exit(authority, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from fulcra_media import cli as module, webhook_receiver as wr
    from test_webhook_v1 import request, jellyfin_payload, pending
    original_make, original_echo = wr.make_server, module.click.echo
    servers, responses = [], []
    def make(**kwargs):
        server = original_make(**kwargs)
        servers.append(server)
        return server
    def fatal():
        raise SystemExit('synthetic fatal after durable dispatch')
    authority.before_post = fatal
    monkeypatch.setattr(wr, 'make_server', make)
    def echo(message=None, **kwargs):
        result = original_echo(message, **kwargs)
        if message and json.loads(message).get('stage') == 'ready':
            with ThreadPoolExecutor(1) as pool:
                responses.append(pool.submit(request, servers[0], jellyfin_payload()).result(5))
        return result
    monkeypatch.setattr(module.click, 'echo', echo)
    result = CliRunner().invoke(cli, ['webhook', '--port', '0', '--json'])
    assert result.exit_code == 2
    assert responses[0][0] == 502 and 'posted' not in responses[0][1]
    assert pending(authority)['groups']
    assert not [t for t in threading.enumerate() if t.name in ('fulcra-media-owner', 'fulcra-webhook')]


def test_actual_cli_subprocess_delivers_over_real_http_before_shutdown(tmp_path):
    import http.client
    from test_webhook_receiver import jellyfin_payload
    env = os.environ.copy()
    root = Path(__file__).resolve().parents[3]
    env['PYTHONPATH'] = os.pathsep.join((str(Path(__file__).parent), str(root)))
    env['PYTHONUNBUFFERED'] = '1'
    proc = subprocess.Popen([sys.executable, '-c', CHILD, str(tmp_path / 'profile')],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        ready = ready_line(proc)
        assert ready['stage'] == 'ready', ready
        conn = http.client.HTTPConnection('127.0.0.1', ready['port'], timeout=5)
        conn.request('POST', '/webhook', body=json.dumps(jellyfin_payload()), headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer synthetic-bearer'})
        reply = conn.getresponse()
        assert reply.status == 200, reply.read()
        assert json.loads(reply.read()) == {'posted': 1, 'skipped': 0}
        conn.close()
        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0, stderr.decode()
        shutdown = json.loads(stdout.splitlines()[-1])
        assert shutdown['stage'] == 'shutdown' and shutdown['posted'] == shutdown['received'] == 1
        # Independent SQLite read of the child runtime's persisted outbox.
        import sqlite3
        with sqlite3.connect(tmp_path / 'profile' / 'state.db') as conn:
            raw = conn.execute('SELECT value_json FROM record_outboxes WHERE plugin_id=? AND key=?',
                               ('cli-media', 'v1_media_pending')).fetchone()[0]
        assert json.loads(raw) == {'groups': []}
        assert not (tmp_path / 'legacy').exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_persistent_stdout_failure_still_exits_two_after_cleanup(authority, monkeypatch):
    from fulcra_media import cli as module, webhook_receiver as wr
    echo, make = module.click.echo, wr.make_server
    addresses = []
    def capture(**kw):
        server = make(**kw)
        addresses.append(server.server_address)
        return server
    def unavailable(message=None, **kw):
        if not kw.get('err'):
            raise BrokenPipeError('synthetic stdout unavailable')
        return echo(message, **kw)
    monkeypatch.setattr(wr, 'make_server', capture)
    monkeypatch.setattr(module.click, 'echo', unavailable)
    result = CliRunner().invoke(cli, ['webhook', '--port', '0', '--json'])
    assert result.exit_code == 2, result.exception
    no_leaks(authority, addresses)


def test_keyboard_interrupt_during_owner_startup_cannot_reopen_or_bind(authority, monkeypatch):
    from fulcra_media import media_config
    from fulcra_media.webhook_owner import MediaOwner
    original_context, original_init = media_config.configured_media, MediaOwner.__init__
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    reopened, peers = [], []
    @contextmanager
    def context(path):
        entered.set()
        assert release.wait(5)
        with original_context(path) as run:
            yield run
    monkeypatch.setattr(media_config, 'configured_media', context)
    def initialize(owner, *args):
        original_init(owner, *args)
        real_wait, real_stop = owner._ready.wait, owner.stop_admission
        def interrupt(*_args, **_kw):
            assert entered.wait(5)
            raise KeyboardInterrupt('synthetic startup interruption')
        def stop():
            real_stop()
            stopped.set()
        monkeypatch.setattr(owner._ready, 'wait', interrupt)
        monkeypatch.setattr(owner, 'stop_admission', stop)
        def rescue_red_thread():
            assert stopped.wait(5)
            release.set()
            assert real_wait(5)
            reopened.append(owner._open)
            # Only a test cleanup rescue: old production otherwise joins forever.
            owner.stop_admission()
        peer = threading.Thread(target=rescue_red_thread, name='webhook-red-rescue')
        peers.append(peer)
        peer.start()
    monkeypatch.setattr(MediaOwner, '__init__', initialize)
    monkeypatch.setattr('fulcra_media.webhook_receiver.make_server', lambda **_: pytest.fail('bind after interrupted startup'))
    try:
        result = CliRunner().invoke(cli, ['webhook', '--port', '0', '--json'])
        assert result.exit_code == 2, result.output
        assert json.loads(result.output)['errors'][0]['stage'] == 'setup'
        for peer in peers:
            peer.join(5)
            assert not peer.is_alive()
        assert reopened == [False]
        no_leaks(authority)
    finally:
        release.set()
        for peer in peers:
            peer.join(5)


def test_cli_exceptional_started_serve_thread_settles_before_listener_owner_cleanup(authority, monkeypatch):
    from fulcra_media import webhook_receiver as wr
    original_make, original_start = wr.make_server, threading.Thread.start
    entered, release = threading.Event(), threading.Event()
    observed_alive_at_close, servers = [], []
    serve_threads = []
    def make(**kwargs):
        server = original_make(**kwargs)
        servers.append(server)
        original_actions = server.service_actions
        original_shutdown, original_close = server.shutdown, server.server_close
        def actions():
            entered.set()
            assert release.wait(5)
            original_actions()
        def shutdown():
            release.set()
            original_shutdown()
        def close():
            observed_alive_at_close.append(serve_threads[0].is_alive())
            # RED cleanup only: old production skips shutdown, so listener close
            # would otherwise leave the genuine serve thread behind this barrier.
            release.set()
            if serve_threads[0].is_alive():
                original_shutdown()
                serve_threads[0].join(5)
            original_close()
            assert not serve_threads[0].is_alive()
        server.service_actions, server.shutdown, server.server_close = actions, shutdown, close
        return server
    def start(thread):
        original_start(thread)
        if thread.name == 'fulcra-webhook':
            serve_threads.append(thread)
            assert entered.wait(5)
            raise SystemExit('synthetic exception after actual serve-thread launch')
    monkeypatch.setattr(wr, 'make_server', make)
    monkeypatch.setattr(threading.Thread, 'start', start)
    try:
        result = CliRunner().invoke(cli, ['webhook', '--port', '0', '--json'])
        assert result.exit_code == 2, result.output
        assert observed_alive_at_close == [False]
        assert all(json.loads(line).get('stage') != 'ready' for line in result.output.splitlines())
        no_leaks(authority, [s.server_address for s in servers])
    finally:
        release.set()
        for thread in serve_threads:
            thread.join(5)
            assert not thread.is_alive()
