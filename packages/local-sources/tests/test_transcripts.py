import json
from pathlib import Path

import pytest

from fulcra_local_sources.transcripts import render_transcript


def test_vtt_and_srt_are_normalized_without_timing_noise(tmp_path: Path):
    vtt = tmp_path / "call.vtt"
    vtt.write_text(
        "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nAlice: Hello\n\n"
        "00:00:03.100 --> 00:00:04.000\nBob: Hi\n",
        encoding="utf-8",
    )
    rendered = render_transcript(vtt, relative_path=Path("team/call.vtt"))
    assert "Alice: Hello" in rendered
    assert "Bob: Hi" in rendered
    assert "00:00" not in rendered
    assert str(tmp_path) not in rendered
    assert "source-relative-path: team/call.vtt" in rendered

    srt = tmp_path / "call.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nSynthetic first line\n\n"
        "2\n00:00:04,000 --> 00:00:05,000\nSynthetic second line\n",
        encoding="utf-8",
    )
    rendered = render_transcript(srt, relative_path=Path("call.srt"))
    assert "Synthetic first line" in rendered
    assert "Synthetic second line" in rendered
    assert "00:00" not in rendered


def test_json_segment_exports_are_normalized(tmp_path: Path):
    source = tmp_path / "call.json"
    source.write_text(
        json.dumps(
            {
                "title": "Synthetic weekly call",
                "segments": [
                    {"speaker": "Speaker A", "text": "First point"},
                    {"speaker": "Speaker B", "text": "Second point"},
                ],
            }
        ),
        encoding="utf-8",
    )
    rendered = render_transcript(source, relative_path=Path("call.json"))
    assert "# Synthetic weekly call" in rendered
    assert "**Speaker A:** First point" in rendered
    assert "**Speaker B:** Second point" in rendered


def test_unknown_json_shape_fails_closed(tmp_path: Path):
    source = tmp_path / "unknown.json"
    source.write_text('{"private": "not a transcript"}', encoding="utf-8")
    with pytest.raises(ValueError, match="transcript"):
        render_transcript(source, relative_path=Path("unknown.json"))
