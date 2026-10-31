"""Pending batches survive uncertain uploads, indexing delay and consent changes."""
from copy import deepcopy
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from fulcra_common.v1 import build_event_record

DESTINATION = "Event/11111111-2222-4333-8444-555555555555"


class Session:
    account_id = "synthetic-account"

    def __init__(self):
        self.rows = {}
        self.posts = []
        self.visible = True
        self.lost_receipt = False
        self.cancel_commit = False

    def get_records(self, destination, identifiers):
        return {i: deepcopy(self.rows[destination, i]) for i in identifiers if (destination, i) in self.rows}

    def ingest(self, destination, records, *, before_submit=None):
        if before_submit is not None:
            before_submit()
        self.posts.append((destination, deepcopy(records)))
        if self.visible:
            for r in records:
                self.rows[destination, r["id"]] = deepcopy(r)
        if self.lost_receipt:
            raise RuntimeError("uncertain upload")
        return {"upload_id": "synthetic"}

    def commit(self, callback):
        if self.cancel_commit:
            raise RuntimeError("configuration changed")
        return callback()

    def record_write_scope(self):
        return nullcontext()

    def deliver_records(self, destination, records, *, dispatch):
        from fulcra_common.record_delivery import reconcile_records
        policy = SimpleNamespace(data_type=DESTINATION,
                                 excluded_ids=lambda _destination, _identifiers: set())
        return reconcile_records(self, policy, destination, records, dispatch=dispatch)


@pytest.fixture
def setup():
    from fulcra_collect.v1_outbox import V1RecordOutbox
    kv = {}
    receipts = []
    ctx = SimpleNamespace(config_epoch="synthetic-epoch", kv_get=lambda k, d: deepcopy(kv.get(k, d)),
                          kv_set=lambda k, v: kv.__setitem__(k, deepcopy(v)),
                          annotation=lambda message, **kwargs: receipts.append((message, kwargs)))
    session = Session()
    return V1RecordOutbox(ctx, session, key="synthetic_pending"), session, ctx, kv, receipts


def records():
    return [build_event_record(data_type=DESTINATION, source_id=f"synthetic.{i}",
                               start_time=datetime(2026, 1, 2, tzinfo=timezone.utc),
                               fields={"note": "Synthetic record"}) for i in range(3)]


@pytest.mark.parametrize("bad", [{1: "numeric"}, "\ud800"])
def test_staging_rejects_lossy_nested_json_before_any_durable_intent(setup, bad):
    outbox, session, ctx, kv, receipts = setup
    row = records()[0] | {"custom": bad}
    with pytest.raises(ValueError, match="JSON"):
        outbox.stage({DESTINATION: [row]}, message="Synthetic")
    assert not kv and not session.posts and not receipts


def test_stages_independent_snapshot_and_commits_only_verified_batch(setup):
    outbox, session, ctx, kv, receipts = setup
    batch = records()
    outbox.stage({DESTINATION: batch}, message="Synthetic batch", observed_at="2026-01-02T00:00:00Z")
    batch[0]["note"] = "changed by caller"
    assert len(kv["synthetic_pending"]["groups"][0]["records"]) == 3
    assert not receipts
    assert outbox.drain() == 3
    assert len(session.rows) == 3
    assert all(r["note"] == "Synthetic record" for r in session.rows.values())
    assert receipts == [("Synthetic batch", {"observed_at": "2026-01-02T00:00:00Z"})]
    assert outbox.drain() == 0
    assert len(receipts) == 1


def test_lost_receipt_recovers_by_readback_without_another_post(setup):
    from fulcra_collect.v1_outbox import V1RecordOutbox
    outbox, session, ctx, kv, receipts = setup
    outbox.stage({DESTINATION: records()}, message="Synthetic batch")
    session.lost_receipt = True
    with pytest.raises(RuntimeError, match="uncertain"):
        outbox.drain()
    assert kv["synthetic_pending"]["groups"]
    assert not receipts
    # A fresh object/process reconciles the retained KV payload.
    fresh = V1RecordOutbox(ctx, session, key="synthetic_pending")
    assert fresh.drain() == 3
    assert len(session.posts) == 1
    assert receipts


def test_partial_visibility_retries_only_missing_records(setup):
    outbox, session, ctx, kv, receipts = setup
    batch = records()
    session.rows[DESTINATION, batch[0]["id"]] = batch[0]
    outbox.stage({DESTINATION: batch}, message="Synthetic batch")
    assert outbox.drain() == 3
    assert len(session.posts[0][1]) == 2


def test_indexing_delay_retains_pending_batch_and_never_claims_success(setup):
    outbox, session, ctx, kv, receipts = setup
    outbox.stage({DESTINATION: records()}, message="Synthetic batch")
    session.visible = False
    with pytest.raises(RuntimeError, match="visible"):
        outbox.drain()
    assert kv["synthetic_pending"]["groups"]
    assert not receipts


def test_manual_settings_edit_cannot_reuse_pending_batch_with_an_unchanged_epoch(setup):
    outbox, session, ctx, kv, receipts = setup
    outbox.stage({DESTINATION: records()}, message="Synthetic batch")
    ctx.config = {"selected_source": "changed-without-an-epoch-write"}
    with pytest.raises(RuntimeError, match="configuration"):
        outbox.drain()
    assert not session.posts
    assert not receipts


@pytest.mark.parametrize("change", ["account", "epoch"])
def test_pending_batch_cannot_be_reassigned_to_new_consent(setup, change):
    outbox, session, ctx, kv, receipts = setup
    outbox.stage({DESTINATION: records()}, message="Synthetic batch")
    if change == "account":
        session.account_id = "new-account"
    else:
        ctx.config_epoch = "new-epoch"
    with pytest.raises(RuntimeError, match="pending"):
        outbox.drain()
    assert not session.posts
    assert not receipts


def test_cancelled_progress_commit_preserves_verified_pending_for_recovery(setup):
    outbox, session, ctx, kv, receipts = setup
    batch = records()
    outbox.stage({DESTINATION: batch}, message="Synthetic batch")
    session.rows = {(DESTINATION, r["id"]): r for r in batch}
    session.cancel_commit = True
    with pytest.raises(RuntimeError, match="configuration changed"):
        outbox.drain()
    assert kv["synthetic_pending"]["groups"]
    assert not receipts


def test_cannot_overwrite_pending_batch_or_stage_conflicting_same_id(setup):
    outbox, session, ctx, kv, receipts = setup
    batch = records()
    with pytest.raises(ValueError, match="conflict"):
        outbox.stage({DESTINATION: [batch[0], {**batch[0], "note": "conflict"}]}, message="Synthetic")
    assert not kv
    outbox.stage({DESTINATION: batch}, message="Synthetic")
    with pytest.raises(RuntimeError, match="pending"):
        outbox.stage({DESTINATION: records()}, message="Replacement")
    assert not session.posts


def test_extra_fields_on_same_id_are_a_conflict_not_a_verified_subset(setup):
    outbox, session, ctx, kv, receipts = setup
    record = records()[0]
    with pytest.raises(ValueError, match="conflict"):
        outbox.stage({DESTINATION: [record, {**record, "extra": "would be lost"}]}, message="Synthetic")
    assert not kv


def test_corrupt_pending_is_loud_and_never_clean_empty(setup):
    outbox, session, ctx, kv, receipts = setup
    kv["synthetic_pending"] = {"account_id": session.account_id, "epoch": ctx.config_epoch, "groups": "corrupt"}
    with pytest.raises(ValueError, match="outbox"):
        outbox.drain()
    assert not session.posts
    assert not receipts


def test_real_store_supports_pending_batches_larger_than_small_plugin_kv(collect_home):
    from fulcra_collect import db
    from fulcra_collect.v1_outbox import V1RecordOutbox
    conn = db.open()
    receipts = []
    ctx = SimpleNamespace(config_epoch="synthetic-epoch", plugin_id="synthetic",
        outbox_get=lambda key, default: db.get_record_outbox(conn, "synthetic", key, default),
        outbox_set=lambda key, value: db.set_record_outbox(conn, "synthetic", key, value),
        annotation=lambda message, **kwargs: receipts.append((message, kwargs)))
    session = Session()
    outbox = V1RecordOutbox(ctx, session, key="synthetic_pending")
    batch = [build_event_record(data_type=DESTINATION, source_id=f"synthetic.large.{i}",
                start_time=datetime(2026, 1, 2, tzinfo=timezone.utc), fields={"note": "x" * 1000})
             for i in range(200)]
    outbox.stage({DESTINATION: batch}, message="Synthetic large batch")
    assert db.get_record_outbox(conn, "synthetic", "synthetic_pending")["groups"]
    assert db.get_plugin_kv(conn, "synthetic", "synthetic_pending") is None
    assert outbox.drain() == 200
    assert len(session.rows) == 200
    assert receipts


def test_pending_exclusions_do_not_repost_or_claim_delivery(setup):
    from fulcra_common.record_delivery import RecordDelivery
    outbox, session, ctx, kv, receipts = setup
    batch = records()
    outbox.stage({DESTINATION: batch}, message="Synthetic 3 records")
    def deliver(destination, pending, *, dispatch):
        assert pending == batch
        return RecordDelivery((batch[0]["id"],), tuple(r["id"] for r in batch[1:]))
    session.deliver_records = deliver
    assert outbox.drain() == 1
    assert not session.posts
    assert outbox.excluded_ids == {DESTINATION: {r["id"] for r in batch[1:]}}
    assert "1" in receipts[0][0] and "2" in receipts[0][0]
    assert not kv["synthetic_pending"]["groups"]


def test_pending_unknown_policy_is_retained_without_receipt(setup):
    outbox, session, ctx, kv, receipts = setup
    outbox.stage({DESTINATION: records()}, message="Synthetic batch")
    def unknown(*args, **kwargs):
        raise RuntimeError("synthetic unreadable replay policy")
    session.deliver_records = unknown
    with pytest.raises(RuntimeError, match="policy"):
        outbox.drain()
    assert kv["synthetic_pending"]["groups"]
    assert not session.posts and not receipts


@pytest.mark.parametrize("shape", ["missing", "overlap", "unknown"])
def test_incomplete_or_mismatched_delivery_result_cannot_clear_pending(setup, shape):
    from fulcra_common.record_delivery import RecordDelivery
    outbox, session, ctx, kv, receipts = setup
    batch = records()
    identities = tuple(r["id"] for r in batch)
    result = {"missing": RecordDelivery((), ()),
              "overlap": RecordDelivery(identities, (identities[0],)),
              "unknown": RecordDelivery((*identities, "unrequested"), ())}[shape]
    session.deliver_records = lambda *_, **__: result
    outbox.stage({DESTINATION: batch}, message="Synthetic batch")
    with pytest.raises(ValueError, match="delivery result"):
        outbox.drain()
    assert kv["synthetic_pending"]["groups"]
    assert not receipts

@pytest.mark.parametrize("lost", [False, True])
def test_absent_source_restart_never_reissues_original_pending(setup, lost):
    from fulcra_collect.v1_outbox import V1RecordOutbox
    outbox, session, ctx, kv, receipts = setup
    outbox.stage({DESTINATION: records()}, message="Synthetic batch")
    session.visible = False
    session.lost_receipt = lost
    with pytest.raises(RuntimeError):
        outbox.drain()
    session.lost_receipt = False
    with pytest.raises(RuntimeError):
        V1RecordOutbox(ctx, session, key="synthetic_pending").drain()
    assert len(session.posts) == 1
    assert not receipts


def test_legacy_pending_absent_is_unknown_not_unissued(setup):
    outbox, session, ctx, kv, receipts = setup
    outbox.stage({DESTINATION: records()}, message="Synthetic batch")
    kv["synthetic_pending"].pop("dispatch_version", None)
    kv["synthetic_pending"].pop("dispatch", None)
    original = deepcopy(kv)
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        outbox.drain()
    assert not session.posts and kv == original and not receipts


@pytest.mark.parametrize('damage', ['version', 'null-version', 'missing-map', 'missing-state',
    'null-state', 'phase', 'binding', 'issued-empty', 'evidence', 'payload', 'receipt', 'body'])
def test_dispatch_corruption_never_adopts_or_changes_original_pending(setup, damage):
    outbox, session, ctx, kv, receipts = setup
    outbox.stage({DESTINATION: records()}, message='Synthetic')
    pending = kv['synthetic_pending']
    state = pending['dispatch'][DESTINATION]
    if damage == 'version':
        pending['dispatch_version'] = 2
    elif damage == 'null-version':
        pending['dispatch_version'] = None
    elif damage == 'missing-map':
        pending.pop('dispatch')
    elif damage == 'missing-state':
        pending['dispatch'].clear()
    elif damage == 'null-state':
        pending['dispatch'][DESTINATION] = None
    elif damage == 'phase':
        state['phase'] = 'unknown'
    elif damage == 'binding':
        state['binding'] = 'changed'
    elif damage == 'issued-empty':
        state['phase'] = 'issued'
    elif damage == 'evidence':
        state['positive_ids'] = ['not-an-original-record']
    elif damage == 'payload':
        pending['groups'][0]['records'][0]['note'] = 'different original'
    elif damage == 'receipt':
        pending['message'] = 'different original receipt'
    elif damage == 'body':
        state['body_sha256'] = 'changed'
    original = deepcopy(kv)
    with pytest.raises(RuntimeError, match='UNKNOWN'):
        outbox.drain()
    assert not session.posts and not receipts and kv == original


def test_legacy_exact_current_payload_finishes_without_mutating_original_fields(setup):
    outbox, session, ctx, kv, receipts = setup
    batch = records()
    outbox.stage({DESTINATION: batch}, message='Synthetic')
    pending = kv['synthetic_pending']
    pending.pop('dispatch_version')
    pending.pop('dispatch')
    # The old envelope contains duplicates which _groups validates/deduplicates;
    # raw bytes must remain untouched on an UNKNOWN read.
    pending['groups'][0]['records'].append(deepcopy(batch[0]))
    original = deepcopy(kv)
    with pytest.raises(RuntimeError, match='UNKNOWN'):
        outbox.drain()
    assert kv == original and not session.posts
    session.rows = {(DESTINATION, r['id']): deepcopy(r) for r in batch}
    assert outbox.drain() == 3 and not session.posts


def test_issued_historical_positive_evidence_cannot_complete_allowed_current_absence(setup):
    outbox, session, ctx, kv, receipts = setup
    batch = records()
    outbox.stage({DESTINATION: batch}, message='Synthetic')
    original_set = outbox._set
    def fail_clear(key, value):
        if value == {'groups': []}:
            raise RuntimeError('cancelled final receipt')
        original_set(key, value)
    outbox._set = fail_clear
    with pytest.raises(RuntimeError, match='receipt'):
        outbox.drain()
    assert kv['synthetic_pending']['dispatch'][DESTINATION]['positive_ids']
    session.rows.clear()
    with pytest.raises(RuntimeError, match='UNKNOWN'):
        outbox.drain()
    assert len(session.posts) == 1


@pytest.mark.parametrize('shape', ['missing', 'foreign', 'duplicate', 'overlap', 'unhashable', 'wrong'])
def test_progress_rejects_entire_malformed_group_and_retains_prior_group(setup, shape):
    outbox, session, ctx, kv, receipts = setup
    other = 'Event/22222222-2222-4333-8444-555555555555'
    batch = records()
    first, second = batch[:1], batch[1:]
    real_deliver = session.deliver_records
    ids = tuple(row['id'] for row in second)
    def deliver(destination, pending, *, dispatch):
        if destination == DESTINATION:
            return real_deliver(destination, pending, dispatch=dispatch)
        verified, excluded = {
            'missing': ((), ()), 'foreign': ((*ids, 'foreign'), ()),
            'duplicate': ((*ids, ids[0]), ()), 'overlap': (ids, (ids[0],)),
            'unhashable': (([],), ()), 'wrong': (None, ())}[shape]
        return SimpleNamespace(verified_ids=verified, excluded_ids=excluded)
    session.deliver_records = deliver
    outbox.stage({DESTINATION: first, other: second}, message='Synthetic')
    with pytest.raises(ValueError, match='delivery result'):
        outbox.drain()
    assert outbox.progress.verified == 1 and outbox.progress.excluded == 0
    assert kv['synthetic_pending']['groups'] and not receipts


def test_progress_captures_partition_properties_once(setup):
    outbox, session, ctx, kv, receipts = setup
    batch = records()
    class Unstable:
        verified_reads = excluded_reads = 0
        @property
        def verified_ids(self):
            self.verified_reads += 1
            return tuple(row['id'] for row in batch) if self.verified_reads == 1 else ()
        @property
        def excluded_ids(self):
            self.excluded_reads += 1
            return () if self.excluded_reads == 1 else tuple(row['id'] for row in batch)
    result = Unstable()
    session.deliver_records = lambda *_, **__: result
    outbox.stage({DESTINATION: batch}, message='Synthetic')
    assert outbox.drain() == outbox.progress.verified == 3
    assert outbox.progress.excluded == 0
    assert result.verified_reads == result.excluded_reads == 1


def test_same_ids_in_two_destinations_count_separately_and_empty_resets(setup):
    outbox, session, ctx, kv, receipts = setup
    other = 'Event/22222222-2222-4333-8444-555555555555'
    batch = records()
    assert outbox.progress is None
    outbox.stage({DESTINATION: batch, other: batch}, message='Synthetic')
    assert outbox.drain() == outbox.progress.verified == 6
    saved = outbox.progress
    assert outbox.drain() == outbox.progress.verified == 0
    assert saved.verified == 6


def test_pending_destinations_snapshot_is_immutable_validated_and_fenced(setup):
    outbox, session, ctx, kv, _ = setup
    outbox.stage({DESTINATION: records()}, message='Synthetic')
    assert outbox.pending_destinations() == (DESTINATION,)
    ctx.config_epoch = 'changed'
    with pytest.raises(RuntimeError, match='configuration changed'):
        outbox.pending_destinations()
    ctx.config_epoch = 'synthetic-epoch'
    kv['synthetic_pending']['groups'][0]['records'][0]['id'] = 'bad'
    with pytest.raises(ValueError, match='ID'):
        outbox.pending_destinations()
