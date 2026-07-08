"""Explicit-direction sync between the local mirror and the remote tree."""

from fde_engine import sync
from fde_engine_test_helpers import FakeTransport


def test_push_uploads_new_and_changed_files_only(tmp_path):
    t = FakeTransport()
    d = tmp_path / "eng"
    (d / "intake").mkdir(parents=True)
    (d / "engagement.md").write_text("meta", encoding="utf-8")
    (d / "intake" / "brief.md").write_text("v1", encoding="utf-8")
    report = sync.push(t, "x", str(d))
    assert sorted(report["pushed"]) == ["engagement.md", "intake/brief.md"]

    # unchanged content is skipped on the second push
    report2 = sync.push(t, "x", str(d))
    assert report2["pushed"] == [] and report2["skipped"] == 2

    # a content change is pushed again
    (d / "intake" / "brief.md").write_text("v2", encoding="utf-8")
    report3 = sync.push(t, "x", str(d))
    assert report3["pushed"] == ["intake/brief.md"]
    assert t.read("fde/engagements/x/intake/brief.md") == "v2"


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
