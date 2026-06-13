import subprocess

import pytest

from fulcra_vault.store import (
    FulcraVaultStore,
    MissingFileError,
    TransportError,
)


class FakeRunner:
    def __init__(self):
        self.files: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.failures: dict[tuple[str, str], subprocess.CompletedProcess] = {}

    def fail(self, op: str, path: str, stderr: str, rc: int = 1) -> None:
        self.failures[(op, path)] = subprocess.CompletedProcess(
            ["fulcra-api", "file", op, path], rc, "", stderr
        )

    def __call__(self, cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
        self.calls.append(cmd)
        op = cmd[2]
        if op == "upload":
            path = cmd[4]
        else:
            path = cmd[3]
        if (op, path) in self.failures:
            return self.failures[(op, path)]
        if op == "download":
            if path not in self.files:
                return subprocess.CompletedProcess(cmd, 1, "", "HTTP Error 404: Not Found")
            return subprocess.CompletedProcess(cmd, 0, self.files[path], "")
        if op == "upload":
            with open(cmd[3], encoding="utf-8") as f:
                self.files[path] = f.read()
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if op == "stat":
            if path not in self.files:
                return subprocess.CompletedProcess(cmd, 1, "", "Not Found")
            return subprocess.CompletedProcess(cmd, 0, '{"version":"v1","size":3}', "")
        if op == "list":
            prefix = path.rstrip("/")
            names = "\n".join(sorted(p for p in self.files if p.startswith(prefix + "/")))
            return subprocess.CompletedProcess(cmd, 0, names + ("\n" if names else ""), "")
        if op == "delete":
            if path not in self.files:
                return subprocess.CompletedProcess(cmd, 1, "", "Not Found")
            del self.files[path]
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(f"unknown op {op}")


def test_write_and_read_text_normalize_note_names_to_absolute_vault_paths():
    runner = FakeRunner()
    store = FulcraVaultStore(runner=runner, cli_base=["fulcra-api"])

    store.write_text("Project Alpha", "# Alpha\n")

    assert runner.files["/vault/Project Alpha.md"] == "# Alpha\n"
    assert store.read_text("vault/Project Alpha.md") == "# Alpha\n"
    assert runner.calls[0][:3] == ["fulcra-api", "file", "upload"]
    assert runner.calls[0][4] == "/vault/Project Alpha.md"
    assert runner.calls[1] == ["fulcra-api", "file", "download", "/vault/Project Alpha.md", "-"]


def test_missing_download_is_distinct_from_transport_failure():
    runner = FakeRunner()
    store = FulcraVaultStore(runner=runner, cli_base=["fulcra-api"])

    with pytest.raises(MissingFileError):
        store.read_text("Missing")

    runner.fail("download", "/vault/Broken.md", "HTTP Error 504: Gateway Timeout")
    with pytest.raises(TransportError, match="download failed"):
        store.read_text("Broken")


def test_stat_returns_none_for_missing_but_raises_on_transport_failure():
    runner = FakeRunner()
    store = FulcraVaultStore(runner=runner, cli_base=["fulcra-api"])

    assert store.stat("Missing") is None

    runner.files["/vault/Present.md"] = "ok"
    assert store.stat("Present") == {"version": "v1", "size": 3}

    runner.fail("stat", "/vault/Broken.md", "connection reset")
    with pytest.raises(TransportError, match="stat failed"):
        store.stat("Broken")


def test_list_returns_absolute_paths_and_propagates_failures():
    runner = FakeRunner()
    runner.files["/vault/A.md"] = "A"
    runner.files["/vault/folder/B.md"] = "B"
    store = FulcraVaultStore(runner=runner, cli_base=["fulcra-api"])

    assert store.list("vault") == ["/vault/A.md", "/vault/folder/B.md"]

    runner.fail("list", "/vault", "HTTP Error 500")
    with pytest.raises(TransportError, match="list failed"):
        store.list("vault")


def test_explicit_delete_requires_matching_confirmation_stat():
    runner = FakeRunner()
    runner.files["/vault/A.md"] = "A"
    store = FulcraVaultStore(runner=runner, cli_base=["fulcra-api"])

    assert store.delete_explicit(
        "A",
        expected_stat={"version": "v1", "size": 3},
    ) is True
    assert "/vault/A.md" not in runner.files

    runner.files["/vault/B.md"] = "B"
    with pytest.raises(TransportError, match="confirmation stat mismatch"):
        store.delete_explicit("B", expected_stat={"version": "other"})
    assert "/vault/B.md" in runner.files


def test_runner_exceptions_are_transport_errors():
    def boom(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
        raise TimeoutError("no cli")

    store = FulcraVaultStore(runner=boom, cli_base=["fulcra-api"])

    with pytest.raises(TransportError, match="transport command failed"):
        store.read_text("A")
