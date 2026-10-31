"""Allowlisted optional dispatch preserves raw exports and frozen proposals."""
import json
import sys

import pytest

from dayone_history_test_helpers import plan, raw, uid
from fulcra_collect.migration_ledger import MigrationLedger
from fulcra_collect.migration_convert import MediaHistoryConverter
from fulcra_collect.record_actions import _hash, _json


def open_ledger(tmp_path, **changes):
    ledger = MigrationLedger(tmp_path / 'journal.sqlite', plan(**changes))
    ledger.open_window(uid(10), start_time=plan().document['start_time'], end_time=plan().document['end_time'])
    return ledger


def test_journal_ledger_dispatch_reopen_proofs_and_unsealed_originals(tmp_path):
    ledger = open_ledger(tmp_path)
    saved = ledger.capture_export(uid(10), ordinal=0, raw=raw())
    converted = ledger.convert_entry(saved.entry_id)
    assert converted.status == 'converted'
    fresh = MigrationLedger(ledger.path, ledger.plan)
    assert fresh.entry(saved.entry_id).payload == converted.payload
    assert fresh.convert_entry(saved.entry_id).payload == converted.payload
    assert fresh.entry(saved.entry_id).raw == raw()
    assert fresh.window(uid(10))['status'] == 'open'
    assert fresh.status()['coverage_complete'] is fresh.status()['historical_complete'] is False
    with fresh.storage._db() as db:
        proof = json.loads(db.execute('SELECT evidence_json FROM migration_entries').fetchone()[0])
    assert proof == {'converter': 'collect.journal.reader.v1', 'raw_hash': _hash(_json(raw())),
        'payload_hash': _hash(_json(converted.payload))}


@pytest.mark.parametrize('category', ['watched', 'listened', 'read', 'activity'])
@pytest.mark.parametrize('version', [1, 2])
def test_all_eight_media_dispatches_keep_exact_existing_class_payload_and_proofs(tmp_path, category, version):
    from fulcra_collect.migration_codecs import resolve_converter
    from fulcra_media.v1 import media_history_spec
    schema = json.loads(media_history_spec(category).create_body()['record_spec']['schema'])
    schema['properties'].update({key: {} for key in ('id', 'start_time', 'end_time', 'sources', 'tags')})
    ledger = open_ledger(tmp_path, source_data_type='DurationAnnotation/' + uid(3),
        converter=f'collect.media.{category}.v{version}', destination_schema=schema,
        source_prefixes=['com.fulcra.media.synthetic.'])
    original = raw(sources=['com.fulcra.media.synthetic.entry', raw()['sources'][0]],
        recorded_at={'start_time': '2020-06-01T00:00:00Z', 'end_time': '2020-06-01T00:00:30Z'})
    if version == 1:
        for key in ('metadata', 'source_name', 'source_id'):
            original.pop(key)
    else:
        original['metadata']['annotation_type'] = 'duration'
    assert type(resolve_converter(ledger.plan)) is MediaHistoryConverter
    expected = MediaHistoryConverter(ledger.plan).convert(original)
    saved = ledger.capture_export(uid(10), ordinal=0, raw=original)
    actual = ledger.convert_entry(saved.entry_id)
    assert actual.status == expected.status == 'converted'
    assert _json(actual.payload) == _json(expected.payload)
    with ledger.storage._db() as db:
        proof = db.execute('SELECT evidence_json FROM migration_entries').fetchone()[0]
    assert proof == _json({'converter': ledger.plan.document['converter'], 'raw_hash': expected.raw_hash,
        'payload_hash': _hash(_json(expected.payload))})
    assert MigrationLedger(ledger.path, ledger.plan).entry(saved.entry_id).payload == expected.payload


@pytest.mark.parametrize('unavailable', [False, True])
def test_unknown_or_unavailable_codec_fails_before_row_work_but_raw_export_and_reopen_survive(tmp_path, monkeypatch, unavailable):
    ledger = open_ledger(tmp_path, **({} if unavailable else {'converter': 'collect.future.v1'}))
    saved = ledger.capture_export(uid(10), ordinal=0, raw=raw())
    if unavailable:
        monkeypatch.setitem(sys.modules, 'fulcra_dayone.history_v1', None)
    fresh = MigrationLedger(ledger.path, ledger.plan)
    assert fresh.entry(saved.entry_id).raw == raw()
    with fresh.storage._db() as db:
        before = dict(db.execute('SELECT * FROM migration_entries').fetchone())
    monkeypatch.setattr(fresh, '_entry', lambda *args, **kwargs: pytest.fail('conversion read a row before resolving codec'))
    with pytest.raises(ValueError, match='unsupported|unavailable'):
        fresh.convert_entry(saved.entry_id)
    with fresh.storage._db() as db:
        assert dict(db.execute('SELECT * FROM migration_entries').fetchone()) == before
    assert MigrationLedger(ledger.path, ledger.plan).entry(saved.entry_id).status == 'exported'


@pytest.mark.parametrize('kind', ['raw', 'payload', 'proof'])
def test_journal_reopen_rejects_tampering_even_with_recomputed_hashes(tmp_path, kind):
    ledger = open_ledger(tmp_path)
    saved = ledger.capture_export(uid(10), ordinal=0, raw=raw())
    ledger.convert_entry(saved.entry_id)
    with ledger.storage._db(write=True) as db:
        row = db.execute('SELECT * FROM migration_entries').fetchone()
        if kind == 'raw':
            changed = json.loads(row['raw_json'])
            changed['note'] = 'tampered original'
            db.execute('UPDATE migration_entries SET raw_json=?,raw_hash=?', (_json(changed), _hash(_json(changed))))
        elif kind == 'payload':
            changed = json.loads(row['payload_json'])
            changed['note'] = 'tampered proposal'
            text = _json(changed)
            proof = json.loads(row['evidence_json'])
            proof['payload_hash'] = _hash(text)
            evidence = _json(proof)
            db.execute('UPDATE migration_entries SET payload_json=?,payload_hash=?,evidence_json=?,evidence_hash=?',
                (text, _hash(text), evidence, _hash(evidence)))
        else:
            proof = json.loads(row['evidence_json'])
            proof['converter'] = 'collect.media.watched.v2'
            db.execute('UPDATE migration_entries SET evidence_json=?,evidence_hash=?', (_json(proof), _hash(_json(proof))))
    fresh = MigrationLedger(ledger.path, ledger.plan)
    with pytest.raises(ValueError, match='corrupt|unreadable'):
        fresh.entry(saved.entry_id)


def test_journal_target_collision_quarantines_all_peers_and_preserves_each_raw(tmp_path):
    ledger = open_ledger(tmp_path)
    originals = [raw(id=uid(n), note=f'Synthetic variant {n}') for n in (20, 21, 22)]
    saved = []
    for index, original in enumerate(originals):
        item = ledger.capture_export(uid(10), ordinal=index, raw=original)
        saved.append(item.entry_id)
        ledger.convert_entry(item.entry_id)
    fresh = MigrationLedger(ledger.path, ledger.plan)
    for identity, original in zip(saved, originals):
        item = fresh.entry(identity)
        assert (item.status, item.reason) == ('quarantined', 'target-collision')
        assert item.raw == item.payload['legacy_record']['raw'] == original
    assert len({fresh.entry(identity).target_record_id for identity in saved}) == 1
    assert fresh.status()['counts'] == {'quarantined': 3}


def test_existing_converted_proposal_cannot_be_validated_without_optional_codec(tmp_path, monkeypatch):
    ledger = open_ledger(tmp_path)
    saved = ledger.capture_export(uid(10), ordinal=0, raw=raw())
    ledger.convert_entry(saved.entry_id)
    monkeypatch.setitem(sys.modules, 'fulcra_dayone.history_v1', None)
    fresh = MigrationLedger(ledger.path, ledger.plan)
    with pytest.raises(ValueError, match='corrupt|unreadable'):
        fresh.entry(saved.entry_id)
