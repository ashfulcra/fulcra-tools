"""CLI uses actual JournalWriter/session/outbox; all network is synthetic."""

import json
import re

import httpx
import pytest
from click.testing import CliRunner

from fulcra_collect import db
from fulcra_common.data_types import CreationJournal
from fulcra_common.record_exclusions import EXCLUSION_SPEC
from fulcra_dayone import cli as cli_mod
from fulcra_dayone.client import DayOneFulcraClient
from fulcra_dayone.v1 import JOURNAL_SPEC, build_journal_record
from test_collect_v1 import ACCOUNT, DESTINATION, CONTROL, TAG, entry, token


@pytest.mark.parametrize(
    "mode",
    [
        "visible",
        "edited",
        "lost-visible",
        "lost-absent",
        "changed-options",
        "changed-account",
    ],
)
def test_cli_shared_writer_restart_and_import_once(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path / "profile"))
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", token())
    monkeypatch.setattr(
        httpx.HTTPTransport,
        "handle_request",
        lambda *_: pytest.fail("attempted live network"),
    )
    profile = tmp_path / "profile"
    profile.mkdir()
    CreationJournal(profile / "v1-type-creations.sqlite").resolved(
        ACCOUNT, EXCLUSION_SPEC, CONTROL
    )
    original = build_journal_record(entry(), DESTINATION, tags=[TAG])
    rows, posts, source_reads, paths = {}, [], [], []
    auth_account = [ACCOUNT]
    if mode == "edited":
        rows[DESTINATION, original["id"]] = {
            **original,
            "note": "retained edit",
            "tags": [],
        }

    def handle(request):
        path = request.url.path
        paths.append((request.method, path))
        assert request.headers["Authorization"] == "Bearer " + token()
        if path == "/user/v1alpha1/info":
            return httpx.Response(200, json={"userid": auth_account[0]})
        if path == "/input/v1/data_type":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": full,
                        "fulcra_userid": ACCOUNT,
                        "name": name,
                        "api_version": "v1",
                        "recordable": True,
                        "deprecated": False,
                        "record_spec": {"type": "event"},
                    }
                    for full, name in (
                        (DESTINATION, "Journal"),
                        (CONTROL, EXCLUSION_SPEC.name),
                    )
                ],
            )
        if path.endswith("/schema"):
            spec = EXCLUSION_SPEC if CONTROL in path else JOURNAL_SPEC
            return httpx.Response(
                200,
                json={
                    "type": "object",
                    "properties": {
                        **{
                            k: {}
                            for k in ("id", "sources", "tags", "start_time", "end_time")
                        },
                        **spec.fields,
                    },
                    "required": list(spec.required_fields),
                },
            )
        if "/tag/name/" in path or "/tag/id/" in path:
            return httpx.Response(
                200, json={"id": TAG, "name": "synthetic", "fulcra_userid": ACCOUNT}
            )
        if path == "/data/v1/records":
            query = request.url.params["q"]
            full = re.search('__name__="([^"]+)"', query).group(1)
            ids = re.findall('id="([^"]+)"', query)
            return httpx.Response(
                200,
                text="\n".join(
                    json.dumps(rows[full, i]) for i in ids if (full, i) in rows
                ),
            )
        if path == "/ingest/v1/record/" + DESTINATION:
            batch = [json.loads(line) for line in request.content.splitlines()]
            assert batch == [original]
            posts.extend(batch)
            if mode != "lost-absent":
                rows.update({(DESTINATION, row["id"]): row for row in batch})
            if mode.startswith("lost") or mode.startswith("changed"):
                raise httpx.ReadError("synthetic lost receipt")
            return httpx.Response(201, json={"upload_id": "synthetic"})
        pytest.fail("unexpected path " + path)

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(
        cli_mod, "DayOneFulcraClient", lambda: DayOneFulcraClient(transport=transport)
    )
    monkeypatch.setattr(
        cli_mod, "read", lambda *a, **kw: source_reads.append(1) or [entry()]
    )
    args = ["import", str(tmp_path / "source"), "--all", "--destination", DESTINATION]
    result = CliRunner().invoke(cli_mod.cli, args)
    first_failed = mode.startswith("lost") or mode.startswith("changed")
    assert result.exit_code == int(first_failed), repr(result.exception) + result.output
    assert len(posts) == int(mode != "edited"), repr(result.exception)
    pending_before = db.get_record_outbox(
        db.open(profile / "state.db"), "cli-dayone", "v1_dayone_pending"
    )
    if first_failed:
        assert pending_before["groups"]
        # A source access on restart must happen only after pending recovery.
        monkeypatch.setattr(
            cli_mod, "read", lambda *a, **kw: source_reads.append(1) or []
        )
        if mode == "changed-options":
            args += ["--starred"]
        if mode == "changed-account":
            auth_account[0] = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        result = CliRunner().invoke(cli_mod.cli, args)
        assert result.exit_code == int(mode != "lost-visible"), result.output
        assert len(source_reads) == (2 if mode == "lost-visible" else 1)
        assert len(posts) == 1
        after = db.get_record_outbox(
            db.open(profile / "state.db"), "cli-dayone", "v1_dayone_pending"
        )
        if mode != "lost-visible":
            assert after == pending_before
        else:
            assert after == {"groups": []}
    if mode == "edited":
        assert rows[DESTINATION, original["id"]]["note"] == "retained edit"
        assert not any("/tag/" in p for _, p in paths)
    assert not any("/annotation" in p or "/v1alpha1/event/" in p for _, p in paths)
    assert (
        db.get_record_outbox(
            db.open(profile / "state.db"), "dayone", "v1_dayone_pending"
        )
        is None
    )
