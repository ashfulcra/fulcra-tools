"""The enforcing interface (spec §3.4) as a capability boundary (G5; the proof that it is not bypassed is G29).

Two unrelated classes; process launch exists only here, only as subprocess.run with a
literal argv. There is no generic argv receiver anywhere in the package.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import tempfile
from typing import Iterator, Literal, Protocol

ReadState = Literal["ok", "absent", "error"]
_FAR_FUTURE = "2999-01-01T00:00:00Z"


class PointerTransport(Protocol):
    def read_classified(self, path: str) -> tuple[str | None, ReadState]: ...
    def read_events(self, channel: str, since: str) -> Iterator[dict]: ...


class TransportUnavailable(RuntimeError):
    """The event read did not complete. The fold must NOT advance its cursor."""


class CliPointerReader:
    def __init__(self, cli: list[str], timeout: float = 60.0) -> None:
        self._cli = list(cli)
        self._timeout = timeout

    def _stat(self, path: str) -> tuple[int, str, str]:
        try:
            p = subprocess.run([*self._cli, "file", "stat", path], capture_output=True, text=True, timeout=self._timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, "", str(exc)
        return p.returncode, p.stdout, p.stderr

    def _download(self, path: str) -> tuple[int, str, str]:
        # The real CLI validates LOCAL_FILE as a readable path and REFUSES /dev/stdout under a pipe (measured
        # 2026-09-05 on the first real run: every fold refused at the channel config). A private temp file, read
        # back and removed. pathlib + tempfile only: the transport never imports os (Task 1 boundary truth).
        d = pathlib.Path(tempfile.mkdtemp(prefix="coord-fold-read-"))
        d.chmod(0o700)
        local = d / "body"
        try:
            try:
                p = subprocess.run([*self._cli, "file", "download", path, str(local)], capture_output=True, text=True, timeout=self._timeout)
            except (OSError, subprocess.TimeoutExpired) as exc:
                return 127, "", str(exc)
            if p.returncode != 0:
                return p.returncode, "", p.stderr
            try:
                return 0, local.read_text(encoding="utf-8"), p.stderr
            except OSError as exc:
                return 1, "", str(exc)
        finally:
            try:
                local.unlink(missing_ok=True)
                d.rmdir()
            except OSError:
                pass

    def _records(self, channel: str, since: str) -> tuple[int, str, str]:
        try:
            p = subprocess.run([*self._cli, "get-records", channel, since, _FAR_FUTURE], capture_output=True, text=True, timeout=self._timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, "", str(exc)
        return p.returncode, p.stdout, p.stderr

    def read_classified(self, path: str) -> tuple[str | None, ReadState]:
        rc, _out, err = self._stat(path)
        if rc != 0:
            return (None, "absent") if "File not found" in err else (None, "error")
        rc, out, _err = self._download(path)
        return (out, "ok") if rc == 0 else (None, "error")

    def read_events(self, channel: str, since: str) -> Iterator[dict]:
        rc, out, _err = self._records(channel, since)
        if rc != 0:
            raise TransportUnavailable(f"get-records rc={rc}")
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise TransportUnavailable(f"malformed record line: {exc}") from exc


class CliPointerWriter:
    def __init__(self, cli: list[str], timeout: float = 60.0) -> None:
        self._cli = list(cli)
        self._timeout = timeout

    def _record(self, data_type: str, api_version: str, source: str, doc: str) -> tuple[int, str, str]:
        try:
            p = subprocess.run([*self._cli, "record", data_type, "--api-version", api_version, "--source", source],
                               input=doc, capture_output=True, text=True, timeout=self._timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, "", str(exc)
        return p.returncode, p.stdout, p.stderr

    def _upload(self, local: str, remote: str) -> tuple[int, str, str]:
        try:
            p = subprocess.run([*self._cli, "file", "upload", local, remote], capture_output=True, text=True, timeout=self._timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, "", str(exc)
        return p.returncode, p.stdout, p.stderr

    def write_event(self, channel_cfg: dict[str, str], payload: dict, *, sender: str) -> bool:
        # GOLDEN COMPARISON against coord_engine/transport.py record_write (line 414 at 631ba497): the real CLI is
        # `record DATA_TYPE --api-version V --source S` with `{"note", "recorded_at"}` on stdin. The first cut put all
        # five keys in the stdin doc and passed NO positional; the real CLI refused every write ("Missing argument
        # 'DATA_TYPE'", rc 2) while the proof's fake accepted it — found by the G13 drill on the live store
        # (2026-09-05 20:30Z, `open: UNKNOWN`). The fake now refuses the same shapes the CLI refuses.
        doc = {"note": json.dumps(payload, separators=(",", ":")), "recorded_at": payload["at"]}
        rc, _o, _e = self._record(channel_cfg["data_type"], channel_cfg["api_version"], sender, json.dumps(doc, separators=(",", ":")))
        return rc == 0

    def save_doc(self, path: str, text: str) -> bool:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(text)
            tmp = f.name
        try:
            rc, _o, _e = self._upload(tmp, path)
            return rc == 0
        finally:
            pathlib.Path(tmp).unlink()
