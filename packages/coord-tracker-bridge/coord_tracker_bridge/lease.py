"""Cross-process singleton lease for one source/tracker/policy projection."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


class LeaseHeld(RuntimeError):
    pass


@dataclass(slots=True)
class FileLease:
    directory: Path
    source: str
    tracker: str
    policy_hash: str
    ttl_seconds: float = 1800.0
    clock: Callable[[], float] = time.time
    owner: str = ""
    key: str = field(init=False)
    path: Path = field(init=False)
    _held: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        material = json.dumps([self.source, self.tracker, self.policy_hash], separators=(",", ":"))
        self.key = hashlib.sha256(material.encode()).hexdigest()
        self.path = self.directory / f"{self.key}.lease"
        self.owner = self.owner or f"pid:{os.getpid()}"
        self._held = False

    def acquire(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"owner": self.owner, "expires_at": self.clock() + self.ttl_seconds})
        for _attempt in range(2):
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                self._held = True
                return
            except FileExistsError:
                try:
                    current = json.loads(self.path.read_text(encoding="utf-8"))
                    if float(current["expires_at"]) > self.clock():
                        raise LeaseHeld(f"lease held by {current.get('owner', 'unknown')}")
                    self.path.unlink()
                except LeaseHeld:
                    raise
                except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                    raise LeaseHeld("lease state unreadable") from None
        raise LeaseHeld("could not acquire lease")

    def release(self) -> None:
        if self._held:
            try:
                current = json.loads(self.path.read_text(encoding="utf-8"))
                if current.get("owner") == self.owner:
                    self.path.unlink(missing_ok=True)
            finally:
                self._held = False

    def refresh(self) -> None:
        if not self._held:
            raise LeaseHeld("cannot refresh an unheld lease")
        try:
            current = json.loads(self.path.read_text(encoding="utf-8"))
            if current.get("owner") != self.owner:
                raise LeaseHeld("lease ownership changed")
            self.path.write_text(
                json.dumps({"owner": self.owner, "expires_at": self.clock() + self.ttl_seconds}),
                encoding="utf-8",
            )
        except LeaseHeld:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            raise LeaseHeld("lease state unreadable") from None

    def __enter__(self) -> FileLease:
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()
