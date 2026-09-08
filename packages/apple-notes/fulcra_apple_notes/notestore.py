"""Read-only access to the Apple Notes SQLite store.

Schema details relevant to reading the store correctly:

* Everything is one table. ``ZICCLOUDSYNCINGOBJECT`` (214 columns) holds
  notes, folders, attachments, media and accounts, discriminated by
  ``Z_ENT``. Column meanings therefore differ BY ENTITY: ``ZMODIFICATIONDATE``
  is the *attachment* mod date, while a note's is ``ZMODIFICATIONDATE1`` and
  its title is ``ZTITLE1``. Querying a column table-wide mixes entities.
* Some note rows are empty husks: no body, no title, no folder, no
  mod date, and NOT marked deleted. Filtering on a mod date can therefore drop valid notes. The real predicate is "has body content".
* Not every attachment has a backing file; some are inline
  tables, links and drawings with no ``ZMEDIA``. Do not assume every attachment has a file.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import shutil
import sqlite3
import subprocess
import tempfile

# Core Data stores timestamps as seconds since 2001-01-01 UTC.
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

DEFAULT_GROUP_CONTAINER = Path(
    "~/Library/Group Containers/group.com.apple.notes").expanduser()

# Bound the backup process so a blocked Full Disk Access probe cannot
# parking inside open(2), which must surface as a failure and never as
# "no notes found".
SNAPSHOT_TIMEOUT_S = 120


class NoteStoreError(RuntimeError):
    """Raised when the Notes store cannot be read."""


class AccessDeniedError(NoteStoreError):
    """Raised when the store exists but macOS denies access to it."""


def apple_time(value: float | None) -> datetime | None:
    if value is None:
        return None
    try:
        return APPLE_EPOCH + timedelta(seconds=float(value))
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class Note:
    pk: int
    """Core Data primary key. AppleScript addresses notes by an id of the
    form ``x-coredata://<store>/ICNote/p<pk>``, so this is what lets a
    vault note be matched back to the right Apple note for writeback."""
    uuid: str
    title: str
    folder: str
    modified: datetime | None
    created: datetime | None
    body: bytes
    marked_for_deletion: bool


@dataclass(frozen=True)
class Attachment:
    uuid: str
    note_uuid: str
    type_uti: str
    title: str
    filename: str
    media_uuid: str
    modified: datetime | None

    @property
    def has_file(self) -> bool:
        """Whether this attachment has a backing file on disk.

        False for inline tables, links and drawings.
        """
        return bool(self.media_uuid and self.filename)


def default_store_path() -> Path:
    return DEFAULT_GROUP_CONTAINER / "NoteStore.sqlite"


def snapshot(store: Path, dest_dir: Path, *,
             timeout: float = SNAPSHOT_TIMEOUT_S) -> Path:
    """Read a transaction-consistent backup, killing a blocked permission probe."""
    import sys
    dest = dest_dir / "NoteStore.sqlite"
    app_bin = Path(sys.executable).parent
    if app_bin.name == "MacOS" and app_bin.parent.name == "Contents":
        command = [sys.executable, "--notes-snapshot"]
    else:
        command = [sys.executable, "-m", "fulcra_apple_notes._snapshot"]
    try:
        subprocess.run([*command, str(store), str(dest)], check=True,
                       timeout=timeout, capture_output=True)
    except subprocess.TimeoutExpired as exc:
        raise AccessDeniedError(
            "Reading Notes timed out. Check Full Disk Access and retry after Notes finishes syncing."
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace").lower()
        if any(word in detail for word in ("permission", "denied", "unable to open")):
            raise AccessDeniedError("Cannot read Notes; verify Full Disk Access.") from exc
        raise NoteStoreError("Unable to create a consistent Notes snapshot.") from exc
    return dest


class NoteStore:
    """Opens a snapshot of the Notes store and answers entity queries."""

    def __init__(self, db_path: Path):
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self._conn.row_factory = sqlite3.Row
        try:
            rows = self._conn.execute("SELECT Z_NAME, Z_ENT FROM Z_PRIMARYKEY").fetchall()
            self.entities = {row[0]: int(row[1]) for row in rows}
            for name in ("ICNote", "ICFolder", "ICAttachment", "ICMedia"):
                if name not in self.entities:
                    raise NoteStoreError(f"unsupported Notes schema: missing {name}")
        except Exception:
            self._conn.close()
            raise

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "NoteStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _has_column(self, name: str) -> bool:
        cols = {r[1] for r in self._conn.execute(
            "pragma table_info('ZICCLOUDSYNCINGOBJECT')")}
        return name in cols

    def folders(self) -> dict[int, str]:
        """Z_PK -> folder title, for notes' ZFOLDER foreign key."""
        title_col = "ZTITLE2" if self._has_column("ZTITLE2") else "ZTITLE1"
        out: dict[int, str] = {}
        for col in (title_col, "ZTITLE1", "ZTITLE"):
            try:
                rows = self._conn.execute(
                    f"select Z_PK, {col} from ZICCLOUDSYNCINGOBJECT "
                    f"where Z_ENT=? and {col} is not null", (self.entities["ICFolder"],)).fetchall()
            except sqlite3.OperationalError:
                continue
            for row in rows:
                out.setdefault(row["Z_PK"], row[col] or "")
            if out:
                break
        return out

    def _creation_column(self) -> str | None:
        """Pick the creation-date column that notes actually populate.

        Measured: notes populate NEITHER ZCREATIONDATE nor ZCREATIONDATE1
        (both are entirely null for Z_ENT=12), and which of the numbered
        variants carries the value differs by macOS release. So probe for a
        column that is present AND non-null for notes rather than hardcoding
        one and having the whole query fail on a different OS version.
        """
        cols = {r[1] for r in self._conn.execute(
            "pragma table_info('ZICCLOUDSYNCINGOBJECT')")}
        for candidate in ("ZCREATIONDATE3", "ZCREATIONDATE2",
                          "ZCREATIONDATE1", "ZCREATIONDATE"):
            if candidate not in cols:
                continue
            try:
                n = self._conn.execute(
                    f"select count(*) from ZICCLOUDSYNCINGOBJECT "
                    f"where Z_ENT=? and {candidate} is not null",
                    (self.entities["ICNote"],)).fetchone()[0]
            except sqlite3.OperationalError:
                continue
            if n:
                return candidate
        return None

    def notes(self) -> list[Note]:
        """Every note that actually has body content.

        The join to ZICNOTEDATA with a non-null ZDATA is the filter that
        excludes husk rows; it is deliberately NOT a mod-date filter.
        """
        folders = self.folders()
        created_col = self._creation_column()
        created_sel = f"n.{created_col} as created, " if created_col else "NULL as created, "
        rows = self._conn.execute(
            "select n.Z_PK as pk, n.ZIDENTIFIER as uuid, n.ZTITLE1 as title, "
            "       n.ZFOLDER as folder_pk, n.ZMODIFICATIONDATE1 as modified, "
            f"      {created_sel}"
            "       n.ZMARKEDFORDELETION as deleted, d.ZDATA as body "
            "from ZICCLOUDSYNCINGOBJECT n "
            "join ZICNOTEDATA d on n.ZNOTEDATA = d.Z_PK "
            "where n.Z_ENT = ? and d.ZDATA is not null",
            (self.entities["ICNote"],)).fetchall()
        notes = []
        for row in rows:
            notes.append(Note(
                pk=int(row["pk"]),
                uuid=row["uuid"] or "",
                title=row["title"] or "",
                folder=folders.get(row["folder_pk"], ""),
                modified=apple_time(row["modified"]),
                created=apple_time(row["created"]),
                body=bytes(row["body"]),
                marked_for_deletion=bool(row["deleted"]),
            ))
        return notes

    def attachments(self) -> list[Attachment]:
        """Attachments joined to their note and (when present) media row.

        LEFT JOIN on media: an INNER join here would silently drop attachments
        that have no backing file.
        """
        rows = self._conn.execute(
            "select a.ZIDENTIFIER as uuid, n.ZIDENTIFIER as note_uuid, "
            "       a.ZTYPEUTI as type_uti, a.ZTITLE as title, "
            "       a.ZMODIFICATIONDATE as modified, "
            "       m.ZIDENTIFIER as media_uuid, m.ZFILENAME as filename "
            "from ZICCLOUDSYNCINGOBJECT a "
            "join ZICCLOUDSYNCINGOBJECT n on a.ZNOTE = n.Z_PK "
            "left join ZICCLOUDSYNCINGOBJECT m "
            "       on a.ZMEDIA = m.Z_PK and m.Z_ENT = ? "
            "where a.Z_ENT = ? and n.Z_ENT = ?",
            (self.entities["ICMedia"], self.entities["ICAttachment"], self.entities["ICNote"])).fetchall()
        return [
            Attachment(
                uuid=r["uuid"] or "",
                note_uuid=r["note_uuid"] or "",
                type_uti=r["type_uti"] or "",
                title=r["title"] or "",
                filename=r["filename"] or "",
                media_uuid=r["media_uuid"] or "",
                modified=apple_time(r["modified"]),
            )
            for r in rows
        ]


def _usable(path: Path) -> bool:
    """A file we can actually upload.

    Zero-byte files are excluded: the upload CLI sniffs content to determine
    a mime type and fails outright on an empty input, so uploading one is a
    guaranteed error rather than a degraded result.
    """
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _best_in(directory: Path) -> Path | None:
    """Pick the representative file inside an attachment directory.

    Scanned documents ("paper bundles") are stored as a DIRECTORY of page
    images plus a rendered PDF, so the recorded filename resolves to a
    folder rather than a file. Prefer the PDF -- it is the whole document,
    where any single page image is an arbitrary fragment -- then fall back
    to the largest file present.
    """
    candidates = [p for p in directory.rglob("*")
                  if _usable(p) and not p.name.startswith(".")]
    if not candidates:
        return None
    pdfs = [p for p in candidates if p.suffix.lower() == ".pdf"]
    if pdfs:
        return max(pdfs, key=lambda p: p.stat().st_size)
    return max(candidates, key=lambda p: p.stat().st_size)


def media_file(container: Path, media_uuid: str, filename: str) -> Path | None:
    """Locate an attachment's file under Accounts/*/Media/.

    Measured layout is ``Media/<media-uuid>/<dir>/<filename>`` -- the asset
    sits TWO levels down, with an intermediate directory whose name we do
    not control, so we glob rather than assume it.

    Every return path is validated as a usable FILE. A recorded name can
    resolve to a directory (scanned paper bundles) or to an empty file, and
    both fail the upload; returning them produced the only two error classes
    the first production sync hit.
    """
    if not media_uuid:
        return None
    accounts = container / "Accounts"
    if not accounts.is_dir():
        return None
    for account in accounts.iterdir():
        base = account / "Media" / media_uuid
        if not base.is_dir():
            continue
        if filename:
            for match in sorted(base.glob(f"*/{filename}")) + [base / filename]:
                if _usable(match):
                    return match
                if match.is_dir():
                    best = _best_in(match)
                    if best is not None:
                        return best
        best = _best_in(base)
        if best is not None:
            return best
    return None
