"""Actual offline builds with disposable, immutable resource inputs."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import zipfile

import pytest


COLLECT = Path(__file__).resolve().parents[1]
SPA = {
    "index.html": b"<!doctype html><main>fixture SPA</main>\n",
    "static/nested/app.js": b"console.log('fixture');\n",
    ".hidden": b"hidden bytes\x00\xff",
    ".DS_Store": b"included even though Hatchling normally filters it",
    ".git/asset": b"a regular asset beneath a normally filtered directory",
    "__pycache__/asset.pyc": b"not Python: an intentional SPA asset",
}
DOCS = b"# Fixture in-app docs\n"
BUNDLED = "fulcra_collect/_bundled/"
SNAPSHOT = "_build_resources/"


def write_resources(root: Path, *, archive=False, sentinel=False):
    base = root / "_build_resources" if archive else root.parents[1]
    dist = base / ("web-ui/dist" if archive else "packages/web-ui/dist")
    docs = base / "docs/how-do-i-get-my-data.md"
    for name, content in SPA.items():
        path = dist / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"external sentinel" if sentinel else content)
    docs.parent.mkdir(parents=True, exist_ok=True)
    docs.write_bytes(b"external docs sentinel" if sentinel else DOCS)
    return dist, docs


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "workspace/packages/collect"
    root.mkdir(parents=True)
    shutil.copytree(
        COLLECT / "fulcra_collect",
        root / "fulcra_collect",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copy2(COLLECT / "pyproject.toml", root / "pyproject.toml")
    if (COLLECT / "hatch_build.py").exists():
        shutil.copy2(COLLECT / "hatch_build.py", root / "hatch_build.py")
    write_resources(root)
    return root


def tree_identity(root):
    """Include names/types and file bytes; do not follow fixture symlinks."""
    result = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[name] = ("link", os.readlink(path))
        elif path.is_dir():
            result[name] = ("dir",)
        elif path.is_file():
            result[name] = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            result[name] = ("special", path.lstat().st_mode)
    return result


def build(root, output, *targets):
    # All build inputs are disposable; preserve package/source bytes on failure too.
    input_root = root.parents[1] if root.name == "collect" else root
    before = tree_identity(input_root)
    command = [
        "uv",
        "build",
        "--offline",
        str(root),
        "--out-dir",
        str(output),
        *targets,
    ]
    # Suppress Python import bytecode so the whole fixture input tree can be
    # compared unchanged; this does not change Hatchling resource selection.
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=90,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    (output.parent / f"{output.name}.log").write_text(
        f"{command!r}\nrc={result.returncode}\n{result.stdout}{result.stderr}"
    )
    assert tree_identity(input_root) == before, (
        f"build changed source inputs: {command}"
    )
    assert not (root / "fulcra_collect/_bundled").exists()
    return result


def require_success(result):
    assert result.returncode == 0, result.stdout + result.stderr


def safe_unique(names):
    assert len(names) == len(set(names)), "duplicate archive members"
    for name in names:
        path = PurePosixPath(name)
        assert not path.is_absolute() and ".." not in path.parts
        assert str(path) == name and "\\" not in name


def wheel_bytes(path):
    with zipfile.ZipFile(path) as wheel:
        members = wheel.infolist()
        safe_unique([item.filename.rstrip("/") for item in members])
        return {
            item.filename: wheel.read(item) for item in members if not item.is_dir()
        }


def sdist_bytes(path):
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        safe_unique([item.name for item in members])
        assert all(item.isfile() or item.isdir() for item in members)
        return {
            item.name.split("/", 1)[1]: archive.extractfile(item).read()
            for item in members
            if item.isfile()
        }


def unpack_sdist(path, destination):
    # Only our just-built archive, already checked for normalized regular members.
    sdist_bytes(path)
    with tarfile.open(path) as archive:
        archive.extractall(destination, filter="data")
    roots = list(destination.iterdir())
    assert len(roots) == 1
    return roots[0]


def expected_resources(prefix):
    return {
        **{prefix + "web-ui/dist/" + name: content for name, content in SPA.items()},
        prefix + "docs/how-do-i-get-my-data.md": DOCS,
    }


def test_offline_builds_preserve_all_resource_and_wheel_bytes(project, tmp_path):
    before = tree_identity(project.parents[1])
    require_success(build(project, tmp_path / "direct", "--wheel"))
    require_success(build(project, tmp_path / "sdist", "--sdist"))
    sdist = next((tmp_path / "sdist").glob("*.tar.gz"))
    extracted = unpack_sdist(sdist, tmp_path / "extracted")
    # This assertion reproduces the old sdist's escaped external force-includes.
    require_success(build(extracted, tmp_path / "from-sdist", "--wheel"))
    require_success(build(project, tmp_path / "default"))
    direct = wheel_bytes(next((tmp_path / "direct").glob("*.whl")))
    rebuilt = wheel_bytes(next((tmp_path / "from-sdist").glob("*.whl")))
    default = wheel_bytes(next((tmp_path / "default").glob("*.whl")))
    assert direct == rebuilt == default  # includes METADATA, WHEEL, RECORD
    assert {
        name: data for name, data in direct.items() if name.startswith(BUNDLED)
    } == expected_resources(BUNDLED)
    assert not any(
        name.startswith(SNAPSHOT) or name == "hatch_build.py" for name in direct
    )
    snapshot = sdist_bytes(sdist)
    assert {
        name: data for name, data in snapshot.items() if name.startswith(SNAPSHOT)
    } == expected_resources(SNAPSHOT)
    assert snapshot["hatch_build.py"] == (COLLECT / "hatch_build.py").read_bytes()
    assert snapshot["pyproject.toml"] == (COLLECT / "pyproject.toml").read_bytes()
    assert not any(name.startswith(BUNDLED) for name in snapshot)
    assert tree_identity(project.parents[1]) == before


def test_complete_archive_ignores_valid_external_sentinels(project, tmp_path):
    write_resources(project, archive=True)
    (project / "PKG-INFO").write_text(
        "Metadata-Version: 2.4\nName: fulcra-collect\nVersion: 0.1.4\n"
    )
    write_resources(project, sentinel=True)
    for target in ("--wheel", "--sdist"):
        output = tmp_path / target.removeprefix("--")
        require_success(build(project, output, target))
        if target == "--wheel":
            contents = wheel_bytes(next(output.glob("*.whl")))
            prefix = BUNDLED
        else:
            contents = sdist_bytes(next(output.glob("*.tar.gz")))
            prefix = SNAPSHOT
        assert {
            name: data for name, data in contents.items() if name.startswith(prefix)
        } == expected_resources(prefix)


def require_failure(root, output, target, mode, resource):
    result = build(root, output, target)
    assert result.returncode != 0, "invalid inputs unexpectedly built"
    diagnostic = result.stdout + result.stderr
    assert f"Collect {mode} resource" in diagnostic and resource in diagnostic, (
        diagnostic
    )
    if mode == "archive":
        assert "recreate the sdist from complete prebuilt inputs" in diagnostic


@pytest.mark.parametrize("target", ["--wheel", "--sdist"])
@pytest.mark.parametrize(
    "case,resource",
    [
        ("missing-dist", "dist"),
        ("empty-dist", "index.html"),
        ("missing-index", "index.html"),
        ("index-directory", "index.html"),
        ("missing-docs", "how-do-i-get-my-data.md"),
        ("special-file", "pipe"),
        ("special-index", "index.html"),
        ("backslash-name", "bad\\name"),
    ],
)
def test_workspace_invalid_inputs_fail_loud(project, tmp_path, target, case, resource):
    dist = project.parent / "web-ui/dist"
    docs = project.parents[1] / "docs/how-do-i-get-my-data.md"
    if case == "missing-dist":
        shutil.rmtree(dist)
    elif case == "empty-dist":
        shutil.rmtree(dist)
        dist.mkdir()
    elif case in ("missing-index", "index-directory", "special-index"):
        (dist / "index.html").unlink()
        if case == "index-directory":
            (dist / "index.html").mkdir()
        elif case == "special-index":
            os.mkfifo(dist / "index.html")
    elif case == "missing-docs":
        docs.unlink()
    elif case == "special-file":
        os.mkfifo(dist / "pipe")
    elif case == "backslash-name":
        (dist / "bad\\name").write_text("unsafe member name")
    require_failure(project, tmp_path / "out", target, "workspace", resource)


@pytest.mark.parametrize("target", ["--wheel", "--sdist"])
@pytest.mark.parametrize(
    "case,resource",
    [
        ("missing-snapshot", "_build_resources"),
        ("snapshot-file", "_build_resources"),
        ("missing-index", "index.html"),
        ("missing-docs", "how-do-i-get-my-data.md"),
        ("dangling-snapshot", "_build_resources"),
        ("dangling-pkg-info", "PKG-INFO"),
        ("pkg-info-directory", "PKG-INFO"),
        ("pkg-info-link", "PKG-INFO"),
    ],
)
def test_malformed_archive_never_falls_back(project, tmp_path, target, case, resource):
    dist, docs = write_resources(project, archive=True)
    (project / "PKG-INFO").write_text(
        "Metadata-Version: 2.4\nName: fulcra-collect\nVersion: 0.1.4\n"
    )
    write_resources(project, sentinel=True)
    snapshot = project / "_build_resources"
    if case in ("missing-snapshot", "snapshot-file", "dangling-snapshot"):
        shutil.rmtree(snapshot)
        if case == "snapshot-file":
            snapshot.write_text("malformed marker")
        elif case == "dangling-snapshot":
            snapshot.symlink_to("absent")
            (project / "PKG-INFO").unlink()  # dangling marker alone selects archive
    elif case == "missing-index":
        (dist / "index.html").unlink()
    elif case == "missing-docs":
        docs.unlink()
    else:
        marker = project / "PKG-INFO"
        marker.unlink()
        if case == "pkg-info-directory":
            marker.mkdir()
        else:
            metadata = project / "valid-metadata"
            metadata.write_text(
                "Metadata-Version: 2.4\nName: fulcra-collect\nVersion: 0.1.4\n"
            )
            marker.symlink_to(
                "absent" if case == "dangling-pkg-info" else metadata.name
            )
            if case == "dangling-pkg-info":
                shutil.rmtree(snapshot)  # dangling PKG-INFO alone selects archive
    require_failure(project, tmp_path / "out", target, "archive", resource)


@pytest.mark.parametrize("mode", ["workspace", "archive"])
@pytest.mark.parametrize(
    "case",
    [
        "internal-file",
        "external-file",
        "broken-file",
        "internal-directory",
        "external-directory",
        "broken-directory",
        "dist-root",
        "web-ui-parent",
        "docs-file",
        "docs-parent",
        "snapshot-root",
    ],
)
def test_selected_symlinks_are_rejected(project, tmp_path, mode, case):
    if mode == "workspace" and case == "snapshot-root":
        pytest.skip("snapshot-root is an archive marker, covered in archive mode")
    dist, docs = write_resources(project, archive=mode == "archive")
    if mode == "archive":
        write_resources(project, sentinel=True)
    external = tmp_path / "external"
    external.mkdir()
    (external / "asset").write_bytes(b"external")
    if case.endswith("file") and case != "docs-file":
        link = dist / "linked-file"
        target = {
            "internal-file": dist / "index.html",
            "external-file": external / "asset",
            "broken-file": external / "absent",
        }[case]
    elif case.endswith("directory"):
        link = dist / "linked-directory"
        target = {
            "internal-directory": dist / "static",
            "external-directory": external,
            "broken-directory": external / "absent",
        }[case]
    else:
        link = {
            "dist-root": dist,
            "web-ui-parent": dist.parent,
            "docs-file": docs,
            "docs-parent": docs.parent,
            "snapshot-root": project / "_build_resources",
        }[case]
        target = tmp_path / "moved-resource"
        link.rename(target)
    link.symlink_to(target)
    require_failure(project, tmp_path / "out", "--wheel", mode, link.name)


@pytest.mark.parametrize("mode", ["workspace", "archive"])
@pytest.mark.parametrize("case", ["index", "docs", "nested-file", "traversal"])
def test_unreadable_resources_fail_when_permissions_enforced(
    project, tmp_path, mode, case
):
    dist, docs = write_resources(project, archive=mode == "archive")
    path = {
        "index": dist / "index.html",
        "docs": docs,
        "nested-file": dist / "static/nested/app.js",
        "traversal": dist / "static",
    }[case]
    before = tree_identity(project.parents[1])
    original_mode = path.stat().st_mode
    path.chmod(0)
    try:
        try:
            if case == "traversal":
                with os.scandir(path) as entries:
                    list(entries)
            else:
                path.read_bytes()
        except PermissionError:
            pass
        else:
            pytest.skip("execution identity can still read this chmod(0) fixture")
        command = [
            "uv",
            "build",
            "--offline",
            str(project),
            "--wheel",
            "--out-dir",
            str(tmp_path / "out"),
        ]
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=90,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        (tmp_path / "unreadable-resource.log").write_text(
            f"{command!r}\nrc={result.returncode}\n{result.stdout}{result.stderr}"
        )
        assert result.returncode != 0
        assert (
            f"Collect {mode} resource" in result.stderr and path.name in result.stderr
        )
        if mode == "archive":
            assert "recreate the sdist from complete prebuilt inputs" in result.stderr
    finally:
        path.chmod(original_mode)
        assert tree_identity(project.parents[1]) == before


def test_backend_rejects_malformed_pkg_info_before_initialize(project, tmp_path):
    write_resources(project, archive=True)
    write_resources(project, sentinel=True)
    (project / "PKG-INFO").write_text("malformed metadata")
    result = build(project, tmp_path / "out", "--wheel")
    assert result.returncode != 0
    diagnostic = result.stdout + result.stderr
    # Hatchling parses this marker before initialize; no hook diagnostic claim.
    assert "metadata" in diagnostic.lower()
    assert "Collect archive resource" not in diagnostic
    assert not list((tmp_path / "out").glob("*.whl"))


def test_backend_rejects_unreadable_pkg_info_when_permissions_enforced(
    project, tmp_path
):
    write_resources(project, archive=True)
    write_resources(project, sentinel=True)
    marker = project / "PKG-INFO"
    marker.write_text("Metadata-Version: 2.4\nName: fulcra-collect\nVersion: 0.1.4\n")
    before = tree_identity(project.parents[1])
    original_mode = marker.stat().st_mode
    marker.chmod(0)
    try:
        try:
            marker.read_bytes()
        except PermissionError:
            pass
        else:
            pytest.skip("execution identity can still read this chmod(0) fixture")
        command = [
            "uv",
            "build",
            "--offline",
            str(project),
            "--wheel",
            "--out-dir",
            str(tmp_path / "out"),
        ]
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=90,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        (tmp_path / "unreadable-marker.log").write_text(
            f"{command!r}\nrc={result.returncode}\n{result.stdout}{result.stderr}"
        )
        assert result.returncode != 0
        assert "PKG-INFO" in result.stderr
        assert "Collect archive resource" not in result.stderr
        assert not list((tmp_path / "out").glob("*.whl"))
    finally:
        marker.chmod(original_mode)
        assert tree_identity(project.parents[1]) == before
