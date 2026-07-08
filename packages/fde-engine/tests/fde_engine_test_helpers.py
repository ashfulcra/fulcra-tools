"""Package-prefixed test helpers (root-pythonpath convention — see the root
pyproject: each package's tests/ dir is on sys.path, so helper modules need a
unique, package-prefixed name)."""

from __future__ import annotations


class FakeTransport:
    """In-memory stand-in for FulcraFileTransport. Same duck-typed surface."""

    def __init__(self):
        self.files: dict[str, str] = {}

    def read(self, path):
        return self.files.get(path)

    def write(self, path, content):
        self.files[path] = content
        return True

    def delete(self, path):
        return self.files.pop(path, None) is not None

    def list_dir(self, prefix):
        prefix = prefix if prefix.endswith("/") else prefix + "/"
        names: dict[str, bool] = {}
        for p in self.files:
            if p.startswith(prefix):
                rest = p[len(prefix):]
                if "/" in rest:
                    names[rest.split("/", 1)[0] + "/"] = True
                else:
                    names[rest] = False
        return sorted(
            ({"name": n, "size": None, "mtime": None, "is_dir": d}
             for n, d in names.items()),
            key=lambda e: e["name"],
        )
