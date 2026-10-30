"""Canonical script, installed adapter and real durable runtime; synthetic HTTP only."""
import json
import re
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from fulcra_common.data_types import CreationJournal
from fulcra_common.record_exclusions import EXCLUSION_SPEC

ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
DEST = "Event/11111111-2222-4333-8444-555555555555"
CONTROL = "Event/aaaaaaaa-2222-4333-8444-555555555555"


@pytest.fixture
def native(tmp_path, monkeypatch, netflix_isolation, ni, capsys):
    from fulcra_collect import standalone_v1

    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(profile))
    CreationJournal(profile / "v1-type-creations.sqlite").resolved(ACCOUNT, EXCLUSION_SPEC, CONTROL)
    state = dict(profile=profile, paths=[], posts=[], batches=[], rows={}, name="Watched")

    def handle(req):
        path = req.url.path
        state["paths"].append((req.method, path))
        if path == "/user/v1alpha1/info":
            return httpx.Response(200, json={"userid": state.get("account", ACCOUNT)})
        if req.method == "POST" and path.startswith("/input/v1/data_type/"):
            state["absent"] = False
            state["created"] = json.loads(req.content)
            if state.get("create_redirect"):
                return httpx.Response(303, headers={"Location": state["create_redirect"]})
            return httpx.Response(201, json={"id": DEST})
        if path == "/input/v1/data_type":
            catalog = [dict(id=d, name=n, fulcra_userid=ACCOUNT,
                            api_version="v1", recordable=True, deprecated=False,
                            record_spec={"type": "event"})
                       for d, n in [(DEST, state["name"]), (CONTROL, EXCLUSION_SPEC.name)]]
            if state.get("absent"):
                catalog = catalog[1:]
            if state.get("fault") and catalog:
                catalog[0].update(state["fault"])
            if state.get("ambiguous"):
                catalog.append({**catalog[0], "id": "Event/cccccccc-2222-4333-8444-555555555555"})
            return httpx.Response(200, json=catalog)
        if path == "/data/v1/catalog/DeletedRecord/v1/schema":
            return httpx.Response(200, json={"type": "object", "properties": {
                key: {"type": "string"} for key in ("id", "record_id", "data_type")}})
        if path == "/ingest/v1/record/DeletedRecord":
            for line in req.content.splitlines():
                state["rows"].pop((DEST, json.loads(line)["record_id"]), None)
            return httpx.Response(201, json={"upload_id": "synthetic-delete"})
        if path.endswith("/schema"):
            from fulcra_media.v1 import media_spec
            spec = EXCLUSION_SPEC if CONTROL in path else media_spec("watched")
            schema = dict(type="object", properties={
                **{k: {} for k in ("id", "sources", "tags", "start_time", "end_time")},
                **spec.fields}, required=list(spec.required_fields))
            if CONTROL not in path:
                schema.update(state.get("schema_rules", {}))
                if state.get("bad_schema"):
                    schema["properties"].pop("service")
            return httpx.Response(200, json=schema)
        if path == "/data/v1/records":
            if state.get("read_error"):
                return httpx.Response(503)
            q = req.url.params["q"]
            full = re.search('__name__="([^"]+)"', q).group(1)
            ids = re.findall('id="([^"]+)"', q)
            return httpx.Response(200, text="\n".join(
                json.dumps(state["rows"][full, i]) for i in ids if (full, i) in state["rows"]))
        if path == "/ingest/v1/record/" + DEST:
            assert req.url.params["api_version"] == "v1"
            assert req.headers["content-type"] == "application/x-jsonl"
            batch = [json.loads(line) for line in req.content.splitlines()]
            state["batches"].append(deepcopy(batch))
            state["posts"].extend(deepcopy(batch))
            if state.get("visible", True):
                state["rows"].update({(DEST, r["id"]): deepcopy(r) for r in batch})
            if state.get("lost") or len(state["batches"]) == state.get("fail_batch"):
                raise httpx.ReadError("SECRET source-value credential-canary")
            return httpx.Response(201, json={"upload_id": "synthetic"})
        pytest.fail("unexpected synthetic route " + req.method + " " + path)

    transport = httpx.MockTransport(handle)
    if hasattr(ni, "make_api_client"):
        monkeypatch.setattr(ni, "make_api_client", lambda token: httpx.Client(
            transport=transport, base_url="https://synthetic.invalid"))
    original = standalone_v1.standalone_runtime

    from contextlib import contextmanager

    @contextmanager
    def runtime(namespace, settings, **kwargs):
        with original(namespace, settings, transport=transport, identity_transport=transport,
                      **kwargs) as pair:
            if state.get("runtime_hook"):
                state["runtime_hook"](*pair)
            yield pair

    monkeypatch.setattr(standalone_v1, "standalone_runtime", runtime)

    def invoke(path, *args):
        code = state.get("module", ni).main([str(path), "--json", *args])
        output = capsys.readouterr()
        assert "SECRET" not in output.out + output.err
        return code, json.loads(output.out)

    state["invoke"] = invoke
    return state


def test_actual_main_native_slim(native, fixtures_dir, ni):
    code, env = native["invoke"](fixtures_dir / "slim.csv")
    assert code == 0, env
    assert env["posted"] == env["verified"] == 5
    events = list(ni.parse_slim(fixtures_dir / "slim.csv"))
    for row, event in zip(native["posts"], events):
        assert row["sources"] == [event.det_id] and row["tags"] == []
        assert row["note"] == event.note and row["title"] == event.title
        assert row["duration_seconds"] == 1 and type(row["duration_seconds"]) is int
        assert row["service"] == "netflix" and row["importer"] == "netflix-slim"
        assert row["timestamp_confidence"] == "low"
        assert row["external_ids"] == {**event.external, "content_fingerprint": event.fingerprint,
                                       "timestamp_confidence": event.confidence}
        assert "metadata" not in row and "data" not in row
    assert not any("annotation" in p for _, p in native["paths"])


def source(tmp_path, rows=5):
    path = tmp_path / "synthetic.csv"
    path.write_text("Title,Date\n" + "".join(f"Title {n},1/1/24\n" for n in range(rows)))
    return path


def pending(native, value=None):
    from fulcra_collect import db
    conn = db.open(native["profile"] / "state.db")
    # db.open owns a per-thread cached connection; callers must not close it.
    if value is not None:
        db.set_record_outbox(conn, "cli-netflix", "v1_netflix_pending", value)
    return db.get_record_outbox(conn, "cli-netflix", "v1_netflix_pending")


def test_actual_rich_and_identical_existing_are_verified_without_post(native, fixtures_dir, ni):
    path = fixtures_dir / "gdpr.csv"
    code, env = native["invoke"](path)
    assert code == 0 and env["verified"] == 3
    events = list(ni.parse_rich(path))
    from fulcra_netflix.native import build_netflix_record
    assert native["posts"] == [build_netflix_record(e, DEST, "rich") for e in events]
    assert any(row["title"] == "Dune: Part Two" for row in native["posts"])
    assert all(row["importer"] == "netflix-rich" and row["timestamp_confidence"] == "high"
               for row in native["posts"])
    for row in native["rows"].values():
        row["unrelated_cloud_enrichment"] = {"keep": True}
    code, env = native["invoke"](path)
    assert code == 0 and env["verified"] == 3 and env["skipped_existing"] is None
    assert len(native["posts"]) == 3


@pytest.mark.parametrize("variant", ["slim", "rich"])
def test_actual_source_opens_one_reader_and_iterates_once(native, fixtures_dir, ni, monkeypatch, variant):
    path = fixtures_dir / ("slim.csv" if variant == "slim" else "gdpr.csv")
    opens, reads, rows = [], [], []
    original_open, original_reader = Path.open, ni.csv.DictReader
    def opened(self, *args, **kwargs):
        if self == path:
            opens.append(self)
        return original_open(self, *args, **kwargs)
    class Reader(original_reader):
        def __init__(self, *args, **kwargs):
            reads.append(1)
            super().__init__(*args, **kwargs)
        def __next__(self):
            row = super().__next__()
            rows.append(row)
            return row
    monkeypatch.setattr(Path, "open", opened)
    monkeypatch.setattr(ni.csv, "DictReader", Reader)
    code, env = native["invoke"](path)
    assert code == 0 and len(opens) == len(reads) == 1
    assert len(rows) == (5 if variant == "slim" else 4)  # includes filtered trailer
    assert env["variant"] == variant


@pytest.mark.parametrize("bad", ["late-date", "negative-duration", "conflict", "nonfinite", "naive"])
def test_complete_local_preflight_before_current_metadata(native, tmp_path, ni, monkeypatch, bad):
    path = source(tmp_path)
    if bad == "late-date":
        path.write_text(path.read_text() + "SECRET,SECRET\n")
    else:
        original = ni._read_events
        def corrupted(path):
            variant, events = original(path)
            if bad == "negative-duration":
                from datetime import timedelta
                events[-1].end = events[-1].start - timedelta(seconds=1)
            elif bad == "nonfinite":
                events[-1].external["synthetic"] = float("nan")
            elif bad == "naive":
                events[-1].start = events[-1].start.replace(tzinfo=None)
            else:
                events[-1].det_id = events[0].det_id
            # Exercise main's local proof with synthetic malformed codec data.
            ni._validate_events(events)
            return variant, events
        monkeypatch.setattr(ni, "_read_events", corrupted)
    native["absent"] = True
    code, env = native["invoke"](path)
    assert code == 2 and env["posted"] == 0 and env["errors"][0]["stage"] == "parse"
    assert not native["posts"] and not any(m == "POST" for m, _ in native["paths"])


@pytest.mark.parametrize("mode", ["adopt", "create", "pin", "rename"])
def test_native_owned_default_create_pin_and_shared_rename(native, tmp_path, mode):
    from fulcra_media.v1 import media_spec
    path = source(tmp_path, 1)
    if mode == "create":
        native["absent"] = True
    if mode in ("pin", "rename"):
        native["name"] = "Renamed shared Watched"
    if mode == "rename":
        # Same account/spec journal is also used by cli-media.
        CreationJournal(native["profile"] / "v1-type-creations.sqlite").resolved(
            ACCOUNT, media_spec("watched"), DEST)
    code, env = native["invoke"](path, *(["--data-type", DEST] if mode == "pin" else []))
    assert code == 0 and env["verified"] == 1, env
    assert ("created" in native) == (mode == "create")
    assert CreationJournal(native["profile"] / "v1-type-creations.sqlite").read(
        ACCOUNT, media_spec("watched"))[2] == DEST


@pytest.mark.parametrize("fault", ["owner", "base", "api", "deprecated", "schema", "ambiguous", "journal"])
def test_native_destination_holds_before_records(native, tmp_path, fault):
    if fault == "owner":
        native["fault"] = {"fulcra_userid": "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee"}
    elif fault == "base":
        native["fault"] = {"record_spec": {"type": "metric"}}
    elif fault == "api":
        native["fault"] = {"api_version": "v1alpha1"}
    elif fault == "deprecated":
        native["fault"] = {"deprecated": True}
    elif fault == "schema":
        native["bad_schema"] = True
    elif fault == "ambiguous":
        native["ambiguous"] = True
    else:
        from fulcra_media.v1 import media_spec
        CreationJournal(native["profile"] / "v1-type-creations.sqlite").resolved(
            ACCOUNT, media_spec("watched"), "Event/cccccccc-2222-4333-8444-555555555555")
    code, env = native["invoke"](source(tmp_path, 1), *([] if fault == "ambiguous" else ["--data-type", DEST]))
    assert code == 2 and env["verified"] == 0 and not native["posts"], env
    assert not any(m == "POST" for m, _ in native["paths"])


def test_full_served_schema_rejection_after_creation_preserves_journal(native, tmp_path):
    from fulcra_media.v1 import media_spec
    native["absent"] = True
    native["schema_rules"] = {"if": {"properties": {"service": {"const": "netflix"}}},
                             "then": {"properties": {"duration_seconds": {"minimum": 20}}}}
    code, env = native["invoke"](source(tmp_path, 1))
    assert code == 2 and env["posted"] == 0 and native["created"] and not native["posts"]
    assert CreationJournal(native["profile"] / "v1-type-creations.sqlite").read(
        ACCOUNT, media_spec("watched"))[2] == DEST
    assert not any("RecordExclusions" in p for _, p in native["paths"])


@pytest.mark.parametrize("missing", ["absent", "empty", "bad"])
def test_recovery_before_missing_empty_bad_source(native, tmp_path, missing):
    path = source(tmp_path, 1)
    native["lost"] = True
    code, _ = native["invoke"](path)
    assert code == 2 and pending(native)["groups"]
    original = deepcopy(pending(native)["groups"])
    native["lost"] = False
    if missing == "absent":
        path.unlink()
    else:
        path.write_text("Title,Date\n" if missing == "empty" else "SECRET\n")
    code, env = native["invoke"](path)
    assert code == (0 if missing == "empty" else 2), env
    assert env["recovery"] == {"verified": 1, "excluded": 0}
    assert env["posted"] == 0 and len(native["posts"]) == 1
    assert pending(native) == {"groups": []}
    assert native["posts"][0] == original[0]["records"][0]


@pytest.mark.parametrize("fault", ["account", "settings", "target", "spec", "corrupt", "profile"])
def test_pending_authority_unknown_holds_before_source(native, tmp_path, monkeypatch, fault):
    path = source(tmp_path, 1)
    native["lost"] = True
    assert native["invoke"](path)[0] == 2
    original = deepcopy(pending(native))
    native["lost"] = False
    args = []
    if fault == "account":
        native["account"] = "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    elif fault == "settings":
        value = deepcopy(original)
        value["settings_fingerprint"] = "unknown"
        pending(native, value)
        original = value
    elif fault == "target":
        args = ["--data-type", DEST]
    elif fault == "spec":
        from fulcra_media import v1
        monkeypatch.setitem(v1._MEDIA_FIELDS, "future", {"type": "string"})
    elif fault == "corrupt":
        value = deepcopy(original)
        value["groups"] = "unknown"
        pending(native, value)
        original = value
    else:
        def changed(ctx, session):
            monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path / "wrong"))
        native["runtime_hook"] = changed
    original_open = Path.open
    def opened(self, *args, **kwargs):
        if self == path:
            pytest.fail("source opened before original authority settled")
        return original_open(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", opened)
    code, env = native["invoke"](path, *args)
    assert code == 2 and env["errors"][0]["stage"] == "recovery"
    assert len(native["posts"]) == 1 and pending(native) == original
    assert not (tmp_path / "wrong").exists()


def test_all_pending_targets_proven_before_any_drain(native, tmp_path, monkeypatch):
    from fulcra_collect.v1_outbox import dispatch_states
    path = source(tmp_path, 1)
    native["lost"] = True
    assert native["invoke"](path)[0] == 2
    value = pending(native)
    unknown = "Event/cccccccc-2222-4333-8444-555555555555"
    value["groups"].append({"data_type": unknown, "records": deepcopy(value["groups"][0]["records"])})
    value["dispatch"] = dispatch_states(value, {g["data_type"]: g["records"] for g in value["groups"]})
    pending(native, value)
    native["lost"] = False
    native["paths"].clear()
    code, env = native["invoke"](path)
    assert code == 2 and env["recovery"]["verified"] == 0 and pending(native) == value
    assert not any(p == "/data/v1/records" for _, p in native["paths"])


def test_issued_unknown_is_never_blindly_resubmitted(native, tmp_path):
    path = source(tmp_path, 2)
    native.update(lost=True, visible=False)
    assert native["invoke"](path)[0] == 2
    value = pending(native)
    native["lost"] = False
    assert native["invoke"](path)[0] == 2 and len(native["batches"]) == 1
    first, second = native["posts"]
    native["rows"][DEST, first["id"]] = deepcopy(first)
    assert native["invoke"](path)[0] == 2 and len(native["batches"]) == 1
    native["rows"][DEST, second["id"]] = deepcopy(second)
    path.unlink()
    code, env = native["invoke"](path)
    assert code == 2 and env["recovery"]["verified"] == 2 and env["posted"] == 0
    assert len(native["batches"]) == 1 and pending(native) == {"groups": []}
    assert value["groups"][0]["records"] == native["posts"]


@pytest.mark.parametrize("field", ["note", "nested", "sources", "tags", "time"])
def test_late_existing_mismatch_blocks_whole_current_before_chunk_one(native, tmp_path, ni, monkeypatch, field):
    from fulcra_netflix.native import build_netflix_record
    path = source(tmp_path)
    row = build_netflix_record(list(ni.parse_slim(path))[-1], DEST, "slim")
    if field == "nested":
        row["external_ids"]["cloud_extra"] = True
    elif field == "sources":
        row["sources"].append("different")
    elif field == "tags":
        row["tags"] = [ACCOUNT]
    elif field == "time":
        row["end_time"] = "2024-01-01T12:00:02Z"
    else:
        row["note"] = "changed"
    native["rows"][DEST, row["id"]] = row
    monkeypatch.setattr(ni, "CHUNK_SIZE", 2)
    code, env = native["invoke"](path)
    assert code == 2 and env["posted"] == 0 and not native["posts"]
    assert not any(m == "POST" for m, _ in native["paths"])


def exclude(native, row):
    from datetime import datetime, timezone
    from fulcra_common.record_exclusions import _record
    record = _record(CONTROL, DEST, row["id"], action_id=ACCOUNT,
        requested_at=datetime(2026, 1, 1, tzinfo=timezone.utc), reason="operator", evidence=[])
    native["rows"][CONTROL, record["id"]] = record


def test_excluded_is_separate_from_verified(native, tmp_path, ni):
    from fulcra_netflix.native import build_netflix_record
    path = source(tmp_path, 2)
    row = build_netflix_record(next(ni.parse_slim(path)), DEST, "slim")
    exclude(native, row)
    code, env = native["invoke"](path)
    assert code == 0 and env["posted"] == env["verified"] == 1 and env["excluded"] == 1
    assert len(native["posts"]) == 1 and native["posts"][0]["id"] != row["id"]


def test_unknown_issued_then_excluded_requires_positive_evidence(native, tmp_path, ni):
    from fulcra_netflix.native import build_netflix_record
    path = source(tmp_path, 1)
    row = build_netflix_record(next(ni.parse_slim(path)), DEST, "slim")
    native.update(lost=True, visible=False)
    assert native["invoke"](path)[0] == 2
    exclude(native, row)
    native["lost"] = False
    code, env = native["invoke"](path)
    assert code == 2 and env["recovery"] == {"verified": 0, "excluded": 0}
    assert pending(native)["groups"] and len(native["batches"]) == 1


def test_later_chunk_unknown_preserves_prior_counts_and_only_inflight_durable(native, tmp_path, ni, monkeypatch):
    path = source(tmp_path)
    monkeypatch.setattr(ni, "CHUNK_SIZE", 2)
    native["fail_batch"] = 2
    code, env = native["invoke"](path)
    assert code == 2 and env["posted"] == env["verified"] == 2
    assert len(native["batches"]) == 2 and len(pending(native)["groups"][0]["records"]) == 2
    assert len(native["posts"]) == 4  # fifth record was never staged/dispatched
    del native["fail_batch"]
    path.unlink()
    code, env = native["invoke"](path)
    assert code == 2 and env["posted"] == 0 and env["recovery"]["verified"] == 2
    assert len(native["batches"]) == 2 and pending(native) == {"groups": []}


@pytest.mark.parametrize("fault", ["stage", "receipt", "clear", "close"])
def test_late_failure_retains_complete_chunk_lowerbounds(native, tmp_path, ni, monkeypatch, fault):
    from fulcra_collect.v1_outbox import V1RecordOutbox
    from fulcra_collect.plugin import RunContext
    from fulcra_collect.standalone_v1 import StandaloneRecordSession
    path = source(tmp_path)
    monkeypatch.setattr(ni, "CHUNK_SIZE", 2)
    def fail(*args, **kwargs):
        raise RuntimeError("SECRET credential-canary")
    if fault == "stage":
        original = V1RecordOutbox.stage
        calls = []
        def stage(self, *args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                fail()
            return original(self, *args, **kwargs)
        monkeypatch.setattr(V1RecordOutbox, "stage", stage)
    elif fault == "receipt":
        monkeypatch.setattr(RunContext, "annotation", fail)
    elif fault == "clear":
        original = RunContext.outbox_set
        def clear(self, key, value):
            if value == {"groups": []}:
                fail()
            return original(self, key, value)
        monkeypatch.setattr(RunContext, "outbox_set", clear)
    else:
        original = StandaloneRecordSession.close
        def close(self):
            original(self)
            fail()
        monkeypatch.setattr(StandaloneRecordSession, "close", close)
    code, env = native["invoke"](path)
    assert code == 2 and not env["ok"]
    assert env["verified"] == env["posted"] == (5 if fault == "close" else 2)
    assert env["recovery"] == {"verified": 0, "excluded": 0}


def test_recovery_receipt_failure_preserves_separate_evidence(native, tmp_path, monkeypatch):
    from fulcra_collect.plugin import RunContext
    path = source(tmp_path, 2)
    native["lost"] = True
    assert native["invoke"](path)[0] == 2
    native["lost"] = False
    def fail(*args, **kwargs):
        raise RuntimeError("SECRET credential-canary")
    monkeypatch.setattr(RunContext, "annotation", fail)
    path.unlink()
    code, env = native["invoke"](path)
    assert code == 2 and env["recovery"]["verified"] == 2 and env["posted"] == 0
    assert env["total"] == 0 and pending(native)["groups"] and len(native["batches"]) == 1


def test_primary_read_unknown_holds_without_mutation(native, tmp_path):
    native["read_error"] = True
    code, env = native["invoke"](source(tmp_path))
    assert code == 2 and env["status"] == "unknown" and not native["posts"]
    assert not any(m == "POST" for m, _ in native["paths"])


def test_excluded_first_chunk_counts_survive_later_unknown(native, tmp_path, ni, monkeypatch):
    from fulcra_netflix.native import build_netflix_record
    path = source(tmp_path)
    exclude(native, build_netflix_record(next(ni.parse_slim(path)), DEST, "slim"))
    monkeypatch.setattr(ni, "CHUNK_SIZE", 2)
    native["fail_batch"] = 2
    code, env = native["invoke"](path)
    assert code == 2 and env["verified"] == 1 and env["excluded"] == 1
    assert env["recovery"] == {"verified": 0, "excluded": 0}
    assert len(pending(native)["groups"][0]["records"]) == 2


def test_actual_main_equivalent_utc_spelling_matches(native, tmp_path):
    path = source(tmp_path, 1)
    assert native["invoke"](path)[0] == 0
    for row in native["rows"].values():
        row["start_time"] = row["start_time"].replace("Z", "+00:00")
        row["end_time"] = row["end_time"].replace("Z", "+00:00")
    assert native["invoke"](path)[0] == 0 and len(native["batches"]) == 1


def test_actual_bundled_loader_and_console_use_native_runtime(native, tmp_path, monkeypatch, capsys):
    import importlib.util
    import sys
    from fulcra_netflix import loader

    path = source(tmp_path, 1)
    package = tmp_path / "wheel-layout" / "fulcra_netflix"
    package.mkdir(parents=True)
    copied_loader = package / "loader.py"
    copied_loader.write_bytes(Path(loader.__file__).read_bytes())
    bundled = package / "_bundled" / loader._SCRIPT_REL
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(loader.SCRIPT_PATH.read_bytes())
    spec = importlib.util.spec_from_file_location("netflix_native_bundled_loader", copied_loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    native["module"] = module.load()
    assert native["invoke"](path)[0] == 0
    assert module.SCRIPT_PATH == bundled and len(native["posts"]) == 1
    monkeypatch.setattr(sys, "argv", ["fulcra-netflix", str(path), "--json"])
    assert loader.main() == 0
    env = json.loads(capsys.readouterr().out)
    assert env["verified"] == 1 and len(native["posts"]) == 1


def test_duplicate_input_count_is_distinct_from_existing_cloud(native, tmp_path):
    path = tmp_path / "rich.csv"
    path.write_text("Profile Name,Start Time,Duration,Attributes,Title,Supplemental Video Type,Device Type,Bookmark,Latest Bookmark,Country\n"
                    + "User,2024-01-01 12:00:00,0:01:00,,Movie,,TV,,,US\n" * 2)
    code, env = native["invoke"](path)
    assert code == 0 and env["total"] == 2 and env["duplicate_input"] == 1
    assert env["verified"] == 1 and env["skipped_existing"] is None and len(native["posts"]) == 1


@pytest.mark.parametrize("fault", ["duration", "duplicate"])
def test_real_rich_late_codec_failure_before_new_metadata(native, tmp_path, fault):
    path = tmp_path / "rich.csv"
    header = "Profile Name,Start Time,Duration,Attributes,Title,Supplemental Video Type,Device Type,Bookmark,Latest Bookmark,Country\n"
    first = "User,2024-01-01 12:00:00,0:01:00,,Movie,,TV,,,US\n"
    late = ("User,2024-01-01 12:00:00,-1:00:00,,Later,,TV,,,US\n" if fault == "duration"
            else first.replace(",TV,", ",SECRET,"))
    path.write_text(header + first + late)
    native["absent"] = True
    code, env = native["invoke"](path)
    assert code == 2 and env["errors"][0]["stage"] == "parse"
    assert not any(m == "POST" for m, _ in native["paths"])


def test_late_primary_hold_precedes_excluded_revival_repair(native, tmp_path, ni, monkeypatch):
    from fulcra_netflix.native import build_netflix_record
    path = source(tmp_path)
    rows = [build_netflix_record(e, DEST, "slim") for e in ni.parse_slim(path)]
    exclude(native, rows[0])
    native["rows"][DEST, rows[0]["id"]] = deepcopy(rows[0])
    native["rows"][DEST, rows[-1]["id"]] = {**rows[-1], "note": "mismatch"}
    monkeypatch.setattr(ni, "CHUNK_SIZE", 2)
    code, env = native["invoke"](path)
    assert code == 2 and env["verified"] == env["excluded"] == 0
    assert (DEST, rows[0]["id"]) in native["rows"]
    assert not any(m == "POST" for m, _ in native["paths"])


@pytest.mark.parametrize("location", ["/input/v1/data_type/" + DEST,
                                    "https://unsafe.invalid/input/v1/data_type/" + DEST])
def test_actual_native_creation_303_uses_shared_resolver_proof(native, tmp_path, location):
    native.update(absent=True, create_redirect=location)
    code, env = native["invoke"](source(tmp_path, 1))
    allowed = location.startswith("/")
    assert code == (0 if allowed else 2), env
    assert len(native["posts"]) == (1 if allowed else 0)
    assert env["verified"] == (1 if allowed else 0)
    assert not any("unsafe.invalid" in p for _, p in native["paths"])
