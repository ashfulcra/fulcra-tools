"""Dormant cursor tests: synthetic authority and actual configured writer."""

from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from fulcra_common.client import ImportResult


def test_overlap_and_naive_explicit_are_utc():
    from fulcra_media.poll_cursors import resolve_since

    assert resolve_since(datetime(2026, 1, 1), None, 1) == datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="^poll selection unavailable$"):
        resolve_since(None, datetime.min.replace(tzinfo=timezone.utc), 1)


def test_bounded_credentials_changed_malformed_fifo(tmp_path):
    from fulcra_media.poll_cursors import credential_check
    import os

    path = tmp_path / "source.json"
    path.write_text('{"username":"synthetic","api_key":"secret","unused":1}')
    check = credential_check(path, {"username": "synthetic", "api_key": "secret"})
    assert check()
    path.write_text('{"username":"synthetic","api_key":"secret","unused":2}')
    assert check()
    path.write_text('{"username":"other","api_key":"secret"}')
    assert not check()
    path.write_bytes(b"x" * 65537)
    assert not check()
    path.unlink()
    os.mkfifo(path)
    assert not check()


def test_unknown_scan_and_invalid_result_refuse():
    from fulcra_media.poll_cursors import decide_progress
    from fulcra_media.importers.poll_scan import ScanReport

    with pytest.raises(RuntimeError, match="^source scan failed$"):
        decide_progress(None, None, [], ScanReport(), ImportResult(0, 0, 0, 0), False)


@pytest.fixture
def configured(tmp_path, monkeypatch):
    import base64
    import json
    import re
    import httpx
    from fulcra_common.client import BaseFulcraClient
    from fulcra_common.data_types import CreationJournal
    from fulcra_common.record_exclusions import EXCLUSION_SPEC
    from fulcra_media.media_config import CATEGORIES, FILENAME, configured_media
    from fulcra_media.v1 import media_spec

    profile = tmp_path / "profile"
    account = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    pins = {
        category: "Event/" + str(i) * 8 + "-2222-4333-8444-555555555555"
        for i, category in enumerate(CATEGORIES, 1)
    }
    control = "Event/aaaaaaaa-2222-4333-8444-555555555555"
    claims = (
        base64.urlsafe_b64encode(
            json.dumps({"fulcradynamics.com/userid": account, "exp": 4102444800}).encode()
        )
        .decode()
        .rstrip("=")
    )
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(profile))
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "synthetic." + claims + ".signature")
    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", lambda *_: pytest.fail("external HTTP")
    )
    profile.mkdir()
    CreationJournal(profile / "v1-type-creations.sqlite").resolved(account, EXCLUSION_SPEC, control)
    settings = {
        "version": 1,
        "account_id": account,
        "v1_destination_ids": pins,
        "legacy_selection": dict.fromkeys(CATEGORIES),
    }
    (profile / FILENAME).write_text(json.dumps(settings))
    rows, posts = {}, []
    tags = {}
    state = {"mode": "ok", "account": account}

    def handle(request):
        path = request.url.path
        if path == "/user/v1alpha1/info":
            return httpx.Response(200, json={"userid": state["account"]})
        if path == "/input/v1/data_type":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": full,
                        "fulcra_userid": account,
                        "name": name,
                        "api_version": "v1",
                        "recordable": True,
                        "deprecated": False,
                        "record_spec": {"type": "event"},
                    }
                    for full, name in [
                        *[(pins[c], media_spec(c).name) for c in CATEGORIES],
                        (control, EXCLUSION_SPEC.name),
                    ]
                ],
            )
        if path.endswith("/schema"):
            spec = (
                EXCLUSION_SPEC
                if control in path
                else next(media_spec(c) for c in CATEGORIES if pins[c] in path)
            )
            return httpx.Response(
                200,
                json={
                    "type": "object",
                    "properties": {
                        **{k: {} for k in ("id", "sources", "tags", "start_time", "end_time")},
                        **spec.fields,
                    },
                    "required": list(spec.required_fields),
                },
            )
        if "/tag/name/" in path or "/tag/id/" in path:
            name = path.rsplit("/", 1)[1]
            if "/tag/id/" in path:
                name = next(n for n, identifier in tags.items() if identifier == name)
            identifier = tags.setdefault(name, f"{len(tags)+1:08d}-1111-4111-8111-111111111111")
            return httpx.Response(200, json={"id": identifier, "name": name, "fulcra_userid": account})
        if path == "/data/v1/records":
            if state["mode"] == "unknown-read":
                return httpx.Response(503)
            query = request.url.params["q"]
            full = re.search('__name__="([^"]+)"', query).group(1)
            ids = re.findall('id="([^"]+)"', query)
            return httpx.Response(
                200, text="\n".join(json.dumps(rows[full, i]) for i in ids if (full, i) in rows)
            )
        if path.startswith("/ingest/v1/record/"):
            full = path.removeprefix("/ingest/v1/record/")
            batch = [json.loads(line) for line in request.content.splitlines()]
            posts.extend(batch)
            mode = "unknown" if len(posts) > state.get("unknown_after", float("inf")) else state["mode"]
            if mode == "reject":
                return httpx.Response(400, json={"error": "private rejection"})
            if mode != "unknown":
                rows.update({(full, r["id"]): r for r in batch})
            if mode in ("lost-reply", "unknown"):
                raise httpx.ReadError("private receipt")
            return httpx.Response(200, json={"upload_id": "synthetic"})
        pytest.fail("unexpected synthetic path " + path)

    client = BaseFulcraClient(
        base_url="https://synthetic.invalid", transport=httpx.MockTransport(handle)
    )
    return SimpleNamespace(
        open=lambda **kw: configured_media(tmp_path / "legacy", client=client, **kw),
        profile=profile,
        settings=settings,
        rows=rows,
        posts=posts,
        state=state,
        tmp=tmp_path,
    )


def scan_sample():
    import httpx
    import json
    from fulcra_media.importers import lastfm
    from fulcra_media.importers.poll_scan import ScanReport

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield json.dumps(
                {
                    "recenttracks": {
                        "track": [
                            {
                                "name": "Song",
                                "artist": {"#text": "Artist"},
                                "date": {"uts": "1700000000"},
                            }
                        ],
                        "@attr": {"totalPages": "1"},
                    }
                }
            ).encode()

    report = ScanReport()
    raw = lastfm.fetch_recent_tracks(
        {"username": "synthetic", "api_key": "secret"},
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Stream())),
        strict_scan=True,
        scan_report=report,
    )
    events = list(lastfm.normalize_history(raw, strict_scan=True, scan_report=report))
    return events, report


def binding(configured):
    from fulcra_media.poll_cursors import source_binding

    path = configured.tmp / "creds.json"
    path.write_text('{"username":"synthetic","api_key":"secret"}')
    return source_binding(
        "lastfm", credentials={"username": "synthetic", "api_key": "secret"}, credential_path=path
    )


def test_actual_delivery_cursor_replay_and_preview_share_authority(configured):
    from fulcra_media.poll_cursors import (
        capture_scope,
        read_cursor,
        decide_progress,
        commit_progress,
    )
    from copy import deepcopy
    from fulcra_collect import db

    source = binding(configured)
    events, report = scan_sample()
    with configured.open() as run:
        scope = capture_scope(run, "lastfm", "listened", "lastfm", source)
        assert read_cursor(run, scope) is None
        result = run.import_events(events)
        assert result.posted == result.verified == 1
        decision = decide_progress(scope, None, events, report, result, False)
        with pytest.raises(RuntimeError):
            decision.public()
        decision = commit_progress(run, scope, decision)
        assert decision.public() == {"state": "advanced", "reason": "complete"}
        assert decision.new_watermark == events[0].start_time.isoformat()
        fingerprint = run.session.settings_fingerprint
    assert len(configured.posts) == 1
    with configured.open() as run:
        scope2 = capture_scope(run, "lastfm", "listened", "lastfm", source)
        assert scope2.key == scope.key
        prior = read_cursor(run, scope2)
        events, report = scan_sample()
        result = run.import_events(events)
        assert result.skipped_existing == 1 and result.verified == 0
        decision = commit_progress(
            run, scope2, decide_progress(scope2, prior, events, report, result, False)
        )
        assert decision.public() == {"state": "unchanged", "reason": "no-increase"}
    conn = db.open(configured.profile / "state.db")
    before = conn.iterdump()
    before = "\n".join(before)
    with configured.open(check_only=True) as run:
        settings = deepcopy(run.session.settings)
        epoch = run.session.epoch
        scope3 = capture_scope(run, "lastfm", "listened", "lastfm", source)
        assert scope3.key == scope.key and read_cursor(run, scope3) == prior
        assert run.session.settings_fingerprint != fingerprint
        result = run.import_events(events)
        decision = commit_progress(
            run, scope3, decide_progress(scope3, prior, events, report, result, True)
        )
        assert decision.public() == {"state": "withheld", "reason": "preview"}
        assert run.session.settings == settings and run.session.epoch == epoch
    assert "\n".join(conn.iterdump()) == before
    assert len(configured.posts) == 1


@pytest.mark.parametrize("mode", ["lost-reply", "unknown", "reject"])
def test_actual_delivery_failure_no_cursor_and_recovery_before_source(configured, mode):
    from fulcra_media.poll_cursors import capture_scope, read_cursor

    source = binding(configured)
    events, report = scan_sample()
    configured.state["mode"] = mode
    with pytest.raises(Exception):
        with configured.open() as run:
            scope = capture_scope(run, "lastfm", "listened", "lastfm", source)
            assert read_cursor(run, scope) is None
            run.import_events(events)
    if mode == "lost-reply":
        configured.state["mode"] = "ok"
        with configured.open() as run:
            assert run.ctx.outbox_get("v1_media_pending") == {"groups": []}
            assert (
                read_cursor(run, capture_scope(run, "lastfm", "listened", "lastfm", source)) is None
            )
    else:
        configured.state["mode"] = "unknown-read"
        with pytest.raises(Exception):
            with configured.open():
                pytest.fail("source before failed recovery")


@pytest.mark.parametrize("mode", ["source", "account", "authority", "kv-before", "kv-after"])
def test_commit_fences_and_unknown_ack_replay(configured, monkeypatch, mode):
    from fulcra_media.poll_cursors import (
        capture_scope,
        read_cursor,
        decide_progress,
        commit_progress,
    )
    import json
    from fulcra_media.media_config import FILENAME

    source = binding(configured)
    events, report = scan_sample()
    with configured.open() as run:
        scope = capture_scope(run, "lastfm", "listened", "lastfm", source)
        result = run.import_events(events)
        decision = decide_progress(scope, None, events, report, result, False)
        if mode == "source":
            (configured.tmp / "creds.json").write_text('{"username":"other","api_key":"secret"}')
            assert commit_progress(run, scope, decision).public() == {
                "state": "withheld",
                "reason": "source-changed",
            }
            assert read_cursor(run, scope) is None
        else:
            if mode == "account":
                configured.state["account"] = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            if mode == "authority":
                (configured.profile / FILENAME).write_text(
                    json.dumps(
                        {
                            **configured.settings,
                            "account_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                        }
                    )
                )
            if mode.startswith("kv-"):
                original = run.ctx._plugin_kv_set

                def fail(key, value):
                    if mode == "kv-after":
                        original(key, value)
                    raise OSError("private path and source")

                monkeypatch.setattr(run.ctx, "_plugin_kv_set", fail)
            with pytest.raises(RuntimeError, match="^cursor state unavailable$"):
                commit_progress(run, scope, decision)
            if mode.startswith("kv-"):
                monkeypatch.setattr(run.ctx, "_plugin_kv_set", original)
                assert (read_cursor(run, scope) is not None) == (mode == "kv-after")
        assert result.total == result.posted == result.verified == 1


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"version": True},
        {"version": 1, "scope": {}, "watermark": "2026-01-01"},
        "x" * 4097,
    ],
)
def test_corrupt_cursor_even_with_explicit_since(configured, value):
    from fulcra_media.poll_cursors import capture_scope, read_cursor, resolve_since

    with configured.open() as run:
        scope = capture_scope(run, "lastfm", "listened", "lastfm", binding(configured))
        run.ctx.kv_set(scope.key, value)
        with pytest.raises(RuntimeError, match="^cursor state unavailable$"):
            saved = read_cursor(run, scope)
            resolve_since(datetime(2026, 1, 1), saved, 1)


@pytest.mark.parametrize(
    "reason", ["page-cap", "entry-cap", "normalization-skips", "no-events", "no-increase"]
)
def test_source_changed_precedes_all_nonpreview_success_reasons(configured, reason):
    from fulcra_media.poll_cursors import (
        capture_scope,
        decide_progress,
        commit_progress,
        read_cursor,
    )

    source = binding(configured)
    events, report = scan_sample()
    with configured.open() as run:
        scope = capture_scope(run, "lastfm", "listened", "lastfm", source)
        if reason == "page-cap":
            report.configured_page_cap = True
        if reason == "entry-cap":
            report.configured_entry_cap = True
        if reason == "normalization-skips":
            report.malformed_rows = 1
        if reason == "no-events":
            events = []
        result = run.import_events(events)
        prior = events[0].start_time if reason == "no-increase" else None
        decision = decide_progress(scope, prior, events, report, result, False)
        assert decision.reason == reason
        (configured.tmp / "creds.json").write_text("{}")
        assert commit_progress(run, scope, decision).reason == "source-changed"
        assert read_cursor(run, scope) is None


def test_binding_cannot_be_used_for_another_route(configured):
    from fulcra_media.poll_cursors import capture_scope

    with configured.open() as run:
        with pytest.raises(RuntimeError, match="^cursor state unavailable$"):
            capture_scope(run, "deezer", "listened", "deezer", binding(configured))


def test_all_skipped_actual_result_can_advance_without_cursor(configured):
    from fulcra_media.poll_cursors import (
        capture_scope,
        read_cursor,
        decide_progress,
        commit_progress,
    )

    source = binding(configured)
    events, report = scan_sample()
    with configured.open() as run:
        assert run.import_events(events).verified == 1
    with configured.open() as run:
        scope = capture_scope(run, "lastfm", "listened", "lastfm", source)
        assert read_cursor(run, scope) is None
        result = run.import_events(events)
        assert result.skipped_existing == 1 and result.posted == result.verified == 0
        decision = commit_progress(
            run, scope, decide_progress(scope, None, events, report, result, False)
        )
        assert decision.public() == {"state": "advanced", "reason": "complete"}
    assert len(configured.posts) == 1


@pytest.mark.parametrize("kind", ["extra", "dry_run", "bool-version", "profile", "destination"])
def test_capture_rejects_nonexact_authority(configured, kind, monkeypatch):
    from fulcra_media.poll_cursors import capture_scope

    with configured.open() as run:
        if kind in ("extra", "dry_run"):
            run.session.settings[kind] = False
        if kind == "bool-version":
            run.session.settings["version"] = True
        if kind == "profile":
            monkeypatch.setenv("FULCRA_COLLECT_HOME", str(configured.tmp / "other"))
        if kind == "destination":
            run.writer._destination_ids["listened"] = "Event/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        with pytest.raises(RuntimeError, match="^cursor state unavailable$"):
            capture_scope(run, "lastfm", "listened", "lastfm", binding(configured))


@pytest.mark.parametrize("counter", [True, -1, 0.0, "1"])
def test_rejects_counter_coercion(configured, counter):
    from fulcra_media.poll_cursors import capture_scope, decide_progress

    with configured.open() as run:
        scope = capture_scope(run, "lastfm", "listened", "lastfm", binding(configured))
        events, report = scan_sample()
        with pytest.raises(RuntimeError, match="^cursor state unavailable$"):
            decide_progress(
                scope, None, events, report, ImportResult(1, 0, counter, counter), False
            )


def test_scope_and_envelope_privacy_exact_keys_and_mismatch(configured):
    from fulcra_media.poll_cursors import (
        capture_scope,
        read_cursor,
        decide_progress,
        commit_progress,
    )
    from fulcra_media.poll_cursors import source_binding
    import json

    source = binding(configured)
    events, report = scan_sample()
    with configured.open() as run:
        scope = capture_scope(run, "lastfm", "listened", "lastfm", source)
        result = run.import_events(events)
        commit_progress(run, scope, decide_progress(scope, None, events, report, result, False))
        envelope = run.ctx.kv_get(scope.key)
        assert set(envelope) == {"version", "scope", "watermark"}
        assert set(envelope["scope"]) == {
            "authority_fingerprint",
            "profile_digest",
            "account_id",
            "destination_id",
            "importer",
            "category",
            "service_digest",
            "source_binding_digest",
        }
        assert len(json.dumps(envelope).encode()) <= 4096
        assert all(
            s not in json.dumps(envelope) for s in ("synthetic", "secret", str(configured.profile))
        )
        envelope["scope"]["service_digest"] = "0" * 64
        run.ctx.kv_set(scope.key, envelope)
        with pytest.raises(RuntimeError, match="^cursor state unavailable$"):
            read_cursor(run, scope)
    assert (
        source_binding(
            "lastfm",
            credentials={"username": "synthetic", "api_key": "rotated"},
            credential_path=configured.tmp / "creds.json",
        ).digest
        == source.digest
    )
    assert (
        source_binding(
            "deezer",
            credentials={"access_token": "first"},
            credential_path=configured.tmp / "creds.json",
        ).digest
        != source_binding(
            "deezer",
            credentials={"access_token": "second"},
            credential_path=configured.tmp / "creds.json",
        ).digest
    )


def test_credential_guard_symlink_race_duplicate_json_and_exact_byte_limit(tmp_path, monkeypatch):
    from fulcra_media.poll_cursors import credential_check
    import os

    path = tmp_path / "creds"
    target = tmp_path / "target"
    target.write_text('{"token":"synthetic"}')
    check = credential_check(path, {"token": "synthetic"})
    path.symlink_to(target)
    assert not check()
    path.unlink()
    path.write_text('{"token":"wrong","token":"synthetic"}')
    assert not check()
    prefix = b'{"token":"synthetic","padding":"'
    suffix = b'"}'
    path.write_bytes(prefix + b"x" * (65536 - len(prefix) - len(suffix)) + suffix)
    assert check()
    path.write_bytes(path.read_bytes() + b" ")
    assert not check()
    path.write_text('{"token":"synthetic"}')
    original = os.open

    def substituted(file, flags, *args, **kwargs):
        if file == path:
            path.unlink()
            os.mkfifo(path)
        return original(file, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", substituted)
    assert not check()


def test_copied_profile_cannot_adopt_original_scope(configured, monkeypatch):
    from fulcra_media.poll_cursors import (
        capture_scope,
        read_cursor,
        decide_progress,
        commit_progress,
    )
    import shutil

    source = binding(configured)
    events, report = scan_sample()
    with configured.open() as run:
        original = capture_scope(run, "lastfm", "listened", "lastfm", source)
        result = run.import_events(events)
        commit_progress(
            run, original, decide_progress(original, None, events, report, result, False)
        )
        envelope = run.ctx.kv_get(original.key)
    copied = configured.tmp / "copied"
    shutil.copytree(configured.profile, copied)
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(copied))
    with configured.open() as run:
        scope = capture_scope(run, "lastfm", "listened", "lastfm", source)
        assert scope.key != original.key and read_cursor(run, scope) is None
        run.ctx.kv_set(scope.key, envelope)
        with pytest.raises(RuntimeError, match="^cursor state unavailable$"):
            read_cursor(run, scope)
