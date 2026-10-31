"""Policy absence requires fresh control proof and the original local binding."""
import json
import re

import httpx
import pytest

from fulcra_collect import config
from fulcra_collect.v1_session import CollectRecordSession
from fulcra_common.data_types import CreationJournal
from fulcra_common.record_exclusions import EXCLUSION_SPEC
from test_v1_session import ACCOUNT, DESTINATION, context, record, schema

CONTROL = "Event/aaaaaaaa-2222-4333-8444-555555555555"


@pytest.fixture
def policy_runtime(tmp_path, monkeypatch):
    ctx = context(tmp_path, monkeypatch)
    path = config.config_dir() / "v1-type-creations.sqlite"
    journal = CreationJournal(path)
    journal.resolved(ACCOUNT, EXCLUSION_SPEC, CONTROL)
    state = {"missing": False, "retired": False, "broken": False,
             "name": EXCLUSION_SPEC.name, "during_read": None, "during_schema": None,
             "schema_extra": False, "after_post": None,
             "posts": [], "requests": [], "rows": {}}

    def handler(request):
        state["requests"].append((request.method, request.url.path))
        if request.url.path == "/input/v1/data_type":
            rows = [] if state["missing"] else [{"id": CONTROL, "name": state["name"],
                "api_version": "v1", "recordable": True, "deprecated": state["retired"],
                "record_spec": {"type": "event"}}]
            return httpx.Response(200, json=rows)
        if request.url.path.endswith("/schema"):
            if CONTROL in request.url.path:
                fields = dict(EXCLUSION_SPEC.fields)
                if state["broken"]:
                    fields.pop("action_id")
                if state["schema_extra"]:
                    fields["synthetic_optional"] = {"type": "string"}
                if state["during_schema"]:
                    callback, state["during_schema"] = state["during_schema"], None
                    callback()
                return httpx.Response(200, json={"type": "object", "properties": {
                    **schema()["properties"], **fields}, "required": list(EXCLUSION_SPEC.required_fields)})
            return httpx.Response(200, json=schema())
        if request.url.path == "/data/v1/records":
            query = request.url.params["q"]
            destination = re.search('__name__="([^"]+)"', query).group(1)
            identifiers = re.findall('id="([^"]+)"', query)
            if destination == CONTROL and state["during_read"]:
                callback, state["during_read"] = state["during_read"], None
                callback()
            return httpx.Response(200, text="\n".join(json.dumps(state["rows"][destination, i])
                for i in identifiers if (destination, i) in state["rows"]))
        assert request.url.path == "/ingest/v1/record/" + DESTINATION
        for line in request.content.splitlines():
            row = json.loads(line)
            state["posts"].append(row)
            state["rows"][DESTINATION, row["id"]] = row
        if state["after_post"]:
            state["after_post"]()
        return httpx.Response(201, json={"upload_id": "synthetic-source"})

    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as session:
        yield session, journal, state


@pytest.mark.parametrize("change", ["missing", "retired", "broken"])
def test_cached_control_is_reproved_before_each_policy_read(policy_runtime, change):
    session, _, state = policy_runtime
    assert session.excluded_ids(DESTINATION, [record()["id"]], check_only=True) == set()
    state[change] = True
    with pytest.raises(ValueError):
        session.excluded_ids(DESTINATION, [record()["id"]], check_only=True)
    assert not state["posts"]


@pytest.mark.parametrize("valid", [False, True])
def test_supplied_input_fragment_must_agree_with_owned_control_contract(policy_runtime, valid):
    session, _, _ = policy_runtime
    http = session.client._client()
    original = http._transport.handler

    def handler(request):
        response = original(request)
        if request.url.path == "/input/v1/data_type":
            rows = response.json()
            fragment = json.loads(EXCLUSION_SPEC._schema_json)
            if not valid:
                fragment["properties"]["schema_version"]["const"] = 2
            rows[0]["record_spec"]["schema"] = json.dumps(fragment)
            return httpx.Response(200, json=rows)
        return response

    http._transport.handler = handler
    if valid:
        assert session.excluded_ids(DESTINATION, [record()["id"]], check_only=True) == set()
    else:
        with pytest.raises(ValueError, match="incompatible"):
            session.excluded_ids(DESTINATION, [record()["id"]], check_only=True)


@pytest.mark.parametrize("number", ["1.0000", "1e0"])
def test_equivalent_numeric_schema_spelling_remains_compatible(policy_runtime, number):
    session, _, _ = policy_runtime
    http = session.client._client()
    original = http._transport.handler

    def handler(request):
        response = original(request)
        if request.url.path == f"/data/v1/catalog/{CONTROL}/v1/schema":
            return httpx.Response(200, text=response.text.replace('"const":1', '"const":' + number))
        return response

    http._transport.handler = handler
    assert session.excluded_ids(DESTINATION, [record()["id"]], check_only=True) == set()


@pytest.mark.parametrize("change", ["missing", "pending", "different"])
@pytest.mark.parametrize("during", [False, True])
def test_policy_read_never_heals_lost_or_downgraded_binding(policy_runtime, change, during):
    session, journal, state = policy_runtime
    assert session.excluded_ids(DESTINATION, [record()["id"]], check_only=True) == set()

    def invalidate():
        if change == "missing":
            journal.path.unlink()
        else:
            with journal._connect() as db:
                if change == "pending":
                    db.execute("UPDATE creations SET status='pending',destination=NULL")
                else:
                    db.execute("UPDATE creations SET destination=?", (DESTINATION,))

    if during:
        state["during_read"] = invalidate
    else:
        invalidate()
    with pytest.raises((ValueError, FileNotFoundError)):
        session.excluded_ids(DESTINATION, [record()["id"]], check_only=True)
    if change == "missing":
        assert not journal.path.exists()
    else:
        row = journal.read(ACCOUNT, EXCLUSION_SPEC)
        assert row[1:] == (("pending", None) if change == "pending" else ("resolved", DESTINATION))


def test_readonly_policy_does_not_adopt_a_remote_control(policy_runtime):
    session, journal, state = policy_runtime
    journal.path.unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        session.prepare_record_policy(check_only=True)
    assert not state["requests"]
    assert not journal.path.exists()


def test_readonly_policy_never_calls_setup_journal_resolution(policy_runtime, monkeypatch):
    session, _, _ = policy_runtime
    monkeypatch.setattr(CreationJournal, "resolved", lambda *args: pytest.fail("read-only policy rewrote authority"))
    assert session.prepare_record_policy(check_only=True) == CONTROL


@pytest.mark.parametrize("change", ["retired", "broken"])
def test_policy_change_during_source_post_cannot_settle_delivery(policy_runtime, change):
    session, _, state = policy_runtime
    state["after_post"] = lambda: state.__setitem__(change, True)
    with pytest.raises(ValueError):
        session.deliver_records(DESTINATION, [record()], dispatch=memory_dispatch([record()]))
    assert state["posts"] == [record()]


def test_verified_original_binding_survives_a_remote_rename(policy_runtime):
    session, journal, state = policy_runtime
    state["name"] = "Renamed synthetic control"
    assert session.excluded_ids(DESTINATION, [record()["id"]], check_only=True) == set()
    assert journal.read(ACCOUNT, EXCLUSION_SPEC)[2] == CONTROL
    assert not state["posts"]


def test_schema_change_across_empty_policy_read_holds_even_if_still_compatible(policy_runtime):
    session, _, state = policy_runtime
    state["during_read"] = lambda: state.__setitem__("schema_extra", True)
    with pytest.raises(ValueError, match="schema changed"):
        session.excluded_ids(DESTINATION, [record()["id"]], check_only=True)
    assert not state["posts"]


@pytest.mark.parametrize("change", ["retire", "pending"])
def test_verifier_rechecks_owned_and_local_authority_after_served_schema(policy_runtime, change):
    session, journal, state = policy_runtime

    def invalidate():
        if change == "retire":
            state["retired"] = True
        else:
            with journal._connect() as db:
                db.execute("UPDATE creations SET status='pending',destination=NULL")

    state["during_schema"] = invalidate
    with pytest.raises(ValueError):
        session.prepare_record_policy(check_only=True)
    assert not state["posts"]


@pytest.mark.parametrize("mode", ["partial", "redirect", "duplicate", "owner", "nonfinite", "encoding",
    "catalog_duplicate", "catalog_owner", "catalog_schema", "rounded", "catalog_rounded", "decimal_range"])
def test_policy_proof_requires_complete_lossless_original_json_response(policy_runtime, mode):
    session, _, state = policy_runtime
    http = session.client._client()
    original = http._transport.handler
    expected = f"/data/v1/catalog/{CONTROL}/v1/schema"
    alias = f"/data/v1/catalog/{DESTINATION}/v1/schema"

    def handler(request):
        if mode.startswith("catalog_") and request.url.path == "/input/v1/data_type":
            response = original(request)
            if mode == "catalog_duplicate":
                text = response.text.replace('"deprecated":false', '"deprecated":true,"deprecated":false')
                assert text != response.text
                return httpx.Response(200, text=text)
            rows = response.json()
            if mode == "catalog_owner":
                rows[0]["userid"] = DESTINATION.split("/")[1]
            elif mode == "catalog_rounded":
                text = EXCLUSION_SPEC._schema_json.replace('"const": 1', '"const": 1.0000000000000001')
                assert text != EXCLUSION_SPEC._schema_json
                rows[0]["record_spec"]["schema"] = text
            else:
                rows[0]["record_spec"]["schema"] = "{"
            return httpx.Response(200, json=rows)
        if request.url.path == expected:
            if mode == "redirect":
                return httpx.Response(302, headers={"location": alias})
            response = original(request)
            if mode == "partial":
                return httpx.Response(206, text=response.text, headers={"content-range": "bytes 0-99/200"})
            if mode == "duplicate":
                return httpx.Response(200, text=response.text.replace('"const":1', '"const":2,"const":1'))
            if mode == "rounded":
                return httpx.Response(200, text=response.text.replace('"const":1', '"const":1.0000000000000001'))
            if mode == "owner":
                return httpx.Response(200, text=response.text, headers={"x-fulcra-userid": DESTINATION.split("/")[1]})
            if mode == "nonfinite":
                return httpx.Response(200, text=response.text[:-1] + ',"synthetic":NaN}')
            if mode == "decimal_range":
                return httpx.Response(200, text=response.text[:-1] + ',"synthetic":0e99999999999999999999}')
            if mode == "encoding":
                import gzip
                return httpx.Response(200, content=gzip.compress(response.content)[:-8], headers={"content-encoding": "gzip"})
        if request.url.path == alias:
            return original(httpx.Request("GET", expected))
        return original(request)

    http._transport.handler = handler
    with pytest.raises((ValueError, httpx.HTTPStatusError)):
        session.excluded_ids(DESTINATION, [record()["id"]], check_only=True)
    assert not state["posts"]


def memory_dispatch(rows):
    from fulcra_common.record_delivery import SourceDispatch, prepared_source_dispatch
    return SourceDispatch({}, rows, prepared_source_dispatch({}, rows), lambda _: None)


def test_ordinary_policy_verifier_never_enters_inactive_historical_dispatch(policy_runtime, monkeypatch):
    from fulcra_common import _historical_observed_transport as historical
    from fulcra_common.record_exclusions import verify_exclusion_type
    session, journal, state = policy_runtime
    monkeypatch.setattr(historical, '_dispatch_observed', lambda **kwargs: pytest.fail('ordinary policy entered historical dispatch'))
    verified = verify_exclusion_type(client=session.client, account_id=ACCOUNT,
        journal=CreationJournal(journal.path, read_only=True), data_type=CONTROL)
    assert verified.data_type == CONTROL
    assert state['requests'] == [('GET', '/input/v1/data_type'),
        ('GET', f'/data/v1/catalog/{CONTROL}/v1/schema'), ('GET', '/input/v1/data_type')]
    assert not state['posts']
