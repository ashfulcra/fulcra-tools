"""Provider identity admission through contained real CLI/Keychain callers."""
import pytest
from test_trakt_source_authority import contained as contained, owner as owner  # noqa: F401

from test_trakt_source_callers import caller as caller  # noqa: F401
from test_poll_cursors import configured as configured  # noqa: F401


def refused(caller):
    if caller.mode == 'cli':
        result, data = caller.invoke()
        assert result.exit_code == 2, result.output
        assert data['posted'] == data['verified'] == 0
        assert 'Opaque' not in result.output
    else:
        with pytest.raises((RuntimeError, __import__("httpx").HTTPStatusError)):
            caller.invoke()
    assert caller.configured.posts == []
    assert not any(k.startswith('v1_media_cursor:') for k in caller.kv)


@pytest.mark.parametrize('samples', [[None], ['Original', 'Contradiction']])
def test_actual_identity_holds_before_normalization(caller, monkeypatch, samples):
    from fulcra_media.importers import trakt
    caller.info.update(source='ok', identities=samples)
    normalized = []
    original = trakt.normalize_history
    monkeypatch.setattr(trakt, 'normalize_history', lambda *a, **k: normalized.append(True) or original(*a, **k))
    refused(caller)
    assert normalized == []
    assert len(caller.info.get('gets', [])) == (0 if samples == [None] else 2)


def test_actual_edit_during_probe_cannot_pin(caller):
    caller.info['source'] = 'ok'
    def edit():
        if caller.mode == 'cli':
            import json
            values = json.loads(caller.creds.read_text())
            values['access_token'] = 'edited'
            caller.creds.write_text(json.dumps(values))
        else:
            caller.source_creds['access_token'] = 'edited'
    caller.info['during_probe'] = edit
    refused(caller)
    assert caller.info['gets'] == []


def test_new_journal_and_valid_v1_upgrade(owner):
    from fulcra_collect import trakt_source as source
    owner.capture(lambda: None, lambda: None)
    path = owner.root / 'journal.json'
    journal = source._read_json(path)
    assert journal['version'] == 2 and journal['identities'] == {}
    legacy = {k: v for k, v in journal.items() if k != 'identities'}
    legacy['version'] = 1
    source._atomic_json(path, legacy)
    owner.capture(lambda: None, lambda: None)
    upgraded = source._read_json(path)
    assert upgraded == {**legacy, 'version': 2, 'identities': {}}


@pytest.mark.parametrize('raw', [b'{}', b'[]', b'{"user":[]}',
    b'{"user":{"ids":{"uuid":1}}}', b'{"user":{"ids":{"uuid":" "}}}',
    b'{"user":{"ids":{"uuid":"x","uuid":"y"}}}',
    b'{"user":{"ids":{"uuid":"x"}},"extra":{"a":1,"a":2}}',
    b'{"user":{"ids":{"uuid":"x"}},"extra":NaN}',
    b'{"user":{"ids":{"uuid":"x"}},"extra":1e999}',
    b'{"user":{"ids":{"uuid":"x"}},"extra":"\\ud800"}',
    b'{"user":{"ids":{"uuid":"x"}},"\\udfff":0}',
    b'{"user":{"ids":{"uuid":"x\\u0080"}}}', b'\xff',
    b'{"user":{"ids":{"uuid":"x"}},"deep":' + b'[' * 1500 + b'0' + b']' * 1500 + b'}'])
def test_parser_rejects_entire_invalid_response(raw):
    from fulcra_collect.trakt_identity import parse_identity
    from fulcra_collect.trakt_source import SourceHold
    with pytest.raises(SourceHold):
        parse_identity(raw)


@pytest.mark.parametrize('identifier', ['Opaque / not-a-uuid', ' Case ', 'case', '\u2603', 'a' * 1024])
def test_parser_preserves_opaque_exact_identity(identifier):
    import json
    from fulcra_collect.trakt_identity import parse_identity
    assert parse_identity(json.dumps({'user': {'ids': {'uuid': identifier}}}).encode()) == identifier


@pytest.mark.parametrize('identifier', ['x\x00', 'x\x1f', 'x\x7f', 'x\x9f', 'a' * 1025, '\u2603' * 342])
def test_identifier_closed_bounds(identifier):
    import json
    from fulcra_collect.trakt_identity import parse_identity
    from fulcra_collect.trakt_source import SourceHold
    with pytest.raises(SourceHold):
        parse_identity(json.dumps({'user': {'ids': {'uuid': identifier}}}).encode())


def sample(owner, identifier='Opaque / A', *, binding=None):
    from fulcra_collect.trakt_identity import snapshot_probe_subject, ProbeWindow, Observation
    from fulcra_media.importers.trakt import _ScanBounds
    binding = binding or owner.capture(lambda: None, lambda: None)
    subject = snapshot_probe_subject(binding, ProbeWindow(_ScanBounds()))
    from fulcra_collect.trakt_identity import ENDPOINT
    return binding, subject, Observation(subject, identifier, ENDPOINT, 1.0)


def test_pin_exact_sticky_and_refresh_survival(owner):
    from copy import deepcopy
    from fulcra_collect.trakt_identity import accept_identity, MISMATCH
    from fulcra_collect.trakt_source import _read_json, SourceHold
    binding, subject, observation = sample(owner)
    admission = accept_identity(binding, subject, observation)
    path = owner.root / 'journal.json'
    first = deepcopy(_read_json(path)['identities'])
    binding.refresh(lambda _: {'access_token': 'next', 'refresh_token': 'next-refresh'})
    assert _read_json(path)['identities'] == first
    with pytest.raises(SourceHold):
        admission.check_local(binding)
    _, next_subject, next_observation = sample(owner, binding=binding)
    accept_identity(binding, next_subject, next_observation)
    assert _read_json(path)['identities'] == first
    _, conflict_subject, conflict = sample(owner, 'opaque / A', binding=binding)
    with pytest.raises(SourceHold, match=MISMATCH):
        accept_identity(binding, conflict_subject, conflict)
    held = deepcopy(_read_json(path)['identities'])
    assert held[owner.key]['state'] == 'held'
    assert held[owner.key]['revision'] != first[owner.key]['revision']
    for identifier in ('Opaque / A', 'Third'):
        _, retry_subject, retry = sample(owner, identifier, binding=binding)
        with pytest.raises(SourceHold, match=MISMATCH):
            accept_identity(binding, retry_subject, retry)
        assert _read_json(path)['identities'] == held


def test_detached_old_bearer_cannot_pin_successor(owner):
    from fulcra_collect.trakt_identity import accept_identity
    from fulcra_collect.trakt_source import _read_json, SourceHold
    binding, subject, observation = sample(owner)
    binding.refresh(lambda _: {'access_token': 'new', 'refresh_token': 'new-refresh'})
    with pytest.raises(SourceHold):
        accept_identity(binding, subject, observation)
    assert _read_json(owner.root / 'journal.json')['identities'] == {}
    assert subject.bearer == 'synthetic-access'


@pytest.mark.parametrize('mutate', ['missing-identities', 'unknown-field', 'bad-source', 'bad-attempt', 'nonfinite', 'duplicate'])
def test_journal_corruption_never_bootstraps(owner, mutate):
    from fulcra_collect import trakt_source as source
    owner.capture(lambda: None, lambda: None)
    path = owner.root / 'journal.json'
    journal = source._read_json(path)
    if mutate == 'missing-identities':
        journal.pop('identities')
    elif mutate == 'unknown-field':
        journal['extra'] = {}
    elif mutate == 'bad-source':
        journal['sources'][owner.key]['digest'] = 'bad'
    elif mutate == 'bad-attempt':
        journal['attempts']['bad'] = {}
    source._atomic_json(path, journal)
    if mutate == 'nonfinite':
        path.write_text('{"version":1,"sources":{},"attempts":{},"consumed":{},"x":1e999}')
    if mutate == 'duplicate':
        path.write_text('{"version":1,"sources":{},"sources":{},"attempts":{},"consumed":{}}')
    before = path.read_bytes()
    with pytest.raises(source.SourceHold):
        owner.capture(lambda: None, lambda: None)
    assert path.read_bytes() == before


@pytest.mark.parametrize('when', ['initial', 'post-refresh', 'terminal'])
def test_actual_identity_401_uses_only_one_outer_allowance(caller, when):
    caller.info['source'] = 'ok'
    caller.info['identities'] = {'initial': [401, 'Stable', 'Stable'],
        'post-refresh': [401, 401], 'terminal': ['Stable', 401]}[when]
    if caller.mode == 'keychain' and when == 'initial':
        caller.invoke()
        assert len(caller.configured.posts) == 3
    else:
        refused(caller)
    grants = sum(t[0] == 'grant' for t in caller.info['trace'])
    assert grants == int(caller.mode == 'keychain' and when != 'terminal')
    if grants:
        assert caller.source_creds['refresh_token'] == 'new-refresh'
        assert caller.info['probes'][1].headers['Authorization'] == 'Bearer new-access'


def test_actual_refresh_reproof_mismatch_keeps_pair(caller):
    if caller.mode != 'keychain':
        pytest.skip('reactive daemon only')
    caller.info.update(source='refresh', identities=['Original', 'Contradiction'])
    refused(caller)
    assert caller.source_creds['refresh_token'] == 'new-refresh'
    assert sum(t[0] == 'grant' for t in caller.info['trace']) == 1
    assert len(caller.info['gets']) == 2


@pytest.mark.parametrize('kind', ['ok', 'no-length', 'length-short', 'length-long', 'duplicate-length',
    'invalid-length', 'encoding', 'redirect', '403', '401', '500', 'oversize', 'oversize-error',
    'partial-range', 'ambiguous-framing', 'invalid-transfer', 'duplicate-transfer', 'chunked'])
def test_complete_transport_and_shared_accounting(owner, kind):
    import httpx
    import json
    from fulcra_collect import trakt_identity as identity
    from fulcra_collect.trakt_source import SourceHold
    binding, subject, _ = sample(owner)
    raw = json.dumps({'user': {'ids': {'uuid': 'Synthetic'}}}).encode()
    headers = {}
    status = 200
    if kind == 'ok':
        headers['Content-Length'] = str(len(raw))
    if kind.startswith('length-'):
        headers['Content-Length'] = str(len(raw) + (1 if kind == 'length-long' else -1))
    if kind == 'duplicate-length':
        headers = [('Content-Length', str(len(raw))), ('Content-Length', str(len(raw)))]
    if kind == 'invalid-length':
        headers['Content-Length'] = '+1'
    if kind == 'partial-range':
        headers['Content-Range'] = 'bytes 0-47/999'
    if kind == 'ambiguous-framing':
        headers.update({'Content-Length': str(len(raw)), 'Transfer-Encoding': 'chunked'})
    if kind == 'invalid-transfer':
        headers['Transfer-Encoding'] = 'gzip, chunked'
    if kind == 'duplicate-transfer':
        headers = [('Transfer-Encoding', 'chunked'), ('Transfer-Encoding', 'chunked')]
    if kind == 'chunked':
        headers['Transfer-Encoding'] = 'chunked'
    if kind == 'encoding':
        headers['Content-Encoding'] = 'gzip'
    if kind == 'redirect':
        status = 302
        headers['Location'] = identity.ENDPOINT + '?other'
    if kind in ('401', '403', '500'):
        status = int(kind)
        raw = b'synthetic error'
    if kind.startswith('oversize'):
        status = 401 if kind == 'oversize-error' else 200
        raw = b'x' * (identity.MAX_BYTES + 1)
    closed = []
    class Body(httpx.SyncByteStream):
        def __iter__(self):
            for pos in range(0, len(raw), 8192):
                yield raw[pos:pos+8192]
        def close(self):
            closed.append(True)
    requests = []
    def serve(request):
        requests.append(request)
        return httpx.Response(status, headers=headers, stream=Body())
    if kind in ('ok', 'no-length', 'chunked'):
        result = identity.probe_identity(subject, subject.window.bounds, transport=httpx.MockTransport(serve))
        assert result.identifier == 'Synthetic'
    else:
        with pytest.raises((SourceHold, httpx.HTTPStatusError)):
            identity.probe_identity(subject, subject.window.bounds, transport=httpx.MockTransport(serve))
    assert closed == [True]
    assert len(requests) == subject.window.bounds.requests == 1
    assert requests[0].url == identity.ENDPOINT
    assert requests[0].headers['Accept-Encoding'] == 'identity'
    assert requests[0].headers['trakt-api-version'] == '2'
    assert requests[0].headers['Authorization'] == 'Bearer synthetic-access'
    assert requests[0].headers['trakt-api-key'] == 'synthetic-client'
    if kind in ('ok', 'no-length', 'chunked', '401', '403', '500'):
        assert subject.window.bounds.bytes == len(raw)
    assert subject.window.bounds.rows == 0


@pytest.mark.parametrize('phase', ['authority', 'sampling', 'dispatch', 'chunk', 'parse', 'accept'])
def test_probe_cooperative_deadline_includes_all_slow_phases(owner, monkeypatch, phase):
    import httpx
    import json
    from fulcra_collect import trakt_identity as identity
    from fulcra_collect.trakt_source import SourceHold
    from fulcra_media.importers.trakt import _ScanBounds
    clock = [0.0]
    monkeypatch.setattr(identity.time, 'monotonic', lambda: clock[0])
    bounds = _ScanBounds()
    window = identity.ProbeWindow(bounds)
    binding = owner.capture(lambda: None, lambda: None)
    if phase == 'authority':
        binding.authority = lambda: clock.__setitem__(0, 8.1)
    if phase == 'sampling':
        original = owner._snapshot
        def slow(**kw):
            result = original(**kw)
            clock[0] = 8.1
            return result
        monkeypatch.setattr(owner, '_snapshot', slow)
    if phase in ('authority', 'sampling'):
        with pytest.raises(SourceHold):
            identity.snapshot_probe_subject(binding, window)
        assert bounds.requests == 0
        return
    subject = identity.snapshot_probe_subject(binding, window)
    raw = json.dumps({'user': {'ids': {'uuid': 'Synthetic'}}}).encode()
    class Body(httpx.SyncByteStream):
        def __iter__(self):
            if phase == 'chunk':
                clock[0] = 8.1
            yield raw
    def serve(request):
        if phase == 'dispatch':
            clock[0] = 8.1
        return httpx.Response(200, stream=Body())
    if phase == 'parse':
        original = identity.parse_identity
        def slow_parse(raw):
            result = original(raw)
            clock[0] = 8.1
            return result
        monkeypatch.setattr(identity, 'parse_identity', slow_parse)
    if phase == 'accept':
        observation = identity.probe_identity(subject, bounds, transport=httpx.MockTransport(serve))
        binding.authority = lambda: clock.__setitem__(0, 8.1)
        with pytest.raises(SourceHold):
            identity.accept_identity(binding, subject, observation)
    else:
        with pytest.raises(SourceHold):
            identity.probe_identity(subject, bounds, transport=httpx.MockTransport(serve))


@pytest.mark.parametrize('boundary', ['normalized', 'policy', 'post', 'receipt', 'progress', 'cursor'])
@pytest.mark.parametrize('mutation', ['pin', 'lineage'])
def test_actual_current_boundary_invalidation(caller, monkeypatch, boundary, mutation):
    from fulcra_collect import trakt_source as source
    from fulcra_media.importers import trakt
    from fulcra_media import cli, collect_v1
    caller.info['source'] = 'ok'
    def invalidate():
        from pathlib import Path
        root = caller.ctx._trakt_source_owner.root
        with caller.ctx._trakt_source_owner._locked() as journal:
            if mutation == 'pin':
                for pin in journal['identities'].values():
                    pin['revision'] = 'f' * 32
            else:
                for row in journal['sources'].values():
                    row['generation'] = 'f' * 32
            source._atomic_json(Path(root) / 'journal.json', journal)
    if boundary == 'normalized':
        original = trakt.normalize_history
        def normalized(*a, **kw):
            events = list(original(*a, **kw))
            invalidate()
            return events
        monkeypatch.setattr(trakt, 'normalize_history', normalized)
    elif boundary == 'policy':
        target = cli if caller.mode == 'cli' else __import__('fulcra_media.plugins.trakt', fromlist=['apply_cluster_policy'])
        name = '_resolve_cluster_policy' if caller.mode == 'cli' else 'apply_cluster_policy'
        original = getattr(target, name)
        def policy(*a, **kw):
            result = original(*a, **kw)
            invalidate()
            return result
        monkeypatch.setattr(target, name, policy)
    elif boundary in ('post', 'receipt'):
        original = caller.native.handle_request
        def handle(request):
            if (boundary == 'post' and request.url.path.startswith('/ingest/v1/record/')):
                invalidate()
            result = original(request)
            if boundary == 'receipt' and request.url.path.startswith('/ingest/v1/record/'):
                invalidate()
            return result
        monkeypatch.setattr(caller.native, 'handle_request', handle)
    elif boundary == 'progress':
        original = collect_v1.MediaRecordWriter.run_import
        def complete(*a, **kw):
            result = original(*a, **kw)
            invalidate()
            return result
        monkeypatch.setattr(collect_v1.MediaRecordWriter, 'run_import', complete)
    else:
        if caller.mode == 'cli':
            pytest.skip('Trakt CLI has no cursor')
        from fulcra_media.plugins import trakt as plugin
        original = plugin.newest_event_iso
        def newest(*a, **kw):
            invalidate()
            return original(*a, **kw)
        monkeypatch.setattr(plugin, 'newest_event_iso', newest)
    if caller.mode == 'cli':
        result, data = caller.invoke()
        assert result.exit_code == 2, result.output
    else:
        with pytest.raises(RuntimeError):
            caller.invoke()
    assert not any(k.startswith('v1_media_cursor:') for k in caller.kv)
    if boundary == 'progress':
        assert not any(e.get('stage') == 'imported' for e in caller.emitted)
    if boundary in ('normalized', 'policy'):
        assert caller.configured.posts == []


def test_concurrent_first_pin_and_conflict_publication_are_serialized(owner):
    import threading
    from copy import deepcopy
    from fulcra_collect.trakt_identity import accept_identity
    from fulcra_collect.trakt_source import SourceHold, _read_json
    first = sample(owner, 'First')
    second = sample(owner, 'Second', binding=first[0])
    barrier = threading.Barrier(2)
    outcomes = []
    def accept(args):
        barrier.wait()
        try:
            accept_identity(*args)
            outcomes.append('active')
        except SourceHold:
            outcomes.append('held')
    threads = [threading.Thread(target=accept, args=(args,)) for args in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
        assert not t.is_alive()
    assert sorted(outcomes) == ['active', 'held']
    pin = deepcopy(_read_json(owner.root / 'journal.json')['identities'][owner.key])
    assert {pin['first']['identifier'], pin['conflict']['identifier']} == {'First', 'Second'}
    assert pin['state'] == 'held'


@pytest.mark.parametrize('operation', ['manual', 'client', 'disconnect', 'oauth', 'recovery'])
def test_identity_survives_every_credential_row_replacement(owner, monkeypatch, operation):
    from copy import deepcopy
    from fulcra_collect.trakt_identity import accept_identity
    from fulcra_collect.trakt_source import _read_json
    binding, subject, observation = sample(owner)
    accept_identity(binding, subject, observation)
    path = owner.root / 'journal.json'
    first = deepcopy(_read_json(path)['identities'])
    if operation == 'manual':
        owner.edit('access_token', 'manual-access')
        owner.edit('refresh_token', 'manual-refresh')
    elif operation == 'client':
        owner.edit('client_id', 'another-client')
        owner.edit('access_token', 'manual-access')
        owner.edit('refresh_token', 'manual-refresh')
    elif operation == 'disconnect':
        owner.edit('access_token', None)
    elif operation == 'oauth':
        authorization = owner.begin_authorization('s', 'v', 'http://127.0.0.1/cb')
        authorization.exchange('code', lambda _: {'access_token': 'new', 'refresh_token': 'new-refresh',
            'created_at': '1', 'expires_in': '2', 'redirect_uri': 'http://127.0.0.1/cb'})
    else:
        original = owner._publish
        monkeypatch.setattr(owner, '_publish', lambda *a: (_ for _ in ()).throw(OSError('synthetic lost acknowledgement')))
        with pytest.raises(OSError):
            binding.refresh(lambda _: {'access_token': 'rotated', 'refresh_token': 'rotated-refresh'})
        monkeypatch.setattr(owner, '_publish', original)
        assert owner.capture(lambda: None, lambda: None).values['access_token'] == 'rotated'
    assert _read_json(path)['identities'] == first


@pytest.mark.parametrize('existing', [False, True])
def test_pin_persistence_failure_never_authorizes(owner, monkeypatch, existing):
    from fulcra_collect.trakt_identity import accept_identity
    from fulcra_collect.trakt_source import _read_json
    binding, subject, observation = sample(owner)
    if existing:
        accept_identity(binding, subject, observation)
        binding, subject, observation = sample(owner, 'Contradiction', binding=binding)
    path = owner.root / 'journal.json'
    before = path.read_bytes()
    monkeypatch.setattr(owner, '_save', lambda _: (_ for _ in ()).throw(OSError('synthetic storage failure')))
    with pytest.raises(OSError):
        accept_identity(binding, subject, observation)
    assert path.read_bytes() == before
    if not existing:
        assert _read_json(path)['identities'] == {}


def test_identity_get_uses_remaining_shared_request_and_byte_caps(owner, monkeypatch):
    import httpx
    import json
    from fulcra_collect import trakt_identity as identity
    from fulcra_media.importers import trakt
    binding, subject, _ = sample(owner)
    bounds = subject.window.bounds
    monkeypatch.setattr(trakt, 'MAX_REQUESTS', 1)
    monkeypatch.setattr(trakt, 'MAX_TOTAL_BYTES', 4)
    raw = json.dumps({'user': {'ids': {'uuid': 'Synthetic'}}}).encode()
    transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=httpx.ByteStream(raw)))
    with pytest.raises(RuntimeError):
        identity.probe_identity(subject, bounds, transport=transport)
    assert bounds.requests == 1 and bounds.bytes == len(raw)
    with pytest.raises(RuntimeError):
        identity.probe_identity(subject, bounds, transport=transport)
    assert bounds.requests == 1


def test_source_edit_at_acceptance_rejects_observation(owner):
    from fulcra_collect.trakt_identity import accept_identity
    from fulcra_collect.trakt_source import SourceHold, _read_json
    binding, subject, observation = sample(owner)
    binding.authority = lambda: owner.edit('access_token', 'new-selection')
    with pytest.raises(SourceHold):
        accept_identity(binding, subject, observation)
    assert _read_json(owner.root / 'journal.json')['identities'] == {}


def test_direct_parser_never_accepts_oversize_body():
    from fulcra_collect.trakt_identity import parse_identity, MAX_BYTES
    from fulcra_collect.trakt_source import SourceHold
    raw = b'{"user":{"ids":{"uuid":"synthetic"}},"extra":"' + b'x' * MAX_BYTES + b'"}'
    with pytest.raises(SourceHold):
        parse_identity(raw)


def test_malformed_legacy_consumed_value_holds_without_upgrade(owner):
    from fulcra_collect import trakt_source as source
    owner.capture(lambda: None, lambda: None)
    path = owner.root / 'journal.json'
    journal = source._read_json(path)
    journal.pop('identities')
    journal.update(version=1, consumed={'a' * 64: {}})
    source._atomic_json(path, journal)
    before = path.read_bytes()
    with pytest.raises(source.SourceHold):
        owner.capture(lambda: None, lambda: None)
    assert path.read_bytes() == before


def test_real_old_reader_rejects_upgraded_journal(owner):
    from contextlib import contextmanager
    from pathlib import Path
    from fulcra_collect import trakt_source as source
    owner.capture(lambda: None, lambda: None)
    # Exact immutable BASE Owner._locked body, extracted without loading old
    # source or touching the shared checkout. It delegates to this private owner.
    namespace = {'_read_json': source._read_json, '_hold': source._hold}
    code = (Path(__file__).parent / 'fixtures/trakt_v1_locked.txt').read_text()
    exec(compile(code, '<BASE-v1-reader>', 'exec'), namespace)
    with pytest.raises(source.SourceHold):
        with contextmanager(namespace['_locked'])(owner):
            pytest.fail('old writer accepted identity journal')


def test_upgrade_preserves_full_attempt_consumption_and_source_values(owner):
    from copy import deepcopy
    from fulcra_collect import trakt_source as source
    binding = owner.capture(lambda: None, lambda: None)
    binding.refresh(lambda _: {'access_token': 'next', 'refresh_token': 'next-refresh'})
    path = owner.root / 'journal.json'
    legacy = source._read_json(path)
    del legacy['identities']
    legacy['version'] = 1
    original = deepcopy(legacy)
    source._atomic_json(path, legacy)
    owner.capture(lambda: None, lambda: None)
    upgraded = source._read_json(path)
    assert upgraded == {**original, 'version': 2, 'identities': {}}
    assert len(upgraded['attempts']) == len(upgraded['consumed']) == 1


def test_actual_initial_slow_capture_opens_probe_deadline_first(caller, monkeypatch):
    from fulcra_collect import trakt_source as source, trakt_identity as identity
    clock = [0.0]
    monkeypatch.setattr(identity.time, 'monotonic', lambda: clock[0])
    original = source.Owner.capture
    def slow(*a, **kw):
        result = original(*a, **kw)
        clock[0] = 9.0
        return result
    monkeypatch.setattr(source.Owner, 'capture', slow)
    caller.info['source'] = 'ok'
    refused(caller)
    assert caller.info.get('probes', []) == []
    assert caller.info['gets'] == []


def test_actual_worker_missing_identity_blocks_normalization(caller, monkeypatch):
    if caller.mode != 'keychain':
        pytest.skip('ordinary daemon entry')
    from fulcra_media.importers import trakt
    caller.info.update(source='ok', identities=[None])
    monkeypatch.setattr(trakt, 'normalize_history', lambda *a, **kw: pytest.fail('missing identity normalized'))
    outcome, output = caller.worker()
    assert outcome == 'error'
    assert output[-1]['outcome'] == 'error'
    assert caller.configured.posts == [] and caller.info['gets'] == []


def test_actual_saved_pin_conflict_holds_before_history_and_is_static(caller):
    from fulcra_collect.trakt_identity import MISMATCH
    caller.info['source'] = 'ok'
    caller.invoke()
    originals = list(caller.configured.posts)
    caller.info['gets'].clear()
    caller.info['identities'] = ['Contradiction']
    if caller.mode == 'cli':
        result, data = caller.invoke()
        assert result.exit_code == 2
        assert data['errors'][0]['message'] == MISMATCH
        assert data['total'] == data['posted'] == data['verified'] == 0
    else:
        with pytest.raises(RuntimeError, match=MISMATCH):
            caller.invoke()
    assert caller.configured.posts == originals and caller.info['gets'] == []


@pytest.mark.parametrize('corruption', ['committed-pending', 'missing-consumption', 'utf16'])
def test_invalid_v1_state_is_preserved_and_never_upgraded(owner, corruption):
    import json
    from fulcra_collect import trakt_source as source
    binding = owner.capture(lambda: None, lambda: None)
    if corruption == 'missing-consumption':
        binding.refresh(lambda _: {'access_token': 'next', 'refresh_token': 'next-refresh'})
    path = owner.root / 'journal.json'
    journal = source._read_json(path)
    journal.pop('identities')
    journal['version'] = 1
    if corruption == 'committed-pending':
        journal['sources'][owner.key]['pending'] = {}
    elif corruption == 'missing-consumption':
        journal['consumed'] = {}
    source._atomic_json(path, journal)
    if corruption == 'utf16':
        path.write_bytes(json.dumps(journal).encode('utf-16'))
    before = path.read_bytes()
    with pytest.raises(source.SourceHold):
        owner.capture(lambda: None, lambda: None)
    assert path.read_bytes() == before


@pytest.mark.parametrize('changes', [{'identifier': None}, {'identifier': ' '}, {'observed_at': float('nan')}])
def test_invalid_observation_cannot_publish_pin(owner, changes):
    from dataclasses import replace
    from fulcra_collect.trakt_identity import accept_identity
    from fulcra_collect.trakt_source import SourceHold, _read_json
    binding, subject, observation = sample(owner)
    with pytest.raises(SourceHold):
        accept_identity(binding, subject, replace(observation, **changes))
    assert _read_json(owner.root / 'journal.json')['identities'] == {}


def test_outer_allowance_does_not_refresh_on_fulcra_authority_401(caller, monkeypatch):
    if caller.mode != 'keychain':
        pytest.skip('reactive daemon only')
    import httpx
    from fulcra_collect import trakt_identity as identity
    caller.info['source'] = 'ok'
    original = identity.snapshot_probe_subject
    attempts = []
    def authority_failure(*a, **kw):
        if not attempts:
            attempts.append(True)
            request = httpx.Request('GET', 'https://synthetic.invalid/user/v1alpha1/info')
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError('synthetic authority failure', request=request, response=response)
        return original(*a, **kw)
    monkeypatch.setattr(identity, 'snapshot_probe_subject', authority_failure)
    refused(caller)
    assert not any(t[0] == 'grant' for t in caller.info['trace'])


@pytest.mark.parametrize('mutation', ['pin', 'lineage'])
def test_actual_between_chunks_holds_with_original_verified_lower_bound(caller, monkeypatch, mutation):
    from fulcra_collect import trakt_source as source
    from fulcra_collect.v1_outbox import V1RecordOutbox
    from fulcra_media import collect_v1
    caller.info['source'] = 'ok'
    original_import = collect_v1.MediaRecordWriter.run_import
    def small(writer, events, **kw):
        kw['chunk_size'] = 1
        return original_import(writer, events, **kw)
    monkeypatch.setattr(collect_v1.MediaRecordWriter, 'run_import', small)
    original_drain = V1RecordOutbox.drain
    invalidated = []
    def drained(outbox):
        result = original_drain(outbox)
        if result and not invalidated:
            invalidated.append(True)
            owner = caller.ctx._trakt_source_owner
            with owner._locked() as journal:
                family = 'identities' if mutation == 'pin' else 'sources'
                field = 'revision' if mutation == 'pin' else 'generation'
                for row in journal[family].values():
                    row[field] = 'f' * 32
                source._atomic_json(owner.root / 'journal.json', journal)
        return result
    monkeypatch.setattr(V1RecordOutbox, 'drain', drained)
    if caller.mode == 'cli':
        result, data = caller.invoke()
        assert result.exit_code == 2
        assert data['posted'] == data['verified'] == 1
    else:
        with pytest.raises(RuntimeError):
            caller.invoke()
    assert len(caller.configured.posts) == 1
    assert not any(k.startswith('v1_media_cursor:') for k in caller.kv)
    assert len(caller.info['probes']) == 2


def test_nested_admission_held_pin_reports_static_mismatch(owner):
    from fulcra_collect.trakt_identity import accept_identity, MISMATCH
    from fulcra_collect.trakt_source import SourceHold
    binding, subject, observation = sample(owner)
    admission = accept_identity(binding, subject, observation)
    _, conflicting_subject, conflict = sample(owner, 'Contradiction', binding=binding)
    with pytest.raises(SourceHold, match=MISMATCH):
        accept_identity(binding, conflicting_subject, conflict)
    with pytest.raises(SourceHold, match=MISMATCH):
        admission.check_local(binding)


class _Round1Interruption(OSError):
    pass


def _round1_transition(owner, monkeypatch, phase):
    """Journal values emitted by unchanged owner writers, with private faults."""
    from copy import deepcopy
    from fulcra_collect import trakt_source as source
    def stop(*a, **kw):
        raise _Round1Interruption('synthetic persistence boundary')
    def rotated(_):
        return {'access_token': 'round1-next-access', 'refresh_token': 'round1-next-refresh'}
    def manual_pair():
        owner.edit('access_token', 'round1-manual-access')
        owner.edit('refresh_token', 'round1-manual-refresh')
    def authorize():
        return owner.begin_authorization('synthetic-state', 'synthetic-verifier', 'http://127.0.0.1/cb')
    def exchanged(_):
        return {'access_token': 'round1-oauth-access', 'refresh_token': 'round1-oauth-refresh',
                'created_at': '1', 'expires_in': '2', 'redirect_uri': 'http://127.0.0.1/cb'}
    if phase == 'empty':
        with owner._locked() as journal:
            return deepcopy(journal)
    binding = owner.capture(lambda: None, lambda: None)
    with monkeypatch.context() as fault:
        if phase == 'initial-committed':
            pass
        elif phase == 'editing-before-authorization':
            fault.setattr(owner.store, 'authorize', stop)
            with pytest.raises(_Round1Interruption):
                owner.edit('access_token', 'round1-manual-access')
        elif phase in ('editing-verified', 'old-intent-list'):
            assert owner.edit('access_token', 'round1-manual-access')['ready'] is False
        elif phase == 'disconnected':
            owner.edit('access_token', None)
        elif phase in ('authorization-pre-setup', 'superseded-pre-setup'):
            original = owner._save
            def pre_setup(journal):
                original(journal)
                if any(a['kind'] == 'authorization' and 'setup' not in a for a in journal['attempts'].values()):
                    stop()
            fault.setattr(owner, '_save', pre_setup)
            with pytest.raises(_Round1Interruption):
                authorize()
            if phase == 'superseded-pre-setup':
                fault.setattr(owner, '_save', original)
                authorize()
        elif phase in ('authorization-await-code', 'superseded-authorizing'):
            authorize()
            if phase == 'superseded-authorizing':
                authorize()
        elif phase == 'authorization-exchanged':
            authorization = authorize()
            with pytest.raises(_Round1Interruption):
                authorization.exchange('synthetic-code', stop)
        elif phase.startswith('authorization-publishing') or phase == 'authorization-partial-mirror':
            if phase == 'authorization-publishing-after-refresh':
                binding.refresh(rotated)
            authorization = authorize()
            def interrupted_publish(values):
                if phase == 'authorization-partial-mirror':
                    owner.store.set('access_token', values['access_token'])
                stop()
            fault.setattr(owner.store, 'publish', interrupted_publish)
            with pytest.raises(_Round1Interruption):
                authorization.exchange('synthetic-code', exchanged)
            if phase == 'authorization-publishing-quarantined':
                owner.store.set('client_id', 'round1-external-client')
                attempt = source._read_json(owner.root / 'journal.json')['sources'][owner.key]['attempt']
                with pytest.raises(source.SourceHold):
                    owner._publish(attempt, lambda: None, lambda: None)
        elif phase == 'authorization-committed':
            authorize().exchange('synthetic-code', exchanged)
        elif phase == 'authorization-quarantined':
            def drifted_authorization(payload):
                owner.store.set('access_token', 'round1-external-drift')
                return exchanged(payload)
            with pytest.raises(source.SourceHold):
                authorize().exchange('synthetic-code', drifted_authorization)
        elif phase == 'refresh-committed':
            binding.refresh(rotated)
        elif phase == 'refresh-committed-unchanged-digest':
            binding.refresh(lambda old: {k: old[k] for k in ('access_token', 'refresh_token')})
        elif phase in ('refresh-claimed', 'refresh-from-prior-successor', 'prior-successor-quarantined'):
            if phase != 'refresh-claimed':
                binding.refresh(rotated)
                previous = binding.row['successor']
            with pytest.raises(_Round1Interruption):
                binding.refresh(stop)
            if phase == 'prior-successor-quarantined':
                with pytest.raises(source.SourceHold):
                    owner._publish(previous, lambda: None, lambda: None)
        elif phase in ('refresh-publishing', 'refresh-partial-mirror', 'publishing-quarantined',
                       'publishing-from-prior-successor'):
            if phase == 'publishing-from-prior-successor':
                binding.refresh(rotated)
            def interrupted_publish(values):
                if phase == 'refresh-partial-mirror':
                    owner.store.set('access_token', values['access_token'])
                stop()
            fault.setattr(owner.store, 'publish', interrupted_publish)
            with pytest.raises(_Round1Interruption):
                binding.refresh(lambda _: {'access_token': 'round1-final-access', 'refresh_token': 'round1-final-refresh'})
            if phase == 'publishing-quarantined':
                owner.store.set('client_id', 'round1-external-client')
                attempt = source._read_json(owner.root / 'journal.json')['sources'][owner.key]['attempt']
                with pytest.raises(source.SourceHold):
                    owner._publish(attempt, lambda: None, lambda: None)
        elif phase == 'refresh-quarantined':
            def drifted_refresh(old):
                owner.store.set('access_token', 'round1-external-drift')
                return rotated(old)
            with pytest.raises(source.SourceHold):
                binding.refresh(drifted_refresh)
        elif phase == 'committed-successor-quarantined':
            binding.refresh(rotated)
            owner.store.set('access_token', 'round1-external-drift')
            with pytest.raises(source.SourceHold):
                owner._publish(binding.row['successor'], lambda: None, lambda: None)
        elif phase == 'superseded-committed':
            binding.refresh(rotated)
            manual_pair()
        elif phase in ('superseded-claimed', 'superseded-quarantined'):
            def superseded(old):
                manual_pair()
                return stop() if phase == 'superseded-claimed' else rotated(old)
            with pytest.raises(_Round1Interruption if phase == 'superseded-claimed' else source.SourceHold):
                binding.refresh(superseded)
        else:
            raise AssertionError('unknown synthetic transition')
    journal = source._read_json(owner.root / 'journal.json')
    row = journal['sources'][owner.key]
    expected = {
        'editing-before-authorization': 'editing', 'editing-verified': 'editing', 'old-intent-list': 'editing',
        'disconnected': 'disconnected', 'authorization-pre-setup': 'authorizing',
        'authorization-await-code': 'authorizing', 'superseded-authorizing': 'authorizing',
        'superseded-pre-setup': 'authorizing', 'authorization-publishing-quarantined': 'publishing',
        'authorization-exchanged': 'authorizing', 'authorization-quarantined': 'authorizing',
        'authorization-publishing': 'publishing', 'authorization-publishing-after-refresh': 'publishing',
        'authorization-partial-mirror': 'publishing', 'refresh-claimed': 'refresh-claimed',
        'refresh-from-prior-successor': 'refresh-claimed', 'prior-successor-quarantined': 'refresh-claimed',
        'refresh-publishing': 'publishing', 'refresh-partial-mirror': 'publishing',
        'publishing-quarantined': 'publishing', 'publishing-from-prior-successor': 'publishing',
        'refresh-quarantined': 'refresh-claimed',
    }.get(phase, 'committed')
    assert row['status'] == expected
    if phase in ('authorization-quarantined', 'refresh-quarantined', 'publishing-quarantined',
                 'authorization-publishing-quarantined'):
        assert journal['attempts'][row['attempt']]['status'] == 'quarantined'
    if phase in ('committed-successor-quarantined', 'prior-successor-quarantined'):
        assert journal['attempts'][row['successor']]['status'] == 'quarantined'
    if phase == 'old-intent-list':
        row['pending'] = list(row['pending'])  # supported older intent-only on-disk representation
    return deepcopy(journal)


_ROUND1_VALID_PHASES = (
    'empty', 'initial-committed', 'editing-before-authorization', 'editing-verified', 'old-intent-list',
    'disconnected', 'authorization-pre-setup', 'authorization-await-code', 'authorization-exchanged',
    'authorization-publishing', 'authorization-publishing-after-refresh', 'authorization-partial-mirror',
    'authorization-committed', 'authorization-quarantined', 'refresh-claimed', 'refresh-publishing',
    'refresh-partial-mirror', 'refresh-committed', 'refresh-committed-unchanged-digest',
    'refresh-quarantined', 'publishing-quarantined', 'committed-successor-quarantined',
    'refresh-from-prior-successor', 'publishing-from-prior-successor', 'prior-successor-quarantined',
    'superseded-claimed', 'superseded-authorizing', 'superseded-committed', 'superseded-quarantined',
    'superseded-pre-setup', 'authorization-publishing-quarantined',
)


@pytest.mark.parametrize('phase', _ROUND1_VALID_PHASES)
def test_round1_transition_derived_v1_upgrade_and_v2_reopen(owner, monkeypatch, phase):
    from copy import deepcopy
    from fulcra_collect import trakt_source as source
    journal = _round1_transition(owner, monkeypatch, phase)
    journal.pop('identities')
    journal['version'] = 1
    original = deepcopy(journal)
    path = owner.root / 'journal.json'
    source._atomic_json(path, journal)
    expected = {**original, 'version': 2, 'identities': {}}
    with owner._locked() as upgraded:
        assert upgraded == expected
    assert source._read_json(path) == expected
    before = path.read_bytes()
    reopened = source.Owner(owner.store, _root=owner.root)
    with reopened._locked() as retained:
        assert retained == expected
    assert path.read_bytes() == before


_ROUND1_INVALID_RELATIONS = (
    'authorizing-points-to-refresh', 'committed-successor-claimed', 'current-other-consent',
    'current-other-store', 'current-other-generation', 'current-other-digest', 'current-committed-phase',
    'refresh-points-to-authorization', 'publishing-other-generation', 'publishing-auth-unexchanged',
    'publishing-auth-retained-successor', 'successor-other-store', 'successor-other-consent',
    'successor-predecessor-generation', 'authorization-successor-unexchanged',
    'authorization-successor-no-setup', 'exchanged-authorization-without-setup', 'historical-missing-store',
    'current-other-existing-store', 'successor-other-existing-store', 'authorizing-current-committed',
)


@pytest.mark.parametrize('version', [1, 2])
@pytest.mark.parametrize('relation', _ROUND1_INVALID_RELATIONS)
def test_round1_incoherent_journal_refuses_without_any_rewrite(owner, monkeypatch, version, relation):
    from copy import deepcopy
    from fulcra_collect import trakt_source as source
    phase = {
        'authorizing-points-to-refresh': 'authorization-await-code',
        'committed-successor-claimed': 'refresh-committed',
        'refresh-points-to-authorization': 'authorization-await-code',
        'publishing-other-generation': 'refresh-publishing',
        'publishing-auth-unexchanged': 'authorization-publishing',
        'publishing-auth-retained-successor': 'authorization-publishing-after-refresh',
        'successor-other-store': 'refresh-committed', 'successor-other-consent': 'refresh-committed',
        'successor-predecessor-generation': 'refresh-committed',
        'authorization-successor-unexchanged': 'authorization-committed',
        'authorization-successor-no-setup': 'authorization-committed',
        'exchanged-authorization-without-setup': 'authorization-await-code',
        'historical-missing-store': 'superseded-committed',
        'successor-other-existing-store': 'refresh-committed',
        'authorizing-current-committed': 'authorization-exchanged',
    }.get(relation, 'refresh-claimed')
    journal = _round1_transition(owner, monkeypatch, phase)
    row = journal['sources'][owner.key]
    attempt_id = row.get('attempt') or row.get('successor') or next(iter(journal['attempts']))
    attempt = journal['attempts'][attempt_id]
    if relation == 'authorizing-points-to-refresh':
        attempt['kind'] = 'refresh'
        for key in ('state', 'exchanged', 'setup'):
            attempt.pop(key)
        journal['consumed']['f' * 64] = attempt_id
    elif relation in ('committed-successor-claimed', 'current-committed-phase', 'authorizing-current-committed'):
        attempt['status'] = 'claimed' if relation == 'committed-successor-claimed' else 'committed'
    elif relation in ('current-other-consent', 'successor-other-consent'):
        attempt['consent'] = '1' * 32
    elif relation in ('current-other-store', 'successor-other-store', 'historical-missing-store',
                      'current-other-existing-store', 'successor-other-existing-store'):
        attempt['store'] = '1' * 64
        if relation.endswith('existing-store'):
            journal['sources']['1' * 64] = {'consent': '2' * 32, 'generation': '3' * 32,
                'status': 'committed', 'digest': '4' * 64}
    elif relation in ('current-other-generation', 'publishing-other-generation'):
        attempt['generation'] = '1' * 32
    elif relation == 'current-other-digest':
        attempt['digest'] = '1' * 64
    elif relation == 'refresh-points-to-authorization':
        row['status'] = 'refresh-claimed'
    elif relation in ('publishing-auth-unexchanged', 'authorization-successor-unexchanged'):
        attempt['exchanged'] = False
    elif relation == 'publishing-auth-retained-successor':
        row['successor'] = next(key for key in journal['attempts'] if key != attempt_id)
    elif relation == 'successor-predecessor-generation':
        row['generation'] = attempt['generation']
    elif relation == 'authorization-successor-no-setup':
        attempt.pop('setup')
    else:
        attempt.pop('setup')
        attempt['exchanged'] = True
    journal['version'] = version
    if version == 1:
        journal.pop('identities')
    path = owner.root / 'journal.json'
    source._atomic_json(path, journal)
    before = path.read_bytes()
    saves = []
    original_save = owner._save
    def tracked_save(candidate):
        saves.append(deepcopy(candidate))
        original_save(candidate)
    monkeypatch.setattr(owner, '_save', tracked_save)
    outcome = 'accepted'
    try:
        with owner._locked():
            pass
    except source.SourceHold:
        outcome = 'held'
    assert (outcome, path.read_bytes() == before, len(saves)) == ('held', True, 0)
