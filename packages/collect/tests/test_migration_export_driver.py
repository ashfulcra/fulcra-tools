"""Synthetic paired export, atomic checkpoints and conservative resume."""
import json
from datetime import timedelta

import pytest

from fulcra_collect.migration_export import HistoricalReader
from fulcra_collect.migration_ledger import MigrationLedger
from fulcra_collect.migration_plan import _time
from test_migration_export import Client, response
from test_migration_ledger import ACCOUNT, START, END, plan, raw, uid


CONTRACT = {'version': 1, 'semantics': 'complete-below-row-cap', 'row_cap': 4,
            'basis': 'Synthetic fixture server returns every intersecting record below its explicit cap.'}


def setup_driver(tmp_path, handler, **kwargs):
    from fulcra_collect.migration_export_driver import ExportDriver
    ledger = MigrationLedger(tmp_path / 'migration.sqlite', plan())
    reader = HistoricalReader(plan(), client=Client(handler), guard=lambda: ACCOUNT,
        artifacts=tmp_path / 'raw', pass_id=uid(30), timeout=1)
    return ExportDriver(ledger, reader, provider_contract=kwargs.pop('contract', CONTRACT), **kwargs)


def test_driver_requires_explicit_complete_read_contract_before_new_window_or_http(tmp_path):
    for contract in (None, {}, {**CONTRACT, 'row_cap': True}, {**CONTRACT, 'basis': ''},
                     {**CONTRACT, 'semantics': 'HTTP-200-means-complete'}):
        with pytest.raises(ValueError, match='contract|completeness'):
            setup_driver(tmp_path, lambda request: pytest.fail('unexpected HTTP'), contract=contract)
    ledger = MigrationLedger(tmp_path / 'migration.sqlite', plan())
    assert ledger.status()['counts'] == {} and ledger.status()['coverage_complete'] is False


def test_pair_preserves_duplicate_observations_and_resumes_without_any_new_get(tmp_path):
    calls = []
    records = [raw(), raw()]
    def handle(request):
        calls.append(request)
        return response(200, content=json.dumps(records).encode())
    driver = setup_driver(tmp_path, handle)
    result = driver.run(max_windows=1, budget_seconds=10)
    assert result['sealed'] == 1 and result['window_queue_empty'] is True
    assert result['historical_complete'] is False and result['coverage_complete'] is False
    assert driver.ledger.status()['observations'] == 2
    assert driver.ledger.status()['counts'] == {'exported': 1}
    proof = driver.window_proof(driver.root_window_id)
    assert proof['outcome'] == 'sealed-matched-below-cap' and proof['provider_count'] == 2
    assert proof['provider_contract'] == CONTRACT
    assert proof['asc']['attempt_id'] != proof['desc']['attempt_id']
    driver.run(max_windows=1, budget_seconds=10)
    assert len(calls) == 2
    fresh = setup_driver(tmp_path, handle)
    assert fresh.run(max_windows=1, budget_seconds=10)['processed'] == 0
    assert len(calls) == 2


def test_changed_contract_or_pass_cannot_reinterpret_existing_export(tmp_path):
    driver = setup_driver(tmp_path, lambda request: response(200, content=b'[]'))
    with pytest.raises(ValueError, match='pass|scope|contract'):
        setup_driver(tmp_path, lambda request: pytest.fail('unexpected HTTP'),
            contract={**CONTRACT, 'row_cap': 3})
    from fulcra_collect.migration_export_driver import ExportDriver
    changed = HistoricalReader(plan(), client=driver.reader.client, guard=lambda: ACCOUNT,
        artifacts=tmp_path / 'raw', pass_id=uid(31), timeout=1)
    with pytest.raises(ValueError, match='pass|scope'):
        ExportDriver(driver.ledger, changed, provider_contract=CONTRACT)


def test_unbound_existing_windows_cannot_be_adopted_by_a_new_pass(tmp_path):
    ledger = MigrationLedger(tmp_path / 'migration.sqlite', plan())
    ledger.open_window(uid(55), start_time=START, end_time=END)
    with pytest.raises(ValueError, match='unbound|existing|adopt'):
        setup_driver(tmp_path, lambda request: pytest.fail('unexpected HTTP'))


def test_saturated_parent_splits_with_overlap_and_resumes_each_leaf_once(tmp_path):
    calls = []
    def handle(request):
        calls.append(request)
        start, end = [_time(request.url.params[key]) for key in ('start_time', 'end_time')]
        rows = [raw(id=uid(i + 70)) for i in range(4)] if end - start == timedelta(days=2) else []
        return response(200, content=json.dumps(rows).encode())
    driver = setup_driver(tmp_path, handle)
    first = driver.run(max_windows=1, budget_seconds=10)
    assert first['split'] == 1 and first['sealed'] == 0 and first['window_queue_empty'] is False
    parent = driver.ledger.window(driver.root_window_id)
    children = [driver.ledger.window(identity) for identity in parent['evidence']['children']]
    children.sort(key=lambda child: child['start_time'])
    assert _time(children[0]['end_time']) > _time(children[1]['start_time'])
    assert children[0]['start_time'] == START and children[1]['end_time'] == END
    assert driver.ledger.status()['coverage_complete'] is False
    assert driver.ledger.status()['counts'] == {'exported': 4}
    assert driver.window_proof(driver.root_window_id)['outcome'] == 'split-saturated'
    final = setup_driver(tmp_path, handle).run(max_windows=2, budget_seconds=10)
    assert final['sealed'] == 2 and final['window_queue_empty'] is True
    assert len(calls) == 6
    assert driver.ledger.status()['coverage_complete'] is True  # conditional explicit contract only
    assert driver.ledger.status()['historical_complete'] is False


def test_disagreeing_dual_read_is_retained_and_split_without_choosing_a_version(tmp_path):
    driver = setup_driver(tmp_path, lambda request: response(200, content=json.dumps([
        raw(note=request.url.params['sort'])]).encode()))
    result = driver.run(max_windows=1, budget_seconds=10)
    assert result['split'] == 1 and driver.ledger.status()['counts'] == {'exported': 2}
    proof = driver.window_proof(driver.root_window_id)
    assert proof['outcome'] == 'split-inconsistent'
    assert len(proof['entry_ids']) == 2
    assert {driver.ledger.entry(identity).raw['note'] for identity in proof['entry_ids']} == {'asc', 'desc'}
    bodies = list((tmp_path / 'raw').rglob('body.json'))
    assert len(bodies) == 2 and bodies[0].read_bytes() != bodies[1].read_bytes()


def test_sort_reversal_is_not_a_content_disagreement(tmp_path):
    rows = [raw(id=uid(60)), raw(id=uid(61))]
    driver = setup_driver(tmp_path, lambda request: response(200, content=json.dumps(
        rows if request.url.params['sort'] == 'asc' else rows[::-1]).encode()))
    assert driver.run(max_windows=1, budget_seconds=10)['sealed'] == 1


def test_second_read_failure_leaves_window_open_and_retry_uses_new_raw_attempts(tmp_path):
    calls = []
    def handle(request):
        calls.append(request)
        return response(500 if len(calls) == 2 else 200, content=b'[]')
    driver = setup_driver(tmp_path, handle)
    with pytest.raises(RuntimeError, match='HTTP'):
        driver.run(max_windows=1, budget_seconds=10)
    assert driver.ledger.window(driver.root_window_id)['status'] == 'open'
    assert driver.window_proof(driver.root_window_id) is None
    assert driver.run(max_windows=1, budget_seconds=10)['sealed'] == 1
    assert len(list((tmp_path / 'raw').rglob('body.json'))) == 4


def test_proof_and_rows_rollback_together_on_seal_failure(tmp_path, monkeypatch):
    driver = setup_driver(tmp_path, lambda request: response(200, content=json.dumps([raw()]).encode()))
    original = driver.ledger._seal_window
    def fail(*args):
        original(*args)
        raise RuntimeError('synthetic final write interruption')
    monkeypatch.setattr(driver.ledger, '_seal_window', fail)
    with pytest.raises(RuntimeError, match='interruption'):
        driver.run(max_windows=1, budget_seconds=10)
    assert driver.window_proof(driver.root_window_id) is None
    assert driver.ledger.status()['counts'] == {} and driver.ledger.status()['observations'] == 0
    assert driver.ledger.window(driver.root_window_id)['status'] == 'open'


def test_tampered_response_artifact_cannot_back_a_complete_window(tmp_path, monkeypatch):
    driver = setup_driver(tmp_path, lambda request: response(200, content=b'[]'))
    original = driver.reader.read
    def corrupt(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs['order'] == 'asc':
            result.body_path.write_bytes(b'[{}]')
        return result
    monkeypatch.setattr(driver.reader, 'read', corrupt)
    with pytest.raises(RuntimeError, match='integrity|artifact|hash'):
        driver.run(max_windows=1, budget_seconds=10)
    assert driver.ledger.window(driver.root_window_id)['status'] == 'open'


def test_unknown_pass_version_and_window_id_refuse_before_network(tmp_path):
    driver = setup_driver(tmp_path, lambda request: pytest.fail('unexpected HTTP'))
    with driver.ledger.storage._db(write=True) as db:
        db.execute('UPDATE migration_export_pass SET version=99')
    with pytest.raises(ValueError, match='pass|version'):
        driver.run(max_windows=1, budget_seconds=10)


def test_budget_requires_room_for_a_pair_without_dispatching(tmp_path):
    driver = setup_driver(tmp_path, lambda request: pytest.fail('unexpected HTTP'))
    with pytest.raises(ValueError, match='budget|bound'):
        driver.run(max_windows=1, budget_seconds=1)


def test_saturated_minimum_window_is_unknown_not_split_or_complete(tmp_path):
    from fulcra_collect.migration_export_driver import ExportDriver
    p = plan(end_time='2020-01-01T00:00:00.000002Z')
    ledger = MigrationLedger(tmp_path / 'migration.sqlite', p)
    source = HistoricalReader(p, client=Client(lambda request: response(200,
        content=json.dumps([raw(id=uid(i + 70)) for i in range(4)]).encode())),
        guard=lambda: ACCOUNT, artifacts=tmp_path / 'raw', pass_id=uid(30), timeout=1)
    driver = ExportDriver(ledger, source, provider_contract=CONTRACT)
    with pytest.raises(RuntimeError, match='minimum|narrow|unknown'):
        driver.run(max_windows=1, budget_seconds=10)
    assert driver.ledger.window(driver.root_window_id)['status'] == 'open'
    assert driver.ledger.status()['coverage_complete'] is False


def test_changed_window_identity_cannot_be_consumed_under_pass(tmp_path):
    driver = setup_driver(tmp_path, lambda request: pytest.fail('unexpected HTTP'))
    with driver.ledger.storage._db(write=True) as db:
        db.execute('UPDATE migration_windows SET window_id=?', (uid(99),))
    with pytest.raises(ValueError, match='window|identity|scope'):
        driver.run(max_windows=1, budget_seconds=10)


def test_slow_first_read_cannot_start_second_after_work_budget(tmp_path):
    clock, calls = [0.0], []
    def handle(request):
        calls.append(request)
        clock[0] = 9.0
        return response(200, content=b'[]')
    driver = setup_driver(tmp_path, handle, clock=lambda: clock[0])
    with pytest.raises(RuntimeError, match='deadline|budget'):
        driver.run(max_windows=1, budget_seconds=10)
    assert len(calls) == 1
    assert driver.ledger.window(driver.root_window_id)['status'] == 'open'


def test_future_or_reclassified_pair_proof_is_not_silently_accepted(tmp_path):
    from fulcra_collect.record_actions import _json, _hash
    driver = setup_driver(tmp_path, lambda request: response(200, content=b'[]'))
    driver.run(max_windows=1, budget_seconds=10)
    for change in ({'version': 99}, {'outcome': 'future-state'}, {'provider_count': 2}):
        proof = driver.window_proof(driver.root_window_id)
        changed = _json({**proof, **change})
        with driver.ledger.storage._db(write=True) as db:
            db.execute('UPDATE migration_window_reads SET document=?,fingerprint=?', (changed, _hash(changed)))
        with pytest.raises(ValueError, match='proof|corrupt|version'):
            driver.window_proof(driver.root_window_id)
        original = _json(proof)
        with driver.ledger.storage._db(write=True) as db:
            db.execute('UPDATE migration_window_reads SET document=?,fingerprint=?', (original, _hash(original)))


def test_single_flight_export_lease_refuses_another_runner_before_get(tmp_path):
    driver = setup_driver(tmp_path, lambda request: pytest.fail('unexpected HTTP'))
    with driver._lease():
        with pytest.raises(RuntimeError, match='already running'):
            driver.run(max_windows=1, budget_seconds=10)


def test_pending_lookup_does_not_fold_unrelated_completed_windows(tmp_path, monkeypatch):
    driver = setup_driver(tmp_path, lambda request: response(200, content=b'[]'))
    for offset in range(1, 101):
        start = (_time(START) + timedelta(seconds=offset)).isoformat()
        identity = driver._window_id(start, END)
        driver.ledger.open_window(identity, start_time=start, end_time=END)
        driver.ledger.capture_window(identity, records=[], complete=True, provider_count=0, cap=4)
    monkeypatch.setattr(driver.ledger, '_windows', lambda *args: pytest.fail('unrelated windows materialized'))
    assert driver.run(max_windows=1, budget_seconds=10)['sealed'] == 1


@pytest.mark.parametrize('change', [{'entry_ids': []}, {'multiset_sha256': '0' * 64}],
                         ids=['omitted-entry-membership', 'unrelated-multiset-digest'])
def test_sealed_proof_recomputes_membership_and_multiset_from_observation_metadata(tmp_path, change):
    from fulcra_collect.record_actions import _json, _hash
    driver = setup_driver(tmp_path, lambda request: response(200, content=json.dumps([raw()]).encode()))
    driver.run(max_windows=1, budget_seconds=10)
    proof = driver.window_proof(driver.root_window_id)
    changed = _json({**proof, **change})
    with driver.ledger.storage._db(write=True) as db:
        db.execute('UPDATE migration_window_reads SET document=?,fingerprint=?', (changed, _hash(changed)))
    with pytest.raises(ValueError, match='proof|corrupt'):
        driver.window_proof(driver.root_window_id)
