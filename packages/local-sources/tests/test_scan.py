from pathlib import Path

import pytest

from fulcra_local_sources.scan import ScanLimits, scan_tree


def test_scan_is_read_only_bounded_and_excludes_unsafe_files(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "notes").mkdir()
    (source / "notes" / "brief.md").write_text("synthetic brief", encoding="utf-8")
    (source / "diagram.png").write_bytes(b"synthetic-image")
    (source / ".env").write_text("SYNTHETIC=value", encoding="utf-8")
    (source / "credentials.json").write_text("{}", encoding="utf-8")
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("synthetic", encoding="utf-8")
    (source / "large.txt").write_bytes(b"x" * 20)
    (source / "program.py").write_text("pass", encoding="utf-8")
    (source / "outside.txt").symlink_to(tmp_path / "outside.txt")

    result = scan_tree(
        source,
        allowed_extensions={".md", ".png", ".txt", ".json"},
        limits=ScanLimits(max_files=20, max_file_bytes=16, max_total_bytes=100),
    )

    assert [item.relative_path.as_posix() for item in result.files] == [
        "diagram.png",
        "notes/brief.md",
    ]
    assert result.skipped_by_reason == {
        "hidden": 1,
        "likely-secret": 1,
        "oversized": 1,
        "symlink": 1,
        "unsupported": 1,
    }
    assert str(source) not in repr(result)


def test_scan_refuses_root_and_non_directories(tmp_path: Path):
    with pytest.raises(ValueError, match="directory"):
        scan_tree(tmp_path / "missing", allowed_extensions={".md"})
    with pytest.raises(ValueError, match="root"):
        scan_tree(Path("/"), allowed_extensions={".md"})


def test_scan_fails_closed_when_file_or_byte_limit_is_exceeded(tmp_path: Path):
    for name in ("a.md", "b.md"):
        (tmp_path / name).write_text("1234", encoding="utf-8")
    with pytest.raises(ValueError, match="file limit"):
        scan_tree(
            tmp_path,
            allowed_extensions={".md"},
            limits=ScanLimits(max_files=1, max_file_bytes=10, max_total_bytes=10),
        )
    with pytest.raises(ValueError, match="total byte limit"):
        scan_tree(
            tmp_path,
            allowed_extensions={".md"},
            limits=ScanLimits(max_files=10, max_file_bytes=10, max_total_bytes=4),
        )
