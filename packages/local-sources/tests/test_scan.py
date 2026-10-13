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


@pytest.mark.parametrize("name", ["token.json", "credentials.yaml", "service_account.json",
                                  "client_secret_synthetic.json", "oauth_token.json",
                                  "access_token.toml", "refresh_token.json"])
def test_scan_excludes_common_credential_variants(tmp_path, name):
    (tmp_path / name).write_text("synthetic credential fixture")
    result = scan_tree(tmp_path, allowed_extensions={".json", ".yaml", ".toml"})
    assert not result.files
    assert result.skipped_by_reason == {"likely-secret": 1}


def test_scan_rejects_non_regular_files_without_waiting(tmp_path):
    import os
    import subprocess
    import sys
    os.mkfifo(tmp_path / "pipe.txt")
    script = ("from pathlib import Path; from fulcra_local_sources.scan import scan_tree; "
              "import sys; r=scan_tree(Path(sys.argv[1]), allowed_extensions={'.txt'}); "
              "assert not r.files; assert r.skipped_by_reason == {'non-regular': 1}")
    subprocess.run([sys.executable, "-c", script, str(tmp_path)], check=True, timeout=3)


def test_scan_stops_at_traversal_budget_even_when_files_are_unsupported(tmp_path):
    for name in ("a.exe", "b.exe"):
        (tmp_path / name).write_bytes(b"synthetic")
    with pytest.raises(ValueError, match="traversal entry limit"):
        scan_tree(tmp_path, allowed_extensions={".md"}, limits=ScanLimits(max_entries=1))


@pytest.mark.parametrize("swap_ancestor", [False, True])
def test_selected_path_must_not_be_canonicalized_to_a_new_target(tmp_path, swap_ancestor):
    parent = tmp_path / "selected-parent"
    root = parent / "selected"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "selected").mkdir(parents=True)
    (outside / "selected" / "private.md").write_text("synthetic outside content")
    if swap_ancestor:
        root.rmdir()
        parent.rmdir()
        parent.symlink_to(outside, target_is_directory=True)
    else:
        root.rmdir()
        root.symlink_to(outside / "selected", target_is_directory=True)
    with pytest.raises(ValueError, match="source unavailable"):
        scan_tree(root, allowed_extensions={".md"})
