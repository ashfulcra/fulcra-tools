"""Explicit response account contradictions must remain unusable raw evidence."""
from dataclasses import replace
import hashlib
import json

import httpx
import pytest

from test_migration_export import reader, fetch, response
from test_migration_export_driver import setup_driver
from test_migration_ledger import ACCOUNT, START, END, uid


@pytest.mark.parametrize('account', [uid(99), '', 'synthetic-private-header', ACCOUNT+', '+ACCOUNT])
def test_explicit_other_or_malformed_account_is_retained_but_body_is_not_read(tmp_path, account):
    reads = []
    class Body(httpx.SyncByteStream):
        def __iter__(self):
            reads.append('body')
            yield b'[]'
    export, _ = reader(tmp_path, lambda request: httpx.Response(200,
        headers={'X-Fulcra-UserID': account}, stream=Body()))
    with pytest.raises(RuntimeError, match='account|scope'):
        fetch(export)
    assert reads == []
    assert next((tmp_path/'artifacts').rglob('body.json')).read_bytes() == b''
    receipt = json.loads(next((tmp_path/'artifacts').rglob('receipt.json')).read_bytes())
    assert receipt['transport_eof'] is False and receipt['bytes'] == 0
    assert receipt['response_metadata']['response_account_id'] == (uid(99) if account == uid(99) else 'invalid')
    assert 'synthetic-private-header' not in json.dumps(receipt)


@pytest.mark.parametrize('account', [None, ACCOUNT])
def test_missing_or_matching_header_preserves_current_authority_and_exact_receipt(tmp_path, account):
    headers = {} if account is None else {'X-Fulcra-UserID': account}
    export, _ = reader(tmp_path, lambda request: response(200, content=b'[]', headers=headers))
    result = fetch(export)
    receipt = json.loads(result.receipt_path.read_bytes())
    assert receipt['version'] == 3
    assert receipt['response_metadata']['response_account_id'] == account
    assert result.records == []
    proof = export.verify_result(result, uid(10), start_time=START, end_time=END, order='asc')
    assert proof['count'] == 0 and result.coverage_complete is False


def test_account_fact_is_rechecked_even_with_rebound_receipt_hash(tmp_path):
    export, _ = reader(tmp_path, lambda request: response(200, content=b'[]',
                                                       headers={'X-Fulcra-UserID': ACCOUNT}))
    result = fetch(export)
    receipt = json.loads(result.receipt_path.read_bytes())
    receipt['response_metadata']['response_account_id'] = uid(99)
    result.receipt_path.write_text(json.dumps(receipt))
    rebound = replace(result, _receipt_sha256=hashlib.sha256(result.receipt_path.read_bytes()).hexdigest())
    with pytest.raises(RuntimeError, match='integrity|scope'):
        export.verify_result(rebound, uid(10), start_time=START, end_time=END, order='asc')


def test_wrong_account_empty_pair_cannot_seal_a_window(tmp_path):
    calls = []
    driver = setup_driver(tmp_path, lambda request: calls.append(request) or response(200,
        content=b'[]', headers={'X-Fulcra-UserID':uid(99)}))
    with pytest.raises(RuntimeError, match='account|scope'):
        driver.run(max_windows=1, budget_seconds=10)
    assert len(calls) == 1
    assert driver.ledger.window(driver.root_window_id)['status'] == 'open'
    assert driver.window_proof(driver.root_window_id) is None
    assert driver.ledger.status()['observations'] == 0
    assert driver.ledger.status()['coverage_complete'] is False


@pytest.mark.parametrize('version', [1, 2])
def test_old_pass_versions_cannot_resume_completed_windows_without_account_evidence(tmp_path, version):
    driver = setup_driver(tmp_path, lambda request: response(200, content=b'[]'))
    driver.run(max_windows=1, budget_seconds=10)
    with driver.ledger.storage._db(write=True) as db:
        db.execute('UPDATE migration_export_pass SET version=?', (version,))
    with pytest.raises(ValueError, match='version|scope'):
        setup_driver(tmp_path, lambda request: pytest.fail('old pass must not issue HTTP'))
    with driver.ledger.storage._db() as db:
        assert db.execute('SELECT version FROM migration_export_pass').fetchone()[0] == version
    assert driver.ledger.window(driver.root_window_id)['status'] == 'complete'


@pytest.mark.parametrize('version', [1, 2])
def test_old_raw_pass_marker_is_preserved_and_cannot_be_adopted(tmp_path, version):
    export, _ = reader(tmp_path, lambda request: response(200, content=b'[]'))
    marker = tmp_path/'artifacts'/uid(20)/'scope.json'
    value = json.loads(marker.read_bytes())
    value['version'] = version
    marker.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='scope'):
        reader(tmp_path, lambda request: pytest.fail('old raw scope must not issue HTTP'))
    assert json.loads(marker.read_bytes())['version'] == version


@pytest.mark.parametrize('version', [1, 2])
def test_old_raw_receipt_cannot_be_consumed_even_with_rebound_hash(tmp_path, version):
    export, _ = reader(tmp_path, lambda request: response(200, content=b'[]'))
    result = fetch(export)
    receipt = json.loads(result.receipt_path.read_bytes())
    receipt['version'] = version
    result.receipt_path.write_text(json.dumps(receipt))
    rebound = replace(result, _receipt_sha256=hashlib.sha256(result.receipt_path.read_bytes()).hexdigest())
    with pytest.raises(RuntimeError, match='integrity|scope'):
        export.verify_result(rebound, uid(10), start_time=START, end_time=END, order='asc')


def test_missing_retained_account_fact_is_unknown_even_with_current_version_and_hash(tmp_path):
    export, _ = reader(tmp_path, lambda request: response(200, content=b'[]'))
    result = fetch(export)
    receipt = json.loads(result.receipt_path.read_bytes())
    receipt['response_metadata'].pop('response_account_id')
    result.receipt_path.write_text(json.dumps(receipt))
    rebound = replace(result, _receipt_sha256=hashlib.sha256(result.receipt_path.read_bytes()).hexdigest())
    with pytest.raises(RuntimeError, match='integrity|scope'):
        export.verify_result(rebound, uid(10), start_time=START, end_time=END, order='asc')
