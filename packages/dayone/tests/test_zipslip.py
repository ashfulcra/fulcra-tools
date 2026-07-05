"""Zip-slip hardening for the JSON-export reader.

A Day One export .zip is untrusted user input. Crafted archives can carry
members whose names escape the extraction directory (``../`` traversal or an
absolute path) or symlink members that redirect a later write outside the
extraction root. ``read_json_export`` must reject such archives with a clear
``ValueError`` and must never write anything outside its temp extraction dir.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from fulcra_dayone.readers.json_export import read_json_export


def test_rejects_parent_traversal_member(tmp_path: Path):
    """A ``../../evil.txt`` member must not escape the extraction dir."""
    outside = tmp_path / "evil.txt"
    assert not outside.exists()

    zip_path = tmp_path / "malicious.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("../../evil.txt", "pwned")

    with pytest.raises(ValueError, match="unsafe|traversal|escapes|outside"):
        read_json_export(zip_path)

    # Nothing was written outside the (transient) extraction dir.
    assert not outside.exists()
    assert not (tmp_path.parent / "evil.txt").exists()


def test_rejects_absolute_path_member(tmp_path: Path):
    """An absolute-path member must be rejected, not written to /tmp."""
    abs_target = tmp_path / "evil-abs.txt"
    # Force a genuinely absolute stored name via ZipInfo (writestr normalizes
    # leading slashes off of a plain string arg, so build the ZipInfo directly).
    info = zipfile.ZipInfo(filename=str(abs_target))
    assert Path(info.filename).is_absolute()

    zip_path = tmp_path / "malicious-abs.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(info, "pwned")

    abs_target_before = abs_target.exists()

    with pytest.raises(ValueError, match="unsafe|traversal|escapes|outside|absolute"):
        read_json_export(zip_path)

    # The absolute path must not have been created by extraction.
    assert abs_target.exists() == abs_target_before
    assert not abs_target.exists()


def test_rejects_symlink_member(tmp_path: Path):
    """A symlink member pointing outside, plus a write through it, is rejected.

    The classic exploit: member ``link`` is a symlink to an outside directory,
    then member ``link/evil.txt`` writes a payload through it. The reader must
    refuse symlink members entirely so the follow-up write can never land
    outside the extraction root.
    """
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    payload = outside_dir / "evil.txt"
    assert not payload.exists()

    zip_path = tmp_path / "malicious-symlink.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        # Symlink member: external_attr encodes st_mode in its high 16 bits;
        # S_IFLNK (0o120000) | 0777 marks it a symlink pointing at outside_dir.
        link_info = zipfile.ZipInfo("link")
        link_info.external_attr = (0o120777 << 16)
        zf.writestr(link_info, str(outside_dir))
        # Write a payload through the symlink path.
        zf.writestr("link/evil.txt", "pwned")

    with pytest.raises(ValueError, match="unsafe|symlink|traversal|escapes|outside"):
        read_json_export(zip_path)

    # Payload must not have been written through the symlink.
    assert not payload.exists()
