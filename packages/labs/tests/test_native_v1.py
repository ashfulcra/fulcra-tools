"""Installed Click path with real session/outbox and synthetic transport only."""

import json
import base64
import re
from copy import deepcopy
from uuid import UUID, uuid5, NAMESPACE_URL

import httpx
import pytest
from click.testing import CliRunner
from fulcra_common.client import BaseFulcraClient
from fulcra_common.data_types import CreationJournal
from fulcra_common.record_exclusions import EXCLUSION_SPEC
from fulcra_labs import cli

ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CONTROL = "Event/aaaaaaaa-2222-4333-8444-555555555555"


@pytest.fixture
def native(tmp_path, monkeypatch):
    root = tmp_path / "profile"
    root.mkdir()
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(root))
    claims = (
        base64.urlsafe_b64encode(
            json.dumps({"exp": 4102444800, "fulcradynamics.com/userid": ACCOUNT}).encode()
        )
        .decode()
        .rstrip("=")
    )
    monkeypatch.setattr(
        BaseFulcraClient, "get_token", lambda _: "synthetic." + claims + ".signature"
    )
    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", lambda *_: pytest.fail("live network")
    )
    CreationJournal(root / "v1-type-creations.sqlite").resolved(ACCOUNT, EXCLUSION_SPEC, CONTROL)
    state = dict(root=root, catalog={}, rows={}, posts=[], paths=[])

    def handler(req):
        path = req.url.path
        state["paths"].append((req.method, path))
        if path == "/user/v1alpha1/info":
            return httpx.Response(200, json={"userid": state.get("account", ACCOUNT)})
        if path == "/input/v1/data_type":
            if state.get("catalog_error"):
                return httpx.Response(503)
            return httpx.Response(
                200,
                json=[
                    dict(
                        id=CONTROL,
                        name=EXCLUSION_SPEC.name,
                        fulcra_userid=ACCOUNT,
                        api_version="v1",
                        recordable=True,
                        deprecated=False,
                        record_spec={"type": "event"},
                    ),
                    *state["catalog"].values(),
                ],
            )
        if req.method == "POST" and path.startswith("/input/v1/data_type/"):
            body = json.loads(req.content)
            if state.get("create_lost"):
                raise httpx.ReadError("synthetic lost creation")
            dest = "Metric/" + str(uuid5(NAMESPACE_URL, body["name"]))
            state["catalog"][dest] = dict(
                id=dest,
                name=body["name"],
                fulcra_userid=ACCOUNT,
                api_version="v1",
                recordable=True,
                deprecated=False,
                record_spec=body["record_spec"],
            )
            return httpx.Response(201, json={"id": dest})
        if path.endswith("/schema"):
            dest = path.split("/catalog/")[1].split("/v1/schema")[0]
            spec = EXCLUSION_SPEC if dest == CONTROL else None
            fields = (
                spec.fields
                if spec
                else json.loads(state["catalog"][dest]["record_spec"]["schema"])["properties"]
            )
            schema = dict(
                type="object",
                properties={
                    **{k: {} for k in ("id", "sources", "tags", "start_time", "end_time")},
                    **fields,
                },
            )
            if spec:
                schema["required"] = list(spec.required_fields)
            else:
                schema["properties"]["value"] = {"type": "number"}
                schema["required"] = list(fields)
                schema.update(state.get("schema_rules", {}))
                schema["properties"].update(state.get("extra_fields", {}))
            if dest != CONTROL and "schema_override" in state:
                schema = state["schema_override"]
            return httpx.Response(200, json=schema)
        if path == "/data/v1/records":
            if state.get("read_error"):
                return httpx.Response(503)
            query = req.url.params["q"]
            dest = re.search('__name__="([^"]+)"', query).group(1)
            ids = re.findall('id="([^"]+)"', query)
            return httpx.Response(
                200,
                text="\n".join(
                    json.dumps(state["rows"][dest, i]) for i in ids if (dest, i) in state["rows"]
                ),
            )
        if req.method == "POST" and path.startswith("/ingest/v1/record/Metric/"):
            dest = path.split("/record/")[1]
            if len(state["posts"]) >= state.get("fail_after", float("inf")):
                return httpx.Response(422)
            batch = [json.loads(line) for line in req.content.splitlines()]
            state["posts"].append((dest, deepcopy(batch)))
            state["rows"].update({(dest, row["id"]): row for row in batch})
            if state.get("lost"):
                raise httpx.ReadError("synthetic lost reply")
            return httpx.Response(201, json={"upload_id": "synthetic"})
        raise AssertionError((req.method, str(req.url)))

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(cli, "build_client", lambda: BaseFulcraClient(transport=transport))
    state["invoke"] = lambda *args: CliRunner().invoke(cli.cli, list(args))
    state["source"] = tmp_path / "synthetic.json"
    state["source"].write_text(
        json.dumps(
            dict(
                lab="Synthetic",
                report_date="2026-01-02",
                collected_at="2026-01-01T00:00:00Z",
                observations=[
                    dict(marker_raw="HbA1c", value_raw="5.4", unit_raw="%"),
                    dict(marker_raw="TSH", value_raw="2.1", unit_raw="uIU/mL"),
                ],
            )
        )
    )
    return state


def test_actual_native_two_marker_payload(native):
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["outcome"]["ingested"] == 2
    assert len(native["posts"]) == 2
    for dest, batch in native["posts"]:
        row = batch[0]
        assert dest.startswith("Metric/")
        assert "unit" not in row and "canonical_unit" not in row
        assert row["raw_unit"] in ("%", "uIU/mL")
        assert row["lab"] == "Synthetic" and row["report_date"] == "2026-01-02"
        assert row["source_doc_token"] is None and row["tags"] == []
        assert UUID(row["id"])
    assert not any("NumericAnnotation" in path for _, path in native["paths"])


def test_dry_run_never_constructs_client(native, monkeypatch):
    monkeypatch.setattr(cli, "build_client", lambda: pytest.fail("preview built client"))
    result = native["invoke"]("ingest", str(native["source"]), "--dry-run", "--json")
    assert result.exit_code == 0, result.stdout
    outcome = json.loads(result.stdout)["outcome"]
    assert outcome["ingested"] == 0 and outcome["would_ingest"] == 2
    assert native["paths"] == []


def test_missing_source_json_is_single_failure(native):
    result = native["invoke"]("ingest", str(native["source"]) + "-missing", "--json")
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["outcome"]["ingested"] == 0
    assert payload["error"]


@pytest.mark.parametrize(
    "pin",
    [
        "hba1c=NumericAnnotation/00000000-0000-4000-8000-000000000000",
        "no-such-marker=Metric/00000000-0000-4000-8000-000000000000",
        "tsh=Metric/no",
    ],
)
def test_bad_pins_fail_before_identity(native, pin):
    result = native["invoke"]("ingest", str(native["source"]), "--marker-target", pin, "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["outcome"]["ingested"] == 0
    assert native["paths"] == []


def test_partial_multigroup_failure_preserves_lower_bound_and_original(native):
    native["fail_after"] = 1
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["outcome"]["ingested"] == 1
    assert payload["outcome"]["marker_attribution"] == "UNKNOWN"
    native["fail_after"] = float("inf")
    native["paths"].clear()
    result = native["invoke"](
        "ingest", str(native["source"]) + "-missing", "--source-doc", "missing.pdf", "--json"
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["recovery"]["verified"] == 1 and payload["outcome"]["ingested"] == 0
    assert len(native["posts"]) == 1
    assert "UNKNOWN" in payload["error"]
    # An issued request is never resubmitted on an absent read. Later exact
    # visibility, rather than retry permission, permits original recovery.
    pending = read_pending(native)
    for group in pending["groups"]:
        for row in group["records"]:
            native["rows"][group["data_type"], row["id"]] = row
    result = native["invoke"]("ingest", str(native["source"]) + "-missing", "--json")
    payload = json.loads(result.stdout)
    assert payload["recovery"]["verified"] == 2
    assert "missing" in payload["error"] and len(native["posts"]) == 1


def test_lost_reply_recovers_without_resubmission_before_empty_source(native):
    native["lost"] = True
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 1 and len(native["posts"]) == 1
    assert json.loads(result.stdout)["outcome"]["ingested"] == 0
    native["lost"] = False
    native["source"].write_text('{"observations": []}')
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["recovery"]["verified"] == 2
    assert payload["outcome"]["ingested"] == 0
    assert len(native["posts"]) == 2


@pytest.mark.parametrize(
    "rules,extra",
    [
        ({"required": ["unit"]}, {"unit": {"type": "string"}}),
        ({}, {"raw_value": {"$ref": "https://unavailable.invalid/schema"}}),
    ],
)
def test_full_served_schema_refuses_before_post(native, rules, extra):
    native["schema_rules"], native["extra_fields"] = rules, extra
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["outcome"]["ingested"] == 0
    assert native["posts"] == []


def test_optional_unit_is_omitted(native):
    native["extra_fields"] = {"unit": {"type": "string"}}
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 0, result.stdout
    assert all("unit" not in row for _, batch in native["posts"] for row in batch)


def test_legacy_selected_pin_and_corrupt_unknown(native, labs_home):
    labs_home.mkdir()
    cache = labs_home / "markers.json"
    original = json.dumps(
        {
            "markers": {"hba1c": {"def_id": "11111111-2222-4333-8444-555555555555"}},
            "last_ingest": "legacy",
        }
    )
    cache.write_text(original)
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 1 and "explicit" in json.loads(result.stdout)["error"]
    assert cache.read_text() == original and native["posts"] == []
    cache.write_text("{bad")
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 1 and "UNKNOWN" in json.loads(result.stdout)["error"]
    assert cache.read_text() == "{bad"


def test_unselected_legacy_needs_no_pin(native, labs_home):
    labs_home.mkdir()
    cache = labs_home / "markers.json"
    cache.write_text('{"markers":{"glucose":{"def_id":"11111111-2222-4333-8444-555555555555"}}}')
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 0, result.stdout


def test_status_is_native_per_invocation_and_legacy_attempt_only(native):
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 0, result.stdout
    result = native["invoke"]("status", "--json")
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data["account_id"] == ACCOUNT
    assert data["native"]["verified"] == 2 and data["native"]["last_verified_ingest"]
    assert data["legacy"]["last_attempt_evidence"] is None
    assert data["native"]["counts_scope"] == "last completed current delivery"


def read_pending(native):
    from fulcra_collect import db

    with db.open(native["root"] / "state.db") as conn:
        return db.get_record_outbox(conn, "cli-labs", "v1_labs_pending", None)


@pytest.mark.parametrize("change", ["missing", "version", "fingerprint", "marker"])
def test_pending_witness_unknown_holds_original_before_source(native, change):
    from fulcra_collect import db

    native["lost"] = True
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 1
    original = read_pending(native)
    with db.open(native["root"] / "state.db") as conn:
        witnesses = db.get_plugin_kv(conn, "cli-labs", "v1_labs_witness", {})
        if change == "missing":
            witnesses = {}
        else:
            first = next(iter(witnesses.values()))
            first[dict(version="codec", fingerprint="fingerprint", marker="marker")[change]] = (
                "unknown"
            )
        db.set_plugin_kv(conn, "cli-labs", "v1_labs_witness", witnesses)
    native["source"].write_text("{broken")
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 1 and "witness UNKNOWN" in json.loads(result.stdout)["error"]
    assert read_pending(native) == original and len(native["posts"]) == 1


@pytest.mark.parametrize("change", ["pin", "account"])
def test_authority_change_holds_pending_before_new_source(native, change):
    native["lost"] = True
    assert native["invoke"]("ingest", str(native["source"]), "--json").exit_code == 1
    original = read_pending(native)
    extra = []
    if change == "pin":
        extra = ["--marker-target", "tsh=Metric/00000000-0000-4000-8000-000000000000"]
    else:
        native["account"] = "11111111-2222-4333-8444-555555555555"
    result = native["invoke"]("ingest", "missing-source", *extra, "--json")
    assert result.exit_code == 1
    assert read_pending(native) == original and len(native["posts"]) == 1


@pytest.mark.parametrize("unit", [None, "percent", ""])
def test_codec_refuses_noncanonical_unit_without_relabel(unit):
    from fulcra_labs.native_v1 import build_record
    from fulcra_labs.validate import validate_extraction

    report = validate_extraction(
        dict(
            collected_at="2026-01-01",
            observations=[dict(marker_raw="HbA1c", value_raw="5.4", unit_raw="%")],
        )
    )
    verdict = report.verdicts[0]
    verdict.canonical_unit = unit
    with pytest.raises(ValueError, match="canonical unit"):
        build_record(verdict, report, "Metric/00000000-0000-4000-8000-000000000000")


@pytest.mark.parametrize("raw", [None, "", "percent"])
def test_codec_preserves_nullable_raw_unit(raw):
    from fulcra_labs.native_v1 import build_record
    from fulcra_labs.validate import validate_extraction

    report = validate_extraction(
        dict(
            collected_at="2026-01-01",
            observations=[dict(marker_raw="HbA1c", value_raw="5.4", unit_raw="%")],
        )
    )
    verdict = report.verdicts[0]
    verdict.raw_unit = raw
    row = build_record(verdict, report, "Metric/00000000-0000-4000-8000-000000000000")
    assert row["raw_unit"] == raw and "unit" not in row


@pytest.mark.parametrize("field", ["lab", "report_date"])
def test_incompatible_metadata_refuses_whole_batch_before_definitions(native, field):
    source = json.loads(native["source"].read_text())
    source[field] = {"bad": "not string"}
    native["source"].write_text(json.dumps(source))
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 1 and "provenance" in json.loads(result.stdout)["error"]
    assert native["posts"] == [] and native["catalog"] == {}


def test_actual_batch_staged_before_first_post_and_witnesses_present(native, monkeypatch):
    from fulcra_collect import v1_session

    original = v1_session.CollectRecordSession.deliver_records

    def deliver(session, destination, records, **kwargs):
        pending = read_pending(native)
        assert len(pending["groups"]) == 2 and len(pending["dispatch"]) == 2
        return original(session, destination, records, **kwargs)

    monkeypatch.setattr(v1_session.CollectRecordSession, "deliver_records", deliver)
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 0, result.stdout


def test_status_unreadable_state_is_unknown(native):
    from fulcra_collect import db

    with db.open(native["root"] / "state.db") as conn:
        db.set_plugin_kv(conn, "cli-labs", "v1_labs_status:" + ACCOUNT, ["corrupt"])
    result = native["invoke"]("status", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["native"] == dict(
        state="UNKNOWN", verified=None, excluded=None
    )


def test_status_malformed_counts_unknown(native):
    from fulcra_collect import db

    with db.open(native["root"] / "state.db") as conn:
        db.set_plugin_kv(conn, "cli-labs", "v1_labs_status:" + ACCOUNT, {"verified": "unreadable"})
    result = native["invoke"]("status", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["native"]["state"] == "UNKNOWN"


def test_archive_hash_and_copy_are_same_snapshot(tmp_path, monkeypatch, labs_home):
    import hashlib
    from fulcra_labs import store

    source = tmp_path / "synthetic.pdf"
    source.write_bytes(b"synthetic original")
    original = store.source_doc_token

    def changed(path):
        token = original(path)
        source.write_bytes(b"synthetic replacement")
        return token

    monkeypatch.setattr(store, "source_doc_token", changed)
    token = store.archive_document(source)
    sha, _ = token.split(":", 1)
    archived = labs_home / "documents" / (sha + ".pdf")
    assert hashlib.sha256(archived.read_bytes()).hexdigest() == sha
    assert archived.stat().st_mode & 0o777 == 0o600


def test_retired_legacy_writes_fail_even_empty_or_preview():
    from fulcra_labs import store

    class Poison:
        def __getattribute__(self, _):
            pytest.fail("retired legacy writer accessed authority/input")

    for extraction, dry_run in [({}, False), ({"observations": []}, False), ({}, True)]:
        with pytest.raises(RuntimeError, match="retired"):
            store.ingest_extraction(Poison(), extraction, state=Poison(), dry_run=dry_run)
    with pytest.raises(RuntimeError, match="retired"):
        store.LabsClient.create_marker_definition(Poison(), Poison())


def test_repeat_exact_readback_no_posts_and_changed_value_new_identity(native):
    assert native["invoke"]("ingest", str(native["source"]), "--json").exit_code == 0
    first = deepcopy(native["posts"])
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["outcome"]["ingested"] == 2
    assert native["posts"] == first  # exact prior visibility counts as verified
    source = json.loads(native["source"].read_text())
    source["observations"][0]["value_raw"] = "5.5"
    native["source"].write_text(json.dumps(source))
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 0, result.stdout
    assert len(native["posts"]) == 3
    assert native["posts"][-1][1][0]["id"] != first[0][1][0]["id"]


@pytest.mark.parametrize("failure", ["annotation", "clear", "close", "status"])
def test_verified_progress_survives_late_bookkeeping_and_cleanup(native, monkeypatch, failure):
    from fulcra_collect import db, plugin, standalone_v1

    if failure == "annotation":
        monkeypatch.setattr(
            plugin.RunContext,
            "annotation",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("synthetic annotation failed")),
        )
    elif failure == "clear":
        original = db.set_record_outbox

        def save(conn, namespace, key, value):
            if value == {"groups": []}:
                raise RuntimeError("synthetic clear failed")
            return original(conn, namespace, key, value)

        monkeypatch.setattr(db, "set_record_outbox", save)
    elif failure == "close":
        original = standalone_v1.StandaloneRecordSession.close

        def close(session):
            original(session)
            raise RuntimeError("synthetic close failed")

        monkeypatch.setattr(standalone_v1.StandaloneRecordSession, "close", close)
    else:
        original = db.set_plugin_kv

        def status(conn, namespace, key, value):
            if key.startswith("v1_labs_status:") and "last_verified_ingest" in value:
                raise RuntimeError("synthetic status failed")
            return original(conn, namespace, key, value)

        monkeypatch.setattr(db, "set_plugin_kv", status)
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["outcome"]["ingested"] == 2
    assert len(native["posts"]) == 2


def test_actual_pdf_token_note_archive_privacy(native, tmp_path, labs_home):
    import hashlib

    pdf = tmp_path / "synthetic-only.pdf"
    pdf.write_bytes(b"%PDF synthetic fixture, no personal report")
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    result = native["invoke"]("ingest", str(native["source"]), "--source-doc", str(pdf), "--json")
    assert result.exit_code == 0, result.stdout
    for _, batch in native["posts"]:
        assert batch[0]["source_doc_token"] == sha + ":" + pdf.name
        assert pdf.name + "#" + sha[:16] in batch[0]["note"]
        assert "Synthetic" in batch[0]["note"]
    archived = labs_home / "documents" / (sha + ".pdf")
    assert archived.read_bytes() == pdf.read_bytes()
    assert archived.stat().st_mode & 0o777 == 0o600
    assert archived.parent.stat().st_mode & 0o777 == 0o700
    assert not any("file" in path or "document" in path for _, path in native["paths"])


def test_preview_pdf_has_no_profile_archive_catalog_or_client(
    native, monkeypatch, tmp_path, labs_home
):
    from fulcra_collect import standalone_v1

    monkeypatch.setattr(cli, "build_client", lambda: pytest.fail("client constructed"))
    monkeypatch.setattr(
        standalone_v1, "standalone_runtime", lambda *_a, **_k: pytest.fail("runtime opened")
    )
    pdf = tmp_path / "synthetic.pdf"
    pdf.write_bytes(b"synthetic preview")
    before = set(native["root"].iterdir())
    result = native["invoke"](
        "ingest", str(native["source"]), "--source-doc", str(pdf), "--dry-run", "--json"
    )
    assert result.exit_code == 0, result.stdout
    assert not labs_home.exists() and set(native["root"].iterdir()) == before


def test_catalog_unknown_and_uncertain_create_never_create_twice(native):
    native["catalog_error"] = True
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 1 and not native["posts"]
    assert not any(method == "POST" for method, _ in native["paths"])
    native["catalog_error"] = False
    native["create_lost"] = True
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 1
    count = sum(method == "POST" for method, _ in native["paths"])
    native["create_lost"] = False
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 1
    assert sum(method == "POST" for method, _ in native["paths"]) == count


def test_renamed_journal_target_retained(native):
    assert native["invoke"]("ingest", str(native["source"]), "--json").exit_code == 0
    for row in native["catalog"].values():
        row["name"] = "Renamed synthetic target"
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 0, result.stdout
    assert len(native["posts"]) == 2 and len(native["catalog"]) == 2


@pytest.mark.parametrize("fault", ["account", "deleted", "unit", "schema"])
def test_existing_wrong_target_proof_refuses_no_new_record(native, fault):
    assert native["invoke"]("ingest", str(native["source"]), "--json").exit_code == 0
    first = next(iter(native["catalog"].values()))
    if fault == "account":
        first["fulcra_userid"] = "11111111-2222-4333-8444-555555555555"
    elif fault == "deleted":
        first["deprecated"] = True
    elif fault == "unit":
        first["record_spec"]["unit"] = "wrong"
    else:
        native["extra_fields"] = {"raw_unit": {"type": "number"}}
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 1 and len(native["posts"]) == 2


def test_all_pending_target_proofs_precede_any_recovery_post(native):
    native["lost"] = True
    assert native["invoke"]("ingest", str(native["source"]), "--json").exit_code == 1
    pending = read_pending(native)
    second = pending["groups"][1]["data_type"]
    native["catalog"][second]["record_spec"]["unit"] = "wrong"
    native["lost"] = False
    native["paths"].clear()
    result = native["invoke"]("ingest", "missing", "--json")
    assert result.exit_code == 1
    assert read_pending(native) == pending
    assert not any(method == "POST" for method, _ in native["paths"])


def test_selected_legacy_exact_explicit_pin_preserves_cache(native, labs_home):
    from fulcra_labs.native_v1 import marker_spec

    key = "hba1c"
    target = "Metric/00000000-0000-4000-8000-000000000000"
    body = marker_spec(key).create_body()
    native["catalog"][target] = dict(
        id=target,
        name=body["name"],
        fulcra_userid=ACCOUNT,
        api_version="v1",
        recordable=True,
        deprecated=False,
        record_spec=body["record_spec"],
    )
    labs_home.mkdir()
    cache = labs_home / "markers.json"
    original = '{"markers":{"hba1c":{"def_id":"00000000-0000-4000-8000-000000000000"}},"last_ingest":"legacy-attempt"}'
    cache.write_text(original)
    result = native["invoke"](
        "ingest", str(native["source"]), "--marker-target", key + "=" + target, "--json"
    )
    assert result.exit_code == 0, result.stdout
    assert native["posts"][0][0] == target and cache.read_text() == original


def test_excluded_is_separate_and_never_verified_timestamp(native):
    from datetime import datetime, timezone
    from fulcra_common.record_exclusions import _record
    from fulcra_labs.native_v1 import build_record, marker_spec
    from fulcra_labs.validate import validate_extraction

    report = validate_extraction(json.loads(native["source"].read_text()))
    for verdict in report.verdicts:
        spec = marker_spec(verdict.marker_key)
        dest = "Metric/" + str(uuid5(NAMESPACE_URL, spec.name))
        row = build_record(verdict, report, dest)
        excluded = _record(
            CONTROL,
            dest,
            row["id"],
            action_id="11111111-2222-4333-8444-555555555555",
            requested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            reason="operator",
            evidence=[],
        )
        native["rows"][CONTROL, excluded["id"]] = excluded
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 0, result.stdout
    outcome = json.loads(result.stdout)["outcome"]
    assert outcome["ingested"] == 0 and outcome["excluded"] == 2 and native["posts"] == []
    result = native["invoke"]("status", "--json")
    assert "last_verified_ingest" not in json.loads(result.stdout)["native"]


def test_review_held_unknown_duplicate_and_override_gates(native):
    source = dict(
        collected_at="2026-01-01",
        observations=[
            dict(marker_raw="Glucose", value_raw="5.5", unit_raw="mg/dL"),
            dict(marker_raw="Glucose", value_raw="5.5", unit_raw="mg/dL"),
            dict(marker_raw="no such marker", value_raw="10", unit_raw="mg/dL"),
            dict(marker_raw="TSH", value_raw="Negative", unit_raw="uIU/mL"),
        ],
    )
    native["source"].write_text(json.dumps(source))
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["outcome"]["ingested"] == 0 and native["posts"] == []
    result = native["invoke"](
        "ingest", str(native["source"]), "--yes-reviewed", "glucose,tsh", "--json"
    )
    assert result.exit_code == 0, result.stdout
    outcome = json.loads(result.stdout)["outcome"]
    assert outcome["ingested"] == 1 and outcome["skipped_duplicate"] == 1
    assert native["posts"][0][1][0]["value"] == 5.5


def test_factor_affine_values_raw_units_and_source_id_unchanged(native):
    from fulcra_labs.validate import validate_extraction
    from fulcra_common.v1 import record_id_for

    source = dict(
        collected_at="2026-01-01",
        observations=[
            dict(marker_raw="Glucose", value_raw="5.5", unit_raw="mmol/L"),
            dict(marker_raw="HbA1c", value_raw="37", unit_raw="mmol/mol"),
        ],
    )
    native["source"].write_text(json.dumps(source))
    report = validate_extraction(source)
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 0, result.stdout
    rows = {row["marker_key"]: (dest, row) for dest, batch in native["posts"] for row in batch}
    for verdict in report.verdicts:
        dest, row = rows[verdict.marker_key]
        assert row["value"] == verdict.canonical_value
        assert row["sources"] == [verdict.det_source_id]
        assert row["id"] == record_id_for(dest, verdict.det_source_id)
        assert row["raw_unit"] == verdict.raw_unit
        assert row["start_time"] == row["end_time"] == "2026-01-01T00:00:00Z"
    assert rows["glucose"][1]["value"] == 99.088
    assert rows["hba1c"][1]["value"] == 5.53676


def test_recovery_only_persists_separate_verified_evidence_before_missing_source(native):
    native["lost"] = True
    assert native["invoke"]("ingest", str(native["source"]), "--json").exit_code == 1
    native["lost"] = False
    result = native["invoke"]("ingest", "missing", "--json")
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["recovery"]["verified"] == 2 and data["outcome"]["ingested"] == 0
    result = native["invoke"]("status", "--json")
    status = json.loads(result.stdout)["native"]
    assert status["last_recovery"]["verified"] == 2
    assert status["last_recovery"]["state"] == "complete"
    assert status["last_verified_ingest"]
    assert status.get("verified") is None


def test_partial_recovery_persists_honest_lower_bound(native):
    native["fail_after"] = 1
    assert native["invoke"]("ingest", str(native["source"]), "--json").exit_code == 1
    result = native["invoke"]("ingest", "missing", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["recovery"]["verified"] == 1
    result = native["invoke"]("status", "--json")
    state = json.loads(result.stdout)["native"]
    assert state["last_recovery"]["verified"] == 1 and state["last_recovery"]["state"] == "UNKNOWN"
    assert state["last_verified_ingest"]
    result = native["invoke"]("ingest", "missing", "--json")
    assert json.loads(result.stdout)["recovery"]["verified"] == 1
    result = native["invoke"]("status", "--json")
    assert json.loads(result.stdout)["native"]["last_recovery"]["verified"] == 1


def test_note_absent_reference_flag_and_full_provenance(native):
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 0, result.stdout
    assert native["posts"][0][1][0]["note"] == "5.4 % — Synthetic"
    assert native["posts"][1][1][0]["note"] == "2.1 uIU/mL — Synthetic"
    assert native["posts"][0][1][0]["flag"] is None
    assert native["posts"][0][1][0]["reference_range"] is None


def test_native_exact_name_adoption_with_full_schema_proof(native):
    from fulcra_labs.native_v1 import marker_spec

    for key in ("hba1c", "tsh"):
        spec = marker_spec(key)
        target = "Metric/" + str(uuid5(NAMESPACE_URL, spec.name))
        body = spec.create_body()
        native["catalog"][target] = dict(
            id=target,
            name=body["name"],
            fulcra_userid=ACCOUNT,
            api_version="v1",
            deprecated=False,
            recordable=True,
            record_spec=body["record_spec"],
        )
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 0, result.stdout
    assert not any(method == "POST" and "/data_type/" in path for method, path in native["paths"])


def test_current_source_unknown_read_refuses_record_post(native):
    native["read_error"] = True
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 1 and json.loads(result.stdout)["outcome"]["ingested"] == 0
    assert native["posts"] == [] and read_pending(native) in (None, {"groups": []})


def test_malformed_unselected_legacy_identifier_is_unknown(native, labs_home):
    labs_home.mkdir()
    (labs_home / "markers.json").write_text('{"markers":{"glucose":{"def_id":"not-a-uuid"}}}')
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 1 and "UNKNOWN" in json.loads(result.stdout)["error"]
    assert native["catalog"] == {} and native["posts"] == []


def test_duplicate_pins_fail_before_effects(native):
    pin = "tsh=Metric/00000000-0000-4000-8000-000000000000"
    result = native["invoke"](
        "ingest", "missing", "--marker-target", pin, "--marker-target", pin, "--json"
    )
    assert result.exit_code == 1 and native["paths"] == []


def test_ambiguous_exact_name_refuses_creation(native):
    from fulcra_labs.native_v1 import marker_spec

    spec = marker_spec("hba1c")
    for number in (1, 2):
        target = "Metric/" + str(uuid5(NAMESPACE_URL, "ambiguous" + str(number)))
        body = spec.create_body()
        native["catalog"][target] = dict(
            id=target,
            name=body["name"],
            fulcra_userid=ACCOUNT,
            api_version="v1",
            deprecated=False,
            recordable=True,
            record_spec=body["record_spec"],
        )
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 1 and native["posts"] == []
    assert not any(method == "POST" for method, _ in native["paths"])


def test_registry_spec_metadata_exact_for_every_marker():
    from fulcra_labs import markers
    from fulcra_labs.native_v1 import marker_spec

    for marker in markers.all_markers():
        spec = marker_spec(marker.key)
        body = spec.create_body()
        assert spec.key == "collect.labs." + marker.key + ".v1"
        assert spec.name == marker.display_name + " (Labs)"
        assert spec.description == (
            f"Laboratory observation for {marker.display_name} [{marker.key}]; "
            f"canonical unit: {marker.canonical_unit}; LOINC: {marker.loinc or 'unspecified'}."
        )
        assert body["record_spec"]["type"] == "metric"
        assert body["record_spec"]["aggregation"] == "discrete"
        assert body["record_spec"]["unit"] == marker.canonical_unit
        assert "scale" not in body["record_spec"] and "value_map" not in body["record_spec"]
        assert len(spec.required_fields) == 10 and set(spec.required_fields) == set(spec.fields)


def test_partial_within_group_exact_readback_failure_has_no_invented_proof(native, monkeypatch):
    # Synthetic transport transforms one served row after actual POST; the real
    # kernel must refuse the group and preserve original dispatch evidence.
    from fulcra_collect import v1_session

    original = v1_session.CollectRecordSession.get_records

    def read(session, target, ids):
        rows = original(session, target, ids)
        if native["posts"] and target == native["posts"][0][0] and rows:
            next(iter(rows.values()))["note"] = "synthetic mismatch"
        return rows

    monkeypatch.setattr(v1_session.CollectRecordSession, "get_records", read)
    source = json.loads(native["source"].read_text())
    source["observations"] = [
        source["observations"][0],
        dict(marker_raw="HbA1c", value_raw="5.5", unit_raw="%"),
    ]
    native["source"].write_text(json.dumps(source))
    result = native["invoke"]("ingest", str(native["source"]), "--yes-reviewed", "hba1c", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["outcome"]["ingested"] == 0
    assert len(read_pending(native)["groups"][0]["records"]) == 2


def test_source_selection_and_new_marker_never_mutate_authority(native):
    from fulcra_collect import db
    from fulcra_labs.native_v1 import authority
    from fulcra_collect.config import settings_fingerprint

    native["lost"] = True
    assert native["invoke"]("ingest", str(native["source"]), "--json").exit_code == 1
    pending = read_pending(native)
    assert pending["settings_fingerprint"] == settings_fingerprint(authority({}))
    assert pending["epoch"] == pending["settings_fingerprint"]
    native["lost"] = False
    native["source"].write_text(
        json.dumps(
            dict(
                collected_at="2026-02-01",
                observations=[dict(marker_raw="Glucose", value_raw="92", unit_raw="mg/dL")],
            )
        )
    )
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["recovery"]["verified"] == 2
    assert json.loads(result.stdout)["outcome"]["ingested"] == 1
    with db.open(native["root"] / "state.db") as conn:
        witnesses = db.get_plugin_kv(conn, "cli-labs", "v1_labs_witness", {})
    assert len(witnesses) == 3


@pytest.mark.parametrize("failure", ["readback", "guard", "commit"])
def test_status_transaction_after_set_failure_keeps_old_evidence(native, monkeypatch, failure):
    from fulcra_collect import db, standalone_v1

    assert native["invoke"]("ingest", str(native["source"]), "--json").exit_code == 0
    with db.open(native["root"] / "state.db") as conn:
        prior = db.get_plugin_kv(conn, "cli-labs", "v1_labs_status:" + ACCOUNT, None)
    original_set, original_get = db.set_plugin_kv, db.get_plugin_kv
    written = {"value": False}

    def save(connection, namespace, key, value):
        result = original_set(connection, namespace, key, value)
        if key.startswith("v1_labs_status:") and "last_recovery" in value:
            assert connection.in_transaction
            written["value"] = True
            if failure == "commit":
                import sqlite3

                connection.set_authorizer(
                    lambda action, arg, *_: (
                        sqlite3.SQLITE_DENY
                        if action == sqlite3.SQLITE_TRANSACTION and arg == "COMMIT"
                        else sqlite3.SQLITE_OK
                    )
                )
        return result

    monkeypatch.setattr(db, "set_plugin_kv", save)
    if failure == "readback":

        def read(connection, namespace, key, default=None):
            if written["value"] and key.startswith("v1_labs_status:"):
                raise RuntimeError("synthetic status readback failure")
            return original_get(connection, namespace, key, default)

        monkeypatch.setattr(db, "get_plugin_kv", read)
    elif failure == "guard":
        original_guard = standalone_v1.StandaloneRecordSession.guard

        def guard(session, **kwargs):
            if written["value"]:
                raise RuntimeError("synthetic guard failure")
            return original_guard(session, **kwargs)

        monkeypatch.setattr(standalone_v1.StandaloneRecordSession, "guard", guard)
    native["lost"] = True
    # New current source changed, leaving real pending for recovery.
    source = json.loads(native["source"].read_text())
    source["observations"][0]["value_raw"] = "5.6"
    native["source"].write_text(json.dumps(source))
    assert native["invoke"]("ingest", str(native["source"]), "--json").exit_code == 1
    # The failed current attempt legitimately updates last_attempt; capture the
    # durable pre-recovery evidence that must survive the failed transaction.
    prior = original_get(conn, "cli-labs", "v1_labs_status:" + ACCOUNT, None)
    native["lost"] = False
    result = native["invoke"]("ingest", "missing", "--json")
    conn.set_authorizer(None)
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["recovery"]["verified"] == 2
    assert "UNKNOWN" in payload["error"]
    assert original_get(conn, "cli-labs", "v1_labs_status:" + ACCOUNT, None) == prior
    assert not conn.in_transaction


def test_verified_status_authority_failure_is_unknown_with_actual_counts(native, monkeypatch):
    from fulcra_labs.native_v1 import LabsWriter

    original = LabsWriter._status

    def status(writer, **kwargs):
        if kwargs.get("verified"):
            native["account"] = "11111111-2222-4333-8444-555555555555"
        return original(writer, **kwargs)

    monkeypatch.setattr(LabsWriter, "_status", status)
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    payload = json.loads(result.stdout)
    assert result.exit_code == 1 and payload["outcome"]["ingested"] == 2
    assert "UNKNOWN" in payload["error"]


def test_reviewed_qualifier_and_note_reference_flag_preserved(native):
    source = dict(
        lab="Synthetic",
        report_date="2026-01-02",
        collected_at="2026-01-01",
        observations=[
            dict(
                marker_raw="TSH",
                value_raw="<0.1",
                unit_raw="uIU/mL",
                reference_range_raw="0.4-4.0",
                flag_raw="L",
            )
        ],
    )
    native["source"].write_text(json.dumps(source))
    result = native["invoke"]("ingest", str(native["source"]), "--yes-reviewed", "tsh", "--json")
    assert result.exit_code == 0, result.stdout
    row = native["posts"][0][1][0]
    assert row["qualifier"] == "lt" and row["raw_value"] == "<0.1"
    assert row["reference_range"] == "0.4-4.0" and row["flag"] == "L"
    assert row["note"] == "L <0.1 uIU/mL [ref 0.4-4.0] — Synthetic"


def test_pending_corrupt_envelope_is_unknown_before_input(native):
    from fulcra_collect import db

    with db.open(native["root"] / "state.db") as conn:
        db.set_record_outbox(conn, "cli-labs", "v1_labs_pending", {"groups": "unreadable"})
    result = native["invoke"]("ingest", "missing", "--json")
    assert result.exit_code == 1
    assert "malformed" in json.loads(result.stdout)["error"]
    assert native["catalog"] == {} and native["posts"] == []


def test_native_status_account_and_profile_are_separate(native, monkeypatch, tmp_path):
    assert native["invoke"]("ingest", str(native["source"]), "--json").exit_code == 0
    native["account"] = "11111111-2222-4333-8444-555555555555"
    result = native["invoke"]("status", "--json")
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["native"]["verified"] is None
    native["account"] = ACCOUNT
    other = tmp_path / "other-profile"
    other.mkdir()
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(other))
    result = native["invoke"]("status", "--json")
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["native"]["verified"] is None


def test_status_refuses_existing_transaction_without_committing_other_work(native, monkeypatch):
    from fulcra_collect import db
    from fulcra_labs.native_v1 import LabsWriter

    original = LabsWriter._status
    connection = db.open(native["root"] / "state.db")

    def status(writer, **kwargs):
        if kwargs.get("attempt"):
            connection.execute("BEGIN IMMEDIATE")
            db.set_plugin_kv(connection, "synthetic-other", "work", {"uncommitted": True})
        return original(writer, **kwargs)

    monkeypatch.setattr(LabsWriter, "_status", status)
    try:
        result = native["invoke"]("ingest", str(native["source"]), "--json")
        assert result.exit_code == 1
        assert "existing transaction retained" in json.loads(result.stdout)["error"]
        assert connection.in_transaction
        assert db.get_plugin_kv(connection, "synthetic-other", "work", None) == {
            "uncommitted": True
        }
    finally:
        connection.execute("ROLLBACK")
    assert db.get_plugin_kv(connection, "synthetic-other", "work", None) is None


@pytest.mark.parametrize("change", ["provenance", "value-collision"])
def test_existing_same_identity_conflict_holds_without_stage_or_post(native, change):
    source = json.loads(native["source"].read_text())
    source["observations"] = [dict(marker_raw="Glucose", value_raw="99.088001", unit_raw="mg/dL")]
    native["source"].write_text(json.dumps(source))
    assert native["invoke"]("ingest", str(native["source"]), "--json").exit_code == 0
    if change == "provenance":
        source["lab"] = "Different synthetic provenance"
    else:
        source["observations"][0]["value_raw"] = "99.088002"
    native["source"].write_text(json.dumps(source))
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 1 and "conflict" in json.loads(result.stdout)["error"]
    assert len(native["posts"]) == 1 and read_pending(native) == {"groups": []}


def test_exact_current_replay_allows_unrelated_cloud_enrichment(native):
    assert native["invoke"]("ingest", str(native["source"]), "--json").exit_code == 0
    for row in native["rows"].values():
        row["unrelated_cloud_enrichment"] = "retained synthetic"
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 0, result.stdout
    assert len(native["posts"]) == 2


def test_preview_reports_unverified_target_hints(native):
    result = native["invoke"]("ingest", str(native["source"]), "--dry-run", "--json")
    assert json.loads(result.stdout)["outcome"]["target_hints"] == {
        "hba1c": "requires live resolution",
        "tsh": "requires live resolution",
    }


def test_status_counts_remain_associated_with_completed_delivery_after_new_attempt(native):
    assert native["invoke"]("ingest", str(native["source"]), "--json").exit_code == 0
    before = json.loads(native["invoke"]("status", "--json").stdout)["native"]
    source = json.loads(native["source"].read_text())
    source["observations"][0]["value_raw"] = "5.6"
    native["source"].write_text(json.dumps(source))
    native["lost"] = True
    assert native["invoke"]("ingest", str(native["source"]), "--json").exit_code == 1
    after = json.loads(native["invoke"]("status", "--json").stdout)["native"]
    assert after["verified"] == before["verified"] == 2
    assert after["current_completed_at"] == before["current_completed_at"]
    assert after["last_attempt"] != before["last_attempt"]
    assert after["counts_scope"] == "last completed current delivery"


def test_partial_recovery_status_has_error_class_and_observation_time(native):
    native["fail_after"] = 1
    assert native["invoke"]("ingest", str(native["source"]), "--json").exit_code == 1
    assert native["invoke"]("ingest", "missing", "--json").exit_code == 1
    state = json.loads(native["invoke"]("status", "--json").stdout)["native"]["last_recovery"]
    assert state["state"] == "UNKNOWN" and state["error_type"] == "RuntimeError"
    assert state["observed_at"]


def test_later_current_group_conflict_blocks_first_new_group_post(native):
    assert native["invoke"]("ingest", str(native["source"]), "--json").exit_code == 0
    source = json.loads(native["source"].read_text())
    source["observations"][0]["value_raw"] = "5.7"  # first group new ID
    source["lab"] = "Changed synthetic provenance"  # later group same ID conflict
    native["source"].write_text(json.dumps(source))
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 1
    assert len(native["posts"]) == 2 and read_pending(native) == {"groups": []}


@pytest.mark.parametrize(
    "schema", [[], {"type": "invalid"}, {"type": "object", "properties": None}]
)
def test_malformed_served_schema_holds_before_records(native, schema):
    native["schema_override"] = schema
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 1 and native["posts"] == []
    assert read_pending(native) in (None, {"groups": []})


def test_current_reverse_witness_conflict_never_rebinds(native):
    from fulcra_collect import db

    assert native["invoke"]("ingest", str(native["source"]), "--json").exit_code == 0
    connection = db.open(native["root"] / "state.db")
    witnesses = db.get_plugin_kv(connection, "cli-labs", "v1_labs_witness", {})
    next(iter(witnesses.values()))["fingerprint"] = "conflicting-spec"
    db.set_plugin_kv(connection, "cli-labs", "v1_labs_witness", witnesses)
    result = native["invoke"]("ingest", str(native["source"]), "--json")
    assert result.exit_code == 1 and "witness conflict" in json.loads(result.stdout)["error"]
    assert len(native["posts"]) == 2 and read_pending(native) == {"groups": []}
    assert db.get_plugin_kv(connection, "cli-labs", "v1_labs_witness", {}) == witnesses


@pytest.mark.parametrize("collected_at", ["2026-01-03", "1989-12-31"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_confirmed_rejected_date_never_becomes_ingestable(
    native, monkeypatch, collected_at, dry_run
):
    from datetime import datetime, timezone
    from fulcra_labs.validate import validate_extraction

    pinned_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(
        cli,
        "validate_extraction",
        lambda extraction: validate_extraction(extraction, now=pinned_now),
    )
    source = dict(
        collected_at=collected_at,
        observations=[dict(marker_raw="Glucose", value_raw="92", unit_raw="mg/dL")],
    )
    native["source"].write_text(json.dumps(source))
    args = ["ingest", str(native["source"]), "--yes-reviewed", "glucose", "--json"]
    if dry_run:
        args.append("--dry-run")
    result = native["invoke"](*args)
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    verdict = payload["verdicts"][0]
    assert verdict["verdict"] == "reject" and verdict["det_source_id"] is not None
    assert payload["outcome"]["rejected"] == 1
    assert payload["outcome"]["ingested"] == payload["outcome"]["would_ingest"] == 0
    assert native["catalog"] == {} and native["posts"] == []
    assert not any(method == "POST" for method, _ in native["paths"])
