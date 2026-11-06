"""Account/destination-scoped hints to verified high-confidence v1 records.

The index stores IDs only. A hint never proves delivery: callers reread the
referenced record in the current account before suppressing a low-confidence
twin. The legacy machine-wide JSON cache cannot supply this evidence.
"""
from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from uuid import UUID

from fulcra_collect.config import config_dir


class MediaTwinIndex:
    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else config_dir() / "v1-media-twins.sqlite"

    def _private_file(self, *, create=False):
        fd = os.open(self.path, os.O_RDWR | (os.O_CREAT if create else 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("media twin index must be a private owned regular file")
        finally:
            os.close(fd)

    def lookup(self, account, destination, fingerprints):
        if not fingerprints:
            return {}
        try:
            self._private_file()
        except FileNotFoundError:
            return {}
        # No creation/schema write in preview or lookup. Corrupt/unreadable
        # state propagates, rather than masquerading as an empty known index.
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            result = {}
            for fingerprint in dict.fromkeys(fingerprints):
                row = db.execute("SELECT record_id FROM twins WHERE account=? AND destination=? AND fingerprint=?",
                                 (account, destination, fingerprint)).fetchone()
                if row:
                    result[fingerprint] = str(UUID(row[0]))
            return result

    def record(self, account, destination, rows):
        entries = []
        for row in rows:
            fingerprint = (row.get("external_ids") or {}).get("content_fingerprint")
            if row.get("timestamp_confidence") == "high" and isinstance(fingerprint, str) and fingerprint:
                entries.append((account, destination, fingerprint, str(UUID(row["id"]))))
        if not entries:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._private_file(create=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS twins (account TEXT, destination TEXT, fingerprint TEXT, "
                       "record_id TEXT NOT NULL, PRIMARY KEY(account,destination,fingerprint))")
            db.executemany("INSERT INTO twins VALUES (?,?,?,?) ON CONFLICT(account,destination,fingerprint) "
                           "DO UPDATE SET record_id=excluded.record_id", entries)
