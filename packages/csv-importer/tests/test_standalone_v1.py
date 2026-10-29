"""Actual Click/parser/session/outbox activation; synthetic network and auth only."""

import base64
import json
import re
from pathlib import Path
from copy import deepcopy
from uuid import uuid5, NAMESPACE_URL

import httpx
import pytest
from click.testing import CliRunner

from fulcra_common.client import BaseFulcraClient
from fulcra_common.data_types import CreationJournal
from fulcra_common.record_exclusions import EXCLUSION_SPEC
from fulcra_csv import cli as cli_mod
from fulcra_csv.fulcra import FulcraClient

ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
DEST = "Event/11111111-2222-4333-8444-555555555555"
CONTROL = "Event/aaaaaaaa-2222-4333-8444-555555555555"
TAG = "aaaaaaaa-bbbb-4ccc-8ddd-123456789abc"
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def native(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(profile))
    claims = (
        base64.urlsafe_b64encode(
            json.dumps({"exp": 4102444800, "fulcradynamics.com/userid": ACCOUNT}).encode()
        )
        .decode()
        .rstrip("=")
    )
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "synthetic." + claims + ".signature")
    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", lambda *_: pytest.fail("live network")
    )
    monkeypatch.setattr(
        BaseFulcraClient, "get_token", lambda _: "synthetic." + claims + ".signature"
    )
    CreationJournal(profile / "v1-type-creations.sqlite").resolved(ACCOUNT, EXCLUSION_SPEC, CONTROL)
    state = dict(rows={}, posts=[], paths=[], profile=profile, dest=DEST, catalog_error=False)

    def handle(req):
        path = req.url.path
        state["paths"].append((req.method, path))
        if path == "/user/v1alpha1/info":
            return httpx.Response(200, json={"userid": state.get("account", ACCOUNT)})
        if req.method == "POST" and path.startswith("/input/v1/data_type/"):
            if state.get("create_lost"):
                raise httpx.ReadError("synthetic unknown creation")
            body = json.loads(req.content)
            state["created_body"] = body
            state["metric_metadata"] = {
                key: body["record_spec"][key]
                for key in ("aggregation", "unit", "scale", "value_map")
                if key in body["record_spec"]
            }
            state["name"] = body["name"]
            state["catalog_absent"] = False
            return httpx.Response(201, json={"id": state["dest"]})
        if path == "/input/v1/data_type":
            if state["catalog_error"]:
                return httpx.Response(503)
            return httpx.Response(
                200,
                json=[]
                if state.get("catalog_absent")
                else [
                    dict(
                        id=d,
                        name=n,
                        fulcra_userid=ACCOUNT if state.get("catalog_fault") != "account" else TAG,
                        api_version="v1"
                        if state.get("catalog_fault") != "api_version"
                        else "v1alpha1",
                        recordable=True,
                        deprecated=False,
                        record_spec={
                            "type": d.split("/")[0].lower()
                            if state.get("catalog_fault") != "base"
                            else "metric",
                            **(
                                state.get(
                                    "metric_metadata", dict(aggregation="discrete", unit="kg")
                                )
                                if d.startswith("Metric/")
                                else {}
                            ),
                        },
                    )
                    for d, n in [
                        (state["dest"], state.get("name", "CSV Events")),
                        (CONTROL, EXCLUSION_SPEC.name),
                    ]
                ],
            )
        if path == "/data/v1/catalog/DeletedRecord/v1/schema":
            return httpx.Response(
                200,
                json={
                    "type": "object",
                    "properties": {
                        key: {"type": "string"} for key in ("id", "record_id", "data_type")
                    },
                },
            )
        if path == "/ingest/v1/record/DeletedRecord":
            for line in req.content.splitlines():
                tombstone = json.loads(line)
                state["rows"].pop((state["dest"], tombstone["record_id"]), None)
            return httpx.Response(201, json={"upload_id": "synthetic-delete"})
        if path.endswith("/schema"):
            from fulcra_csv.v1 import csv_spec

            spec = (
                EXCLUSION_SPEC
                if CONTROL in path
                else csv_spec(
                    state["dest"].split("/")[0],
                    unit="kg" if state["dest"].startswith("Metric/") else None,
                )
            )
            return httpx.Response(
                200,
                json={
                    "type": "object",
                    "properties": {
                        **{k: {} for k in ("id", "sources", "tags", "start_time", "end_time")},
                        **(
                            {"value": {"type": "number"}}
                            if state["dest"].startswith("Metric/") and CONTROL not in path
                            else {}
                        ),
                        **(
                            {}
                            if state.get("catalog_fault") == "schema" and CONTROL not in path
                            else spec.fields
                        ),
                        **state.get("extra_schema", {}),
                    },
                    "required": list(spec.required_fields),
                    **(state.get("schema_rules", {}) if CONTROL not in path else {}),
                },
            )
        if "/tag/" in path or path == "/user/v1alpha1/tag":
            tags = state.setdefault("tags", {"synthetic": TAG})
            last = path.rsplit("/", 1)[-1]
            if req.method == "POST":
                label = json.loads(req.content)["name"]
                state.get("missing_tags", set()).discard(label)
                tags.setdefault(label, str(uuid5(NAMESPACE_URL, label)))
                return httpx.Response(
                    201, json={"id": tags[label], "name": label, "fulcra_userid": ACCOUNT}
                )
            if "/name/" in path:
                if last in state.get("missing_tags", set()):
                    return httpx.Response(404)
                tags.setdefault(last, str(uuid5(NAMESPACE_URL, last)))
                name, identity = last, tags[last]
            else:
                name = next(name for name, identity in tags.items() if identity == last)
                identity = last
            return httpx.Response(200, json=dict(id=identity, name=name, fulcra_userid=ACCOUNT))
        if path == "/data/v1/records":
            if state.get("read_error"):
                return httpx.Response(503)
            q = req.url.params["q"]
            full = re.search('__name__="([^"]+)"', q).group(1)
            ids = re.findall('id="([^"]+)"', q)
            return httpx.Response(
                200,
                text="\n".join(
                    json.dumps(state["rows"][full, i]) for i in ids if (full, i) in state["rows"]
                ),
            )
        if path == "/ingest/v1/record/" + state["dest"]:
            batch = [json.loads(line) for line in req.content.splitlines()]
            if len(state["posts"]) >= state.get("reject_after", float("inf")):
                return httpx.Response(422)
            state["posts"].extend(deepcopy(batch))
            state["rows"].update({(state["dest"], row["id"]): row for row in batch})
            if state.get("readback_fault"):
                for row in batch:
                    if state["readback_fault"] == "missing":
                        row.pop("note")
                    elif state["readback_fault"] == "bool":
                        row["csv_value"] = True
                    else:
                        row["title"] = "mismatch"
            if state.get("lost"):
                raise httpx.ReadError("synthetic lost response")
            return httpx.Response(201, json={"upload_id": "synthetic"})
        pytest.fail("unexpected request " + req.method + " " + path)

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(cli_mod, "FulcraClient", lambda: FulcraClient(transport=transport))
    state["transport"] = transport
    state["invoke"] = lambda *args: CliRunner().invoke(cli_mod.cli, list(args))
    return state


def test_actual_import_native_repeat(native):
    args = ["import", str(FIXTURES / "minimal.csv"), "--data-type", DEST]
    result = native["invoke"](*args)
    assert result.exit_code == 0, (result.output, result.exception)
    assert native["posts"] and all("data" not in row and row["sources"] for row in native["posts"])
    assert "posted=2 verified=2" in result.output
    result = native["invoke"](*args)
    assert result.exit_code == 0, result.output
    assert "skipped_existing=2 posted=0 verified=0" in result.output
    assert len(native["posts"]) == 2


def test_actual_bootstrap_explicit_native(native):
    result = native["invoke"]("bootstrap", "--base-type", "Event", "--name", "CSV Events")
    assert result.exit_code == 0, (result.output, result.exception)
    assert DEST in result.output
    assert not any("/annotation" in p for _, p in native["paths"])


@pytest.mark.parametrize(
    "target", ["BodyMass", "11111111-2222-4333-8444-555555555555", "DurationAnnotation"]
)
def test_target_rejected_before_source_or_auth(native, target):
    result = native["invoke"]("import", "/nonexistent/synthetic.csv", "--data-type", target)
    assert result.exit_code == 2
    assert "Event/" in result.output and not native["paths"]


def write_source(tmp_path, text):
    path = tmp_path / "synthetic.csv"
    path.write_text(text)
    return str(path)


@pytest.mark.parametrize(
    "text,extra",
    [
        ("timestamp,title\n,missing\n", []),
        ("timestamp,title\n2026-01-01T00:00:00Z,\n", []),
        ("timestamp,title,title\n2026-01-01T00:00:00Z,a,b\n", []),
        ("timestamp,title\n2026-01-01T00:00:00Z,a,b\n", []),
        ("timestamp,title\n2026-01-01T00:00:00Z\n", []),
        ('timestamp,title\n2026-01-01T00:00:00Z,"unterminated\n', []),
        ("timestamp,title\n2026-01-01T00:00:00Z,a\n", ["--note-col", "absent"]),
        ("timestamp,title\n2026-01-01T00:00:00Z,a\n", ["--title-col", "absent"]),
        (
            "timestamp,title\n2026-01-01T00:00:00Z,a\n",
            ["--data-field", "title=x", "--data-field", "timestamp=x"],
        ),
        ("timestamp,title,n\n2026-01-01T00:00:00Z,a,nan\n", ["--value-col", "n"]),
        ("timestamp,title,n\n2026-01-01T00:00:00Z,a,inf\n", ["--value-col", "n"]),
        (
            "timestamp,title,end\n2026-01-01T00:00:00Z,a,2025-01-01T00:00:00Z\n",
            ["--end-col", "end"],
        ),
    ],
)
def test_strict_actual_cli_no_current_effects(native, tmp_path, text, extra):
    result = native["invoke"](
        "import", write_source(tmp_path, text), "--data-type", DEST, "--tag", "synthetic", *extra
    )
    assert result.exit_code == 1, result.output
    assert "CSV row" in result.output or "header" in result.output or "mapping" in result.output
    assert not native["posts"]
    assert not any(method == "POST" or "/tag/" in path for method, path in native["paths"])


@pytest.mark.parametrize(
    "value,value_type", [("7", "int"), ("true", "bool"), ("fine", "str"), ("", "str")]
)
def test_event_scalars_native(native, tmp_path, value, value_type):
    result = native["invoke"](
        "import",
        write_source(tmp_path, f"timestamp,title,n\n2026-01-01T00:00:00Z,note,{value}\n"),
        "--data-type",
        DEST,
        "--annotation-type",
        "instant",
        "--value-col",
        "n",
        "--value-type",
        value_type,
    )
    assert result.exit_code == 0, result.output
    row = native["posts"][0]
    assert row["csv_value"] == {"int": 7, "bool": True, "str": value or None}[value_type]
    assert row["start_time"] == row["end_time"]
    assert row["csv_annotation_type"] == "instant"
    assert (
        row["csv_extra_tags"] == [] and row["csv_data_fields"] == {} and row["external_ids"] == {}
    )
    assert "unit" not in row and "csv_unit" not in row


def test_metric_weight_actual(native):
    native["dest"] = DEST.replace("Event/", "Metric/")
    result = native["invoke"](
        "import",
        str(FIXTURES / "weight.csv"),
        "--data-type",
        native["dest"],
        "--ts-col",
        "date",
        "--value-col",
        "kg",
        "--unit",
        "kg",
        "--annotation-type",
        "instant",
    )
    assert result.exit_code == 0, result.output
    assert [r["value"] for r in native["posts"]] == [82.4, 82.1, 81.9]
    assert all(
        r["csv_data_fields"] == {"unit": "kg"} and "unit" not in r and "csv_value" not in r
        for r in native["posts"]
    )


@pytest.mark.parametrize(
    "extra", [[], ["--unit", "lb"], ["--unit", "kg", "--aggregation", "cumulative"]]
)
def test_metric_explicit_metadata_authority(native, extra):
    native["dest"] = DEST.replace("Event/", "Metric/")
    result = native["invoke"](
        "import",
        str(FIXTURES / "weight.csv"),
        "--data-type",
        native["dest"],
        "--ts-col",
        "date",
        "--value-col",
        "kg",
        *extra,
    )
    assert result.exit_code == 1 and not native["posts"]
    assert not any("/tag/" in p for _, p in native["paths"])


@pytest.mark.parametrize("value,value_type", [("true", "bool"), ("7", "str"), ("", "float")])
def test_metric_no_coercion(native, tmp_path, value, value_type):
    native["dest"] = DEST.replace("Event/", "Metric/")
    result = native["invoke"](
        "import",
        write_source(tmp_path, f"timestamp,title,n\n2026-01-01T00:00:00Z,note,{value}\n"),
        "--data-type",
        native["dest"],
        "--unit",
        "kg",
        "--value-col",
        "n",
        "--value-type",
        value_type,
    )
    assert result.exit_code == 1 and not native["posts"]
    assert "finite number" in result.output


def test_dry_run_local_only(native, monkeypatch):
    from fulcra_collect import standalone_v1

    monkeypatch.setattr(
        standalone_v1, "standalone_runtime", lambda *a, **kw: pytest.fail("preview opened runtime")
    )
    result = native["invoke"](
        "import", str(FIXTURES / "minimal.csv"), "--data-type", DEST, "--dry-run"
    )
    assert result.exit_code == 0, result.output
    assert "destination not verified" in result.output and not native["paths"]
    assert not (native["profile"] / "state.db").exists()
    result = native["invoke"]("import", str(FIXTURES / "minimal.csv"), "--dry-run")
    assert result.exit_code == 2 and not native["paths"]


def test_full_preflight_bad_last_row_beyond_chunk(native, tmp_path):
    path = write_source(
        tmp_path,
        "timestamp,title,n\n"
        + "".join(f"2026-01-01T00:00:00Z,row{i},{i}\n" for i in range(501))
        + "2026-01-01T00:00:00Z,bad,nan\n",
    )
    result = native["invoke"](
        "import", path, "--data-type", DEST, "--value-col", "n", "--tag", "synthetic"
    )
    assert result.exit_code == 1 and not native["posts"]
    assert not any("/tag/" in p for _, p in native["paths"])


def test_infile_conflict_and_identical_duplicate(native, tmp_path):
    path = write_source(
        tmp_path, "timestamp,title,n\n2026-01-01T00:00:00Z,note,1\n2026-01-01T00:00:00Z,note,2\n"
    )
    result = native["invoke"](
        "import", path, "--data-type", DEST, "--value-col", "n", "--tag", "synthetic"
    )
    assert result.exit_code == 1 and not native["posts"]
    assert not any("/tag/" in p for _, p in native["paths"])
    Path(path).write_text(
        "timestamp,title,n\n2026-01-01T00:00:00Z,note,1\n2026-01-01T00:00:00Z,note,1\n"
    )
    result = native["invoke"]("import", path, "--data-type", DEST, "--value-col", "n")
    assert result.exit_code == 0, result.output
    assert "total=2 skipped_existing=1 posted=1 verified=1" in result.output


def test_actual_correction_retains_nested_enrichment(native, tmp_path):
    path = write_source(
        tmp_path, "timestamp,title,n,field,ext\n2026-01-01T00:00:00Z,note,1,old,old\n"
    )
    args = [
        "import",
        path,
        "--data-type",
        DEST,
        "--value-col",
        "n",
        "--data-field",
        "field=mapped",
        "--extra",
        "ext=source",
    ]
    result = native["invoke"](*args)
    assert result.exit_code == 0, result.output
    original = native["posts"][0]
    cloud = native["rows"][DEST, original["id"]]
    cloud["tags"] = [TAG]
    cloud["sources"].append("other.provenance")
    cloud["csv_data_fields"].update(retained={"a": 1}, untouched=None)
    cloud["external_ids"].update(other={"deep": [1, None]})
    cloud["server_metadata"] = "must not copy"
    Path(path).write_text("timestamp,title,n,field,ext\n2026-01-01T00:00:00Z,note,2,new,new\n")
    result = native["invoke"](*args)
    assert result.exit_code == 0, result.output
    updated = native["posts"][-1]
    assert updated["id"] == original["id"] and updated["csv_value"] == 2
    assert updated["tags"] == [TAG] and updated["sources"] == [
        original["sources"][0],
        "other.provenance",
    ]
    assert updated["csv_data_fields"] == dict(mapped="new", retained={"a": 1}, untouched=None)
    assert updated["external_ids"] == dict(source="new", other={"deep": [1, None]})
    assert "server_metadata" not in updated
    # The same current source plus compatible cloud enrichment is an exact skip.
    result = native["invoke"](*args)
    assert result.exit_code == 0 and "skipped_existing=1 posted=0 verified=0" in result.output


@pytest.mark.parametrize("mutation", ["primary", "alias", "tags", "external", "data", "type"])
def test_existing_bad_primary_or_enrichment_blocks_all_tags(native, tmp_path, mutation):
    path = write_source(tmp_path, "timestamp,title\n2026-01-01T00:00:00Z,note\n")
    args = ["import", path, "--data-type", DEST, "--tag", "synthetic"]
    assert native["invoke"](*args).exit_code == 0
    row = next(iter(native["rows"].values()))
    if mutation == "primary":
        row["sources"] = ["wrong"]
    elif mutation == "alias":
        row["sources"] = ["wrong", row["sources"][0]]
    elif mutation == "tags":
        row["tags"] = ["bad"]
    elif mutation == "external":
        row["external_ids"] = []
    elif mutation == "data":
        row["csv_data_fields"] = None
    else:
        row["note"] = 4
    native["paths"].clear()
    result = native["invoke"](*args)
    assert result.exit_code == 1 and len(native["posts"]) == 1, result.output
    assert not any("/tag/" in p for _, p in native["paths"])


@pytest.mark.parametrize(
    "restart",
    ["missing", "empty", "different", "target", "legacy", "account", "unit", "aggregation"],
)
def test_original_pending_before_new_source_and_authority(native, tmp_path, restart):
    from fulcra_collect import db

    path = write_source(tmp_path, "timestamp,title\n2026-01-01T00:00:00Z,original\n")
    args = ["import", path, "--data-type", DEST]
    native["lost"] = True
    first = native["invoke"](*args)
    assert first.exit_code == 1 and len(native["posts"]) == 1
    conn = db.open(native["profile"] / "state.db")
    before = db.get_record_outbox(conn, "cli-csv", "v1_csv_pending")
    assert before["groups"]
    native["lost"] = False
    changed = restart in ("target", "legacy", "account", "unit")
    if restart == "missing":
        Path(path).unlink()
    elif restart == "empty":
        Path(path).write_text("timestamp,title\n")
    elif restart == "different":
        args = [
            "import",
            write_source(tmp_path, "other,title\n"),
            "--ts-col",
            "other",
            "--data-type",
            DEST,
            "--source-id-prefix",
            "different",
        ]
    elif restart == "target":
        args[-1] = DEST.replace("11111111", "22222222")
    elif restart == "legacy":
        args += ["--definition-id", "historical-selection"]
    elif restart == "account":
        native["account"] = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    elif restart == "unit":
        args += ["--unit", "kg"]
    elif restart == "aggregation":
        # Event aggregation is not Metric authority and must be normalized away.
        args += ["--aggregation", "cumulative"]
    result = native["invoke"](*args)
    after = db.get_record_outbox(conn, "cli-csv", "v1_csv_pending")
    assert result.exit_code == int(changed or restart == "missing"), result.output
    assert len(native["posts"]) == 1
    assert after == (before if changed else {"groups": []})
    if not changed:
        assert "recovered=1" in result.output
        assert "posted=0 verified=0" in result.output


def test_issued_absent_refuses_blind_resubmit(native, tmp_path):
    path = write_source(tmp_path, "timestamp,title\n2026-01-01T00:00:00Z,original\n")
    args = ["import", path, "--data-type", DEST]
    native["lost"] = True
    assert native["invoke"](*args).exit_code == 1
    native["rows"].clear()
    native["lost"] = False
    Path(path).unlink()
    result = native["invoke"](*args)
    assert result.exit_code == 1 and "recovery failed" in result.output
    assert len(native["posts"]) == 1


def test_help_and_legacy_admin_local(native):
    result = native["invoke"]("import", "--help")
    assert "BodyMass" not in result.output and "Event/UUID" in result.output
    result = native["invoke"]("bootstrap", "--help")
    assert (
        "--base-type" in result.output
        and "--annotation-type" not in result.output
        and "--tag" not in result.output
    )
    for option in ("--unit", "--aggregation"):
        result = native["invoke"](
            "bootstrap",
            "--base-type",
            "Event",
            "--name",
            "x",
            option,
            "kg" if option == "--unit" else "discrete",
        )
        assert result.exit_code == 2 and not native["paths"]
    result = native["invoke"]("soft-delete", DEST, "--confirm")
    assert result.exit_code == 2 and not native["paths"]


def test_catalog_unknown_fails_closed(native):
    native["catalog_error"] = True
    result = native["invoke"]("import", str(FIXTURES / "minimal.csv"), "--data-type", DEST)
    assert result.exit_code == 1 and not native["posts"]
    assert not any("/tag/" in p for _, p in native["paths"])


@pytest.mark.parametrize("fault", ["annotation", "clear", "close", "stage_second"])
def test_known_progress_survives_bookkeeping_and_close(native, tmp_path, monkeypatch, fault):
    from fulcra_collect.plugin import RunContext
    from fulcra_collect.standalone_v1 import StandaloneRecordSession
    from fulcra_collect.v1_outbox import V1RecordOutbox

    if fault == "annotation":
        monkeypatch.setattr(
            RunContext,
            "annotation",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("synthetic receipt failure")),
        )
    elif fault == "clear":
        actual = RunContext.outbox_set

        def fail_clear(self, key, value):
            if value == {"groups": []}:
                raise RuntimeError("synthetic clear failure")
            return actual(self, key, value)

        monkeypatch.setattr(RunContext, "outbox_set", fail_clear)
    elif fault == "close":
        actual = StandaloneRecordSession.close

        def fail_close(self):
            actual(self)
            raise RuntimeError("synthetic close failure")

        monkeypatch.setattr(StandaloneRecordSession, "close", fail_close)
    else:
        actual = V1RecordOutbox.stage
        calls = []

        def fail_second(self, *args, **kw):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("synthetic local bookkeeping failure")
            return actual(self, *args, **kw)

        monkeypatch.setattr(V1RecordOutbox, "stage", fail_second)
    count = 501 if fault == "stage_second" else 2
    path = write_source(
        tmp_path,
        "timestamp,title\n" + "".join(f"2026-01-01T00:00:00Z,row{i}\n" for i in range(count)),
    )
    result = native["invoke"]("import", path, "--data-type", DEST)
    verified = 500 if fault == "stage_second" else 2
    assert result.exit_code == 1, result.output
    assert (
        f"total={count} skipped_existing=0 posted={verified} verified={verified}" in result.output
    )
    assert "lower bounds" in result.output and len(native["posts"]) == verified


def test_late_chunk_rejection_preserves_completed_lower_bounds(native, tmp_path):
    native["reject_after"] = 500
    path = write_source(
        tmp_path,
        "timestamp,title\n" + "".join(f"2026-01-01T00:00:00Z,row{i}\n" for i in range(501)),
    )
    result = native["invoke"]("import", path, "--data-type", DEST)
    assert result.exit_code == 1, result.output
    assert "posted=500 verified=500" in result.output and "lower bounds" in result.output


@pytest.mark.parametrize("fault", ["missing", "bool", "mismatch"])
def test_readback_not_source_presence(native, tmp_path, fault):
    native["readback_fault"] = fault
    path = write_source(tmp_path, "timestamp,title,n\n2026-01-01T00:00:00Z,note,1\n")
    result = native["invoke"]("import", path, "--data-type", DEST, "--value-col", "n")
    assert result.exit_code == 1 and len(native["posts"]) == 1, result.output
    assert "posted=0 verified=0" in result.output and "lower bounds" in result.output
    native["readback_fault"] = None
    # Original frozen row repairs are never automatically resubmitted.
    result = native["invoke"]("import", path, "--data-type", DEST, "--value-col", "n")
    assert result.exit_code == 1 and len(native["posts"]) == 1


def test_compatible_extra_declared_field_survives(native, tmp_path):
    path = write_source(tmp_path, "timestamp,title,n\n2026-01-01T00:00:00Z,note,1\n")
    args = ["import", path, "--data-type", DEST, "--value-col", "n"]
    native["extra_schema"] = {"other_note": {"type": "string"}}
    assert native["invoke"](*args).exit_code == 0
    cloud = next(iter(native["rows"].values()))
    cloud["other_note"] = "retained user enrichment"
    Path(path).write_text("timestamp,title,n\n2026-01-01T00:00:00Z,note,2\n")
    result = native["invoke"](*args)
    assert result.exit_code == 0, result.output
    assert native["posts"][-1]["other_note"] == "retained user enrichment"


def test_bootstrap_custom_metadata_then_explicit_import_and_rename(native):
    native["name"] = "Custom CSV name"
    result = native["invoke"](
        "bootstrap",
        "--base-type",
        "Event",
        "--name",
        native["name"],
        "--description",
        "Synthetic custom description",
    )
    assert result.exit_code == 0, result.output
    args = ["import", str(FIXTURES / "minimal.csv"), "--data-type", DEST]
    assert native["invoke"](*args).exit_code == 0
    native["name"] = "Renamed in cloud"
    result = native["invoke"](*args)
    assert result.exit_code == 0 and "skipped_existing=2" in result.output


def test_bootstrap_unknown_create_exact_retry_new_name_and_same_name_intent_hold(native):
    native["catalog_absent"] = True
    native["create_lost"] = True
    args = ["bootstrap", "--base-type", "Event", "--name", "New synthetic CSV"]
    first = native["invoke"](*args)
    assert first.exit_code == 1, first.output

    def creates():
        return [p for m, p in native["paths"] if m == "POST" and "/input/" in p]

    assert len(creates()) == 1
    # Exact retry retains the original unknown reservation, never posts twice.
    assert native["invoke"](*args).exit_code == 1
    assert len(creates()) == 1
    for changed in [
        args + ["--description", "Changed"],
        ["bootstrap", "--base-type", "Metric", "--name", "New synthetic CSV", "--unit", "kg"],
        ["bootstrap", "--base-type", "Metric", "--name", "New synthetic CSV"],
    ]:
        result = native["invoke"](*changed)
        assert result.exit_code == 1 and len(creates()) == 1
    result = native["invoke"](
        "bootstrap", "--base-type", "Event", "--name", "Different synthetic CSV"
    )
    assert result.exit_code == 1 and len(creates()) == 2


def test_bootstrap_actual_create_then_import(native):
    native["catalog_absent"] = True
    result = native["invoke"](
        "bootstrap",
        "--base-type",
        "Event",
        "--name",
        "New synthetic CSV",
        "--description",
        "Custom description",
    )
    assert result.exit_code == 0 and DEST in result.output, result.output
    result = native["invoke"]("import", str(FIXTURES / "minimal.csv"), "--data-type", DEST)
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("bad", ["account", "schema", "api_version", "base"])
def test_owned_type_and_served_schema_are_required(native, bad):
    native["catalog_fault"] = bad
    result = native["invoke"](
        "import", str(FIXTURES / "minimal.csv"), "--data-type", DEST, "--tag", "synthetic"
    )
    assert result.exit_code == 1 and not native["posts"], result.output
    assert not any("/tag/" in p for _, p in native["paths"])


def test_actual_writer_extra_tags_and_nested_null_overlay(native):
    from datetime import datetime, timezone
    from dataclasses import replace
    from fulcra_csv.events import GenericEvent, INSTANT
    from fulcra_csv.standalone_v1 import open_csv, authority

    event = GenericEvent(
        start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        note="note",
        title=None,
        source_id="synthetic.primary",
        annotation_type=INSTANT,
        tag="synthetic",
        extra_tags=("alpha", "synthetic", "beta"),
        value=False,
        external_ids={"source": {"replace": "old", "retain": {"nested": 1}}, "unrelated": [1]},
        data_fields={
            "id": "nested only",
            "sources": None,
            "unit": "source unit",
            "nested": {"replace": "old", "retain": 1},
        },
    )
    with open_csv(authority(DEST), client=FulcraClient(transport=native["transport"])) as writer:
        result = writer.run_import([event])
        assert result.verified == 1
    original = native["posts"][0]
    assert original["csv_extra_tags"] == ["alpha", "synthetic", "beta"]
    assert len(original["tags"]) == 3 and original["tags"][0] == TAG
    assert original["csv_tag"] == "synthetic" and original["csv_value"] is False
    changed = replace(
        event,
        value=True,
        external_ids={"source": {"replace": None, "new": [2]}},
        data_fields={"nested": {"replace": None, "new": [None, 2]}},
    )
    with open_csv(authority(DEST), client=FulcraClient(transport=native["transport"])) as writer:
        assert writer.run_import([changed]).verified == 1
    row = native["posts"][-1]
    assert row["id"] == original["id"]
    assert row["external_ids"] == {
        "source": {"replace": None, "retain": {"nested": 1}, "new": [2]},
        "unrelated": [1],
    }
    assert row["csv_data_fields"] == {
        "id": "nested only",
        "sources": None,
        "unit": "source unit",
        "nested": {"replace": None, "retain": 1, "new": [None, 2]},
    }
    assert row["sources"] == ["synthetic.primary"]


def test_source_id_raw_timestamp_vectors_and_duration_sentinel(native, tmp_path):
    import hashlib

    path = write_source(
        tmp_path,
        "timestamp,title,source\n2026-01-01T00:00:00Z,note,explicit\n2026-01-01T00:00:00+00:00,note,explicit\n",
    )
    result = native["invoke"]("import", path, "--data-type", DEST, "--source-id-col", "source")
    assert result.exit_code == 0, result.output
    rows = native["posts"]
    assert len(rows) == 2 and rows[0]["id"] != rows[1]["id"]
    assert rows[0]["start_time"] == rows[1]["start_time"] == "2026-01-01T00:00:00Z"
    assert rows[0]["end_time"] == "2026-01-01T00:00:01Z"
    for row, raw in zip(rows, ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00+00:00"]):
        expected = (
            "com.fulcradynamics.csv.v1."
            + hashlib.sha256((raw + "|note||explicit").encode()).hexdigest()[:16]
        )
        assert row["sources"] == [expected]


def test_actual_mood_and_per_row_tag_preserve_fields(native):
    result = native["invoke"](
        "import",
        str(FIXTURES / "mood.csv"),
        "--data-type",
        DEST,
        "--annotation-type",
        "instant",
        "--note-col",
        "note",
        "--value-col",
        "mood_score",
        "--value-type",
        "int",
        "--tag-col",
        "tags",
    )
    assert result.exit_code == 0, result.output
    assert (
        native["posts"][0]["csv_value"] == 7 and native["posts"][0]["note"] == "morning coffee good"
    )
    assert native["posts"][0]["csv_tag"] == "morning"
    result = native["invoke"](
        "import",
        str(FIXTURES / "per_row_tag.csv"),
        "--data-type",
        DEST,
        "--subtitle-col",
        "subtitle",
        "--duration-col",
        "duration_s",
        "--source-id-col",
        "id",
        "--tag-col",
        "service",
    )
    assert result.exit_code == 0, result.output
    assert [row["csv_tag"] for row in native["posts"][-3:]] == [
        "spotify",
        "apple-podcasts",
        "netflix",
    ]


def test_excluded_current_row_counts_skip(native, tmp_path):
    from datetime import datetime, timezone
    from fulcra_common.record_exclusions import _record
    from fulcra_csv import parse_csv
    from fulcra_csv.v1 import build_csv_record

    path = write_source(tmp_path, "timestamp,title\n2026-01-01T00:00:00Z,excluded\n")
    event = next(parse_csv(Path(path), source_id_prefix="com.fulcradynamics.csv.v1"))
    row = build_csv_record(event, DEST)
    exclusion = _record(
        CONTROL,
        DEST,
        row["id"],
        action_id=TAG,
        requested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        reason="operator",
        evidence=[],
    )
    native["rows"][CONTROL, exclusion["id"]] = exclusion
    result = native["invoke"]("import", path, "--data-type", DEST)
    assert result.exit_code == 0, result.output
    assert "total=1 skipped_existing=1 posted=0 verified=0" in result.output
    assert not native["posts"]


def test_mid_group_partial_delivery_is_explicit_lower_bound(native, tmp_path):
    # Kernel may observe some rows before a group fails; only complete group
    # progress is a durable exposed lower bound, not an HTTP attempt counter.
    native["readback_fault"] = "missing"
    result = native["invoke"]("import", str(FIXTURES / "minimal.csv"), "--data-type", DEST)
    assert result.exit_code == 1 and len(native["posts"]) == 2
    assert "posted=0 verified=0" in result.output and "lower bounds" in result.output


def test_dry_run_conflicting_identity_and_explicit_missing_title(native, tmp_path):
    path = write_source(
        tmp_path, "timestamp,title,n\n2026-01-01T00:00:00Z,note,1\n2026-01-01T00:00:00Z,note,2\n"
    )
    result = native["invoke"]("import", path, "--data-type", DEST, "--value-col", "n", "--dry-run")
    assert result.exit_code == 1 and "conflicting" in result.output and not native["paths"]
    path = write_source(tmp_path, "timestamp,n\n2026-01-01T00:00:00Z,1\n")
    result = native["invoke"](
        "import", path, "--data-type", DEST, "--value-col", "n", "--title-col", "title", "--dry-run"
    )
    assert result.exit_code == 1 and "header" in result.output and not native["paths"]


def test_writer_settings_snapshot_does_not_mutate_supplied_settings(native):
    from fulcra_csv.standalone_v1 import authority, open_csv

    settings = authority(DEST)
    before = deepcopy(settings)
    with open_csv(settings, client=FulcraClient(transport=native["transport"])):
        pass
    assert settings == before


def test_recovery_progress_before_receipt_failure_is_separate(native, tmp_path, monkeypatch):
    from fulcra_collect.plugin import RunContext

    path = write_source(tmp_path, "timestamp,title\n2026-01-01T00:00:00Z,original\n")
    args = ["import", path, "--data-type", DEST]
    native["lost"] = True
    assert native["invoke"](*args).exit_code == 1
    native["lost"] = False

    def fail_receipt(*args, **kwargs):
        raise RuntimeError("synthetic recovery receipt failure")

    monkeypatch.setattr(RunContext, "annotation", fail_receipt)
    Path(path).unlink()
    result = native["invoke"](*args)
    assert result.exit_code == 1 and "recovery failed" in result.output
    assert "recovered=1 separately" in result.output
    assert "total=0 skipped_existing=0 posted=0 verified=0" in result.output
    assert len(native["posts"]) == 1


@pytest.mark.parametrize(
    "delta", [["--unit", "lb"], ["--aggregation", "cumulative"], ["--description", "changed"]]
)
def test_same_name_metric_uncertain_metadata_cannot_bypass_journal(native, delta):
    native["dest"] = DEST.replace("Event/", "Metric/")
    native["catalog_absent"] = True
    native["create_lost"] = True
    args = ["bootstrap", "--base-type", "Metric", "--name", "Metric synthetic", "--unit", "kg"]
    assert native["invoke"](*args).exit_code == 1
    assert native["invoke"](*args, *delta).exit_code == 1
    creates = [p for m, p in native["paths"] if m == "POST" and "/input/" in p]
    assert len(creates) == 1


def test_bootstrap_name_key_exact_canonical_json():
    import hashlib
    from fulcra_csv.v1 import csv_spec

    name = "  Synthetic ü name  "
    expected = (
        "csv.bootstrap.v1."
        + hashlib.sha256(
            json.dumps(
                {"name": name},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
    )
    first = csv_spec("Event", name=name, bootstrap=True)
    second = csv_spec("Metric", name=name, unit="kg", bootstrap=True)
    assert first.key == second.key == expected and first.fingerprint != second.fingerprint
    assert first.key != csv_spec("Event", name=name.strip(), bootstrap=True).key


def test_explicit_legacy_selection_is_authority_only(native):
    result = native["invoke"](
        "import",
        str(FIXTURES / "minimal.csv"),
        "--data-type",
        DEST,
        "--definition-id",
        "opaque-legacy-selection",
    )
    assert result.exit_code == 0, result.output
    assert all(len(row["sources"]) == 1 for row in native["posts"])
    assert not any("/annotation" in p or "/v1alpha1/event/" in p for _, p in native["paths"])


def test_excluded_exact_existing_is_repaired_before_skip(native, tmp_path):
    from datetime import datetime, timezone
    from fulcra_common.record_exclusions import _record

    path = write_source(tmp_path, "timestamp,title\n2026-01-01T00:00:00Z,revived\n")
    args = ["import", path, "--data-type", DEST]
    assert native["invoke"](*args).exit_code == 0
    row = native["posts"][0]
    exclusion = _record(
        CONTROL,
        DEST,
        row["id"],
        action_id=TAG,
        requested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        reason="operator",
        evidence=[],
    )
    native["rows"][CONTROL, exclusion["id"]] = exclusion
    result = native["invoke"](*args)
    assert result.exit_code == 0, result.output
    assert (DEST, row["id"]) not in native["rows"]
    assert "skipped_existing=1 posted=0 verified=0" in result.output


def test_known_exact_skip_survives_later_tag_failure(native, tmp_path, monkeypatch):
    from fulcra_csv import parse_csv, ColumnMap
    from fulcra_csv.v1 import build_csv_record
    from fulcra_collect.standalone_v1 import StandaloneRecordSession

    path = write_source(
        tmp_path,
        "timestamp,title,service\n2026-01-01T00:00:00Z,existing,\n2026-01-01T00:00:00Z,new,synthetic\n",
    )
    event = next(
        parse_csv(
            Path(path),
            column_map=ColumnMap(tag="service"),
            source_id_prefix="com.fulcradynamics.csv.v1",
        )
    )
    row = build_csv_record(event, DEST)
    native["rows"][DEST, row["id"]] = row

    def fail_tag(*args, **kwargs):
        raise RuntimeError("synthetic metadata failure")

    monkeypatch.setattr(StandaloneRecordSession, "resolve_tag", fail_tag)
    result = native["invoke"]("import", path, "--data-type", DEST, "--tag-col", "service")
    assert result.exit_code == 1
    assert "total=2 skipped_existing=1 posted=0 verified=0" in result.output
    assert not native["posts"]


def test_metric_bootstrap_defaults_explicit_unitless_then_import(native, tmp_path):
    native["dest"] = DEST.replace("Event/", "Metric/")
    native["catalog_absent"] = True
    result = native["invoke"]("bootstrap", "--base-type", "Metric", "--name", "Unitless synthetic")
    assert result.exit_code == 0, result.output
    body = native["created_body"]
    assert body["record_spec"]["aggregation"] == "discrete" and "unit" not in body["record_spec"]
    path = write_source(tmp_path, "timestamp,title,n\n2026-01-01T00:00:00Z,note,7\n")
    result = native["invoke"]("import", path, "--data-type", native["dest"], "--value-col", "n")
    assert result.exit_code == 0, result.output
    assert native["posts"][0]["value"] == 7 and native["posts"][0]["csv_data_fields"] == {}


def test_mapped_metric_unit_wins_over_constant_and_must_match(native, tmp_path):
    native["dest"] = DEST.replace("Event/", "Metric/")
    path = write_source(tmp_path, "timestamp,title,n,u\n2026-01-01T00:00:00Z,note,7,lb\n")
    result = native["invoke"](
        "import",
        path,
        "--data-type",
        native["dest"],
        "--value-col",
        "n",
        "--unit",
        "kg",
        "--data-field",
        "u=unit",
    )
    assert result.exit_code == 1 and "row unit" in result.output and not native["posts"]


def test_last_uuid_primary_proof_finishes_before_tags(native, tmp_path):
    from fulcra_csv import parse_csv
    from fulcra_csv.v1 import build_csv_record

    path = write_source(
        tmp_path,
        "timestamp,title\n" + "".join(f"2026-01-01T00:00:00Z,row{i}\n" for i in range(501)),
    )
    event = list(
        parse_csv(Path(path), default_tag="synthetic", source_id_prefix="com.fulcradynamics.csv.v1")
    )[-1]
    row = build_csv_record(event, DEST)
    row["sources"] = ["wrong.primary"]
    native["rows"][DEST, row["id"]] = row
    result = native["invoke"]("import", path, "--data-type", DEST, "--tag", "synthetic")
    assert result.exit_code == 1 and "primary source" in result.output
    assert not native["posts"] and not any("/tag/" in p for _, p in native["paths"])


def test_pending_changed_family_schema_retained_before_source(native, tmp_path, monkeypatch):
    from fulcra_csv.v1 import FIELDS
    from fulcra_collect import db

    path = write_source(tmp_path, "timestamp,title\n2026-01-01T00:00:00Z,original\n")
    args = ["import", path, "--data-type", DEST]
    native["lost"] = True
    assert native["invoke"](*args).exit_code == 1
    native["lost"] = False
    before = db.get_record_outbox(
        db.open(native["profile"] / "state.db"), "cli-csv", "v1_csv_pending"
    )
    Path(path).unlink()
    monkeypatch.setitem(FIELDS, "future_field", {"type": "string"})
    result = native["invoke"](*args)
    assert result.exit_code == 1 and "configuration changed" in result.output
    after = db.get_record_outbox(
        db.open(native["profile"] / "state.db"), "cli-csv", "v1_csv_pending"
    )
    assert after == before and len(native["posts"]) == 1


def test_codec_instant_end_is_always_start():
    from datetime import datetime, timedelta, timezone
    from fulcra_csv.events import GenericEvent, INSTANT
    from fulcra_csv.v1 import build_csv_record

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    event = GenericEvent(
        start_time=start,
        end_time=start + timedelta(hours=1),
        note="instant",
        title=None,
        source_id="synthetic.instant",
        annotation_type=INSTANT,
    )
    row = build_csv_record(event, DEST)
    assert row["end_time"] == row["start_time"]


def test_native_read_unknown_is_not_empty(native):
    native["read_error"] = True
    result = native["invoke"](
        "import", str(FIXTURES / "minimal.csv"), "--data-type", DEST, "--tag", "synthetic"
    )
    assert result.exit_code == 1 and not native["posts"]
    assert not any("/tag/" in p for _, p in native["paths"])
    assert "failed" in result.output and "total=2" in result.output


def test_preview_with_real_pending_never_opens_or_recovers_it(native, tmp_path, monkeypatch):
    from fulcra_collect import db, standalone_v1

    path = write_source(tmp_path, "timestamp,title\n2026-01-01T00:00:00Z,original\n")
    args = ["import", path, "--data-type", DEST]
    native["lost"] = True
    assert native["invoke"](*args).exit_code == 1
    conn = db.open(native["profile"] / "state.db")
    before = db.get_record_outbox(conn, "cli-csv", "v1_csv_pending")
    assert before["groups"]
    native["paths"].clear()

    def forbidden(*args, **kwargs):
        pytest.fail("preview entered runtime or opened DB")

    monkeypatch.setattr(standalone_v1, "standalone_runtime", forbidden)
    actual_open = db.open
    monkeypatch.setattr(db, "open", forbidden)
    result = native["invoke"](*args, "--dry-run")
    assert result.exit_code == 0, result.output
    assert not native["paths"] and len(native["posts"]) == 1
    monkeypatch.setattr(db, "open", actual_open)
    after = db.get_record_outbox(conn, "cli-csv", "v1_csv_pending")
    assert after == before


@pytest.mark.parametrize("constraint", ["count", "uuid", "conditional", "retained-union"])
def test_final_tag_proof_precedes_missing_tag_creation_and_exclusion_repair(
    native, tmp_path, constraint
):
    from datetime import datetime, timezone
    from fulcra_csv import parse_csv, ColumnMap
    from fulcra_csv.v1 import build_csv_record
    from fulcra_common.record_exclusions import _record

    path = write_source(
        tmp_path,
        "timestamp,title,service\n2026-01-01T00:00:00Z,excluded,\n2026-01-01T00:00:00Z,new,missing\n",
    )
    events = list(
        parse_csv(
            Path(path),
            column_map=ColumnMap(tag="service"),
            source_id_prefix="com.fulcradynamics.csv.v1",
        )
    )
    revived = build_csv_record(events[0], DEST)
    native["rows"][DEST, revived["id"]] = revived
    exclusion = _record(
        CONTROL,
        DEST,
        revived["id"],
        action_id=TAG,
        requested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        reason="operator",
        evidence=[],
    )
    native["rows"][CONTROL, exclusion["id"]] = exclusion
    native["missing_tags"] = {"missing"}
    if constraint == "count":
        native["extra_schema"] = {"tags": {"type": "array", "maxItems": 0}}
    elif constraint == "uuid":
        native["extra_schema"] = {"tags": {"type": "array", "items": {"enum": [TAG]}}}
    elif constraint == "conditional":
        native["schema_rules"] = {
            "if": {"properties": {"csv_tag": {"const": "missing"}}},
            "then": {"properties": {"tags": {"maxItems": 0}}},
        }
    else:
        prior = build_csv_record(events[1], DEST, tags=[TAG])
        native["rows"][DEST, prior["id"]] = prior
        native["extra_schema"] = {"tags": {"type": "array", "maxItems": 1}}
    result = native["invoke"]("import", path, "--data-type", DEST, "--tag-col", "service")
    assert result.exit_code == 1, result.output
    assert not any(method == "POST" for method, _ in native["paths"]), native["paths"]
    assert (DEST, revived["id"]) in native["rows"]


def test_missing_tag_plain_uuid_schema_creates_only_after_plan(native, tmp_path):
    native["missing_tags"] = {"missing"}
    native["extra_schema"] = {
        "tags": {
            "type": "array",
            "items": {"type": "string", "format": "uuid"},
            "uniqueItems": True,
        }
    }
    path = write_source(tmp_path, "timestamp,title\n2026-01-01T00:00:00Z,new\n")
    result = native["invoke"]("import", path, "--data-type", DEST, "--tag", "missing")
    assert result.exit_code == 0, result.output
    actual_id = str(uuid5(NAMESPACE_URL, "missing"))
    planned_id = str(uuid5(NAMESPACE_URL, "csv:planned-tag:missing"))
    assert native["posts"][0]["tags"] == [actual_id]
    assert actual_id != planned_id
    from fulcra_collect import db

    pending = db.get_record_outbox(
        db.open(native["profile"] / "state.db"), "cli-csv", "v1_csv_pending"
    )
    assert pending == {"groups": []}
    tag_post = native["paths"].index(("POST", "/user/v1alpha1/tag"))
    assert ("GET", "/data/v1/catalog/" + DEST + "/v1/schema") in native["paths"][:tag_post]


@pytest.mark.parametrize("constraint", ["count-uuid", "conditional"])
def test_known_tags_support_constrained_schema_without_tag_creation(native, tmp_path, constraint):
    if constraint == "count-uuid":
        native["extra_schema"] = {
            "tags": {"type": "array", "maxItems": 1, "items": {"enum": [TAG]}}
        }
    else:
        native["schema_rules"] = {
            "if": {"properties": {"csv_tag": {"const": "synthetic"}}},
            "then": {"properties": {"tags": {"maxItems": 1, "items": {"enum": [TAG]}}}},
        }
    path = write_source(tmp_path, "timestamp,title\n2026-01-01T00:00:00Z,new\n")
    result = native["invoke"]("import", path, "--data-type", DEST, "--tag", "synthetic")
    assert result.exit_code == 0, result.output
    assert native["posts"][0]["tags"] == [TAG]
    assert ("POST", "/user/v1alpha1/tag") not in native["paths"]


def test_successful_import_remembers_preexisting_tag_across_rename(native, tmp_path):
    from fulcra_common.data_types import CreationJournal

    path = write_source(tmp_path, "timestamp,title\n2026-01-01T00:00:00Z,new\n")
    args = ["import", path, "--data-type", DEST, "--tag", "synthetic"]
    result = native["invoke"](*args)
    assert result.exit_code == 0, result.output
    journal = CreationJournal(native["profile"] / "v1-type-creations.sqlite", read_only=True)
    assert journal.read_tag(ACCOUNT, "synthetic") == ("resolved", TAG)
    native["tags"] = {"renamed": TAG}
    native["missing_tags"] = {"synthetic"}
    native["paths"].clear()
    result = native["invoke"](*args)
    assert result.exit_code == 0 and "skipped_existing=1" in result.output, result.output
    assert ("GET", "/user/v1alpha1/tag/id/" + TAG) in native["paths"]
    assert ("POST", "/user/v1alpha1/tag") not in native["paths"]


def test_restrictive_known_tag_refusal_does_not_remember_binding(native, tmp_path):
    from fulcra_common.data_types import CreationJournal

    native["extra_schema"] = {"tags": {"type": "array", "maxItems": 0}}
    path = write_source(tmp_path, "timestamp,title\n2026-01-01T00:00:00Z,new\n")
    result = native["invoke"]("import", path, "--data-type", DEST, "--tag", "synthetic")
    assert result.exit_code == 1, result.output
    assert not any(method == "POST" for method, _ in native["paths"])
    journal = CreationJournal(native["profile"] / "v1-type-creations.sqlite", read_only=True)
    assert journal.read_tag(ACCOUNT, "synthetic") is None
