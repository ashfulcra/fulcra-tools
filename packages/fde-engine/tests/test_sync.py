"""Explicit-direction sync between the local mirror and the remote tree."""

import pytest

from fde_engine import sync
from fde_engine_test_helpers import FakeTransport


def test_push_uploads_new_and_changed_files_only(tmp_path):
    t = FakeTransport()
    d = tmp_path / "eng"
    (d / "intake").mkdir(parents=True)
    (d / "engagement.md").write_text("meta", encoding="utf-8")
    (d / "intake" / "brief.md").write_text("v1", encoding="utf-8")
    report = sync.push(t, "x", str(d))
    # engagement.md is machine-managed and never pushed (Fix I2) -- only the
    # other file is uploaded.
    assert report["pushed"] == ["intake/brief.md"]
    assert report["excluded"] == ["engagement.md"]
    assert t.read("fde/engagements/x/engagement.md") is None

    # unchanged content is skipped on the second push
    report2 = sync.push(t, "x", str(d))
    assert report2["pushed"] == [] and report2["skipped"] == 1
    assert report2["excluded"] == ["engagement.md"]

    # a content change is pushed again
    (d / "intake" / "brief.md").write_text("v2", encoding="utf-8")
    report3 = sync.push(t, "x", str(d))
    assert report3["pushed"] == ["intake/brief.md"]
    assert t.read("fde/engagements/x/intake/brief.md") == "v2"


# --- Fix I2: push must never revert the machine-managed engagement.md -----


def test_push_never_uploads_engagement_md_even_when_locally_stale(tmp_path):
    t = FakeTransport()
    t.write("fde/engagements/x/engagement.md", "remote meta (phase: build)")
    d = tmp_path / "eng"
    d.mkdir()
    (d / "engagement.md").write_text("stale local meta (phase: intake)", encoding="utf-8")
    (d / "notes.md").write_text("some notes", encoding="utf-8")

    report = sync.push(t, "x", str(d))

    assert report["pushed"] == ["notes.md"]
    assert report["excluded"] == ["engagement.md"]
    # remote engagement.md is untouched by the push
    assert t.read("fde/engagements/x/engagement.md") == "remote meta (phase: build)"


def test_push_excluded_is_empty_when_no_local_engagement_md(tmp_path):
    t = FakeTransport()
    d = tmp_path / "eng"
    d.mkdir()
    (d / "notes.md").write_text("some notes", encoding="utf-8")
    report = sync.push(t, "x", str(d))
    assert report["excluded"] == []


# --- Fix minor a: push on a nonexistent local dir must not report success --


def test_push_raises_when_local_dir_does_not_exist(tmp_path):
    t = FakeTransport()
    missing = tmp_path / "does-not-exist"
    with pytest.raises(sync.SyncError, match="does not exist"):
        sync.push(t, "x", str(missing))


# --- Fix minor b: pull must reject path-escaping remote entries -----------


class TraversalTransport(FakeTransport):
    """A remote listing carrying a malicious/corrupt entry name that would
    escape the local mirror directory if written unsanitized."""

    def list_dir(self, prefix):
        if prefix == "fde/engagements/x/":
            return [{"name": "../escape.md", "size": None,
                     "mtime": None, "is_dir": False}]
        return []

    def read(self, path):
        return "malicious content"


def test_pull_rejects_path_escaping_remote_entry():
    t = TraversalTransport()
    with pytest.raises(sync.SyncError, match=r"\.\./escape\.md"):
        sync.pull(t, "x", "/tmp/does-not-matter-should-not-be-written")


def test_pull_downloads_new_and_changed_files_only(tmp_path):
    t = FakeTransport()
    t.write("fde/engagements/x/engagement.md", "meta")
    t.write("fde/engagements/x/interview/plan.md", "topics")
    d = tmp_path / "eng"
    report = sync.pull(t, "x", str(d))
    assert sorted(report["pulled"]) == ["engagement.md", "interview/plan.md"]
    assert (d / "interview" / "plan.md").read_text(encoding="utf-8") == "topics"

    report2 = sync.pull(t, "x", str(d))
    assert report2["pulled"] == [] and report2["skipped"] == 2


def test_push_skips_hidden_files(tmp_path):
    t = FakeTransport()
    d = tmp_path / "eng"
    d.mkdir()
    (d / ".DS_Store").write_text("junk", encoding="utf-8")
    (d / "retro.md").write_text("done", encoding="utf-8")
    report = sync.push(t, "x", str(d))
    assert report["pushed"] == ["retro.md"]


class FailingWriteTransport(FakeTransport):
    """FakeTransport whose write() reports failure for one specific path."""

    def __init__(self, fail_path):
        super().__init__()
        self.fail_path = fail_path

    def write(self, path, content):
        if path == self.fail_path:
            return False
        return super().write(path, content)


def test_push_raises_when_a_transport_write_fails(tmp_path):
    t = FailingWriteTransport("fde/engagements/x/intake/brief.md")
    d = tmp_path / "eng"
    (d / "intake").mkdir(parents=True)
    (d / "engagement.md").write_text("meta", encoding="utf-8")
    (d / "intake" / "brief.md").write_text("v1", encoding="utf-8")
    with pytest.raises(sync.SyncError, match="intake/brief.md"):
        sync.push(t, "x", str(d))


def test_pull_raises_actionable_error_on_file_vs_directory_collision(tmp_path):
    t = FakeTransport()
    t.write("fde/engagements/x/build/logs/x.md", "log line")
    d = tmp_path / "eng"
    d.mkdir()
    # A plain local file where the remote tree needs a directory.
    (d / "build").write_text("i am a file, not a directory", encoding="utf-8")
    with pytest.raises(sync.SyncError, match="build/logs/x.md"):
        sync.pull(t, "x", str(d))


# --- Bug #1: sync must not crash on binary files --------------------------


def test_push_skips_a_stray_binary_file_without_crashing(tmp_path):
    """A non-UTF-8 file dropped anywhere in the mirror (e.g. a PDF in intake/)
    must not crash the push — it is reported and skipped, text still uploads."""
    t = FakeTransport()
    d = tmp_path / "eng"
    (d / "intake").mkdir(parents=True)
    (d / "intake" / "deck.pdf").write_bytes(b"%PDF-1.7\x00\x01\x02\xff\xfe binary")
    (d / "intake" / "brief.md").write_text("real brief", encoding="utf-8")

    report = sync.push(t, "x", str(d))

    assert report["pushed"] == ["intake/brief.md"]
    assert report["skipped_binary"] == ["intake/deck.pdf"]
    # the binary was never uploaded through the text mirror
    assert t.read("fde/engagements/x/intake/deck.pdf") is None


def test_push_ignores_the_intake_originals_binaries_area(tmp_path):
    """intake/originals/ is the designated binaries area — managed directly
    with `fulcra file`, never walked by sync in either direction."""
    t = FakeTransport()
    d = tmp_path / "eng"
    (d / "intake" / "originals").mkdir(parents=True)
    (d / "intake" / "originals" / "deck.pdf").write_bytes(b"\x00\xff binary original")
    (d / "intake" / "brief.md").write_text("brief", encoding="utf-8")

    report = sync.push(t, "x", str(d))

    assert report["pushed"] == ["intake/brief.md"]
    # not even reported as skipped_binary — the whole area is out of scope
    assert report["skipped_binary"] == []


def test_pull_ignores_the_intake_originals_binaries_area(tmp_path):
    t = FakeTransport()
    t.write("fde/engagements/x/intake/brief.md", "brief")
    t.write("fde/engagements/x/intake/originals/deck.pdf", "binary-ish")
    d = tmp_path / "eng"
    report = sync.pull(t, "x", str(d))
    assert report["pulled"] == ["intake/brief.md"]
    assert not (d / "intake" / "originals" / "deck.pdf").exists()
