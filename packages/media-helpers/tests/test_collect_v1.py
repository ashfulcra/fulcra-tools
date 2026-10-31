"""Guarded media runtime: exact progress, cross-source twins and true replays."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

from fulcra_media.importers.base import NormalizedEvent


def event(*, importer="synthetic", primary="one", extras=("com.fulcra.content.watched.v1.synthetic",), **overrides):
    start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    return NormalizedEvent(**{**dict(importer=importer, service=importer, category="watched", note="Synthetic film",
        title="Synthetic film", start_time=start, end_time=start + timedelta(minutes=1),
        deterministic_id=f"com.fulcra.media.{importer}.v1.{primary}", timestamp_confidence="high",
        external_ids={"example_id": "one"}, extra_source_ids=extras), **overrides})


class Session:
    account_id = "synthetic-account"

    def __init__(self):
        self.rows = {}
        self.posts = []
        self.resolved = []
        self.tags = []
        self.visible = True
        self.fail_upload = False
        self.fail_read = False
        self.cancel = False
        self.closed = False
        self._excluded = set()
        self.after_post = lambda: None

    def __enter__(self):
        self.closed = False
        return self

    def __exit__(self, *_):
        self.closed = True

    def resolve_destination(self, spec, *, pinned_id=None, allow_create=False):
        from fulcra_media.v1 import media_history_spec
        self.resolved.append((spec, pinned_id, allow_create))
        return SimpleNamespace(data_type=pinned_id or "Event/" + str(uuid5(NAMESPACE_URL, spec.key)),
                               schema={"properties": {**spec.fields, "personal_label": {"type": "string"},
                                   "legacy_record": media_history_spec('watched').fields['legacy_record']}})

    def resolve_tag(self, name, *, allow_create=True):
        self.tags.append((name, allow_create))
        return str(uuid5(NAMESPACE_URL, "synthetic-tag:" + name))

    def get_records(self, destination, identifiers):
        if self.fail_read:
            raise RuntimeError("readback unavailable")
        return {i: deepcopy(self.rows[destination, i]) for i in identifiers if (destination, i) in self.rows}

    def get_source_records(self, destination, sources, *, start, end):
        if self.fail_read:
            raise RuntimeError("readback unavailable")
        return [deepcopy(r) for (d, _), r in self.rows.items() if d == destination and set(r["sources"]) & set(sources)]

    def ingest(self, destination, records, *, before_submit=None):
        if before_submit is not None:
            before_submit()
        self.posts.append((destination, deepcopy(records)))
        if self.visible:
            for r in records:
                self.rows[destination, r["id"]] = deepcopy(r)
        if self.fail_upload:
            raise RuntimeError("uncertain upload")
        self.after_post()
        return {"upload_id": "synthetic"}

    def prepare_record_policy(self, *, check_only=False):
        return "Event/aaaaaaaa-2222-4333-8444-555555555555"

    def record_write_scope(self):
        from fulcra_collect.config_leases import record_mutation_scope
        return record_mutation_scope(self.account_id)

    def _policy(self):
        return SimpleNamespace(data_type=self.prepare_record_policy(),
            excluded_ids=lambda _destination, identifiers: self._excluded.intersection(identifiers))

    def excluded_ids(self, destination, identifiers, *, check_only=False):
        from fulcra_common.record_delivery import enforce_exclusions
        if check_only:
            return self._excluded.intersection(identifiers)
        return enforce_exclusions(self, self._policy(), destination, identifiers)

    def delete_records(self, destination, identifiers, *, action_id):
        for identifier in identifiers:
            self.rows.pop((destination, identifier), None)

    def deliver_records(self, destination, records, *, dispatch):
        from fulcra_common.record_delivery import reconcile_records
        return reconcile_records(self, self._policy(), destination, records, dispatch=dispatch)

    def commit(self, callback):
        if self.cancel:
            raise RuntimeError("configuration changed")
        return callback()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from fulcra_media.collect_v1 import MediaRecordWriter
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path / "collect"))
    kv = {}
    receipts = []
    ctx = SimpleNamespace(config={}, config_epoch="synthetic-epoch", kv_get=lambda k, d: deepcopy(kv.get(k, d)),
        kv_set=lambda k, v: kv.__setitem__(k, deepcopy(v)),
        state=SimpleNamespace(watermark="2020-01-01T00:00:00Z", definition_id=None),
        annotation=lambda message, **kwargs: receipts.append((message, kwargs)))
    session = Session()
    return MediaRecordWriter(ctx, session), ctx, session, kv, receipts


@pytest.mark.parametrize("revived", [False, True])
def test_source_replay_obeys_exclusion_before_existing_or_upload_shortcuts(setup, revived):
    from fulcra_media.v1 import build_media_record
    writer, ctx, session, kv, receipts = setup
    item = event()
    destination = writer._destinations({item.category}, check_only=False)[item.category]
    row = build_media_record(item, destination, tags=(session.resolve_tag(item.service),))
    session._excluded.add(row["id"])
    if revived:
        session.rows[destination, row["id"]] = row
    result = writer.run_import([item])
    assert (result.total, result.skipped_existing, result.posted, result.verified) == (1, 1, 0, 0)
    assert (destination, row["id"]) not in session.rows
    assert not session.posts and not receipts


def test_exclusion_during_media_upload_counts_as_skipped_and_does_not_seed_twin_index(setup):
    from fulcra_media.collect_v1 import MediaTwinIndex
    writer, ctx, session, kv, receipts = setup
    def race():
        session._excluded.add(session.posts[-1][1][0]["id"])
    session.after_post = race
    result = writer.run_import([event(external_ids={"content_fingerprint": "synthetic-fingerprint"})])
    assert (result.total, result.skipped_existing, result.posted, result.verified) == (1, 1, 0, 0)
    assert not session.rows
    destination = next(iter(writer.destination_ids.values()))
    assert not MediaTwinIndex().lookup(session.account_id, destination, ["synthetic-fingerprint"])
    assert "0" in receipts[0][0] and "1" in receipts[0][0]


def test_two_importers_serialize_source_twin_planning_with_the_write(setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, BrokenBarrierError
    from fulcra_media.collect_v1 import MediaRecordWriter
    writer, ctx, session, kv, receipts = setup
    other = Session()
    other.rows = session.rows
    other_kv = {}
    other_ctx = SimpleNamespace(**vars(ctx))
    other_ctx.kv_get = lambda key, default: deepcopy(other_kv.get(key, default))
    other_ctx.kv_set = lambda key, value: other_kv.__setitem__(key, deepcopy(value))
    other_writer = MediaRecordWriter(other_ctx, other)
    barrier = Barrier(2)
    def synchronized_read(original):
        def read(*args, **kwargs):
            snapshot = original(*args, **kwargs)
            try:
                barrier.wait(timeout=0.2)
            except BrokenBarrierError:
                pass
            return snapshot
        return read
    session.get_source_records = synchronized_read(session.get_source_records)
    other.get_source_records = synchronized_read(other.get_source_records)
    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = [pool.submit(writer.run_import, [event(importer="netflix")]),
                pool.submit(other_writer.run_import, [event(importer="trakt")])]
        results = [run.result(timeout=5) for run in runs]
    assert len(session.rows) == 1
    assert sum(result.posted for result in results) == 1
    assert sum(result.skipped_existing for result in results) == 1


@pytest.mark.parametrize("service", [None, "", "\x00", "\ud800"])
def test_invalid_live_service_labels_fail_before_destination_or_tag_metadata(setup, service):
    writer, ctx, session, kv, receipts = setup
    with pytest.raises(ValueError, match="tag label|JSON"):
        writer.run_import([event(service=service)])
    assert not session.resolved and not session.tags and not session.posts
    assert not receipts


def test_dst_fold_input_is_processed_in_elapsed_time_order(setup):
    from zoneinfo import ZoneInfo
    writer, ctx, session, kv, receipts = setup
    zone = ZoneInfo("America/New_York")
    first = event(primary="earlier", start_time=datetime(2025, 11, 2, 1, 55, tzinfo=zone, fold=0),
                  end_time=datetime(2025, 11, 2, 1, 56, tzinfo=zone, fold=0))
    second = event(primary="later", start_time=datetime(2025, 11, 2, 1, 5, tzinfo=zone, fold=1),
                   end_time=datetime(2025, 11, 2, 1, 6, tzinfo=zone, fold=1))
    result = writer.run_import([second, first], chunk_size=1)
    assert result.verified == 2
    assert [rows[0]["sources"][0] for _, rows in session.posts] == [first.deterministic_id, second.deterministic_id]



def test_media_runtime_writes_declared_enrichment_and_only_reports_exact_visibility(setup):
    writer, ctx, session, kv, receipts = setup
    result = writer.run_import([event()])
    assert (result.total, result.posted, result.verified, result.skipped_existing) == (1, 1, 1, 0)
    row = next(iter(session.rows.values()))
    assert row["external_ids"] == {"example_id": "one"}
    assert row["title"] == row["note"] == "Synthetic film"
    assert receipts
    result = writer.run_import([event()])
    assert (result.posted, result.verified, result.skipped_existing) == (0, 0, 1)
    assert len(session.posts) == 1
    assert len(receipts) == 1


def test_same_importer_quick_replays_keep_distinct_ids_and_provenance(setup):
    writer, ctx, session, kv, receipts = setup
    items = [event(), event(primary="replay")]
    original = deepcopy(items)
    result = writer.run_import(items)
    assert items == original
    assert result.verified == 2
    assert len(session.rows) == 2
    assert all("com.fulcra.content.watched.v1.synthetic" in r["sources"] for r in session.rows.values())
    result = writer.run_import([event(primary="another-replay")])
    assert result.posted == result.verified == 1
    assert len(session.rows) == 3


def test_opaque_primary_ids_do_not_make_same_importer_replays_into_twins(setup):
    writer, ctx, session, kv, receipts = setup
    result = writer.run_import([event(deterministic_id="synthetic:one"), event(deterministic_id="synthetic:two")])
    assert result.verified == 2 and result.skipped_existing == 0


@pytest.mark.parametrize("with_fingerprint", [False, True])
def test_migrated_primary_source_alias_already_covered_by_an_owned_record_is_not_duplicated(setup, with_fingerprint):
    writer, ctx, session, kv, receipts = setup
    alias = "com.fulcra.media.other.v1.alias"
    fingerprint = "com.fulcra.content.watched.v1.synthetic"
    writer.run_import([event(extras=(fingerprint, alias) if with_fingerprint else (alias,))])
    result = writer.run_import([event(importer="other", primary="alias", extras=(fingerprint,) if with_fingerprint else ())])
    assert result.posted == 0 and result.skipped_existing == 1
    assert len(session.rows) == 1


def test_cross_source_twin_is_skipped_in_current_destination_only(setup):
    writer, ctx, session, kv, receipts = setup
    writer.run_import([event()])
    result = writer.run_import([event(importer="other")])
    assert result.skipped_existing == 1
    assert result.posted == 0
    # A different semantic destination never borrows another type's twin.
    result = writer.run_import([event(importer="other", category="listened")])
    assert result.posted == result.verified == 1


def test_same_id_changed_content_updates_in_place_even_when_fingerprint_is_shared(setup):
    writer, ctx, session, kv, receipts = setup
    writer.run_import([event()])
    identities = set(session.rows)
    result = writer.run_import([event(title="Corrected title", external_ids={"example_id": "corrected"})])
    assert result.posted == result.verified == 1
    assert set(session.rows) == identities
    assert next(iter(session.rows.values()))["title"] == "Corrected title"


def test_delayed_record_does_not_manufacture_verified_count_or_receipt(setup):
    writer, ctx, session, kv, receipts = setup
    session.visible = False
    with pytest.raises(RuntimeError, match="visible"):
        writer.run_import([event()])
    assert not receipts
    assert kv["v1_media_pending"]["groups"]


def test_lost_receipt_resumes_before_new_batch_and_does_not_repost_landed_ids(setup):
    writer, ctx, session, kv, receipts = setup
    session.fail_upload = True
    with pytest.raises(RuntimeError, match="uncertain"):
        writer.run_import([event()])
    session.fail_upload = False
    assert writer.resume() == 1
    assert len(session.posts) == 1
    assert receipts


def test_unknown_readback_stops_before_post_or_progress(setup):
    writer, ctx, session, kv, receipts = setup
    session.fail_read = True
    with pytest.raises(RuntimeError, match="readback unavailable"):
        writer.run_import([event()])
    assert not session.posts
    assert not receipts


def test_conflicting_primary_identity_fails_whole_input_before_destination_creation(setup):
    writer, ctx, session, kv, receipts = setup
    with pytest.raises(ValueError, match="conflict"):
        writer.run_import([event(), event(note="conflicting payload")])
    assert not session.resolved
    assert not session.posts


def test_old_account_cache_and_legacy_definition_ids_are_not_v1_pins(setup):
    writer, ctx, session, kv, receipts = setup
    kv["v1_media_destinations"] = {"account_id": "previous-account", "ids": {"watched": "old"}}
    ctx.config["definition_id"] = "legacy-definition"
    writer.run_import([event()])
    assert all(pin is None for _, pin, _ in session.resolved)
    assert kv["v1_media_destinations"]["account_id"] == session.account_id


def test_check_only_does_not_upload_resume_pending_or_create_destinations_tags(setup):
    writer, ctx, session, kv, receipts = setup
    session.visible = False
    with pytest.raises(RuntimeError):
        writer.run_import([event()])
    session.posts.clear()
    session.resolved.clear()
    session.tags.clear()
    result = writer.run_import([event(primary="new")], check_only=True)
    assert result.posted == 1 and result.verified == 0
    assert not session.posts
    assert not receipts
    assert all(not allow_create for _, _, allow_create in session.resolved)
    assert all(not allow_create for _, allow_create in session.tags)
    assert kv["v1_media_pending"]["groups"]


def test_existing_user_tags_provenance_and_extra_declared_fields_survive_source_correction(setup):
    writer, ctx, session, kv, receipts = setup
    writer.run_import([event()])
    row = next(iter(session.rows.values()))
    manual_tag = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    row["tags"].append(manual_tag)
    row["personal_label"] = "Synthetic personal label"
    row["external_ids"]["historical_extra"] = {"id": "retained"}
    legacy = {"id": "11111111-2222-4333-8444-555555555555", "data_type": "DurationAnnotation/synthetic",
        "api_version": "v1alpha1", "source_id": "com.fulcradynamics.annotation.synthetic",
        "source_name": "Synthetic original name", "metadata": {"opaque": [False, 0, None]}}
    row["legacy_record"] = deepcopy(legacy)
    row["sources"].append("com.fulcradynamics.annotation.synthetic-legacy-binding")
    result = writer.run_import([event(title="Corrected title")])
    assert result.posted == result.verified == 1
    updated = next(iter(session.rows.values()))
    assert manual_tag in updated["tags"]
    assert updated["personal_label"] == "Synthetic personal label"
    assert updated["external_ids"]["historical_extra"] == {"id": "retained"}
    assert updated["legacy_record"] == legacy
    assert "com.fulcradynamics.annotation.synthetic-legacy-binding" in updated["sources"]
    assert writer.run_import([event(title="Corrected title")]).posted == 0


def test_existing_same_uuid_with_unrelated_primary_identity_is_quarantined(setup):
    writer, ctx, session, kv, receipts = setup
    writer.run_import([event()])
    row = next(iter(session.rows.values()))
    row["sources"] = ["unrelated.primary.identity"]
    before = len(session.posts)
    with pytest.raises(ValueError, match="primary identity"):
        writer.run_import([event(title="Should not overwrite unrelated row")])
    assert len(session.posts) == before


def test_check_only_cross_source_twin_count_is_consistent_across_chunks(setup):
    writer, ctx, session, kv, receipts = setup
    result = writer.run_import([event(), event(importer="other")], check_only=True, chunk_size=1)
    assert result.posted == 1
    assert result.skipped_existing == 1
    assert not session.posts


def test_existing_legacy_selection_requires_an_explicit_v1_mapping(setup):
    writer, ctx, session, kv, receipts = setup
    ctx.state = SimpleNamespace(definition_id="synthetic-old-selected-definition")
    with pytest.raises(RuntimeError, match="existing annotation selection"):
        writer.run_import([event()])
    assert not session.resolved
    assert not session.posts


def test_explicit_owned_v1_mapping_is_lookup_only_and_retains_legacy_binding(setup):
    writer, ctx, session, kv, receipts = setup
    ctx.state = SimpleNamespace(definition_id="synthetic-old-selected-definition")
    chosen = "Event/11111111-2222-4333-8444-555555555555"
    ctx.config["v1_destination_ids"] = {"watched": chosen}
    writer.run_import([event()])
    assert session.resolved[0][1:] == (chosen, False)
    assert ctx.state.definition_id == "synthetic-old-selected-definition"
    assert kv["v1_media_destinations"]["legacy_definition_id"] == ctx.state.definition_id


def test_legacy_selection_change_cannot_silently_reuse_cached_default_mapping(setup):
    writer, ctx, session, kv, receipts = setup
    writer.run_import([event()])
    ctx.state = SimpleNamespace(definition_id="new-old-selected-definition")
    with pytest.raises(RuntimeError, match="existing annotation selection"):
        writer.run_import([event(primary="new")])


def test_guarded_run_reconciles_before_source_fetch_and_closes_captured_session(setup, monkeypatch):
    from fulcra_media import collect_v1
    writer, ctx, session, kv, receipts = setup
    session.fail_upload = True
    with pytest.raises(RuntimeError):
        writer.run_import([event()])
    session.fail_upload = False
    monkeypatch.setattr(collect_v1, "CollectRecordSession", lambda _: session, raising=False)
    def fetch_and_import(current):
        assert not kv["v1_media_pending"]["groups"]
        assert receipts
        assert current._media_record_writer.session is session
        return "synthetic source result"
    wrapped = collect_v1.media_run(fetch_and_import)
    assert wrapped(ctx) == "synthetic source result"
    assert session.closed
    assert not hasattr(ctx, "_media_record_writer")
    assert len(session.posts) == 1


def test_guarded_run_releases_session_when_source_fetch_fails(setup, monkeypatch):
    from fulcra_media import collect_v1
    writer, ctx, session, kv, receipts = setup
    monkeypatch.setattr(collect_v1, "CollectRecordSession", lambda _: session, raising=False)
    def offline(current):
        raise RuntimeError("synthetic source offline")
    with pytest.raises(RuntimeError, match="source offline"):
        collect_v1.media_run(offline)(ctx)
    assert session.closed
    assert not hasattr(ctx, "_media_record_writer")


def test_media_cursor_is_verified_and_separate_from_legacy_progress(setup, monkeypatch):
    from fulcra_media import collect_v1
    writer, ctx, session, kv, receipts = setup
    monkeypatch.setattr(collect_v1, "CollectRecordSession", lambda _: session)
    seen = []
    def collect(current):
        seen.append(current.state.watermark)
        current.state.watermark = "2026-01-02T00:00:00Z"
    wrapped = collect_v1.media_run(collect)
    wrapped(ctx)
    assert seen == [None]  # Legacy delivery is not proof of v1 visibility.
    assert ctx.state.watermark == "2020-01-01T00:00:00Z"
    wrapped(ctx)
    assert seen[-1] == "2026-01-02T00:00:00Z"
    assert ctx.state.watermark == "2020-01-01T00:00:00Z"


def test_v1_twin_policy_uses_current_verified_account_and_destination(setup):
    writer, ctx, session, kv, receipts = setup
    high = event(external_ids={"content_fingerprint": "movie:synthetic"}, extras=())
    writer.run_import([high])
    ctx.config["twin_policy"] = "auto-discard"
    low = event(importer="other", timestamp_confidence="low", extras=(),
                external_ids={"content_fingerprint": "movie:synthetic"})
    result = writer.run_import([low])
    assert result.posted == 0 and result.skipped_existing == 1
    # The index alone is not proof: the referenced row must still exist and
    # carry matching high-confidence enrichment in this captured account.
    session.rows.clear()
    assert writer.run_import([low]).posted == 1
    session.rows.clear()
    session.account_id = "other-account"
    assert writer.run_import([low]).posted == 1


def test_v1_twin_policy_does_not_borrow_other_semantic_destination(setup):
    writer, ctx, session, kv, receipts = setup
    writer.run_import([event(external_ids={"content_fingerprint": "synthetic"}, extras=())])
    ctx.config["twin_policy"] = "auto-discard"
    assert writer.run_import([event(category="read", importer="other", timestamp_confidence="low",
        external_ids={"content_fingerprint": "synthetic"}, extras=())]).posted == 1


def test_v1_twin_index_is_not_populated_by_unverified_upload(setup):
    writer, ctx, session, kv, receipts = setup
    session.visible = False
    with pytest.raises(RuntimeError):
        writer.run_import([event(external_ids={"content_fingerprint": "synthetic"}, extras=())])
    # Reconcile the pending write, then remove it. A low-confidence retry
    # cannot be suppressed by a high event that was never visible.
    kv.clear()
    session.visible = True
    ctx.config["twin_policy"] = "auto-discard"
    assert writer.run_import([event(importer="other", timestamp_confidence="low",
        external_ids={"content_fingerprint": "synthetic"}, extras=())]).posted == 1


def test_v1_twin_preview_does_not_create_index_or_drop_default_keep_replay(setup):
    from fulcra_collect.config import config_dir
    writer, ctx, session, kv, receipts = setup
    incoming = [event(external_ids={"content_fingerprint": "synthetic"}, extras=()),
        event(primary="low", timestamp_confidence="low", external_ids={"content_fingerprint": "synthetic"}, extras=())]
    assert writer.run_import(incoming, check_only=True).posted == 2
    assert not (config_dir() / "v1-media-twins.sqlite").exists()
    ctx.config["twin_policy"] = "auto-discard"
    assert writer.run_import(incoming, check_only=True).posted == 1
    assert not (config_dir() / "v1-media-twins.sqlite").exists()


def test_threaded_media_requests_cannot_repost_an_inflight_pending_batch(setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    writer, ctx, session, kv, receipts = setup
    entered, release, overlap = Event(), Event(), Event()
    original_ingest = session.ingest
    def ingest(destination, records, *, before_submit=None):
        if entered.is_set():
            overlap.set()
        else:
            entered.set()
            assert release.wait(2)
        return original_ingest(destination, records, before_submit=before_submit)
    session.ingest = ingest
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(writer.run_import, [event(primary="first", extras=())])
        assert entered.wait(2)
        second = pool.submit(writer.run_import, [event(primary="second", extras=())])
        # The second call must wait on the writer, rather than trying to
        # reconcile/repost the first request while it is still in flight.
        premature = overlap.wait(0.1)
        release.set()
        first.result(timeout=2)
        second.result(timeout=2)
    assert not premature
    assert len(session.posts) == 2


@pytest.mark.parametrize("change", ["account", "epoch", "source", "destination"])
def test_media_cursor_never_crosses_account_or_selection_scope(setup, monkeypatch, change):
    from fulcra_media import collect_v1
    writer, ctx, session, kv, receipts = setup
    monkeypatch.setattr(collect_v1, "CollectRecordSession", lambda _: session)
    seen = []
    def collect(current):
        seen.append(current.state.watermark)
        current.state.watermark = "2026-01-02T00:00:00Z"
    wrapped = collect_v1.media_run(collect)
    wrapped(ctx)
    prior = deepcopy(kv)
    if change == "account":
        session.account_id = "different-account"
    elif change == "epoch":
        ctx.config_epoch = "different-epoch"
    elif change == "source":
        ctx.config["username"] = "different-source"
    else:
        ctx.config["v1_destination_ids"] = {"watched": "Event/11111111-2222-4333-8444-555555555555"}
    wrapped(ctx)
    assert seen[-1] is None
    assert all(kv[key] == value for key, value in prior.items())


@pytest.mark.parametrize("outcome", ["error", "stale", "preview"])
def test_media_cursor_does_not_advance_on_error_revocation_or_preview(setup, monkeypatch, outcome):
    from fulcra_media import collect_v1
    writer, ctx, session, kv, receipts = setup
    monkeypatch.setattr(collect_v1, "CollectRecordSession", lambda _: session)
    if outcome == "preview":
        ctx.config["preview_only"] = True
    def collect(current):
        current.state.watermark = "2026-01-02T00:00:00Z"
        if outcome == "error":
            raise RuntimeError("source failed")
        session.cancel = outcome == "stale"
    wrapped = collect_v1.media_run(collect)
    if outcome == "preview":
        wrapped(ctx)
    else:
        with pytest.raises(RuntimeError):
            wrapped(ctx)
    assert not any(key.startswith("v1_media_cursor:") for key in kv)
    assert ctx.state.watermark == "2020-01-01T00:00:00Z"


@pytest.mark.parametrize("category", ["watched", "listened", "read"])
def test_native_category_routing_empty_and_no_fingerprint_repeat(setup, category):
    writer, ctx, session, kv, receipts = setup
    empty = writer.run_import([])
    assert (empty.total, empty.posted, empty.verified) == (0, 0, 0)
    assert not session.posts
    item = event(category=category, extras=())
    result = writer.run_import([item])
    assert result.posted == result.verified == 1
    assert writer.destination_ids[category] == session.posts[0][0]
    assert session.posts[0][1][0]["sources"][0] == item.deterministic_id
    repeated = writer.run_import([item])
    assert repeated.skipped_existing == 1 and repeated.posted == 0
    assert len(session.posts) == 1


def test_native_netflix_fixture_chunks_and_retains_parser_fields(setup):
    from pathlib import Path
    from fulcra_media.importers.netflix import parse_slim
    writer, ctx, session, kv, receipts = setup
    items = list(parse_slim(Path(__file__).parent / "fixtures" / "netflix_slim_small.csv"))
    assert len(items) > 2
    result = writer.run_import(items, chunk_size=2)
    assert result.total == result.posted == result.verified == len(items)
    assert len(session.posts) == (len(items) + 1) // 2
    rows = [row for _, batch in session.posts for row in batch]
    by_source = {row["sources"][0]: row for row in rows}
    for item in items:
        row = by_source[item.deterministic_id]
        assert row["note"] and row["title"] == item.title
        assert row["service"] == item.service
        assert row["timestamp_confidence"] == item.timestamp_confidence
        assert row["external_ids"] == item.external_ids
    repeated = writer.run_import(items, chunk_size=2)
    assert repeated.skipped_existing == len(items) and repeated.posted == 0


def test_cross_source_distinct_fingerprints_are_separate_native_records(setup):
    writer, ctx, session, kv, receipts = setup
    writer.run_import([event()])
    other = event(importer="other", extras=("com.fulcra.content.watched.v1.different",))
    result = writer.run_import([other])
    assert result.posted == result.verified == 1 and result.skipped_existing == 0
    assert len(session.rows) == 2
