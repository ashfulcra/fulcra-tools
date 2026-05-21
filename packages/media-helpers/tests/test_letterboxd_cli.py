"""Integration tests for `fulcra-media import letterboxd`."""
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
    state_path = tmp_path / "state.json"
    s = State(
        watched_definition_id="def-watched-uuid",
        listened_definition_id=None,
        tag_ids={"letterboxd": "tag-letterboxd-uuid"},
    )
    from fulcra_media import state as state_mod
    state_mod.save(s, state_path)
    monkeypatch.setattr("fulcra_media.cli.STATE_PATH", state_path)
    return state_path


def _make_sample_events() -> list[NormalizedEvent]:
    """Build four canned events matching the letterboxd_sample.xml fixture."""
    from fulcra_media.importers.base import content_fingerprint
    base = []
    rows = [
        ("Sigur Rós Live", 2026, datetime(2026, 5, 12, 23, 30, tzinfo=timezone.utc),
         "99001", "No"),
        ("The Fifth Element", 1997, datetime(2026, 5, 11, 21, 0, tzinfo=timezone.utc),
         "99002", "Yes"),
        ("Unknown Film", None, datetime(2026, 5, 10, 17, 0, tzinfo=timezone.utc),
         "99003", "No"),
        ("Past Lives", 2023, datetime(2026, 5, 9, 2, 15, tzinfo=timezone.utc),
         "99004", "No"),
    ]
    for (title, year, ts, guid, rw) in rows:
        ext = {
            "feed_url": "https://letterboxd.com/ash/rss/",
            "guid": f"letterboxd-watch-{guid}",
            "film_title": title,
            "rewatch": rw,
            "content_fingerprint": content_fingerprint(
                "movie", title=title, year=year),
        }
        if year:
            ext["film_year"] = str(year)
        base.append(NormalizedEvent(
            importer="letterboxd", service="letterboxd", category="watched",
            note=title, title=title,
            start_time=ts, end_time=ts.replace(second=1),
            deterministic_id=f"com.fulcra.media.letterboxd.v1.{guid}aaaaaaaa",
            timestamp_confidence="high", external_ids=ext,
        ))
    return base


def test_letterboxd_cli_missing_definition_emits_error_envelope(
    tmp_path, monkeypatch,
):
    state_path = tmp_path / "state.json"
    from fulcra_media import state as state_mod
    state_mod.save(State(), state_path)
    monkeypatch.setattr("fulcra_media.cli.STATE_PATH", state_path)

    res = CliRunner().invoke(cli, ["import", "letterboxd",
                                    "--username", "ash", "--json"])
    assert res.exit_code == 2
    payload = json.loads(res.output.strip())
    assert payload["ok"] is False
    assert payload["errors"][0]["stage"] == "setup"


def test_letterboxd_cli_missing_username_errors_via_click(fake_state):
    """Click's own required-option machinery rejects the call before we run."""
    res = CliRunner().invoke(cli, ["import", "letterboxd", "--json"])
    # Click exits 2 on usage error with --json absent in its stderr path; the
    # important thing is we don't crash and the user is told what's missing.
    assert res.exit_code != 0
    assert "username" in res.output.lower() or "username" in (res.stderr or "").lower()


def test_letterboxd_cli_cold_start_no_watermark(fake_state, monkeypatch):
    captured_urls: list[str] = []
    sample_events = _make_sample_events()

    def fake_fetch(username, *, transport=None):
        captured_urls.append(f"https://letterboxd.com/{username}/rss/")
        return iter(sample_events)

    monkeypatch.setattr(
        "fulcra_media.importers.letterboxd.fetch_diary", fake_fetch,
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

    res = CliRunner().invoke(cli, ["import", "letterboxd",
                                    "--username", "ash", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output.strip())
    assert payload["ok"] is True
    assert payload["importer"] == "letterboxd"
    assert payload["since_watermark"] is None
    assert payload["posted"] == 4
    assert payload["new_watermark"] is not None
    assert captured_urls == ["https://letterboxd.com/ash/rss/"]


def test_letterboxd_cli_watermark_driven_incremental(fake_state, monkeypatch):
    from fulcra_media import state as state_mod, watermarks
    s = state_mod.load(fake_state)
    watermarks.set_iso(s, "letterboxd",
                       datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc))
    state_mod.save(s, fake_state)

    sample_events = _make_sample_events()

    monkeypatch.setattr(
        "fulcra_media.importers.letterboxd.fetch_diary",
        lambda username, **kw: iter(sample_events),
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

    res = CliRunner().invoke(cli, ["import", "letterboxd",
                                    "--username", "ash", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output.strip())
    # When a watermark is set, since_watermark surfaces (watermark - 1h overlap)
    # to catch any late server-side reordering.
    assert payload["since_watermark"] == "2026-05-10T23:00:00+00:00"


def test_letterboxd_cli_check_only_does_not_post(fake_state, monkeypatch):
    sample_events = _make_sample_events()
    monkeypatch.setattr(
        "fulcra_media.importers.letterboxd.fetch_diary",
        lambda username, **kw: iter(sample_events),
    )
    posted: list[bool] = []

    def fake_run(self, events, state, *, check_only=False, **kw):
        posted.append(check_only)
        return ImportResult(
            total=len(events), skipped_existing=0,
            posted=len(events) if check_only else 0, verified=0,
        )
    monkeypatch.setattr("fulcra_media.fulcra.FulcraClient.run_import", fake_run)

    res = CliRunner().invoke(cli, ["import", "letterboxd",
                                    "--username", "ash",
                                    "--check-only", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output.strip())
    assert payload["would_post"] == 4
    assert posted == [True]
    # No watermark update on --check-only.
    from fulcra_media import state as state_mod, watermarks
    s2 = state_mod.load(fake_state)
    assert watermarks.get_iso(s2, "letterboxd") is None


def test_letterboxd_cli_json_envelope_shape(fake_state, monkeypatch):
    sample_events = _make_sample_events()
    monkeypatch.setattr(
        "fulcra_media.importers.letterboxd.fetch_diary",
        lambda username, **kw: iter(sample_events),
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

    res = CliRunner().invoke(cli, ["import", "letterboxd",
                                    "--username", "ash", "--json"])
    assert res.exit_code == 0
    payload = json.loads(res.output.strip())
    required = {
        "importer", "ok", "total", "skipped_existing", "posted", "verified",
        "since_watermark", "new_watermark", "would_post", "errors",
    }
    assert required <= set(payload.keys())
    lines = [ln for ln in res.output.split("\n") if ln.strip()]
    assert len(lines) == 1


def test_letterboxd_cli_fetch_error_surfaces_in_envelope(fake_state, monkeypatch):
    import httpx

    def boom(username, **kw):
        raise httpx.HTTPStatusError(
            "404 Not Found",
            request=httpx.Request("GET", "https://letterboxd.com/x/rss/"),
            response=httpx.Response(404),
        )
    monkeypatch.setattr(
        "fulcra_media.importers.letterboxd.fetch_diary", boom,
    )
    res = CliRunner().invoke(cli, ["import", "letterboxd",
                                    "--username", "nobody", "--json"])
    assert res.exit_code == 2
    payload = json.loads(res.output.strip())
    assert payload["ok"] is False
    assert payload["errors"][0]["stage"] == "fetch"


def test_letterboxd_cli_invalid_since_format_emits_args_error(fake_state):
    res = CliRunner().invoke(cli, [
        "import", "letterboxd", "--username", "ash",
        "--since", "not a date", "--json",
    ])
    assert res.exit_code == 2
    payload = json.loads(res.output.strip())
    assert payload["ok"] is False
    assert payload["errors"][0]["stage"] == "args"


def test_letterboxd_wizard_prints_setup_steps():
    res = CliRunner().invoke(cli, ["wizard", "letterboxd"])
    assert res.exit_code == 0
    assert "letterboxd" in res.output.lower()
    assert "/rss/" in res.output
    assert "rewatch" in res.output.lower()


def test_generic_rss_cli_imports_arbitrary_feed(fake_state, monkeypatch):
    """import generic-rss <url> --service x --category watched works end-to-end."""
    raw = (FIXTURES / "letterboxd_sample.xml").read_bytes()
    # Patch fetch_feed so the CLI sees a parsed feedparser dict — cleaner than
    # monkeying around with httpx.Client constructor patching.
    import feedparser
    monkeypatch.setattr(
        "fulcra_media.importers.generic_rss.fetch_feed",
        lambda url, **kw: feedparser.parse(raw),
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

    res = CliRunner().invoke(cli, [
        "import", "generic-rss", "https://example.org/feed.xml",
        "--service", "letterboxd",
        "--category", "watched",
        "--json",
    ])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output.strip())
    assert payload["ok"] is True
    assert payload["importer"] == "generic-rss:letterboxd"
    assert payload["posted"] == 4


def test_generic_rss_cli_distinct_watermark_per_feed(fake_state, monkeypatch):
    """Each feed URL gets its own watermark key."""
    from fulcra_media import state as state_mod, watermarks
    s = state_mod.load(fake_state)
    watermarks.set_iso(
        s, "generic-rss:https://example.org/feedA.xml",
        datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
    )
    state_mod.save(s, fake_state)

    # Confirm only feedA's watermark gets read; feedB starts cold.
    raw = (FIXTURES / "letterboxd_sample.xml").read_bytes()
    import feedparser
    monkeypatch.setattr(
        "fulcra_media.importers.generic_rss.fetch_feed",
        lambda url, **kw: feedparser.parse(raw),
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

    res = CliRunner().invoke(cli, [
        "import", "generic-rss", "https://example.org/feedB.xml",
        "--service", "letterboxd", "--category", "watched", "--json",
    ])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output.strip())
    # feedB had no watermark — should be cold-start.
    assert payload["since_watermark"] is None
