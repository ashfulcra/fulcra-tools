"""Bundle prebuilt resources directly; an sdist carries its own input snapshot."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import stat

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root)
        archive = any(
            os.path.lexists(root / name) for name in ("_build_resources", "PKG-INFO")
        )
        mode = "archive" if archive else "workspace"
        guidance = (
            "recreate the sdist from complete prebuilt inputs"
            if archive
            else "prepare the prebuilt web UI and in-app docs before building"
        )

        def fail(path, reason):
            raise ValueError(f"Collect {mode} resource {path}: {reason}; {guidance}")

        def check(path, *, directory=False):
            try:
                kind = path.lstat().st_mode
                if stat.S_ISLNK(kind):
                    fail(path, "symlinks are not allowed")
                if directory:
                    if not stat.S_ISDIR(kind):
                        fail(path, "expected a directory")
                elif not stat.S_ISREG(kind):
                    fail(path, "expected a readable regular file")
                else:
                    with path.open("rb") as stream:
                        while stream.read(1024 * 1024):
                            pass
            except OSError as exc:
                fail(path, str(exc))

        def components(base, relative, *, directory=False):
            # Check only the selected lexical resource components, not unrelated
            # host ancestors (e.g. macOS's /var -> /private/var).
            path = base
            parts = Path(relative).parts
            for number, part in enumerate(parts):
                path = path / part
                check(path, directory=directory or number < len(parts) - 1)
            return path

        if archive:
            if os.path.lexists(root / "PKG-INFO"):
                check(root / "PKG-INFO")
            base = root
            dist = components(base, "_build_resources/web-ui/dist", directory=True)
            docs = components(base, "_build_resources/docs/how-do-i-get-my-data.md")
        else:
            base = root.parents[1]
            dist = components(base, "packages/web-ui/dist", directory=True)
            docs = components(base, "docs/how-do-i-get-my-data.md")

        def walk_error(exc):
            fail(exc.filename or dist, str(exc))

        files = []
        for current, directories, filenames in os.walk(
            dist, followlinks=False, onerror=walk_error
        ):
            parent = Path(current)
            for name in sorted(directories):
                check(parent / name, directory=True)
            directories.sort()
            for name in sorted(filenames):
                path = parent / name
                check(path)
                relative = path.relative_to(dist).as_posix()
                member = PurePosixPath(relative)
                if (
                    member.is_absolute()
                    or ".." in member.parts
                    or str(member) != relative
                    or "\\" in relative
                ):
                    fail(path, "expected a normalized relative member path")
                files.append((path, "web-ui/dist/" + relative))
        if not files:
            fail(
                dist / "index.html",
                "SPA is empty; a readable regular index.html is required",
            )
        if not any(member == "web-ui/dist/index.html" for _, member in files):
            fail(dist / "index.html", "a readable regular index.html is required")
        files.append((docs, "docs/how-do-i-get-my-data.md"))

        destination = (
            "_build_resources/"
            if self.target_name == "sdist"
            else "fulcra_collect/_bundled/"
        )
        # Hatchling seeds this map with pyproject/config/metadata inputs for the
        # sdist. Update it, preserving those backend defaults.
        force_include = build_data.setdefault("force_include", {})
        force_include.update(
            (str(path), destination + member) for path, member in files
        )
        if self.target_name == "sdist":
            force_include[str(root / "hatch_build.py")] = "hatch_build.py"
