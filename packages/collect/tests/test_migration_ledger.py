"""Private raw export retention and honest bounded-window coverage."""
from uuid import UUID

import pytest

from fulcra_collect.migration_plan import MigrationPlan

ACCOUNT = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
START = '2020-01-01T00:00:00Z'
MID = '2020-01-02T00:00:00Z'
END = '2020-01-03T00:00:00Z'


def uid(number):
    return str(UUID(int=number))


def plan(**changes):
    return MigrationPlan.from_document({'version': 1, 'plan_id': uid(1), 'account_id': ACCOUNT,
        'source_api_version': 'v1alpha1', 'source_data_type': 'DurationAnnotation/' + uid(2),
        'destination_api_version': 'v1', 'destination_data_type': 'Event/' + uid(3),
        'converter': 'collect.media.watched.v1', 'identity_version': 1,
        'source_prefixes': ['com.fulcra.netflix.'], 'start_time': START, 'end_time': END,
        'destination_schema': {'properties': {key: {} for key in ('id', 'start_time', 'end_time', 'sources', 'tags')}},
        **changes})


@pytest.fixture
def ledger(tmp_path):
    from fulcra_collect.migration_ledger import MigrationLedger
    return MigrationLedger(tmp_path / 'migration.sqlite', plan())


def raw(**changes):
    return {'id': uid(9), 'recorded_at': {'start_time': START, 'end_time': MID},
            'sources': ['com.fulcra.netflix.synthetic'], 'tags': [], 'note': 'Synthetic original 🐈', **changes}


def test_private_plan_binding_and_raw_export_survive_restart_without_mutable_snapshots(ledger):
    ledger.open_window(uid(10), start_time=START, end_time=END)
    saved = ledger.capture_export(uid(10), ordinal=0, raw=raw())
    assert saved.status == 'exported' and saved.raw == raw()
    saved.raw['note'] = 'Changed returned value'
    fresh = type(ledger)(ledger.path, plan())
    assert fresh.entry(saved.entry_id).raw == raw()
    assert ledger.path.stat().st_mode & 0o777 == 0o600
    assert fresh.status()['coverage_complete'] is False
    assert fresh.status()['counts'] == {'exported': 1}


@pytest.mark.parametrize('change', [{'account_id': 'aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff'},
    {'destination_data_type': 'Event/' + uid(4)}, {'end_time': MID}, {'converter': 'collect.media.read.v1'}])
def test_existing_ledger_never_adopts_changed_account_mapping_range_or_codec(ledger, change):
    with pytest.raises(ValueError, match='plan|scope'):
        type(ledger)(ledger.path, plan(**change))
    assert type(ledger)(ledger.path, plan()).status()['counts'] == {}


def test_overlapping_windows_retain_observations_but_deduplicate_identical_raw_versions(ledger):
    ledger.open_window(uid(10), start_time=START, end_time=END)
    ledger.open_window(uid(11), start_time=START, end_time=MID)
    first = ledger.capture_export(uid(10), ordinal=0, raw=raw())
    assert ledger.capture_export(uid(11), ordinal=0, raw=raw()).entry_id == first.entry_id
    assert ledger.capture_export(uid(10), ordinal=0, raw=raw()).entry_id == first.entry_id
    assert ledger.status()['observations'] == 2 and ledger.status()['counts'] == {'exported': 1}
    with pytest.raises(ValueError, match='changed|observation'):
        ledger.capture_export(uid(10), ordinal=0, raw=raw(note='Changed export at same position'))
    assert ledger.entry(first.entry_id).raw == raw()


def test_changed_versions_of_an_original_are_retained_separately_and_reported(ledger):
    ledger.open_window(uid(10), start_time=START, end_time=END)
    first = ledger.capture_export(uid(10), ordinal=0, raw=raw())
    second = ledger.capture_export(uid(10), ordinal=1, raw=raw(note='Changed original'))
    assert first.entry_id != second.entry_id
    assert ledger.entry(first.entry_id).raw['note'] == raw()['note']
    assert ledger.status()['original_variants'] == 1


@pytest.mark.parametrize('complete,count,cap', [(False, 0, 4000), (True, 4000, 4000),
    (True, 1, 4000), (True, 0, True)])
def test_unknown_saturated_or_unreconciled_window_cannot_seal_as_complete(ledger, complete, count, cap):
    ledger.open_window(uid(10), start_time=START, end_time=END)
    with pytest.raises(ValueError, match='complete|count|bound|saturat'):
        ledger.seal_window(uid(10), complete=complete, provider_count=count, cap=cap)
    assert ledger.status()['coverage_complete'] is False


def test_true_empty_windows_need_full_range_coverage_and_no_unread_gap(ledger):
    ledger.open_window(uid(10), start_time=START, end_time=MID)
    ledger.seal_window(uid(10), complete=True, provider_count=0, cap=4000)
    assert ledger.status()['coverage_complete'] is False
    ledger.open_window(uid(11), start_time=MID, end_time=END)
    ledger.seal_window(uid(11), complete=True, provider_count=0, cap=4000)
    assert ledger.status()['coverage_complete'] is True
    assert ledger.status()['historical_complete'] is False  # no cutoff/delta receipts
    with pytest.raises(ValueError, match='sealed|complete'):
        ledger.capture_export(uid(10), ordinal=0, raw=raw())


def test_saturated_parent_is_replaced_by_explicit_covering_children(ledger):
    ledger.open_window(uid(10), start_time=START, end_time=END)
    ledger.open_window(uid(11), start_time=START, end_time=MID)
    ledger.open_window(uid(12), start_time=MID, end_time=END)
    ledger.mark_split(uid(10), children=[uid(11), uid(12)])
    ledger.seal_window(uid(11), complete=True, provider_count=0, cap=4000)
    assert ledger.status()['coverage_complete'] is False
    ledger.seal_window(uid(12), complete=True, provider_count=0, cap=4000)
    assert ledger.status()['coverage_complete'] is True


def test_missing_original_record_identity_is_preserved_as_quarantine(ledger):
    ledger.open_window(uid(10), start_time=START, end_time=END)
    original = raw(id=None)
    saved = ledger.capture_export(uid(10), ordinal=0, raw=original)
    assert saved.status == 'quarantined' and saved.reason == 'missing-record-id'
    assert ledger.entry(saved.entry_id).raw == original
    assert ledger.status()['counts'] == {'quarantined': 1}


def test_unknown_entry_classification_is_loud_even_when_a_filter_would_hide_it(ledger):
    ledger.open_window(uid(10), start_time=START, end_time=END)
    saved = ledger.capture_export(uid(10), ordinal=0, raw=raw())
    with ledger.storage._db(write=True) as db:
        db.execute("UPDATE migration_entries SET status='future-unknown' WHERE entry_id=?", (saved.entry_id,))
    with pytest.raises(ValueError, match='unknown|corrupt|classification'):
        ledger.status()


def test_capturing_one_row_does_not_rescan_every_export_window(ledger, monkeypatch):
    import sqlite3
    for number in range(100, 200):
        ledger.open_window(uid(number), start_time=START, end_time=END)
    original = sqlite3.connect
    statements = []
    def traced(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection
    monkeypatch.setattr(sqlite3, 'connect', traced)
    ledger.capture_export(uid(100), ordinal=0, raw=raw())
    selects = [sql for sql in statements if sql.upper().startswith('SELECT')]
    assert len(selects) <= 12, 'single-row capture scales with unrelated export windows'


def test_sealed_window_binds_ordered_observations_not_just_their_count(ledger):
    ledger.open_window(uid(10), start_time=START, end_time=END)
    ledger.capture_export(uid(10), ordinal=0, raw=raw())
    second = ledger.capture_export(uid(10), ordinal=1, raw=raw(id=uid(8)))
    ledger.seal_window(uid(10), complete=True, provider_count=2, cap=4000)
    assert ledger.status()['coverage_complete'] is True
    with ledger.storage._db(write=True) as db:
        db.execute('UPDATE migration_observations SET entry_id=? WHERE window_id=? AND ordinal=0',
                   (second.entry_id, uid(10)))
    with pytest.raises(ValueError, match='corrupt|coverage|observation'):
        ledger.status()


def test_row_ordinal_gaps_cannot_seal_even_when_total_count_matches(ledger):
    ledger.open_window(uid(10), start_time=START, end_time=END)
    ledger.capture_export(uid(10), ordinal=1, raw=raw())
    with pytest.raises(ValueError, match='count|contiguous'):
        ledger.seal_window(uid(10), complete=True, provider_count=1, cap=4000)


def test_coverage_receipt_never_claims_a_fresh_raw_content_audit(ledger):
    ledger.open_window(uid(10), start_time=START, end_time=END)
    saved = ledger.capture_export(uid(10), ordinal=0, raw=raw())
    ledger.seal_window(uid(10), complete=True, provider_count=1, cap=4000)
    with ledger.storage._db(write=True) as db:
        db.execute("UPDATE migration_entries SET raw_json='{}' WHERE entry_id=?", (saved.entry_id,))
    status = ledger.status()
    assert status['coverage_complete'] is True  # window receipt, not fresh raw verification
    assert status['raw_integrity'] == 'not-scanned'
    assert status['historical_complete'] is False
    with pytest.raises(ValueError, match='corrupt|hash|identity'):
        ledger.entry(saved.entry_id)


@pytest.fixture
def convertible_ledger(tmp_path):
    from fulcra_collect.migration_ledger import MigrationLedger
    document = plan().document
    document['destination_schema']['properties'].update({'category': {'enum': ['watched']},
        'duration_seconds': {'type': 'number'}, 'note': {'type': ['string', 'null']}})
    ledger = MigrationLedger(tmp_path / 'convert.sqlite', MigrationPlan(document))
    ledger.open_window(uid(10), start_time=START, end_time=END)
    return ledger


def test_converted_payload_scope_and_proof_survive_restart(convertible_ledger):
    ledger = convertible_ledger
    saved = ledger.capture_export(uid(10), ordinal=0, raw=raw())
    converted = ledger.convert_entry(saved.entry_id)
    assert converted.status == 'converted' and converted.reason is None
    assert converted.source_id == raw()['sources'][0]
    assert converted.payload['note'] == raw()['note']
    assert converted.target_record_id == converted.payload['id']
    payload = converted.payload
    payload['note'] = 'mutated returned copy'
    fresh = type(ledger)(ledger.path, ledger.plan)
    assert fresh.convert_entry(saved.entry_id).payload['note'] == raw()['note']
    assert fresh.status()['counts'] == {'converted': 1}
    assert fresh.status()['historical_complete'] is False


def test_quarantine_retains_raw_and_never_invents_a_target(convertible_ledger):
    ledger = convertible_ledger
    original = raw(unknown_enrichment='synthetic must survive')
    saved = ledger.capture_export(uid(10), ordinal=0, raw=original)
    converted = ledger.convert_entry(saved.entry_id)
    assert converted.status == 'quarantined' and converted.reason == 'undeclared-fields'
    assert converted.payload is None and converted.target_record_id is None
    assert converted.raw == original
    assert type(ledger)(ledger.path, ledger.plan).convert_entry(saved.entry_id).reason == converted.reason


def test_conflicting_primary_identity_quarantines_every_prepared_target_variant(convertible_ledger):
    ledger = convertible_ledger
    first = ledger.capture_export(uid(10), ordinal=0, raw=raw())
    second = ledger.capture_export(uid(10), ordinal=1, raw=raw(id=uid(8), note='Conflicting old content'))
    prepared = ledger.convert_entry(first.entry_id)
    assert prepared.status == 'converted'
    conflict = ledger.convert_entry(second.entry_id)
    assert conflict.status == 'quarantined' and conflict.reason == 'target-collision'
    previous = ledger.entry(first.entry_id)
    assert previous.status == 'quarantined' and previous.reason == 'target-collision'
    assert previous.payload == prepared.payload  # preserve both frozen proposals for review
    assert previous.target_record_id == conflict.target_record_id
    third = ledger.capture_export(uid(10), ordinal=2, raw=raw(id=uid(7)))
    assert ledger.convert_entry(third.entry_id).reason == 'target-collision'
    assert ledger.status()['counts'] == {'quarantined': 3}


def test_identical_target_payloads_from_duplicate_originals_converge(convertible_ledger):
    ledger = convertible_ledger
    first = ledger.capture_export(uid(10), ordinal=0, raw=raw())
    second = ledger.capture_export(uid(10), ordinal=1, raw=raw(id=uid(8)))
    assert ledger.convert_entry(first.entry_id).payload == ledger.convert_entry(second.entry_id).payload
    assert ledger.status()['counts'] == {'converted': 2}


def test_recomputed_payload_hash_does_not_hide_converter_mismatch(convertible_ledger):
    import json
    from fulcra_collect.record_actions import _hash, _json
    ledger = convertible_ledger
    saved = ledger.capture_export(uid(10), ordinal=0, raw=raw())
    converted = ledger.convert_entry(saved.entry_id)
    changed = converted.payload
    changed['note'] = 'Corrupt proposal with internally consistent hashes'
    encoded = _json(changed)
    with ledger.storage._db(write=True) as db:
        row = db.execute('SELECT evidence_json FROM migration_entries WHERE entry_id=?', (saved.entry_id,)).fetchone()
        evidence = json.loads(row[0])
        evidence['payload_hash'] = _hash(encoded)
        proof = _json(evidence)
        db.execute('UPDATE migration_entries SET payload_json=?,payload_hash=?,evidence_json=?,evidence_hash=? WHERE entry_id=?',
            (encoded, _hash(encoded), proof, _hash(proof), saved.entry_id))
    with pytest.raises(ValueError, match='corrupt|converter|payload'):
        ledger.entry(saved.entry_id)


def test_export_only_schema_one_upgrades_without_replacing_raw_state(convertible_ledger):
    ledger = convertible_ledger
    saved = ledger.capture_export(uid(10), ordinal=0, raw=raw())
    with ledger.storage._db(write=True) as db:
        db.execute('UPDATE migration_meta SET version=1')
    fresh = type(ledger)(ledger.path, ledger.plan)
    assert fresh.entry(saved.entry_id).raw == raw()
    assert fresh.convert_entry(saved.entry_id).status == 'converted'
    with fresh.storage._db() as db:
        assert db.execute('SELECT version FROM migration_meta').fetchone()[0] == 2


def test_missing_identity_cannot_be_relabelled_as_converted_without_a_proposal(ledger):
    ledger.open_window(uid(10), start_time=START, end_time=END)
    saved = ledger.capture_export(uid(10), ordinal=0, raw=raw(id=None))
    with ledger.storage._db(write=True) as db:
        db.execute("UPDATE migration_entries SET status='converted' WHERE entry_id=?", (saved.entry_id,))
    with pytest.raises(ValueError, match='corrupt|classification'):
        ledger.entry(saved.entry_id)
    with pytest.raises(ValueError, match='corrupt|classification'):
        ledger.status()


def test_collision_reclassification_is_atomic_and_resumable(convertible_ledger, monkeypatch):
    import sqlite3
    ledger = convertible_ledger
    first = ledger.capture_export(uid(10), ordinal=0, raw=raw())
    second = ledger.capture_export(uid(10), ordinal=1, raw=raw(id=uid(8), note='Conflicting content'))
    ledger.convert_entry(first.entry_id)
    connect = sqlite3.connect
    class Interrupted(sqlite3.Connection):
        def execute(self, sql, *args):
            result = super().execute(sql, *args)
            if "SET status='quarantined',reason='target-collision'" in sql:
                raise RuntimeError('synthetic interrupted conversion transaction')
            return result
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, 'connect', lambda *args, **kwargs: connect(*args, **kwargs, factory=Interrupted))
        with pytest.raises(RuntimeError, match='interrupted conversion'):
            ledger.convert_entry(second.entry_id)
    fresh = type(ledger)(ledger.path, ledger.plan)
    assert fresh.entry(first.entry_id).status == 'converted'
    assert fresh.entry(second.entry_id).status == 'exported'
    assert fresh.convert_entry(second.entry_id).reason == 'target-collision'
    assert fresh.entry(first.entry_id).reason == 'target-collision'


def test_concurrent_conflicting_proposals_leave_no_eligible_target(convertible_ledger):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    ledger = convertible_ledger
    entries = [ledger.capture_export(uid(10), ordinal=index, raw=raw(id=uid(100 + index), note=f'Synthetic {index}'))
               for index in range(2)]
    barrier = Barrier(2)
    def run(entry):
        fresh = type(ledger)(ledger.path, ledger.plan)
        barrier.wait(timeout=5)
        return fresh.convert_entry(entry.entry_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, entries))
    assert all(ledger.entry(entry.entry_id).reason == 'target-collision' for entry in entries)
    assert ledger.status()['counts'] == {'quarantined': 2}


def test_open_ledger_rechecks_schema_before_an_offline_conversion(convertible_ledger):
    ledger = convertible_ledger
    saved = ledger.capture_export(uid(10), ordinal=0, raw=raw())
    with ledger.storage._db(write=True) as db:
        db.execute('UPDATE migration_meta SET version=999')
    with pytest.raises(ValueError, match='schema'):
        ledger.convert_entry(saved.entry_id)
    with ledger.storage._db() as db:
        assert db.execute('SELECT status FROM migration_entries WHERE entry_id=?', (saved.entry_id,)).fetchone()[0] == 'exported'


def test_converted_read_cannot_ignore_a_conflicting_quarantined_peer(convertible_ledger):
    import json
    from fulcra_collect.record_actions import _hash, _json
    ledger = convertible_ledger
    first = ledger.capture_export(uid(10), ordinal=0, raw=raw())
    second = ledger.capture_export(uid(10), ordinal=1, raw=raw(id=uid(8), note='Conflicting content'))
    ledger.convert_entry(first.entry_id)
    ledger.convert_entry(second.entry_id)
    with ledger.storage._db(write=True) as db:
        proof = json.loads(db.execute('SELECT evidence_json FROM migration_entries WHERE entry_id=?', (first.entry_id,)).fetchone()[0])
        proof.pop('collision')
        evidence = _json(proof)
        db.execute("UPDATE migration_entries SET status='converted',reason=NULL,evidence_json=?,evidence_hash=? WHERE entry_id=?",
            (evidence, _hash(evidence), first.entry_id))
    with pytest.raises(ValueError, match='corrupt|collision|conflict'):
        ledger.entry(first.entry_id)


def test_identical_target_fast_path_verifies_peer_proposals(convertible_ledger):
    import json
    from fulcra_collect.record_actions import _hash, _json
    from fulcra_collect.migration_convert import MediaHistoryConverter
    ledger = convertible_ledger
    first = ledger.capture_export(uid(10), ordinal=0, raw=raw())
    second = ledger.capture_export(uid(10), ordinal=1, raw=raw(id=uid(8), note='Conflicting content'))
    ledger.convert_entry(first.entry_id)
    changed = _json(MediaHistoryConverter(ledger.plan).convert(second.raw).payload)
    with ledger.storage._db(write=True) as db:
        proof = json.loads(db.execute('SELECT evidence_json FROM migration_entries WHERE entry_id=?', (first.entry_id,)).fetchone()[0])
        proof['payload_hash'] = _hash(changed)
        evidence = _json(proof)
        db.execute('UPDATE migration_entries SET payload_json=?,payload_hash=?,evidence_json=?,evidence_hash=? WHERE entry_id=?',
            (changed, _hash(changed), evidence, _hash(evidence), first.entry_id))
    with pytest.raises(ValueError, match='corrupt|converter|payload'):
        ledger.convert_entry(second.entry_id)
    assert ledger.entry(second.entry_id).status == 'exported'  # transaction rolled back


def test_large_duplicate_target_group_fails_before_unbounded_peer_materialization(convertible_ledger):
    ledger = convertible_ledger
    prepared = []
    for index in range(6):
        saved = ledger.capture_export(uid(10), ordinal=index,
            raw=raw(id=uid(100 + index), note='x' * 320000))
        prepared.append(ledger.convert_entry(saved.entry_id))
    extra = ledger.capture_export(uid(10), ordinal=6, raw=raw(id=uid(106), note='x' * 320000))
    with pytest.raises(ValueError, match='group.*bound'):
        ledger.convert_entry(extra.entry_id)
    assert ledger.entry(extra.entry_id).status == 'exported'
    assert ledger.entry(prepared[0].entry_id).status == 'converted'
    assert ledger.status()['counts'] == {'converted': 6, 'exported': 1}


def test_reader_codec_provenance_is_frozen_and_reverified_after_restart(tmp_path, convertible_ledger):
    from fulcra_media.v1 import media_history_spec
    document = convertible_ledger.plan.document
    document['converter'] = 'collect.media.watched.v2'
    document['destination_schema']['properties']['legacy_record'] = media_history_spec('watched').fields['legacy_record']
    ledger = type(convertible_ledger)(tmp_path / 'reader.sqlite', MigrationPlan(document))
    ledger.open_window(uid(10), start_time=START, end_time=END)
    binding = 'com.fulcradynamics.annotation.' + uid(2)
    original = raw(source_id=binding, source_name='Synthetic original source', metadata={
        'id': uid(2), 'fulcra_userid': ACCOUNT, 'fulcra_source_id': binding,
        'annotation_type': 'duration', 'name': 'Synthetic renamed definition',
        'spec': {'opaque': [0, False, None]}, 'converted_from_mesh': False})
    saved = ledger.capture_export(uid(10), ordinal=0, raw=original)
    converted = ledger.convert_entry(saved.entry_id)
    assert converted.status == 'converted'
    fresh = type(ledger)(ledger.path, ledger.plan)
    restored = fresh.entry(saved.entry_id)
    assert restored.payload == converted.payload
    assert restored.payload['legacy_record']['metadata'] == original['metadata']
    assert restored.raw == original


def test_complete_raw_snapshot_is_atomic_and_idempotent(ledger):
    ledger.open_window(uid(10), start_time=START, end_time=END)
    records = [raw(), raw(id=uid(8), note='Synthetic second original')]
    result = ledger.capture_window(uid(10), records=records, complete=True, provider_count=2, cap=4000)
    assert result['observations'] == 2 and len(result['entry_ids']) == 2
    assert ledger.capture_window(uid(10), records=records, complete=True, provider_count=2, cap=4000) == result
    assert ledger.status()['coverage_complete'] is True
    assert ledger.status()['counts'] == {'exported': 2}
    with pytest.raises(ValueError, match='changed|evidence'):
        ledger.capture_window(uid(10), records=records, complete=True, provider_count=2, cap=3999)
    with pytest.raises(ValueError, match='changed|observation'):
        ledger.capture_window(uid(10), records=[raw(note='Changed read'), records[1]], complete=True, provider_count=2, cap=4000)


@pytest.mark.parametrize('records,complete,count,cap', [
    ([raw()], False, 1, 4000), ([raw()], True, 2, 4000), ([raw()], True, 1, 1),
    ([raw()], True, True, 4000), ([raw(), raw(note=float('nan'))], True, 2, 4000),
])
def test_unproven_or_unretainable_snapshot_cannot_partially_advance(ledger, records, complete, count, cap):
    ledger.open_window(uid(10), start_time=START, end_time=END)
    with pytest.raises(ValueError):
        ledger.capture_window(uid(10), records=records, complete=complete, provider_count=count, cap=cap)
    assert ledger.status()['counts'] == {} and ledger.status()['observations'] == 0
    assert ledger.status()['coverage_complete'] is False


def test_interrupted_snapshot_rolls_back_every_raw_row_and_seal(ledger, monkeypatch):
    import sqlite3
    ledger.open_window(uid(10), start_time=START, end_time=END)
    connect = sqlite3.connect
    class Interrupted(sqlite3.Connection):
        def execute(self, sql, args=()):
            result = super().execute(sql, args)
            if sql.startswith('INSERT INTO migration_observations') and args[1] == 1:
                raise RuntimeError('synthetic interrupted raw snapshot')
            return result
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, 'connect', lambda *args, **kwargs: connect(*args, **kwargs, factory=Interrupted))
        with pytest.raises(RuntimeError, match='interrupted raw snapshot'):
            ledger.capture_window(uid(10), records=[raw(), raw(id=uid(8))], complete=True, provider_count=2, cap=4000)
    fresh = type(ledger)(ledger.path, ledger.plan)
    assert fresh.status()['counts'] == {} and fresh.status()['observations'] == 0
    assert fresh.status()['coverage_complete'] is False
    fresh.capture_window(uid(10), records=[raw(), raw(id=uid(8))], complete=True, provider_count=2, cap=4000)
    assert fresh.status()['coverage_complete'] is True


def test_split_does_not_rescan_unrelated_export_windows(ledger, monkeypatch):
    import sqlite3
    for number in range(100, 200):
        ledger.open_window(uid(number), start_time=START, end_time=END)
    ledger.open_window(uid(10), start_time=START, end_time=END)
    ledger.open_window(uid(11), start_time=START, end_time=MID)
    ledger.open_window(uid(12), start_time=MID, end_time=END)
    connect = sqlite3.connect
    statements = []
    visited = []
    original = ledger._window_row
    def visit(db, row):
        visited.append(row['window_id'])
        return original(db, row)
    monkeypatch.setattr(ledger, '_window_row', visit)
    def traced(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection
    monkeypatch.setattr(sqlite3, 'connect', traced)
    ledger.mark_split(uid(10), children=[uid(11), uid(12)])
    assert len([sql for sql in statements if sql.upper().startswith('SELECT')]) <= 12
    assert len(visited) <= 3, 'one split materializes unrelated window metadata'


def test_atomic_snapshot_handles_proven_empty_and_retained_quarantine(ledger):
    ledger.open_window(uid(10), start_time=START, end_time=MID)
    assert ledger.capture_window(uid(10), records=[], complete=True, provider_count=0, cap=4000)['entry_ids'] == []
    ledger.open_window(uid(11), start_time=MID, end_time=END)
    result = ledger.capture_window(uid(11), records=[raw(id=None)], complete=True, provider_count=1, cap=4000)
    assert ledger.entry(result['entry_ids'][0]).status == 'quarantined'
    assert ledger.status()['coverage_complete'] is True and ledger.status()['historical_complete'] is False


def test_snapshot_byte_bound_and_non_sequence_refusal_leave_the_window_open(ledger):
    ledger.open_window(uid(10), start_time=START, end_time=END)
    records = [raw(id=uid(number), note='x' * 900000) for number in range(100, 119)]
    with pytest.raises(ValueError, match='byte bound'):
        ledger.capture_window(uid(10), records=records, complete=True, provider_count=len(records), cap=4000)
    def unbounded():
        raise AssertionError('must not consume an unbounded iterator')
        yield raw()
    with pytest.raises(ValueError, match='snapshot'):
        ledger.capture_window(uid(10), records=unbounded(), complete=True, provider_count=1, cap=4000)
    assert ledger.status()['observations'] == 0 and ledger.status()['coverage_complete'] is False


def test_dangling_observation_cannot_turn_capture_into_a_silent_missing_entry(ledger):
    import sqlite3
    ledger.open_window(uid(10), start_time=START, end_time=END)
    saved = ledger.capture_export(uid(10), ordinal=0, raw=raw())
    # Simulate pre-existing corruption; an ordinary journal transaction has FK checks.
    with sqlite3.connect(ledger.path) as db:
        db.execute('DELETE FROM migration_entries WHERE entry_id=?', (saved.entry_id,))
    with pytest.raises(ValueError, match='unavailable|corrupt'):
        ledger.capture_export(uid(10), ordinal=0, raw=raw())


def test_atomic_split_and_direct_window_metadata_resume_without_shared_mutability(ledger):
    assert ledger.window(uid(10)) is None
    ledger.open_window(uid(10), start_time=START, end_time=END)
    children = [{'window_id': uid(11), 'start_time': START, 'end_time': MID},
                {'window_id': uid(12), 'start_time': MID, 'end_time': END}]
    assert ledger.split_window(uid(10), children=children)['children'] == [uid(11), uid(12)]
    observed = ledger.window(uid(10))
    assert observed['status'] == 'split' and observed['raw_integrity'] == 'not-scanned'
    observed['evidence']['children'].append(uid(13))
    fresh = type(ledger)(ledger.path, ledger.plan)
    assert fresh.window(uid(10))['evidence']['children'] == [uid(11), uid(12)]
    assert fresh.split_window(uid(10), children=children)['children'] == [uid(11), uid(12)]
    assert fresh.window(uid(11))['status'] == 'open'
    changed = [{'window_id': uid(13), 'start_time': START, 'end_time': MID}, children[1]]
    with pytest.raises(ValueError, match='changed'):
        fresh.split_window(uid(10), children=changed)
    assert fresh.window(uid(13)) is None  # conflicting split cannot strand a new child


def test_atomic_split_rolls_back_child_creation_and_parent_status(ledger, monkeypatch):
    import sqlite3
    ledger.open_window(uid(10), start_time=START, end_time=END)
    connect = sqlite3.connect
    class Interrupted(sqlite3.Connection):
        def execute(self, sql, args=()):
            result = super().execute(sql, args)
            if sql.startswith('INSERT INTO migration_windows') and args[0] == uid(12):
                raise RuntimeError('synthetic interrupted split')
            return result
    children = [{'window_id': uid(11), 'start_time': START, 'end_time': MID},
                {'window_id': uid(12), 'start_time': MID, 'end_time': END}]
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, 'connect', lambda *args, **kwargs: connect(*args, **kwargs, factory=Interrupted))
        with pytest.raises(RuntimeError, match='interrupted split'):
            ledger.split_window(uid(10), children=children)
    assert ledger.window(uid(10))['status'] == 'open'
    assert ledger.window(uid(11)) is None and ledger.window(uid(12)) is None
    ledger.split_window(uid(10), children=children)
    assert ledger.window(uid(10))['status'] == 'split'


def test_fractional_interior_ordinal_cannot_satisfy_contiguous_window_proof(ledger):
    ledger.open_window(uid(10), start_time=START, end_time=END)
    for ordinal in range(3):
        ledger.capture_export(uid(10), ordinal=ordinal, raw=raw(id=uid(100 + ordinal)))
    with ledger.storage._db(write=True) as db:
        db.execute('UPDATE migration_observations SET ordinal=0.5 WHERE window_id=? AND ordinal=1', (uid(10),))
    with pytest.raises(ValueError, match='contiguous|integer|corrupt'):
        ledger.seal_window(uid(10), complete=True, provider_count=3, cap=4000)
    assert ledger.window(uid(10))['status'] == 'open'
    assert ledger.status()['coverage_complete'] is False


def test_resume_refuses_fractional_ordinals_even_with_recomputed_seal_hash(ledger):
    import hashlib
    from fulcra_collect.record_actions import _hash, _json
    ledger.open_window(uid(10), start_time=START, end_time=END)
    ledger.capture_window(uid(10), records=[raw(id=uid(100 + index)) for index in range(3)],
        complete=True, provider_count=3, cap=4000)
    with ledger.storage._db(write=True) as db:
        db.execute('UPDATE migration_observations SET ordinal=0.5 WHERE window_id=? AND ordinal=1', (uid(10),))
        digest = hashlib.sha256()
        for row in db.execute('SELECT observations.ordinal,entries.entry_id,entries.raw_hash FROM migration_observations observations '
                'JOIN migration_entries entries ON entries.entry_id=observations.entry_id WHERE window_id=? ORDER BY ordinal', (uid(10),)):
            digest.update((_json(dict(row)) + '\n').encode())
        evidence = _json({'complete': True, 'provider_count': 3, 'cap': 4000, 'observation_hash': digest.hexdigest()})
        db.execute('UPDATE migration_windows SET evidence=?,evidence_hash=? WHERE window_id=?',
            (evidence, _hash(evidence), uid(10)))
    with pytest.raises(ValueError, match='corrupt|contiguous|integer'):
        ledger.window(uid(10))
    with pytest.raises(ValueError, match='corrupt|contiguous|integer'):
        ledger.status()
