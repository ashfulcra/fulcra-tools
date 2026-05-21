"""Tests for the shared --json / human-output helpers."""
import json

import click
import pytest
from click.testing import CliRunner

from fulcra_media.cli_common import (
    ImportEnvelope,
    emit_result,
    import_result_to_dict,
    safe_exc_message,
)


def test_emit_result_json_writes_one_line_to_stdout():
    runner = CliRunner()
    @click.command()
    def cmd():
        emit_result(
            ImportEnvelope(importer="lastfm", ok=True, total=3, posted=3),
            json_mode=True,
        )
    res = runner.invoke(cmd)
    assert res.exit_code == 0
    payload = json.loads(res.output.strip())
    assert payload == {
        "importer": "lastfm", "ok": True, "total": 3,
        "skipped_existing": 0, "posted": 3, "verified": 0,
        "since_watermark": None, "new_watermark": None,
        "would_post": None,
        "errors": [],
    }


def test_emit_result_json_includes_errors_list():
    runner = CliRunner()
    @click.command()
    def cmd():
        emit_result(
            ImportEnvelope(
                importer="lastfm", ok=False,
                errors=[{"stage": "fetch", "message": "rate limited"}],
            ),
            json_mode=True,
        )
    res = runner.invoke(cmd)
    payload = json.loads(res.output.strip())
    assert payload["ok"] is False
    assert payload["errors"] == [{"stage": "fetch", "message": "rate limited"}]


def test_emit_result_human_mode_writes_compact_string():
    runner = CliRunner()
    @click.command()
    def cmd():
        emit_result(
            ImportEnvelope(
                importer="lastfm", ok=True, total=10, skipped_existing=2,
                posted=8, verified=8,
            ),
            json_mode=False,
        )
    res = runner.invoke(cmd)
    out = res.output
    assert "lastfm" in out
    assert "total=10" in out
    assert "skipped_existing=2" in out
    assert "posted=8" in out


def test_emit_result_human_mode_errors_go_to_stderr():
    """Human mode: errors should be visible but not on stdout (so agents
    grepping stdout don't get noise).

    Click 8.2+ removed mix_stderr — use click.testing's default which
    separates streams when available, falling back to combined.
    """
    runner = CliRunner()
    @click.command()
    def cmd():
        emit_result(
            ImportEnvelope(
                importer="lastfm", ok=False,
                errors=[{"stage": "fetch", "message": "boom"}],
            ),
            json_mode=False,
        )
    res = runner.invoke(cmd)
    # res.stderr_bytes contains stderr only; res.output combines both
    stderr = res.stderr if res.stderr_bytes is not None else res.output
    assert "boom" in stderr


def test_emit_result_json_failure_exits_nonzero():
    """When ok=False, the command should exit with a non-zero code so
    agents can detect failure by exit status alone."""
    runner = CliRunner()
    @click.command()
    def cmd():
        emit_result(
            ImportEnvelope(importer="x", ok=False),
            json_mode=True,
        )
    res = runner.invoke(cmd)
    assert res.exit_code == 2


def test_import_result_to_dict_handles_none_watermarks():
    from fulcra_media.fulcra import ImportResult
    env = import_result_to_dict(
        "lastfm",
        ImportResult(total=5, skipped_existing=1, posted=4, verified=4),
        since_watermark=None, new_watermark=None,
    )
    assert env.importer == "lastfm"
    assert env.posted == 4
    assert env.since_watermark is None


def test_import_envelope_optional_fields_default_sensibly():
    env = ImportEnvelope(importer="lastfm", ok=True)
    assert env.total == 0
    assert env.posted == 0
    assert env.errors == []
    assert env.would_post is None  # set on --check-only


# ---------- safe_exc_message ----------

def test_safe_exc_message_scrubs_access_token():
    exc = RuntimeError(
        "Client error '403 Forbidden' for url "
        "'https://api.deezer.com/user/me/history?access_token=secret123abc&limit=200'"
    )
    msg = safe_exc_message(exc)
    assert "secret123abc" not in msg
    assert "access_token=REDACTED" in msg
    # Non-secret params survive
    assert "limit=200" in msg


def test_safe_exc_message_scrubs_api_key():
    exc = RuntimeError(
        "GET https://ws.audioscrobbler.com/2.0/?api_key=k1234567890abcdef&user=ash failed"
    )
    msg = safe_exc_message(exc)
    assert "k1234567890abcdef" not in msg
    assert "api_key=REDACTED" in msg
    assert "user=ash" in msg


def test_safe_exc_message_scrubs_multiple_secrets():
    exc = RuntimeError(
        "POST /token?client_secret=abc&refresh_token=def returned 401"
    )
    msg = safe_exc_message(exc)
    assert "abc" not in msg
    assert "def" not in msg
    assert msg.count("REDACTED") == 2


def test_safe_exc_message_is_case_insensitive():
    """Some services capitalize URL params; the scrubber catches both."""
    exc = RuntimeError("url=https://x.com/?Access_Token=secret&foo=bar")
    msg = safe_exc_message(exc)
    assert "secret" not in msg


def test_safe_exc_message_passes_through_clean_strings():
    exc = RuntimeError("not a network error: file not found")
    assert safe_exc_message(exc) == "not a network error: file not found"


def test_safe_exc_message_doesnt_overscrub():
    """Don't redact legitimate similar-looking text outside URL query strings."""
    # access_token at start of string (no preceding ? or &) shouldn't match
    exc = RuntimeError("access_token=foo (this is informational text, not a URL)")
    msg = safe_exc_message(exc)
    # The literal hasn't been preceded by ? or &, so it stays
    assert msg == "access_token=foo (this is informational text, not a URL)"
