"""Real CLI/daemon strict source acquisition before guarded native delivery.

All legacy file, replacement keychain, Fulcra authentication and HTTP seams
are synthetic; the root offline guard is an additional fence, not auth setup.
"""

from copy import deepcopy
import json
import logging
import os
from types import SimpleNamespace

import httpx
import pytest
from click.testing import CliRunner

from test_poll_cursors import configured as configured  # noqa: F401
from test_trakt_strict_scan import metadata
from fulcra_media.importers import trakt


def history(identity):
    return {
        "id": identity,
        "watched_at": f"2026-01-{identity:02}T00:00:00Z",
        "action": "scrobble",
        "type": "movie",
        "movie": {"title": "Synthetic", "year": 2020, "runtime": 80, "ids": {"trakt": identity}},
    }


@pytest.fixture(params=["cli", "keychain"])
def caller(request, configured, monkeypatch):
    from fulcra_collect import config, credentials, db, state
    from fulcra_collect.plugin import RunContext
    from fulcra_collect.v1_session import CollectRecordSession
    from fulcra_media import collect_v1, media_config
    from fulcra_media.cli import cli
    from fulcra_media.plugins import trakt as plugin
    from fulcra_media.state import State

    mode = request.param
    trace = []
    info = {"source": "missing", "trace": trace, "gets": [], "rotated": [], "refresh": "ok"}
    # Both old file/OOB auth and replacement ctx/keychain path are fenced before
    # any invocation; expiry and refresh tests use only this temporary file.
    creds = configured.tmp / "synthetic-trakt.json"
    creds.write_text(
        json.dumps(
            {
                "client_id": "synthetic-id",
                "client_secret": "SECRET",
                "access_token": "synthetic-token",
                "refresh_token": "synthetic-refresh",
                "created_at": 4102444800,
                "expires_in": 86400,
            }
        )
    )
    monkeypatch.setattr(trakt, "CREDS_PATH", creds)
    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", lambda *_: pytest.fail("external HTTP")
    )
    monkeypatch.setattr(
        credentials, "get_user_secret_fresh", lambda _: os.environ["FULCRA_ACCESS_TOKEN"]
    )
    monkeypatch.setattr(plugin, "_state_load", lambda _: State())
    with configured.open() as run:
        native = run.session.authority._transport
    monkeypatch.setattr(
        collect_v1, "CollectRecordSession", lambda ctx: CollectRecordSession(ctx, transport=native)
    )
    monkeypatch.setattr(media_config, "configured_media", lambda *a, **kw: configured.open(**kw))
    cfg = config.load()
    settings = {
        "v1_destination_ids": {"watched": configured.settings["v1_destination_ids"]["watched"]},
        "clusters": "keep",
        "twin_policy": "keep",
    }
    cfg.enable("trakt")
    cfg.update_plugin_settings("trakt", settings)
    config.save(cfg)
    kv, emitted = {}, []
    source_creds = {
        "client_id": "synthetic-id",
        "client_secret": "SECRET",
        "access_token": "synthetic-token",
        "refresh_token": "synthetic-refresh",
    }

    def persist(key, value):
        trace.append(("persist", key))
        info["rotated"].append((key, value))

    def get_secret(plugin_id, key):
        assert plugin_id == "trakt"
        return source_creds.get(key)
    def put_secret(plugin_id, key, value):
        assert plugin_id == "trakt"
        source_creds[key] = value
        if key in {"access_token", "refresh_token"}:
            persist(key, value)
    monkeypatch.setattr(credentials, "get_secret", get_secret)
    monkeypatch.setattr(credentials, "authorize_secret", lambda p, k: bool(get_secret(p, k)))
    monkeypatch.setattr(credentials, "set_secret", put_secret)
    monkeypatch.setattr(credentials, "delete_secret", lambda plugin_id, key: source_creds.pop(key, None))
    from fulcra_collect.trakt_source import Owner, KeychainStore
    original_init = KeychainStore.__init__
    def synthetic_init(self, *, _quiet_write=None):
        def quiet(key, value):
            if value is None:
                source_creds.pop(key, None)
            else:
                put_secret('trakt', key, value)
        original_init(self, _quiet_write=_quiet_write or quiet)
    monkeypatch.setattr(KeychainStore, '__init__', synthetic_init)
    ctx = RunContext(
        plugin_id="trakt",
        config=deepcopy(settings),
        config_epoch=cfg.plugin_epochs["trakt"],
        credentials=source_creds if mode == "keychain" else {},
        state=state.PluginState("trakt"),
        log=logging.getLogger("synthetic"),
        _emit=emitted.append,
        _set_credential=persist,
        _trakt_source_owner=Owner(KeychainStore()),
    )
    ctx.state.watermark = "2020-01-01T00:00:00Z"
    monkeypatch.setattr(ctx, "kv_get", lambda k, default=None: deepcopy(kv.get(k, default)))
    monkeypatch.setattr(ctx, "kv_set", lambda k, v: kv.__setitem__(k, deepcopy(v)))
    ctx._record_outbox_get = lambda k, d: db.get_record_outbox(db.open(), "trakt", k, d)
    ctx._record_outbox_set = lambda k, v: db.set_record_outbox(db.open(), "trakt", k, v)
    source_client = httpx.Client

    def source(req):
        if req.url.path == "/users/settings":
            probes = info.setdefault("probes", [])
            probes.append(req)
            info.get("during_probe", lambda: None)()
            samples = info.get("identities", ["Opaque Synthetic / a"])
            sample = samples[min(len(probes) - 1, len(samples) - 1)]
            if type(sample) is int:
                return httpx.Response(sample, stream=httpx.ByteStream(b"synthetic error"))
            body = {} if sample is None else {"user": {"ids": {"uuid": sample}}}
            return httpx.Response(200, stream=httpx.ByteStream(json.dumps(body).encode()))
        page = int(req.url.params["page"])
        info["gets"].append(req)
        trace.append(("get", req.headers["Authorization"], page))
        info.get("before_source", lambda: None)()
        name = info["source"]
        if name in ("encoded-401", "huge-401"):
            return httpx.Response(
                401,
                headers={"Content-Encoding": "gzip"} if name == "encoded-401" else {},
                stream=httpx.ByteStream(b"SECRET" * 10),
            )
        if name == "empty":
            return httpx.Response(
                200,
                headers=metadata(total=0, pages=info["empty_pages"]),
                stream=httpx.ByteStream(b"[]"),
            )
        if name == "401" or (
            name.startswith("refresh")
            and req.headers["Authorization"] == "Bearer synthetic-token"
            and page == 2
        ):
            return httpx.Response(401, stream=httpx.ByteStream(b"SECRET"))
        if name in ("403", "429", "500"):
            return httpx.Response(int(name), stream=httpx.ByteStream(b"SECRET"))
        headers = {} if name == "missing" else metadata(page=page, total=3)
        rows = [history(1), history(2)] if page == 1 else [history(3)]
        if name == "three":
            headers = metadata(page=page, total=5, pages=3)
            rows = (
                [history(1), history(2)]
                if page == 1
                else ([history(3), history(4)] if page == 2 else [history(5)])
            )
        if name == "empty-last" and page == 2:
            rows = []
        if name == "normalize" and page == 2:
            rows[0]["watched_at"] = "SECRET"
        if name == "late-fail" and page == 2:
            return httpx.Response(503, stream=httpx.ByteStream(b"SECRET"))
        return httpx.Response(
            200, headers=headers, stream=httpx.ByteStream(json.dumps(rows).encode())
        )

    def source_factory(**kw):
        kw["transport"] = httpx.MockTransport(source)
        return source_client(**kw)

    monkeypatch.setattr(
        trakt,
        "httpx",
        SimpleNamespace(Client=source_factory, HTTPStatusError=httpx.HTTPStatusError),
    )
    from fulcra_collect import trakt_identity
    monkeypatch.setattr(trakt_identity, 'httpx',
        SimpleNamespace(Client=source_factory, HTTPStatusError=httpx.HTTPStatusError))

    def auth(req):
        trace.append(("grant",))
        assert req.url.host == "auth.trakt.tv"
        info["grant_timeout"] = req.extensions["timeout"]["read"]
        assert 0 < info["grant_timeout"] <= 30
        info.get("during_grant", lambda: None)()
        return httpx.Response(
            400 if info["refresh"] == "reject" else 200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 86400,
                "created_at": 4102444800,
            },
        )

    def post(url, **kw):
        with source_client(transport=httpx.MockTransport(auth)) as client:
            return client.post(url, **kw)

    monkeypatch.setattr(httpx, "post", post)
    # TraktAuth shares the module facade with fetch; explicitly fence its POST.
    trakt.httpx.post = post
    original = collect_v1.MediaRecordWriter.run_import

    def observed(writer, events, **kw):
        events = list(events)
        trace.append(("current-writer", len(events)))
        return original(writer, events, **kw)

    monkeypatch.setattr(collect_v1.MediaRecordWriter, "run_import", observed)

    def invoke():
        if mode == "cli":
            result = CliRunner().invoke(
                cli, ["import", "trakt", "--json", "--clusters", "keep", "--twin-policy", "keep"]
            )
            return result, json.loads(result.stdout)
        plugin.PLUGIN.run(ctx)
        return None, None

    def actual_worker():
        import io
        from fulcra_collect import worker
        monkeypatch.setattr(worker.state, 'load', lambda _: ctx.state)
        monkeypatch.setattr(worker, '_plugin_kv_get', lambda p, k, d: deepcopy(kv.get(k, d)))
        monkeypatch.setattr(worker, '_plugin_kv_set', lambda p, k, v: kv.__setitem__(k, deepcopy(v)))
        out = io.StringIO()
        outcome = worker.run_plugin(plugin.PLUGIN, out=out)
        return outcome, [json.loads(line) for line in out.getvalue().splitlines()]

    return SimpleNamespace(
        worker=actual_worker,
        native=native,
        source_creds=source_creds,
        mode=mode,
        info=info,
        ctx=ctx,
        kv=kv,
        emitted=emitted,
        configured=configured,
        invoke=invoke,
        creds=creds,
    )


@pytest.mark.parametrize("failure", ["missing", "empty-last", "late-fail", "normalize"])
def test_actual_callers_hold_incomplete_source_before_current_writer(caller, failure):
    caller.info["source"] = failure
    if caller.mode == "cli":
        result, data = caller.invoke()
        assert result.exit_code == 2, result.output
        assert data["errors"][0]["stage"] == "fetch"
        assert data["total"] == data["posted"] == data["verified"] == 0
        assert data["cursor"] is None
        assert "SECRET" not in result.output and "synthetic-token" not in result.output
    else:
        with pytest.raises((RuntimeError, ValueError)):
            caller.invoke()
    assert caller.configured.posts == []
    assert not any(row[0] == "current-writer" for row in caller.info["trace"])
    assert not any(key.startswith("v1_media_cursor:") for key in caller.kv)
    assert caller.ctx.state.watermark == "2020-01-01T00:00:00Z"


def test_actual_callers_complete_source_then_native_writer_and_replay(caller):
    caller.info["source"] = "three"
    result, data = caller.invoke()
    if data:
        assert result.exit_code == 0, result.output
        assert data["posted"] == data["verified"] == 5 and data["cursor"] is None
    trace = caller.info["trace"]
    assert [(row[0], row[-1]) for row in trace] == [
        ("get", 1),
        ("get", 2),
        ("get", 3),
        ("current-writer", 5),
    ]
    assert len(caller.configured.posts) == 5
    assert {row["external_ids"]["trakt_history_id"] for row in caller.configured.posts} == {
        1,
        2,
        3,
        4,
        5,
    }
    assert all(
        row["sources"][0].startswith("com.fulcra.media.trakt.v1.history.")
        for row in caller.configured.posts
    )
    if caller.mode != "cli":
        assert any(key.startswith("v1_media_cursor:") for key in caller.kv)
    caller.invoke()
    assert len(caller.configured.posts) == 5  # unchanged IDs are verified replays


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_actual_auth_stage_only_bounded_401_refreshes(caller, status):
    caller.info["source"] = str(status)
    if caller.mode == "cli":
        result, data = caller.invoke()
        assert result.exit_code == 2
        assert data["errors"][0]["stage"] == ("auth" if status in (401, 403) else "fetch")
        assert "SECRET" not in result.output
    else:
        with pytest.raises((RuntimeError, httpx.HTTPStatusError)):
            caller.invoke()
    assert sum(t[0] == "grant" for t in caller.info["trace"]) == int(
        caller.mode == "keychain" and status == 401
    )
    assert len(caller.info["gets"]) == (2 if caller.mode == "keychain" and status == 401 else 1)
    assert caller.configured.posts == []


@pytest.mark.parametrize(
    "cap,exact", [("MAX_REQUESTS", 7), ("MAX_ROWS", 5), ("MAX_TOTAL_BYTES", None)]
)
@pytest.mark.parametrize("over", [False, True])
def test_keychain_restart_shares_cumulative_bounds(caller, monkeypatch, cap, exact, over):
    if caller.mode != "keychain":
        pytest.skip("reactive keychain path")
    caller.info["source"] = "refresh"
    if exact is None:
        first = len(json.dumps([history(1), history(2)]).encode())
        last = len(json.dumps([history(3)]).encode())
        identity_bytes = len(json.dumps({"user": {"ids": {"uuid": "Opaque Synthetic / a"}}}).encode())
        exact = first * 2 + last + len(b"SECRET") + 3 * identity_bytes
    monkeypatch.setattr(trakt, cap, exact - int(over))
    # Keep requested limit independent from injected resource limits.
    if cap == "MAX_ROWS":
        original = trakt.fetch_history_with_headers

        def small(headers, **kw):
            return original(headers, per_page=2, **kw)

        monkeypatch.setattr(trakt, "fetch_history_with_headers", small)
    if over:
        with pytest.raises(RuntimeError, match="^(source scan failed|Trakt source identity unavailable; preserve pending work)$"):
            caller.invoke()
        assert caller.configured.posts == []
        assert not any(k.startswith("v1_media_cursor:") for k in caller.kv)
    else:
        caller.invoke()
        assert len(caller.configured.posts) == 3
    trace = caller.info["trace"]
    assert trace[:5] == [
        ("get", "Bearer synthetic-token", 1),
        ("get", "Bearer synthetic-token", 2),
        ("grant",),
        ("persist", "access_token"),
        ("persist", "refresh_token"),
    ]
    assert caller.info["rotated"] == [
        ("access_token", "new-access"),
        ("refresh_token", "new-refresh"),
    ]
    assert sum(t[0] == "grant" for t in trace) == 1
    assert [r.url.params["page"] for r in caller.info["gets"]][:3] == ["1", "2", "1"]


@pytest.mark.parametrize("failure", ["reject", "late", "second-401"])
def test_keychain_grant_failure_late_rotation_and_no_second_refresh(caller, monkeypatch, failure):
    if caller.mode != "keychain":
        pytest.skip("reactive keychain path")
    caller.info["source"] = "401" if failure == "second-401" else "refresh"
    if failure == "reject":
        caller.info["refresh"] = "reject"
    if failure == "late":
        clock = [0.0]
        monkeypatch.setattr(trakt.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(trakt, "SCAN_SECONDS", 5)
        caller.info["during_grant"] = lambda: clock.__setitem__(0, 6)
    with pytest.raises((RuntimeError, httpx.HTTPStatusError)):
        caller.invoke()
    assert sum(t[0] == "grant" for t in caller.info["trace"]) == 1
    assert len(caller.info["rotated"]) == (0 if failure == "reject" else 2)
    assert caller.configured.posts == []
    assert len(caller.info["gets"]) == (2 if failure != "second-401" else 2)
    assert not any(k.startswith("v1_media_cursor:") for k in caller.kv)


@pytest.mark.parametrize("late", [False, True])
def test_proactive_file_refresh_persists_rotated_pair_before_deadline_check(
    caller, monkeypatch, late
):
    if caller.mode == "keychain":
        pytest.skip("legacy proactive auth path")
    caller.info["source"] = "ok"
    saved = json.loads(caller.creds.read_text())
    saved["created_at"] = 0
    caller.creds.write_text(json.dumps(saved))
    clock = [0.0]
    monkeypatch.setattr(trakt.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(trakt, "SCAN_SECONDS", 5)
    caller.info["during_grant"] = lambda: clock.__setitem__(0, 6 if late else 1)
    if late and caller.mode == "legacy":
        with pytest.raises(RuntimeError, match="^source scan failed$"):
            caller.invoke()
    else:
        result, data = caller.invoke()
        if data:
            assert result.exit_code == (2 if late else 0)
    saved = json.loads(caller.creds.read_text())
    assert (saved["access_token"], saved["refresh_token"]) == ("new-access", "new-refresh")
    assert caller.info["grant_timeout"] == 5
    assert len(caller.info["gets"]) == (0 if late else 2)
    if not late:
        assert all(req.extensions["timeout"]["read"] == 4 for req in caller.info["gets"])
    assert len(caller.configured.posts) == (0 if late else 3)


@pytest.mark.parametrize("resolved", [False, True])
@pytest.mark.parametrize("current_failure", ["history", "identity"])
def test_pending_original_recovery_precedes_new_source_failure(caller, resolved, current_failure):
    from fulcra_collect import db
    from fulcra_media.collect_v1 import MediaRecordWriter
    from fulcra_media import collect_v1

    pending = list(trakt.normalize_history([history(7)]))
    caller.configured.state["mode"] = "unknown"
    with pytest.raises(Exception):
        if caller.mode == "cli":
            with caller.configured.open() as run:
                run.import_events(pending)
        else:
            with collect_v1.CollectRecordSession(caller.ctx) as session:
                MediaRecordWriter(caller.ctx, session).run_import(pending)
    originals = deepcopy(caller.configured.posts)
    assert len(originals) == 1
    caller.configured.state["mode"] = "ok" if resolved else "unknown"
    destination = caller.configured.settings["v1_destination_ids"]["watched"]
    if resolved:
        caller.configured.rows.update(
            {(destination, row["id"]): deepcopy(row) for row in originals}
        )
    plugin_id = "cli-media" if caller.mode == "cli" else "trakt"
    conn = db.open(caller.configured.profile / "state.db")
    assert db.get_record_outbox(conn, plugin_id, "v1_media_pending")["groups"]

    def recovered():
        assert db.get_record_outbox(conn, plugin_id, "v1_media_pending") == {"groups": []}

    caller.info["before_source"] = (
        recovered if resolved else lambda: pytest.fail("source before recovery")
    )
    caller.info["source"] = "empty-last"
    if current_failure == "identity":
        caller.info["identities"] = [None]
        caller.info["during_probe"] = recovered if resolved else lambda: pytest.fail("identity before recovery")
    caller.info["trace"].clear()
    if caller.mode == "cli":
        result, data = caller.invoke()
        assert result.exit_code == 2
        assert data["total"] == data["posted"] == data["verified"] == 0
    else:
        with pytest.raises(Exception):
            caller.invoke()
    if not resolved:
        assert caller.info["trace"] == []
        assert db.get_record_outbox(conn, plugin_id, "v1_media_pending")["groups"]
        assert not any(k.startswith("v1_media_cursor:") for k in caller.kv)
        return
    assert caller.configured.posts == originals  # readback recovery, no new POST
    assert not any(t[0] == "current-writer" for t in caller.info["trace"])
    assert not any(k.startswith("v1_media_cursor:") for k in caller.kv)


@pytest.mark.parametrize("pages", [0, 1])
def test_actual_callers_explicit_empty_is_success_without_cursor(caller, pages):
    caller.info.update(source="empty", empty_pages=pages)
    result, data = caller.invoke()
    if data:
        assert result.exit_code == 0 and data["total"] == 0 and data["cursor"] is None
    assert caller.configured.posts == []
    assert not any(k.startswith("v1_media_cursor:") for k in caller.kv)
    assert len(caller.info["gets"]) == 1


@pytest.mark.parametrize("failure", ["encoded-401", "huge-401"])
def test_actual_keychain_unbounded_or_encoded_error_never_refreshes(caller, monkeypatch, failure):
    if caller.mode != "keychain":
        pytest.skip("reactive keychain path")
    monkeypatch.setattr(trakt, "MAX_RESPONSE_BYTES", 32)
    caller.info["source"] = failure
    with pytest.raises(RuntimeError, match="^source scan failed$"):
        caller.invoke()
    assert caller.info["trace"] == [("get", "Bearer synthetic-token", 1)]
    assert caller.info["rotated"] == [] and caller.configured.posts == []


@pytest.mark.parametrize("failure", ["reject", "unknown"])
def test_complete_source_writer_failure_keeps_pending_and_withholds_cursor(caller, failure):
    from fulcra_collect import db

    caller.info["source"] = "ok"
    caller.configured.state["mode"] = failure
    if caller.mode == "cli":
        result, data = caller.invoke()
        assert result.exit_code == 2
        assert data["errors"][0]["stage"] == "import"
        assert data["total"] == 3 and data["posted"] == data["verified"] == 0
        assert data["cursor"] is None
    else:
        with pytest.raises(Exception):
            caller.invoke()
    assert [(row[0], row[-1]) for row in caller.info["trace"]] == [
        ("get", 1),
        ("get", 2),
        ("current-writer", 3),
    ]
    assert len(caller.configured.posts) == 3
    plugin_id = "cli-media" if caller.mode == "cli" else "trakt"
    conn = db.open(caller.configured.profile / "state.db")
    pending = db.get_record_outbox(conn, plugin_id, "v1_media_pending")
    assert pending["groups"]
    assert not any(k.startswith("v1_media_cursor:") for k in caller.kv)


def test_actual_worker_complete_then_source_changed_after_page_one(caller):
    if caller.mode != 'keychain':
        pytest.skip('ordinary daemon Keychain entry')
    caller.info['source'] = 'ok'
    def revoke():
        caller.source_creds['access_token'] = 'different-explicit-selection'
    caller.info['before_source'] = revoke
    outcome, output = caller.worker()
    assert outcome == 'error'
    assert output[-1]['outcome'] == 'error'
    assert len(caller.info['gets']) == 1
    assert caller.configured.posts == []
    assert not any(k.startswith('v1_media_cursor:') for k in caller.kv)


@pytest.mark.parametrize('field', ['client_id', 'access_token', 'refresh_token', 'client_secret', 'redirect_uri', 'created_at', 'expires_in'])
@pytest.mark.parametrize('kind', ['missing', 'unknown'])
def test_actual_worker_missing_or_unknown_never_selects_legacy(caller, monkeypatch, field, kind):
    if caller.mode != 'keychain':
        pytest.skip('ordinary daemon Keychain entry')
    from fulcra_collect import credentials
    caller.info['source'] = 'ok'
    if kind == 'missing':
        caller.source_creds.pop(field, None)
        if field in ('client_secret', 'redirect_uri', 'created_at', 'expires_in'):
            outcome, _ = caller.worker()
            assert outcome == 'done'  # confirmed optional absence retains old OOB contract
            return
    else:
        original = credentials.get_secret
        def unknown(plugin, key):
            if key == field:
                raise TimeoutError('synthetic UNKNOWN')
            return original(plugin, key)
        monkeypatch.setattr(credentials, 'get_secret', unknown)
    outcome, _ = caller.worker()
    assert outcome == 'error'
    assert caller.info['gets'] == [] and caller.configured.posts == []
    assert caller.info['rotated'] == []


@pytest.mark.parametrize('point', ['page-one', 'normalized', 'before-import'])
def test_actual_file_source_replacement_holds(caller, monkeypatch, point):
    if caller.mode != 'cli':
        pytest.skip('explicit CLI file selection')
    caller.info['source'] = 'ok'
    def revoke():
        value = json.loads(caller.creds.read_text())
        value['access_token'] = 'new-explicit-source'
        caller.creds.write_text(json.dumps(value))
    if point == 'page-one':
        caller.info['before_source'] = revoke
    elif point == 'normalized':
        original = trakt.normalize_history
        def normalized(*args, **kwargs):
            yield from original(*args, **kwargs)
            revoke()
        monkeypatch.setattr(trakt, 'normalize_history', normalized)
    else:
        from fulcra_media import cli as media_cli
        original = media_cli._resolve_cluster_policy
        def prompt(*args, **kwargs):
            result = original(*args, **kwargs)
            revoke()
            return result
        monkeypatch.setattr(media_cli, '_resolve_cluster_policy', prompt)
    result, data = caller.invoke()
    assert result.exit_code == 2
    assert data['posted'] == data['verified'] == 0
    assert caller.configured.posts == []
    if point == 'page-one':
        assert len(caller.info['gets']) == 1


@pytest.mark.parametrize('point', ['before-receipt', 'between-chunks'])
def test_actual_current_writer_rechecks_source_at_later_mutations(caller, monkeypatch, point):
    from fulcra_media import collect_v1
    from fulcra_collect.v1_outbox import V1RecordOutbox
    caller.info['source'] = 'ok'
    revoked = []
    def revoke():
        if revoked:
            return
        revoked.append(True)
        if caller.mode == 'keychain':
            caller.source_creds['access_token'] = 'revoked-during-write'
        else:
            values = json.loads(caller.creds.read_text())
            values['access_token'] = 'revoked-during-write'
            caller.creds.write_text(json.dumps(values))
    if point == 'before-receipt':
        original = caller.native.handle_request
        def transport(request):
            response = original(request)
            if request.method == 'POST' and request.url.path.startswith('/ingest/'):
                revoke()
            return response
        monkeypatch.setattr(caller.native, 'handle_request', transport)
    else:
        original_import = collect_v1.MediaRecordWriter.run_import
        def small(writer, events, **kwargs):
            kwargs['chunk_size'] = 1
            return original_import(writer, events, **kwargs)
        monkeypatch.setattr(collect_v1.MediaRecordWriter, 'run_import', small)
        original_drain = V1RecordOutbox.drain
        def drained(outbox):
            count = original_drain(outbox)
            if count:
                revoke()
            return count
        monkeypatch.setattr(V1RecordOutbox, 'drain', drained)
    if caller.mode == 'cli':
        result, data = caller.invoke()
        assert result.exit_code == 2
        assert data['posted'] == data['verified'] == int(point == 'between-chunks')
    else:
        outcome, _ = caller.worker()
        assert outcome == 'error'
    assert revoked
    assert len(caller.configured.posts) == (3 if point == 'before-receipt' else 1)
    assert not any(k.startswith('v1_media_cursor:') for k in caller.kv)
    assert not any(event.get('type') == 'annotation' for event in caller.emitted) if point == 'before-receipt' else True


def test_actual_worker_revocation_after_plugin_return_blocks_final_cursor(caller, monkeypatch):
    if caller.mode != 'keychain':
        pytest.skip('daemon final cursor boundary')
    from fulcra_media.plugins import trakt as plugin
    caller.info['source'] = 'ok'
    original = plugin.report_import
    def reported(*args, **kwargs):
        original(*args, **kwargs)
        caller.source_creds['access_token'] = 'new-selection-after-import'
    monkeypatch.setattr(plugin, 'report_import', reported)
    outcome, output = caller.worker()
    assert outcome == 'error'
    assert len(caller.configured.posts) == 3
    assert not any(k.startswith('v1_media_cursor:') for k in caller.kv)
    assert output[-1]['watermark'] == '2020-01-01T00:00:00Z'


def test_actual_cli_prompt_revocation_prevents_current_import(caller, monkeypatch):
    if caller.mode != 'cli':
        pytest.skip('interactive file invocation')
    from fulcra_media.cli import cli
    import click
    caller.info['source'] = 'ok'
    original_stream = click.get_text_stream
    monkeypatch.setattr(click, 'get_text_stream', lambda name: SimpleNamespace(isatty=lambda: True)
                        if name == 'stdin' else original_stream(name))
    prompted = []
    def prompt(*args, **kwargs):
        prompted.append(True)
        values = json.loads(caller.creds.read_text())
        values['access_token'] = 'changed-during-prompt'
        caller.creds.write_text(json.dumps(values))
        return 'keep'
    monkeypatch.setattr(click, 'prompt', prompt)
    result = CliRunner().invoke(cli, ['import', 'trakt', '--clusters', 'ask',
                                     '--cluster-threshold', '1', '--twin-policy', 'keep'])
    assert result.exit_code == 2 and prompted
    assert caller.configured.posts == []


def test_actual_worker_captured_profile_cannot_change_during_hydration(caller, monkeypatch):
    if caller.mode != 'keychain':
        pytest.skip('ordinary daemon capture')
    from fulcra_collect import config, credentials
    caller.info['source'] = 'ok'
    original = credentials.get_secret
    selected = config.profile_path()
    other = caller.configured.tmp / 'copied-profile'
    other.mkdir()
    (other / 'config.toml').write_bytes((selected / 'config.toml').read_bytes())
    changed = []
    def sample(plugin, key):
        result = original(plugin, key)
        if not changed:
            changed.append(True)
            monkeypatch.setenv('FULCRA_COLLECT_HOME', str(other))
        return result
    monkeypatch.setattr(credentials, 'get_secret', sample)
    outcome, _ = caller.worker()
    assert outcome == 'error'
    assert caller.info['gets'] == []
    assert caller.configured.posts == []


def test_actual_worker_expired_budget_during_secret_sample_never_claims_grant(caller, monkeypatch):
    if caller.mode != 'keychain':
        pytest.skip('reactive worker grant')
    from fulcra_collect import credentials, trakt_source
    caller.info['source'] = 'refresh'
    clock = [0.0]
    monkeypatch.setattr(trakt.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(trakt, 'SCAN_SECONDS', 5)
    original = credentials.get_secret
    def sample(plugin, key):
        result = original(plugin, key)
        if key == 'refresh_token' and ('get', 'Bearer synthetic-token', 2) in caller.info['trace']:
            clock[0] = 6
        return result
    monkeypatch.setattr(credentials, 'get_secret', sample)
    outcome, _ = caller.worker()
    assert outcome == 'error'
    assert not any(item[0] == 'grant' for item in caller.info['trace'])
    journal = trakt_source._read_json(caller.ctx._trakt_source_owner.root / 'journal.json')
    assert journal['consumed'] == {}


def test_round1_actual_worker_cannot_restart_with_other_profile_successor(caller, monkeypatch):
    if caller.mode != 'keychain':
        pytest.skip('ordinary worker shared Keychain source')
    from fulcra_collect import config, trakt_source as source
    caller.info['source'] = 'refresh'
    original = source.Owner._publish
    def published_then_superseded(owner, *args):
        result = original(owner, *args)
        with monkeypatch.context() as selected:
            selected.setenv('FULCRA_COLLECT_HOME', str(caller.configured.tmp / 'other-profile'))
            cfg = config.load()
            cfg.enable('trakt')
            config.save(cfg)
            owner.edit('access_token', 'new-consent-a')
            assert owner.edit('refresh_token', 'new-consent-r')['ready'] is True
        return result
    monkeypatch.setattr(source.Owner, '_publish', published_then_superseded)
    outcome, _ = caller.worker()
    assert outcome == 'error'
    assert len(caller.info['gets']) == 2
    assert caller.source_creds['access_token'] == 'new-consent-a'
    assert caller.configured.posts == []
    assert not any(k.startswith('v1_media_cursor:') for k in caller.kv)
