"""Integration tests for `fulcra-media import goodreads`."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from fulcra_media.cli import cli
from fulcra_media.fulcra import ImportResult
from fulcra_media.importers.base import NormalizedEvent
from fulcra_media.state import State

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fake_state(tmp_path, monkeypatch):
    """A state file with the read_definition_id slot populated."""
    state_path = tmp_path / "state.json"
    s = State(
        watched_definition_id=None,
        listened_definition_id=None,
        read_definition_id="def-read-uuid",
        tag_ids={"goodreads": "tag-goodreads-uuid"},
    )
    from fulcra_media import state as state_mod
    state_mod.save(s, state_path)
    monkeypatch.setattr("fulcra_media.cli.STATE_PATH", state_path)
    return state_path


def _make_sample_events() -> list[NormalizedEvent]:
    """Three canned events corresponding to goodreads_sample.xml."""
    from fulcra_media.importers.base import content_fingerprint
    out: list[NormalizedEvent] = []
    rows = [
        ("The Hobbit", "J.R.R. Tolkien", 1937,
         datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc), "8000000001", "high", 5),
        ("Project Hail Mary", "Andy Weir", 2021,
         datetime(2026, 5, 10, 19, 30, tzinfo=timezone.utc), "8000000002", "medium", 4),
        ("Unknown Pleasures", "Anonymous", None,
         datetime(2026, 5, 7, 0, 0, tzinfo=timezone.utc), "8000000003", "high", None),
    ]
    for (title, author, year, ts, guid, conf, rating) in rows:
        fp_kwargs = {"title": title, "author": author}
        if year:
            fp_kwargs["year"] = year
        ext = {
            "feed_url": "https://www.goodreads.com/review/list_rss/12345?shelf=read",
            "review_guid": f"https://www.goodreads.com/review/show/{guid}",
            "book_title": title,
            "author": author,
            "content_fingerprint": content_fingerprint("book", **fp_kwargs),
        }
        if rating is not None:
            ext["rating"] = rating
        if year:
            ext["book_published_year"] = year
        out.append(NormalizedEvent(
            importer="goodreads", service="goodreads", category="read",
            note=f"{author} – {title}", title=title,
            start_time=ts, end_time=ts.replace(second=1),
            deterministic_id=f"com.fulcra.media.goodreads.v1.{guid[:16]}",
            timestamp_confidence=conf, external_ids=ext,
        ))
    return out


def test_goodreads_cli_missing_read_definition_emits_error_envelope(
    tmp_path, monkeypatch,
):
    state_path = tmp_path / "state.json"
    from fulcra_media import state as state_mod
    state_mod.save(State(), state_path)  # no read_definition_id
    monkeypatch.setattr("fulcra_media.cli.STATE_PATH", state_path)

    res = CliRunner().invoke(cli, ["import", "goodreads",
                                    "--user-id", "12345", "--json"])
    assert res.exit_code == 2
    payload = json.loads(res.output.strip())
    assert payload["ok"] is False
    assert payload["errors"][0]["stage"] == "setup"


def test_goodreads_cli_missing_user_id_errors_via_click(fake_state):
    """Click's required-option machinery rejects the call before we run."""
    res = CliRunner().invoke(cli, ["import", "goodreads", "--json"])
    assert res.exit_code != 0
    assert "user-id" in res.output.lower() or "user-id" in (res.stderr or "").lower()


def test_goodreads_cli_cold_start_no_watermark(fake_state, monkeypatch):
    sample_events = _make_sample_events()
    captured_user_ids: list[str] = []

    def fake_fetch(user_id, *, transport=None):
        captured_user_ids.append(user_id)
        return iter(sample_events)

    monkeypatch.setattr(
        "fulcra_media.importers.goodreads.fetch_diary", fake_fetch,
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.ensure_tag",
        lambda self, name, state: "tag",
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.run_import",
        lambda self, events, state, **kw: ImportResult(
            total=len(events), skipped_existing=0,
            posted=len(events), verified=len(events),
        ),
    )

    res = CliRunner().invoke(cli, ["import", "goodreads",
                                    "--user-id", "12345", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output.strip())
    assert payload["ok"] is True
    assert payload["importer"] == "goodreads"
    assert payload["since_watermark"] is None
    assert payload["posted"] == 3
    assert payload["new_watermark"] is not None
    assert captured_user_ids == ["12345"]


def test_goodreads_cli_watermark_keyed_off_user_id(fake_state, monkeypatch):
    """Watermark name must include the user_id so polling multiple accounts works."""
    from fulcra_media import state as state_mod, watermarks
    s = state_mod.load(fake_state)
    watermarks.set_iso(
        s, "goodreads:12345",
        datetime(2026, 5, 9, 0, 0, tzinfo=timezone.utc),
    )
    state_mod.save(s, fake_state)

    sample_events = _make_sample_events()
    monkeypatch.setattr(
        "fulcra_media.importers.goodreads.fetch_diary",
        lambda user_id, **kw: iter(sample_events),
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.ensure_tag",
        lambda self, name, state: "tag",
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.run_import",
        lambda self, events, state, **kw: ImportResult(
            total=len(events), skipped_existing=0,
            posted=len(events), verified=len(events),
        ),
    )

    res = CliRunner().invoke(cli, ["import", "goodreads",
                                    "--user-id", "12345", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output.strip())
    # Watermark - 1h overlap window.
    assert payload["since_watermark"] == "2026-05-08T23:00:00+00:00"


def test_goodreads_cli_distinct_watermark_per_user(fake_state, monkeypatch):
    """user_id=12345 has a watermark; user_id=99999 should still cold-start."""
    from fulcra_media import state as state_mod, watermarks
    s = state_mod.load(fake_state)
    watermarks.set_iso(
        s, "goodreads:12345",
        datetime(2026, 5, 9, 0, 0, tzinfo=timezone.utc),
    )
    state_mod.save(s, fake_state)

    sample_events = _make_sample_events()
    monkeypatch.setattr(
        "fulcra_media.importers.goodreads.fetch_diary",
        lambda user_id, **kw: iter(sample_events),
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.ensure_tag",
        lambda self, name, state: "tag",
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.run_import",
        lambda self, events, state, **kw: ImportResult(
            total=len(events), skipped_existing=0,
            posted=len(events), verified=len(events),
        ),
    )

    res = CliRunner().invoke(cli, ["import", "goodreads",
                                    "--user-id", "99999", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output.strip())
    assert payload["since_watermark"] is None


def test_goodreads_cli_check_only_does_not_post(fake_state, monkeypatch):
    sample_events = _make_sample_events()
    monkeypatch.setattr(
        "fulcra_media.importers.goodreads.fetch_diary",
        lambda user_id, **kw: iter(sample_events),
    )
    posted_flags: list[bool] = []

    def fake_run(self, events, state, *, check_only=False, **kw):
        posted_flags.append(check_only)
        return ImportResult(
            total=len(events), skipped_existing=0,
            posted=len(events) if check_only else 0, verified=0,
        )
    monkeypatch.setattr("fulcra_media.fulcra.FulcraClient.run_import", fake_run)

    res = CliRunner().invoke(cli, ["import", "goodreads",
                                    "--user-id", "12345",
                                    "--check-only", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output.strip())
    assert payload["would_post"] == 3
    assert posted_flags == [True]
    # No watermark update on --check-only.
    from fulcra_media import state as state_mod, watermarks
    s2 = state_mod.load(fake_state)
    assert watermarks.get_iso(s2, "goodreads:12345") is None


def test_goodreads_cli_json_envelope_shape(fake_state, monkeypatch):
    sample_events = _make_sample_events()
    monkeypatch.setattr(
        "fulcra_media.importers.goodreads.fetch_diary",
        lambda user_id, **kw: iter(sample_events),
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.ensure_tag",
        lambda self, name, state: "tag",
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.run_import",
        lambda self, events, state, **kw: ImportResult(
            total=len(events), skipped_existing=0,
            posted=len(events), verified=len(events),
        ),
    )

    res = CliRunner().invoke(cli, ["import", "goodreads",
                                    "--user-id", "12345", "--json"])
    assert res.exit_code == 0
    payload = json.loads(res.output.strip())
    required = {
        "importer", "ok", "total", "skipped_existing", "posted", "verified",
        "since_watermark", "new_watermark", "would_post", "errors",
    }
    assert required <= set(payload.keys())
    lines = [ln for ln in res.output.split("\n") if ln.strip()]
    assert len(lines) == 1


def test_goodreads_cli_fetch_error_surfaces_in_envelope(fake_state, monkeypatch):
    import httpx

    def boom(user_id, **kw):
        raise httpx.HTTPStatusError(
            "404 Not Found",
            request=httpx.Request(
                "GET",
                "https://www.goodreads.com/review/list_rss/0?shelf=read",
            ),
            response=httpx.Response(404),
        )
    monkeypatch.setattr(
        "fulcra_media.importers.goodreads.fetch_diary", boom,
    )
    res = CliRunner().invoke(cli, ["import", "goodreads",
                                    "--user-id", "0", "--json"])
    assert res.exit_code == 2
    payload = json.loads(res.output.strip())
    assert payload["ok"] is False
    assert payload["errors"][0]["stage"] == "fetch"


def test_goodreads_cli_invalid_since_format_emits_args_error(fake_state):
    res = CliRunner().invoke(cli, [
        "import", "goodreads", "--user-id", "12345",
        "--since", "not a date", "--json",
    ])
    assert res.exit_code == 2
    payload = json.loads(res.output.strip())
    assert payload["ok"] is False
    assert payload["errors"][0]["stage"] == "args"


def test_goodreads_cli_max_entries_caps_output(fake_state, monkeypatch):
    sample_events = _make_sample_events()
    captured: list[int] = []

    monkeypatch.setattr(
        "fulcra_media.importers.goodreads.fetch_diary",
        lambda user_id, **kw: iter(sample_events),
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.ensure_tag",
        lambda self, name, state: "tag",
    )

    def fake_run(self, events, state, **kw):
        captured.append(len(events))
        return ImportResult(
            total=len(events), skipped_existing=0,
            posted=len(events), verified=len(events),
        )
    monkeypatch.setattr("fulcra_media.fulcra.FulcraClient.run_import", fake_run)

    res = CliRunner().invoke(cli, [
        "import", "goodreads", "--user-id", "12345",
        "--max-entries", "2", "--json",
    ])
    assert res.exit_code == 0, res.output
    assert captured == [2]


def test_goodreads_wizard_prints_setup_steps():
    res = CliRunner().invoke(cli, ["wizard", "goodreads"])
    assert res.exit_code == 0
    out = res.output.lower()
    assert "goodreads" in out
    assert "user_id" in out or "user id" in out
    assert "list_rss" in out
    assert "read" in out
