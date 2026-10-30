"""Explicit standalone authority must never acquire daemon authority."""

import httpx
import pytest

from fulcra_common.client import BaseFulcraClient

ACCOUNT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def test_standalone_capability_is_pure_and_uses_runtime_allowlist(tmp_path, monkeypatch):
    from fulcra_collect import standalone_v1
    from fulcra_collect import db

    profile = tmp_path / "never-created"
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(profile))
    def forbidden(*args, **kwargs):
        pytest.fail("capability touched auth or profile")
    monkeypatch.setattr(db, "open", forbidden)
    monkeypatch.setattr(BaseFulcraClient, "get_token", forbidden)
    assert type(standalone_v1.STANDALONE_RUNTIME_VERSION) is int
    assert standalone_v1.STANDALONE_RUNTIME_VERSION == 1
    for name in ("cli-dayone", "cli-media", "cli-csv", "cli-labs", "cli-netflix"):
        assert standalone_v1.standalone_namespace_supported(name) is True
    for name in ("netflix", "cli-unknown", None, [], True):
        assert standalone_v1.standalone_namespace_supported(name) is False
    assert not profile.exists()


def runtime(tmp_path, monkeypatch, responder=None, **kwargs):
    from fulcra_collect.standalone_v1 import standalone_runtime

    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path / "profile"))
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "first")

    def handle(request):
        if request.url.path == "/user/v1alpha1/info":
            return httpx.Response(200, json={"userid": ACCOUNT})
        return responder(request) if responder else httpx.Response(200, json={})

    transport = httpx.MockTransport(handle)
    explicit = BaseFulcraClient(base_url="https://example.test", transport=transport)
    return standalone_runtime(
        "cli-dayone",
        {"source": "/fixed/source"},
        client=explicit,
        transport=transport,
        identity_transport=transport,
        **kwargs,
    )


def test_durable_namespaced_context_and_restart(tmp_path, monkeypatch):
    with runtime(tmp_path, monkeypatch) as (ctx, session):
        assert ctx.state.definition_id is None
        ctx.outbox_set("pending", {"sent": [1]})
        ctx.kv_set("cursor", {"value": 2})
        epoch = ctx.config_epoch
        assert session.account_id == ACCOUNT
    with runtime(tmp_path, monkeypatch) as (ctx, session):
        assert ctx.config_epoch == epoch
        assert ctx.outbox_get("pending") == {"sent": [1]}
        assert ctx.kv_get("cursor") == {"value": 2}
    from fulcra_collect import db

    conn = db.open(tmp_path / "profile/state.db")
    assert db.get_record_outbox(conn, "dayone", "pending") is None


def test_refresh_actual_request_and_changed_account(tmp_path, monkeypatch):
    seen = []

    def responder(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={})

    with runtime(tmp_path, monkeypatch, responder) as (ctx, session):
        monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "second")
        session.client._client().get("/test", headers={"Authorization": "Bearer first"})
        assert seen == ["Bearer second"]
        session._identity_http.close()
        session._identity_http = httpx.Client(
            base_url=session.origin,
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"userid": OTHER})
            ),
        )
        with pytest.raises(RuntimeError, match="Standalone authority changed"):
            session.client._client().get("/test")
        assert len(seen) == 1


def test_changed_home_prevents_any_wrong_root_touch(tmp_path, monkeypatch):
    with runtime(tmp_path, monkeypatch) as (ctx, session):
        wrong = tmp_path / "wrong"
        monkeypatch.setenv("FULCRA_COLLECT_HOME", str(wrong))
        for action in (
            lambda: ctx.kv_get("x"),
            lambda: ctx.outbox_set("x", {}),
            lambda: session.resolve_tag("tag"),
            lambda: session.record_policy_binding(),
        ):
            with pytest.raises(RuntimeError, match="profile changed"):
                action()
        assert not wrong.exists()


@pytest.mark.parametrize("change", ["settings", "origin", "cancel", "cross-origin"])
def test_request_fences(tmp_path, monkeypatch, change):
    with runtime(tmp_path, monkeypatch) as (ctx, session):
        path = "/test"
        if change == "settings":
            ctx.config["source"] = "changed"
        if change == "origin":
            session.authority.base_url = "https://other.test"
        if change == "cancel":
            session.cancel()
        if change == "cross-origin":
            path = "https://other.test/test"
        with pytest.raises(RuntimeError):
            session.client._client().get(path)


def test_exclusive_namespace_lease_is_bounded(tmp_path, monkeypatch):
    with runtime(tmp_path, monkeypatch):
        with pytest.raises(TimeoutError):
            with runtime(tmp_path, monkeypatch, lease_timeout_s=0):
                pytest.fail("second invocation acquired same namespace")


def test_bad_identity_and_secret_auth_error_static(tmp_path, monkeypatch):
    from fulcra_collect.standalone_v1 import StandaloneRecordSession
    from types import SimpleNamespace

    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path))
    client = BaseFulcraClient()
    monkeypatch.setattr(
        client, "get_token", lambda: (_ for _ in ()).throw(RuntimeError("SECRET"))
    )
    ctx = SimpleNamespace(config={}, config_epoch="epoch")
    with pytest.raises(RuntimeError) as exc:
        StandaloneRecordSession(ctx, client=client, profile=tmp_path)
    assert "SECRET" not in str(exc.value)
    assert exc.value.__suppress_context__


@pytest.fixture(autouse=True)
def no_external_http(monkeypatch):
    monkeypatch.setattr(
        httpx.HTTPTransport,
        "handle_request",
        lambda *_: pytest.fail("standalone test attempted external HTTP"),
    )


@pytest.mark.parametrize(
    "identity", [None, {}, {"userid": None}, {"userid": 3}, {"userid": "bad"}]
)
def test_malformed_identity_never_dispatches(tmp_path, monkeypatch, identity):
    from fulcra_collect.standalone_v1 import standalone_runtime

    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path))
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "synthetic")
    seen = []

    def handle(request):
        seen.append(request.url.path)
        return httpx.Response(200, json=identity)

    transport = httpx.MockTransport(handle)
    with pytest.raises(RuntimeError, match="Standalone authentication unavailable"):
        with standalone_runtime(
            "cli-dayone", {}, transport=transport, identity_transport=transport
        ):
            pytest.fail("malformed identity admitted")
    assert seen == ["/user/v1alpha1/info"]


@pytest.mark.parametrize(
    "change", ["home", "settings", "origin", "cancel", "adapter-origin"]
)
def test_changes_during_identity_proof_stop_actual_request(
    tmp_path, monkeypatch, change
):
    with runtime(tmp_path, monkeypatch) as (ctx, session):
        actual = []
        wrong = tmp_path / "wrong"

        def identity(request):
            if change == "home":
                monkeypatch.setenv("FULCRA_COLLECT_HOME", str(wrong))
            if change == "settings":
                ctx.config["source"] = "different"
            if change == "origin":
                session.authority.base_url = "https://other.test"
            if change == "cancel":
                session.cancel()
            if change == "adapter-origin":
                session.client.base_url = "https://other.test"
            return httpx.Response(200, json={"userid": ACCOUNT})

        session._identity_http.close()
        session._identity_http = httpx.Client(
            base_url=session.origin, transport=httpx.MockTransport(identity)
        )
        session.client._transport = httpx.MockTransport(
            lambda request: actual.append(request) or httpx.Response(200)
        )
        with pytest.raises(RuntimeError):
            session.client._client().get("/operation")
        assert not actual
        assert not wrong.exists()


@pytest.mark.parametrize("change", ["cancel", "preview"])
def test_actual_schema_get_then_post_is_fenced(tmp_path, monkeypatch, change):
    session_box, posts = [], []
    destination = "Event/11111111-2222-4333-8444-555555555555"

    def responder(request):
        if request.url.path.endswith("/schema"):
            session = session_box[0]
            if change == "cancel":
                session.cancel()
            else:
                session.ctx.config["preview_only"] = True
            return httpx.Response(
                200,
                json={
                    "type": "object",
                    "properties": {
                        k: {}
                        for k in ("id", "sources", "start_time", "end_time", "tags")
                    },
                },
            )
        posts.append(request)
        return httpx.Response(201, json={})

    from fulcra_common.v1 import build_event_record
    from datetime import datetime, timezone

    with runtime(tmp_path, monkeypatch, responder) as (ctx, session):
        session_box.append(session)
        row = build_event_record(
            data_type=destination,
            source_id="synthetic",
            start_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        with pytest.raises(RuntimeError):
            session.ingest(destination, [row])
        assert not posts


def test_media_namespace_isolated_from_plugins_and_dayone(tmp_path, monkeypatch):
    from fulcra_collect.standalone_v1 import standalone_runtime
    from fulcra_collect import db
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(tmp_path))
    monkeypatch.setenv('FULCRA_ACCESS_TOKEN', 'synthetic')
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={'userid': ACCOUNT}))
    for iteration in range(2):
        with standalone_runtime('cli-media', {}, transport=transport, identity_transport=transport) as (ctx, session):
            if iteration:
                assert ctx.outbox_get('pending') == {'original': 1}
            else:
                ctx.outbox_set('pending', {'original': 1})
    conn = db.open(tmp_path / 'state.db')
    for namespace in ('media-webhook', 'netflix', 'cli-dayone'):
        assert db.get_record_outbox(conn, namespace, 'pending') is None
    with pytest.raises(ValueError, match='unsupported'):
        with standalone_runtime('arbitrary', {}):
            pytest.fail('arbitrary namespace admitted')
